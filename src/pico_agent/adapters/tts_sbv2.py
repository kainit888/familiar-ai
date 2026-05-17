"""TTS adapter (Style-BERT-VITS2) - 設計書 v4.0 第 7-2 章。

メイン PC で稼働中の Style-BERT-VITS2 サーバ (デフォルト 192.168.10.104:5000)
に GET /voice?text=... を投げて WAV bytes を取得する。

I/F (設計書 7-2 章):
    async def speak(
        text: str,
        speaker_id: int = 0,
        emotion: dict | None = None,    # valence/arousal で声色変化
        target: str = "discord_vc",     # "discord_vc" | "local_speaker"
    ) -> bytes
    "100 文字制限 → 句読点で分割 → asyncio.Queue でストリーミング"

エラーハンドリング方針 (planner 確認済み):
    - 例外を raise せず silent fail + logger.warning
    - 失敗時は空 bytes を返す
    - target はメタ情報として現状ログのみ (再生先の振り分けは呼び出し側責務)

emotion マッピング (Phase E で再調整、Phase C-1 は保守的デフォルト):
    style_weight = (valence - 0.5) * 2  # -1.0〜+1.0
"""

from __future__ import annotations

import os
import re
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
