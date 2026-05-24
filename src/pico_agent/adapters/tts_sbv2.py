"""TTS adapter (Style-BERT-VITS2) - 設計書 v5 第 14-5 章 (フォールバック撤廃版)。

メイン PC で稼働中の Style-BERT-VITS2 サーバ (デフォルト 192.168.10.104:5000)
に GET /voice?text=...&model_name=... を投げて WAV bytes を取得し、
メイン PC 上で常駐する go2rtc (192.168.10.104:1984) の HTTP API へ POST して
Tapo C210 内蔵スピーカーから再生する。

v5 (2026-05-24) で `play_with_fallback` (`tapo_speaker → main_pc → rpi5` の 3
段フォールバック) を撤廃。実体のあるフォールバック先が存在しないため、
失敗時は無音 + logger.warning で素直に終わる (設計書 14-5-11 / 14-5-12 節)。

公開 I/F (設計書 v5 14-5-11 節):
    async def speak(
        text: str,
        target: Target | str = "tapo_speaker",   # "tapo_speaker" | "discord_vc" | "obs_audio"
        speaker_id: int = 0,
        emotion: EmotionDict | None = None,
    ) -> None
        "SBV2 で合成した TTS を指定 target に送出する (失敗時は無音)。"

    - target="tapo_speaker": go2rtc HTTP API → Tapo C210 (普段の会話、本実装)
    - target="discord_vc":   Phase D で discord.py voice client 実装、現状スタブ
    - target="obs_audio":    Phase K で OBS 音声入力実装、現状スタブ

エラーハンドリング方針 (絶対遵守):
    - 例外を raise せず silent fail + logger.warning
    - 失敗時は無音 (None 返却)、フォールバックは設けない (v5 14-5-11)

go2rtc 連携 (v5 14-5-5 / 14-5-12 確定):
    - POST {GO2RTC_BASE_URL}/api/streams?dst={TAPO_STREAM_NAME}&src=<URL-encoded ffmpeg URL>
    - src 形式: ffmpeg:<wav_path>#audio=pcma#input=file
    - Body 空、Authorization なし、Content-Type なし
    - LAN 内認証なし (192.168.10.104:1984)
    - **Pi 側に go2rtc バイナリを置かない** (HTTP API 単一経路)

emotion マッピング (Phase E で再調整、保守的デフォルト):
    style_weight = (valence - 0.5) * 2  # -1.0〜+1.0

暖機 (案 A): モジュール初回呼び出し時に「ん」1 文字を SBV2 で生成して
    /tmp/pico_v3_warmup.wav に保存。次回以降はファイル存在のみ確認。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Literal, TypeAlias
from urllib.parse import quote, urlencode

import aiohttp
from loguru import logger

# ── 型エイリアス ──────────────────────────────────────────────────────────
# 感情 dict は ``{"valence": 0.0-1.0, "arousal": 0.0-1.0, ...}`` 形式。
# Phase E (感情 3 値実装) で具体的なキーを TypedDict 化する想定。
EmotionDict: TypeAlias = dict[str, float]

# speak() の target 値域 (v5 で用途別 3 経路に再構成、14-5-11 節)。
Target: TypeAlias = Literal["tapo_speaker", "discord_vc", "obs_audio"]

# ── 設定 (環境変数で上書き可能、ハードコード禁止) ────────────────────────────
_DEFAULT_BASE_URL = "http://192.168.10.104:5000"
_DEFAULT_TIMEOUT_SEC = 60.0
_DEFAULT_GO2RTC_BASE_URL = "http://127.0.0.1:1984"
_DEFAULT_TAPO_STREAM_NAME = "tapo_c210"
_DEFAULT_TTS_MODEL_NAME = "jvnv-F1-jp"
_DEFAULT_TTS_VOLUME = 0.5
_DEFAULT_TTS_PRE_RESAMPLE = 16000
_DEFAULT_TTS_TAIL_SILENCE = 0.5
_DEFAULT_TTS_CHUNK_MAX_CHARS = 30
_DEFAULT_TTS_CHUNK_DELAY_MS = 0

# 暖機ファイル (カイニット指定)
_WARMUP_WAV_PATH = Path("/tmp/pico_v3_warmup.wav")
_WARMUP_TEXT = "ん"
_WARMUP_DONE: bool = False

# 分割優先度: 「。」「！」「？」「、」「\n」の順
_SPLIT_PRIORITY: tuple[str, ...] = ("。", "！", "？", "、", "\n")

# speak() の target 値域 (実体は 3 つ、tapo_speaker のみ本実装)
_VALID_TARGETS: tuple[str, ...] = ("tapo_speaker", "discord_vc", "obs_audio")


# ── 環境変数アクセサ ──────────────────────────────────────────────────────


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


def _get_go2rtc_base_url() -> str:
    """go2rtc REST API のベース URL を取得 (環境変数 GO2RTC_BASE_URL)。"""
    return os.environ.get("GO2RTC_BASE_URL", _DEFAULT_GO2RTC_BASE_URL).rstrip("/")


def _get_tapo_stream_name() -> str:
    """go2rtc ストリーム名 (Tapo C210) を取得 (環境変数 TAPO_STREAM_NAME)。"""
    return os.environ.get("TAPO_STREAM_NAME", _DEFAULT_TAPO_STREAM_NAME)


def _get_tts_model_name() -> str:
    """SBV2 の model_name を取得 (環境変数 TTS_MODEL_NAME)。"""
    return os.environ.get("TTS_MODEL_NAME", _DEFAULT_TTS_MODEL_NAME)


def _get_tts_volume() -> float:
    """ffmpeg volume フィルタ係数を取得 (環境変数 TTS_VOLUME)。"""
    raw = os.environ.get("TTS_VOLUME", "")
    try:
        return float(raw) if raw else _DEFAULT_TTS_VOLUME
    except ValueError:
        return _DEFAULT_TTS_VOLUME


def _get_tts_pre_resample() -> int:
    """ffmpeg 再サンプリング rate を取得 (環境変数 TTS_PRE_RESAMPLE)。"""
    raw = os.environ.get("TTS_PRE_RESAMPLE", "")
    try:
        return int(raw) if raw else _DEFAULT_TTS_PRE_RESAMPLE
    except ValueError:
        return _DEFAULT_TTS_PRE_RESAMPLE


def _get_tts_tail_silence() -> float:
    """ffmpeg apad pad_dur 秒数を取得 (環境変数 TTS_TAIL_SILENCE)。"""
    raw = os.environ.get("TTS_TAIL_SILENCE", "")
    try:
        return float(raw) if raw else _DEFAULT_TTS_TAIL_SILENCE
    except ValueError:
        return _DEFAULT_TTS_TAIL_SILENCE


def _get_chunk_max_chars() -> int:
    """テキスト分割の上限文字数を取得 (環境変数 TTS_CHUNK_MAX_CHARS)。"""
    raw = os.environ.get("TTS_CHUNK_MAX_CHARS", "")
    try:
        return int(raw) if raw else _DEFAULT_TTS_CHUNK_MAX_CHARS
    except ValueError:
        return _DEFAULT_TTS_CHUNK_MAX_CHARS


def _get_chunk_delay_ms() -> int:
    """チャンク間のディレイ ms を取得 (環境変数 TTS_CHUNK_DELAY_MS)。"""
    raw = os.environ.get("TTS_CHUNK_DELAY_MS", "")
    try:
        return int(raw) if raw else _DEFAULT_TTS_CHUNK_DELAY_MS
    except ValueError:
        return _DEFAULT_TTS_CHUNK_DELAY_MS


# ── テキスト分割 ──────────────────────────────────────────────────────────


def _split_chunks(text: str) -> list[str]:
    """30 文字制限を満たすように句読点優先でテキストを分割する。

    アルゴリズム:
        1. 残り文字列の先頭から ``max_chars`` 文字ウィンドウを取る
        2. ``_SPLIT_PRIORITY`` の優先順 (。→！→？→、→\\n) で
           ウィンドウ内を ``rfind`` し、見つかった位置で切る
        3. どれも見つからなければ強制 ``max_chars`` で切る
        4. 切った後のチャンクは ``.lstrip()`` で前後空白を除去
        5. 空チャンクは結果に含めない
        6. 1 ステップで必ず 1 文字以上進めて無限ループ防止
    """
    if not text:
        return []

    max_chars = _get_chunk_max_chars()
    if max_chars <= 0:
        # 異常設定の保険。ハードコードではなく定数フォールバック。
        max_chars = _DEFAULT_TTS_CHUNK_MAX_CHARS

    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= max_chars:
            piece = remaining.lstrip()
            if piece:
                chunks.append(piece)
            break

        window = remaining[:max_chars]
        cut_at = -1
        # 優先順に探す。先頭 (idx=0) でしか見つからない場合は無限ループになるので除外。
        for delim in _SPLIT_PRIORITY:
            idx = window.rfind(delim)
            if idx > 0:  # 先頭区切りは無視して次の優先度へ
                cut_at = idx + len(delim)
                break

        if cut_at <= 0:
            # どの区切りも見つからず、または先頭にしか無い → 強制スライス
            cut_at = max_chars

        piece = remaining[:cut_at].lstrip()
        if piece:
            chunks.append(piece)
        remaining = remaining[cut_at:]

    return chunks


# ── emotion → style_weight ────────────────────────────────────────────────


def _emotion_to_style_weight(emotion: EmotionDict | None) -> float:
    """emotion dict から SBV2 の style_weight (-1.0〜+1.0 目安) を導出。

    Phase C-3 では保守的デフォルト (valence のみ参照、arousal は将来用)。
    Phase E (感情 3 値実装) で再調整予定。
    """
    if not emotion:
        return 0.0
    try:
        valence = float(emotion.get("valence", 0.5))
    except (TypeError, ValueError):
        valence = 0.5
    return max(-1.0, min(1.0, (valence - 0.5) * 2.0))


# ── SBV2 GET /voice 呼び出し ──────────────────────────────────────────────


def _build_query(
    text: str,
    speaker_id: int,
    emotion: EmotionDict | None,
) -> str:
    """SBV2 GET /voice 用クエリ文字列を組み立てる。

    形式: ``/voice?text=...&model_name=...&speaker_id=...&style_weight=...``
    """
    style_weight = _emotion_to_style_weight(emotion)
    params: list[tuple[str, str]] = [
        ("text", text),
        ("model_name", _get_tts_model_name()),
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


# ── 暖機 ──────────────────────────────────────────────────────────────────


async def _warmup_once() -> None:
    """プロセス起動後 1 回だけ SBV2 に「ん」を投げて暖機する (silent fail)。

    動作:
        - すでに ``_WARMUP_DONE`` なら何もしない
        - ``/tmp/pico_v3_warmup.wav`` が既存ならファイルを信頼してフラグだけ立てる
        - 存在しなければ SBV2 に「ん」を投げて WAV を取得して保存
        - 取得失敗時は警告のみ、フラグは立てない (次回再試行)
    """
    global _WARMUP_DONE
    if _WARMUP_DONE:
        return

    try:
        if _WARMUP_WAV_PATH.exists() and _WARMUP_WAV_PATH.stat().st_size > 0:
            _WARMUP_DONE = True
            logger.debug("tts_sbv2._warmup: existing file {}, skipping fetch", _WARMUP_WAV_PATH)
            return
    except OSError as e:
        logger.warning("tts_sbv2._warmup: stat failed: {}", e)
        # フラグは立てない

    query = _build_query(_WARMUP_TEXT, speaker_id=0, emotion=None)
    timeout = aiohttp.ClientTimeout(total=_get_timeout())
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            wav = await _fetch_one_chunk(session, _WARMUP_TEXT, query)
        if not wav:
            logger.warning("tts_sbv2._warmup: SBV2 returned empty bytes; will retry next call")
            return
        _WARMUP_WAV_PATH.write_bytes(wav)
        _WARMUP_DONE = True
        logger.info(
            "tts_sbv2._warmup: saved warmup wav to {} ({} bytes)",
            _WARMUP_WAV_PATH,
            len(wav),
        )
    except Exception as e:
        logger.warning("tts_sbv2._warmup: silent fail: {}", e)
        # フラグは立てない、次回再試行


# ── SBV2 WAV bytes 取得 (旧 speak() の本体を helper 化) ───────────────────


async def _fetch_wav_bytes(
    text: str,
    speaker_id: int,
    emotion: EmotionDict | None,
) -> bytes:
    """テキストを 30 文字分割 → SBV2 GET → 連結した WAV bytes を返す (silent fail)。

    Args:
        text: 読み上げ対象テキスト (空 / 空白のみは呼び出し側で除外済の前提)。
        speaker_id: SBV2 サーバ側の speaker ID (デフォルト 0)。
        emotion: ``{"valence": 0.0-1.0, "arousal": 0.0-1.0}`` 形式の感情 dict。

    Returns:
        連結された WAV bytes。失敗時は空 bytes (例外は投げない)。
    """
    chunks = _split_chunks(text.strip())
    if not chunks:
        return b""

    logger.debug(
        "tts_sbv2._fetch_wav_bytes: text={!r} chunks={} speaker_id={}",
        text[:40],
        len(chunks),
        speaker_id,
    )

    timeout = aiohttp.ClientTimeout(total=_get_timeout())
    audio_parts: list[bytes] = []
    chunk_delay_ms = _get_chunk_delay_ms()
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for i, chunk in enumerate(chunks):
                query = _build_query(chunk, speaker_id, emotion)
                part = await _fetch_one_chunk(session, chunk, query)
                if part:
                    audio_parts.append(part)
                if chunk_delay_ms > 0 and i < len(chunks) - 1:
                    await asyncio.sleep(chunk_delay_ms / 1000.0)
    except Exception as e:
        logger.warning("tts_sbv2._fetch_wav_bytes: session-level failure: {}", e)
        return b""

    if not audio_parts:
        logger.warning("tts_sbv2._fetch_wav_bytes: empty WAV bytes after all chunks")
        return b""

    return b"".join(audio_parts)


# ── 一時 WAV / ffmpeg 前処理 ──────────────────────────────────────────────


def _write_tmp_wav(wav_bytes: bytes) -> str:
    """WAV bytes を一時ファイルに書き出してパスを返す。"""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(wav_bytes)
        return f.name


def _which(name: str) -> str | None:
    """shutil.which のラッパ (path にバイナリが存在すれば絶対パス、なければ None)。"""
    return shutil.which(name)


def _build_ffmpeg_args(src: str, dst: str) -> list[str]:
    """ffmpeg 前処理用のコマンド引数を組み立てる。

    生成コマンド:
        ffmpeg -y -loglevel error -i <src> \
          -af "volume=<TTS_VOLUME>,apad=pad_dur=<TTS_TAIL_SILENCE>" \
          -ar <TTS_PRE_RESAMPLE> -ac 1 -f wav <dst>
    """
    volume = _get_tts_volume()
    tail = _get_tts_tail_silence()
    ar = _get_tts_pre_resample()
    af = f"volume={volume},apad=pad_dur={tail}"
    return [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-i",
        src,
        "-af",
        af,
        "-ar",
        str(ar),
        "-ac",
        "1",
        "-f",
        "wav",
        dst,
    ]


async def _preprocess_wav_with_ffmpeg(src_path: str) -> str | None:
    """SBV2 出力 WAV を ffmpeg で前処理 (音量・PCMA 用 16k 単 ch) して新ファイルパスを返す。

    Returns:
        前処理済み WAV の絶対パス。ffmpeg 失敗時 / バイナリ無しなら ``None``。
    """
    ffmpeg = _which("ffmpeg")
    if ffmpeg is None:
        logger.warning("tts_sbv2._preprocess_wav_with_ffmpeg: ffmpeg not found in PATH")
        return None

    dst_path = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
    args = _build_ffmpeg_args(src_path, dst_path)
    # _which が見つけた絶対パスを引数 0 に差し替え
    args[0] = ffmpeg

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning(
                "tts_sbv2._preprocess_wav_with_ffmpeg: ffmpeg rc={} stderr={!r}",
                proc.returncode,
                stderr.decode("utf-8", errors="replace")[:200] if stderr else "",
            )
            try:
                os.unlink(dst_path)
            except OSError:
                pass
            return None
        return dst_path
    except Exception as e:
        logger.warning("tts_sbv2._preprocess_wav_with_ffmpeg: exec failed: {}", e)
        try:
            os.unlink(dst_path)
        except OSError:
            pass
        return None


# ── go2rtc HTTP API POST (v5 14-5-5 / 14-5-12) ────────────────────────────


def _build_go2rtc_url(wav_path: str) -> str:
    """go2rtc POST URL を組み立てる。

    形式:
        {GO2RTC_BASE_URL}/api/streams?dst={TAPO_STREAM_NAME}&src=<URL-encoded ffmpeg src>

    src 形式:
        ffmpeg:<wav_path>#audio=pcma#input=file
    """
    base_url = _get_go2rtc_base_url()
    stream = _get_tapo_stream_name()
    src = f"ffmpeg:{wav_path}#audio=pcma#input=file"
    # # と : を含むので quote(safe="") で完全エンコード
    src_encoded = quote(src, safe="")
    return f"{base_url}/api/streams?dst={quote(stream, safe='')}&src={src_encoded}"


async def _post_to_go2rtc(wav_path: str) -> bool:
    """前処理済み WAV を go2rtc HTTP API へ POST する (silent fail)。

    v5 14-5-5 仕様:
        POST {GO2RTC_BASE_URL}/api/streams?dst={TAPO_STREAM_NAME}&src=ffmpeg:...
        - body 空、Authorization なし、LAN 内認証なし
        - Pi 側に go2rtc バイナリを置かない (HTTP API 単一経路)

    Returns:
        成功時 True、HTTP / ネットワーク失敗時 False (例外は投げない)。
    """
    url = _build_go2rtc_url(wav_path)
    timeout = aiohttp.ClientTimeout(total=_get_timeout())
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.warning(
                        "tts_sbv2._post_to_go2rtc: HTTP {} from {} body={!r}",
                        resp.status,
                        url,
                        body[:200],
                    )
                    return False
                return True
    except Exception as e:
        logger.warning("tts_sbv2._post_to_go2rtc: request failed: {}", e)
        return False


# ── 公開 API: speak() ─────────────────────────────────────────────────────


async def speak(
    text: str,
    target: Target | str = "tapo_speaker",
    speaker_id: int = 0,
    emotion: EmotionDict | None = None,
) -> None:
    """SBV2 で合成した TTS を指定 target に送出する (設計書 v5 14-5-11)。

    Args:
        text: 読み上げ対象テキスト。30 文字 (TTS_CHUNK_MAX_CHARS) を超えると
            句読点優先で分割される。空 / 空白のみは silent fail。
        target: 再生先。
            ``"tapo_speaker"`` → go2rtc HTTP API → Tapo C210 (普段の会話、本実装)
            ``"discord_vc"``   → discord.py voice client (Phase D で本実装、現状スタブ)
            ``"obs_audio"``    → OBS 音声入力 (Phase K で本実装、現状スタブ)
        speaker_id: SBV2 サーバ側の speaker ID (デフォルト 0)。
        emotion: ``{"valence": 0.0-1.0, "arousal": 0.0-1.0}`` 形式の感情 dict。
            ``None`` のときは中立。

    Returns:
        常に ``None``。例外は投げない (失敗時は無音 + logger.warning)。
        v5 14-5-11 でフォールバックは撤廃。
    """
    if not text or not text.strip():
        logger.warning("tts_sbv2.speak: empty text")
        return
    if target not in _VALID_TARGETS:
        logger.warning("tts_sbv2.speak: unknown target {!r}, returning silently", target)
        return

    if target == "discord_vc":
        logger.warning("tts_sbv2.speak: target=discord_vc is not implemented yet (Phase D)")
        return
    if target == "obs_audio":
        logger.warning("tts_sbv2.speak: target=obs_audio is not implemented yet (Phase K)")
        return

    # target == "tapo_speaker": go2rtc HTTP API 経由で Tapo C210 へ送出
    await _warmup_once()

    wav_bytes = await _fetch_wav_bytes(text, speaker_id, emotion)
    if not wav_bytes:
        # _fetch_wav_bytes 内部で warning 済
        return

    src_path = _write_tmp_wav(wav_bytes)
    pre_path: str | None = None
    try:
        pre_path = await _preprocess_wav_with_ffmpeg(src_path)
        if pre_path is None:
            # _preprocess_wav_with_ffmpeg 内部で warning 済
            return

        ok = await _post_to_go2rtc(pre_path)
        if not ok:
            # _post_to_go2rtc 内部で warning 済
            return

        logger.info(
            "tts_sbv2.speak: played via tapo_speaker (text={!r})",
            text[:40],
        )
    finally:
        for p in (src_path, pre_path):
            if not p:
                continue
            try:
                os.unlink(p)
            except OSError:
                pass
