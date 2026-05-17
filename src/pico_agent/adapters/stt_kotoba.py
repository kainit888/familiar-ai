"""STT adapter (Kotoba-Whisper) - 設計書 v4.0 第 7-3 章。

メイン PC で稼働中の ``whisper_server.py`` (デフォルト
192.168.10.104:8765) の /transcribe エンドポイントに音声を POST して
書き起こしテキストを取得する。

I/F (設計書 7-3 章):
    async def transcribe(audio_bytes: bytes, sample_rate: int = 16000) -> str

エラーハンドリング方針 (planner 確認済み):
    - 例外を raise せず silent fail + logger.warning
    - 失敗時は空文字を返す
"""

from __future__ import annotations

import os

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
