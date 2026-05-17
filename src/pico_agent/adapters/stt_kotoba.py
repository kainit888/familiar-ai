"""STT adapter (Kotoba-Whisper) - 設計書 v4.0 第 7-3 章 / v4.2 第 14-4 章。

メイン PC で稼働中の ``whisper_server.py`` (デフォルト
192.168.10.104:8765) の /transcribe エンドポイントに音声を POST して
書き起こしテキストを取得する。

I/F (設計書 7-3 章 + v4.2 14-4 章):
    async def transcribe(audio_bytes: bytes, sample_rate: int = 16000) -> str

    # v4.2 14-4 RTSP 購読常駐 (Phase C-1 で追加):
    async def start_rtsp_subscription(
        rtsp_url: str,
        on_speech: Callable[[str], Awaitable[None]],
        ...
    ) -> asyncio.Task:
        "Tapo C210 RTSP 音声トラックを連続購読し、VAD で発話区切りを検出 →
        Kotoba-Whisper に転送 → on_speech(text) を呼ぶ常駐タスクを起動"

エラーハンドリング方針 (planner 確認済み):
    - 例外を raise せず silent fail + logger.warning
    - 失敗時は空文字を返す
    - start_rtsp_subscription は ffmpeg / VAD 依存ライブラリが揃っていない時
      no-op タスク (即終了) を返す

依存ライブラリ (Phase C-1):
    - ffmpeg (system, RTSP demux + PCMA → PCM 16kHz 変換)
    - silero-vad (optional, 発話区切り検出)
    どちらも未インストールなら start_rtsp_subscription() は no-op
"""

from __future__ import annotations

import asyncio
import os
import shutil
from typing import Awaitable, Callable, Optional

import aiohttp
from loguru import logger

# ── 設定 ─────────────────────────────────────────────────────────────────
_DEFAULT_BASE_URL = "http://192.168.10.104:8765/transcribe"
_DEFAULT_TIMEOUT_SEC = 60.0


def _get_base_url() -> str:
    """環境変数 STT_BASE_URL 経由で whisper_server URL を取得。

    デフォルトは ``http://192.168.10.104:8765/transcribe`` (planner 確定値)。
    末尾の ``/transcribe`` が省略されたベース URL が渡された場合も自動補完。
    """
    raw = os.environ.get("STT_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")
    if not raw.endswith("/transcribe"):
        raw = f"{raw}/transcribe"
    return raw


def _get_timeout() -> float:
    """環境変数 STT_TIMEOUT_SEC で HTTP timeout 秒数を取得。"""
    raw = os.environ.get("STT_TIMEOUT_SEC", "")
    try:
        return float(raw) if raw else _DEFAULT_TIMEOUT_SEC
    except ValueError:
        return _DEFAULT_TIMEOUT_SEC


async def transcribe(audio_bytes: bytes, sample_rate: int = 16000) -> str:
    """音声 bytes を Kotoba-Whisper に投げて書き起こしテキストを返す。

    Args:
        audio_bytes: WAV (PCM16) または OGG/Opus 等の音声 bytes。
        sample_rate: サンプリングレート (whisper_server 側でリサンプルされる
            ためメタデータ扱い、未送信のサーバ実装にも対応)。

    Returns:
        書き起こしテキスト。失敗時 / 空入力時は空文字 (例外は投げない)。
    """
    if not audio_bytes:
        logger.warning("stt_kotoba.transcribe: empty audio_bytes")
        return ""

    url = _get_base_url()
    timeout = aiohttp.ClientTimeout(total=_get_timeout())

    form = aiohttp.FormData()
    form.add_field(
        "file",
        audio_bytes,
        filename="audio.wav",
        content_type="audio/wav",
    )
    form.add_field("sample_rate", str(int(sample_rate)))

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, data=form) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(
                        "stt_kotoba: HTTP {} from {} body={!r}",
                        resp.status,
                        url,
                        body[:200],
                    )
                    return ""
                # whisper_server は JSON {"text": "..."} を返す想定。
                # plain text を返す実装にも対応するため両方試す。
                content_type = resp.headers.get("Content-Type", "")
                if "json" in content_type.lower():
                    try:
                        data = await resp.json()
                    except Exception as e:
                        logger.warning("stt_kotoba: JSON decode failed: {}", e)
                        return ""
                    text = data.get("text", "") if isinstance(data, dict) else ""
                    return str(text).strip()
                # text/plain fallback。
                text = await resp.text()
                return text.strip()
    except Exception as e:
        logger.warning("stt_kotoba.transcribe: request failed: {}", e)
        return ""


# ── v4.2 14-4: Tapo RTSP 音声トラック購読 (Phase C-1 で追加) ──────────────
#
# 設計書 v4.2 14-4 が要求する常駐コンポーネント。Tapo C210 の RTSP 音声トラック
# (PCMA/8000) を ffmpeg で PCM 16kHz に変換しつつ、Silero VAD で発話区切りを
# 検出し、発話単位で transcribe() を呼んで上位 callback に流す。
#
# Phase C-1 では「**依存ライブラリ (ffmpeg + silero-vad) が揃っているか動的に
# 判定し、揃っていなければ no-op タスクを返す**」スケルトン実装にとどめる。
# 実稼働は Phase D Discord 統合と同時に詰める。


def _get_rtsp_url() -> Optional[str]:
    """環境変数 STT_RTSP_URL から Tapo RTSP URL を取得 (未設定なら None)。

    例: ``rtsp://Pico:password@192.168.10.110:554/stream1``
    """
    raw = os.environ.get("STT_RTSP_URL", "").strip()
    return raw or None


def _ffmpeg_available() -> bool:
    """ffmpeg バイナリが PATH にあるかを判定 (RTSP demux + 変換に必要)。"""
    return shutil.which("ffmpeg") is not None


def _silero_vad_available() -> bool:
    """silero-vad (or torch.hub silero_vad) が import 可能かを判定。

    Phase C-1 では import 試行のみ。RPi5 上では torch インストールが
    重いので、`uv add` は本実装時に行う。
    """
    try:
        import silero_vad  # type: ignore  # noqa: F401
        return True
    except Exception:
        pass
    try:
        import torch  # type: ignore  # noqa: F401
        return True
    except Exception:
        return False


async def _noop_subscription_loop(reason: str) -> None:
    """依存が揃っていない時の no-op ループ (起動ログだけ出して即終了)。"""
    logger.warning(
        "stt_kotoba.start_rtsp_subscription: skipped ({}); "
        "STT live capture disabled until dependencies are installed",
        reason,
    )


async def start_rtsp_subscription(
    on_speech: Callable[[str], Awaitable[None]],
    rtsp_url: Optional[str] = None,
    *,
    vad_threshold: float = 0.5,
    min_silence_ms: int = 500,
    chunk_duration_ms: int = 30,
) -> asyncio.Task:
    """Tapo C210 RTSP 音声トラックを連続購読し、発話単位で transcribe を呼ぶ常駐タスクを起動。

    設計書 v4.2 14-4 章の常駐コンポーネント。Phase C-1 ではスケルトン実装で、
    依存ライブラリ (ffmpeg + silero-vad) が揃っていない場合は no-op タスクを
    返す。本実装は Phase D 着手時または依存揃い次第。

    Args:
        on_speech: 1 発話の書き起こしテキストを受け取る async callback。
        rtsp_url: Tapo RTSP URL (未指定なら環境変数 STT_RTSP_URL から取得)。
        vad_threshold: Silero VAD の発話判定閾値 (0.0-1.0)。
        min_silence_ms: 発話終端と判定する無音時間 (ミリ秒)。
        chunk_duration_ms: VAD に流す 1 chunk の長さ (ミリ秒)。

    Returns:
        起動した `asyncio.Task`。停止は ``task.cancel()`` で。
        依存未満の場合も Task を返す (即終了する no-op タスク)。
    """
    url = rtsp_url or _get_rtsp_url()
    if not url:
        return asyncio.create_task(_noop_subscription_loop("STT_RTSP_URL not set"))
    if not _ffmpeg_available():
        return asyncio.create_task(_noop_subscription_loop("ffmpeg not in PATH"))
    if not _silero_vad_available():
        return asyncio.create_task(
            _noop_subscription_loop("silero-vad / torch not installed")
        )

    # 実装本体は Phase D / 本実装時に詰める。
    # スケルトン段階ではここに到達したらログを残して no-op で抜ける。
    logger.warning(
        "stt_kotoba.start_rtsp_subscription: dependencies present but "
        "implementation pending (Phase D or dedicated session). "
        "Returning no-op task. url={}, vad_threshold={}, min_silence_ms={}",
        url,
        vad_threshold,
        min_silence_ms,
    )
    # on_speech は将来の本実装で呼ぶ。ここでは未使用なので参照だけして
    # static analyzer 警告を抑止。
    _ = on_speech, chunk_duration_ms
    return asyncio.create_task(
        _noop_subscription_loop("implementation pending (Phase D)")
    )
