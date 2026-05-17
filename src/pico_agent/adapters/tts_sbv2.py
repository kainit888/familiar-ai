"""TTS adapter (Style-BERT-VITS2) - 設計書 v4.0 第 7-2 章 / v4.2 第 14-5 章。

メイン PC で稼働中の Style-BERT-VITS2 サーバ (デフォルト 192.168.10.104:5000)
に GET /voice?text=... を投げて WAV bytes を取得する。

I/F (設計書 7-2 章 + v4.2 14-5 章):
    async def speak(
        text: str,
        speaker_id: int = 0,
        emotion: dict | None = None,    # valence/arousal で声色変化
        target: str = "discord_vc",     # 既存値域維持
    ) -> bytes
    "100 文字制限 → 句読点で分割 → asyncio.Queue でストリーミング"

    # v4.2 14-5 フォールバックチェーン (Phase C-1 で追加):
    async def play_with_fallback(
        text: str,
        target: str = "auto",  # "tapo_speaker" | "main_pc" | "rpi5" | "auto"
        ...
    ) -> tuple[bool, str]:
        "WAV bytes 取得 + 物理再生まで実行、target 失敗時は順次フォールバック"

エラーハンドリング方針 (planner 確認済み):
    - 例外を raise せず silent fail + logger.warning
    - 失敗時は空 bytes を返す
    - target はメタ情報として現状ログのみ (再生先の振り分けは呼び出し側責務)
    - play_with_fallback() は (success, played_via) を返す (例外は呼ばない)

emotion マッピング (Phase E で再調整、Phase C-1 は保守的デフォルト):
    style_weight = (valence - 0.5) * 2  # -1.0〜+1.0

go2rtc 連携 (v4.2 14-5):
    - go2rtc 本体のセットアップは TP-Link クラウドパスワードが必要 (カイニット手動)
    - _play_via_go2rtc() は API 呼び出しコードのスケルトンのみ、デフォルト off
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any
from urllib.parse import urlencode

import aiohttp
from loguru import logger

# ── 設定 ─────────────────────────────────────────────────────────────────
_DEFAULT_BASE_URL = "http://192.168.10.104:5000"
_DEFAULT_TIMEOUT_SEC = 60.0

# 100 文字を超えるテキストは句読点で分割してから順次送信する (設計書 7-2 章)。
_MAX_CHUNK_CHARS = 100
_SPLIT_PUNCT_PATTERN = re.compile(r"(?<=[。、！？!?,.\n])")

_VALID_TARGETS = ("discord_vc", "local_speaker")


def _get_base_url() -> str:
    """環境変数 TTS_BASE_URL 経由で SBV2 サーバ URL を取得。"""
    return os.environ.get("TTS_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")


def _get_timeout() -> float:
    """環境変数 TTS_TIMEOUT_SEC で HTTP timeout 秒数を取得。"""
    raw = os.environ.get("TTS_TIMEOUT_SEC", "")
    try:
        return float(raw) if raw else _DEFAULT_TIMEOUT_SEC
    except ValueError:
        return _DEFAULT_TIMEOUT_SEC


def _split_chunks(text: str) -> list[str]:
    """100 文字制限を満たすように句読点で分割する。

    SBV2 の安全側として、句読点ごとに切ってから ``_MAX_CHUNK_CHARS`` までを
    1 チャンクにまとめて返す。長文 1 行の場合は強制的にスライス。
    """
    if not text:
        return []

    pieces = [p for p in _SPLIT_PUNCT_PATTERN.split(text) if p]
    chunks: list[str] = []
    buf = ""
    for piece in pieces:
        if not piece:
            continue
        if len(buf) + len(piece) <= _MAX_CHUNK_CHARS:
            buf += piece
        else:
            if buf:
                chunks.append(buf)
            buf = piece
    if buf:
        chunks.append(buf)

    # 句読点がない長文の場合は強制スライス。
    overflow_split: list[str] = []
    for c in chunks:
        if len(c) <= _MAX_CHUNK_CHARS:
            overflow_split.append(c)
        else:
            for i in range(0, len(c), _MAX_CHUNK_CHARS):
                overflow_split.append(c[i : i + _MAX_CHUNK_CHARS])
    return overflow_split


def _emotion_to_style_weight(emotion: dict[str, Any] | None) -> float:
    """emotion dict から SBV2 の style_weight (-1.0〜+1.0 目安) を導出。

    Phase C-1 では保守的デフォルト (valence のみ参照、arousal は将来用)。
    Phase E (感情 3 値実装) で再調整予定。
    """
    if not emotion:
        return 0.0
    try:
        valence = float(emotion.get("valence", 0.5))
    except (TypeError, ValueError):
        valence = 0.5
    return max(-1.0, min(1.0, (valence - 0.5) * 2.0))


def _build_query(
    text: str,
    speaker_id: int,
    emotion: dict[str, Any] | None,
) -> str:
    """SBV2 GET /voice 用クエリ文字列を組み立てる。

    公式 API 仕様の確認時間が取れなかったため、第一候補 (カイニット確定):
    ``/voice?text=...&model_id=0&speaker_id=0`` 形式を採用。
    """
    style_weight = _emotion_to_style_weight(emotion)
    params: list[tuple[str, str]] = [
        ("text", text),
        ("model_id", "0"),
        ("speaker_id", str(int(speaker_id))),
        ("style_weight", f"{style_weight:.2f}"),
    ]
    return urlencode(params)


async def _fetch_one_chunk(session: aiohttp.ClientSession, chunk: str, query_suffix: str) -> bytes:
    """1 チャンクを SBV2 に GET して WAV bytes を返す (silent fail)。"""
    base_url = _get_base_url()
    url = f"{base_url}/voice?{query_suffix}"
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.warning(
                    "tts_sbv2: HTTP {} from {} (chunk={!r}) body={!r}",
                    resp.status,
                    url,
                    chunk[:40],
                    body[:200],
                )
                return b""
            return await resp.read()
    except Exception as e:
        logger.warning("tts_sbv2: request failed for chunk={!r}: {}", chunk[:40], e)
        return b""


async def speak(
    text: str,
    speaker_id: int = 0,
    emotion: dict[str, Any] | None = None,
    target: str = "discord_vc",
) -> bytes:
    """テキストを Style-BERT-VITS2 に投げて WAV bytes を返す (設計書 7-2 章)。

    Args:
        text: 読み上げ対象テキスト (100 文字を超えると句読点で分割)。
        speaker_id: SBV2 サーバ側の speaker ID (デフォルト 0)。
        emotion: ``{"valence": 0.0-1.0, "arousal": 0.0-1.0}`` 形式の感情 dict。
            ``None`` のときは中立。
        target: 再生先のメタ情報 (``"discord_vc"`` または ``"local_speaker"``)。
            実際の再生は呼び出し側 (Phase D Discord bridge) の責務。

    Returns:
        連結された WAV bytes。失敗時は空 bytes (例外は投げない)。
    """
    if not text or not text.strip():
        logger.warning("tts_sbv2.speak: empty text")
        return b""
    if target not in _VALID_TARGETS:
        logger.warning("tts_sbv2.speak: unknown target {!r}, defaulting to discord_vc", target)
        target = "discord_vc"

    chunks = _split_chunks(text.strip())
    if not chunks:
        return b""

    logger.debug(
        "tts_sbv2.speak: text={!r} chunks={} target={} speaker_id={}",
        text[:40],
        len(chunks),
        target,
        speaker_id,
    )

    timeout = aiohttp.ClientTimeout(total=_get_timeout())
    audio_parts: list[bytes] = []
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for chunk in chunks:
                query = _build_query(chunk, speaker_id, emotion)
                part = await _fetch_one_chunk(session, chunk, query)
                if part:
                    audio_parts.append(part)
    except Exception as e:
        logger.warning("tts_sbv2.speak: session-level failure: {}", e)
        return b""

    if not audio_parts:
        return b""

    # 単純連結 (RIFF ヘッダの厳密マージは呼び出し側か Phase D で対応)。
    return b"".join(audio_parts)


# ── v4.2 14-5: フォールバック再生チェーン ────────────────────────────────────
#
# Phase C-1 で追加。speak() (WAV bytes 取得) と再生 (subprocess) を組み合わせて、
# Tapo C210 スピーカー → メイン PC → RPi5 アナログ出力 の順にフォールバックする。
# go2rtc 連携 (_play_via_go2rtc) は本体未セットアップなので no-op 既定。

# サポート target
_FALLBACK_TARGETS: tuple[str, ...] = ("tapo_speaker", "main_pc", "rpi5")
_AUTO_FALLBACK_CHAIN: tuple[str, ...] = ("tapo_speaker", "main_pc", "rpi5")


def _get_go2rtc_url() -> str:
    """go2rtc REST API のベース URL を取得 (環境変数 GO2RTC_URL)。"""
    return os.environ.get("GO2RTC_URL", "http://localhost:1984").rstrip("/")


def _get_go2rtc_stream() -> str:
    """go2rtc ストリーム名を取得 (環境変数 GO2RTC_STREAM、デフォルト pico_camera)。"""
    return os.environ.get("GO2RTC_STREAM", "pico_camera")


def _is_go2rtc_enabled() -> bool:
    """環境変数 GO2RTC_ENABLED=1 のときのみ go2rtc 再生を試みる。

    Phase C-1 では go2rtc 本体のセットアップが完了していないため、デフォルト off。
    本番稼働時にカイニットが go2rtc を起動した上で GO2RTC_ENABLED=1 を設定する。
    """
    return os.environ.get("GO2RTC_ENABLED", "").strip() in ("1", "true", "yes")


def _write_tmp_wav(wav_bytes: bytes) -> str:
    """WAV bytes を一時ファイルに書き出してパスを返す。"""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(wav_bytes)
        return f.name


def _which(name: str) -> str | None:
    """shutil.which のラッパ (path にバイナリが存在すれば絶対パス、なければ None)。"""
    return shutil.which(name)


async def _play_via_main_pc(wav_bytes: bytes) -> bool:
    """メイン PC のスピーカーで WAV を再生する (mpv / ffplay 経由)。

    現状はメイン PC へ実装を委譲する設計だが、Phase D まで Discord 統合が
    入らないので、暫定的に **このプロセスが動いているマシン** (RPi5 か別 PC)
    の音声出力デバイスで mpv / ffplay 再生を試みる。

    Returns:
        再生プロセスが exit code 0 で終わったら True、それ以外 False。
    """
    if not wav_bytes:
        return False
    bin_path = _which("mpv") or _which("ffplay")
    if bin_path is None:
        logger.warning(
            "tts_sbv2._play_via_main_pc: neither mpv nor ffplay found in PATH"
        )
        return False

    tmp_path = _write_tmp_wav(wav_bytes)
    try:
        if bin_path.endswith("mpv"):
            args = [bin_path, "--no-terminal", "--really-quiet", tmp_path]
        else:
            args = [bin_path, "-nodisp", "-autoexit", "-loglevel", "quiet", tmp_path]
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        rc = await proc.wait()
        return rc == 0
    except Exception as e:
        logger.warning("tts_sbv2._play_via_main_pc: failed: {}", e)
        return False
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


async def _play_via_rpi5(wav_bytes: bytes) -> bool:
    """RPi5 のアナログ出力 (Anker Soundcore mini 3) で WAV を再生する。

    aplay (ALSA) が標準で使えるはず。Linux 以外では失敗。

    Returns:
        再生プロセスが exit code 0 で終わったら True、それ以外 False。
    """
    if not wav_bytes:
        return False
    if sys.platform != "linux":
        logger.warning(
            "tts_sbv2._play_via_rpi5: not on Linux ({}); skipping", sys.platform
        )
        return False
    bin_path = _which("aplay") or _which("paplay")
    if bin_path is None:
        logger.warning(
            "tts_sbv2._play_via_rpi5: neither aplay nor paplay found in PATH"
        )
        return False

    tmp_path = _write_tmp_wav(wav_bytes)
    try:
        proc = await asyncio.create_subprocess_exec(
            bin_path,
            "-q",
            tmp_path,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        rc = await proc.wait()
        return rc == 0
    except Exception as e:
        logger.warning("tts_sbv2._play_via_rpi5: failed: {}", e)
        return False
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


async def _play_via_go2rtc(wav_bytes: bytes) -> bool:
    """go2rtc REST API 経由で Tapo C210 スピーカーに WAV を流す (スケルトン)。

    ⚠️ Phase C-1 では **GO2RTC_ENABLED=1 でないと no-op**。go2rtc 本体の
    セットアップ (TP-Link クラウドパスワード必要) が完了するまでこの経路は
    使えない。設計書 v4.2 14-5 章参照。

    go2rtc API 仕様 (案):
        POST {GO2RTC_URL}/api/streams/{stream}/play?file=<tmp_path>
        または PUT {GO2RTC_URL}/api/streams/{stream}/audio (WAV body)

    実 API 仕様は go2rtc 起動後に確認して詰める (Phase C-1 着手時の TODO)。

    Returns:
        GO2RTC_ENABLED=0 のとき False、API 呼び出し成功で True。
    """
    if not wav_bytes:
        return False
    if not _is_go2rtc_enabled():
        logger.debug(
            "tts_sbv2._play_via_go2rtc: GO2RTC_ENABLED not set, skipping"
        )
        return False

    base_url = _get_go2rtc_url()
    stream = _get_go2rtc_stream()
    timeout = aiohttp.ClientTimeout(total=_get_timeout())

    # API 仕様確定までの暫定 endpoint (PUT で WAV body を送る)
    url = f"{base_url}/api/streams/{stream}/audio"
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.put(
                url,
                data=wav_bytes,
                headers={"Content-Type": "audio/wav"},
            ) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.warning(
                        "tts_sbv2._play_via_go2rtc: HTTP {} from {} body={!r}",
                        resp.status,
                        url,
                        body[:200],
                    )
                    return False
                return True
    except Exception as e:
        logger.warning("tts_sbv2._play_via_go2rtc: request failed: {}", e)
        return False


_BACKENDS = {
    "tapo_speaker": _play_via_go2rtc,
    "main_pc": _play_via_main_pc,
    "rpi5": _play_via_rpi5,
}


async def play_with_fallback(
    text: str,
    target: str = "auto",
    speaker_id: int = 0,
    emotion: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """WAV bytes 取得 + 物理再生まで実行し、フォールバックチェーンを回す。

    設計書 v4.2 14-5 章の `speak(target=...)` インターフェース実装。

    Args:
        text: 読み上げ対象テキスト。
        target: 再生先指定。
            ``"tapo_speaker"`` / ``"main_pc"`` / ``"rpi5"``: 単一バックエンド試行。
            ``"auto"``: tapo_speaker → main_pc → rpi5 の順に試行。
        speaker_id: SBV2 speaker ID。
        emotion: 感情 dict (valence/arousal)。

    Returns:
        ``(success, played_via)``。
        - success=True なら played_via は使用されたバックエンド名 (例: "main_pc")
        - success=False なら played_via は理由文字列 (例: "no_audio" / "all_failed")
    """
    if not text or not text.strip():
        return False, "empty_text"

    audio = await speak(text=text, speaker_id=speaker_id, emotion=emotion)
    if not audio:
        return False, "no_audio"

    if target == "auto":
        chain: tuple[str, ...] = _AUTO_FALLBACK_CHAIN
    elif target in _BACKENDS:
        chain = (target,)
    else:
        logger.warning(
            "tts_sbv2.play_with_fallback: unknown target {!r}, falling back to auto",
            target,
        )
        chain = _AUTO_FALLBACK_CHAIN

    for backend_name in chain:
        backend = _BACKENDS[backend_name]
        try:
            ok = await backend(audio)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "tts_sbv2.play_with_fallback: backend {!r} raised: {}",
                backend_name,
                e,
            )
            ok = False
        if ok:
            logger.info(
                "tts_sbv2.play_with_fallback: played via {} (text={!r})",
                backend_name,
                text[:40],
            )
            return True, backend_name
        logger.debug(
            "tts_sbv2.play_with_fallback: backend {!r} failed, trying next",
            backend_name,
        )

    return False, "all_failed"
