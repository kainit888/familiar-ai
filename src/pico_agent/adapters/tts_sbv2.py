"""TTS adapter (Style-BERT-VITS2) - 設計書 v4.0 第 7-2 章 / v4.2 第 14-5 章。

メイン PC で稼働中の Style-BERT-VITS2 サーバ (デフォルト 192.168.10.104:5000)
に GET /voice?text=...&model_name=... を投げて WAV bytes を取得する。

公開 I/F (設計書 7-2 章 + v4.2 14-5 章、Phase C-3 で固定):
    async def speak(
        text: str,
        speaker_id: int = 0,
        emotion: dict | None = None,    # valence/arousal で声色変化
        target: str = "discord_vc",     # 既存値域維持
    ) -> bytes
        "30 文字制限 → 句読点優先で分割 → 順次取得して連結"

    async def play_with_fallback(
        text: str,
        target: str = "auto",  # "tapo_speaker" | "main_pc" | "rpi5" | "auto"
        ...
    ) -> tuple[bool, str]:
        "WAV bytes 取得 + 物理再生まで実行、target 失敗時は順次フォールバック"

エラーハンドリング方針 (絶対遵守):
    - 例外を raise せず silent fail + logger.warning
    - 失敗時は空 bytes を返す / play_with_fallback() は (False, 理由) を返す
    - target=tapo_speaker は go2rtc POST /api/streams?dst=...&src=ffmpeg:... 方式

go2rtc 連携 (v4.2 14-5、カイニット実機検証で確定):
    - POST {GO2RTC_BASE_URL}/api/streams?dst={TAPO_STREAM_NAME}&src=<URL-encoded ffmpeg URL>
    - src 形式: ffmpeg:<wav_path>#audio=pcma#input=file
    - Body 空、Authorization なし、Content-Type なし
    - LAN 内認証なし (192.168.10.104:1984)

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
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import aiohttp
from loguru import logger

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

_VALID_TARGETS = ("discord_vc", "local_speaker")


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


def _is_go2rtc_enabled() -> bool:
    """環境変数 GO2RTC_ENABLED=1/true/yes のときのみ go2rtc 再生を試みる。"""
    return os.environ.get("GO2RTC_ENABLED", "").strip().lower() in ("1", "true", "yes")


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


def _emotion_to_style_weight(emotion: dict[str, Any] | None) -> float:
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
    emotion: dict[str, Any] | None,
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


# ── 公開 API: speak() ─────────────────────────────────────────────────────


async def speak(
    text: str,
    speaker_id: int = 0,
    emotion: dict[str, Any] | None = None,
    target: str = "discord_vc",
) -> bytes:
    """テキストを Style-BERT-VITS2 に投げて WAV bytes を返す (設計書 7-2 章)。

    Args:
        text: 読み上げ対象テキスト。30 文字 (TTS_CHUNK_MAX_CHARS) を超えると
            句読点優先で分割される。
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

    await _warmup_once()

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
        logger.warning("tts_sbv2.speak: session-level failure: {}", e)
        return b""

    if not audio_parts:
        return b""

    return b"".join(audio_parts)


# ── v4.2 14-5: フォールバック再生チェーン ────────────────────────────────────


_AUTO_FALLBACK_CHAIN: tuple[str, ...] = ("tapo_speaker", "main_pc", "rpi5")


def _write_tmp_wav(wav_bytes: bytes) -> str:
    """WAV bytes を一時ファイルに書き出してパスを返す。"""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(wav_bytes)
        return f.name


def _which(name: str) -> str | None:
    """shutil.which のラッパ (path にバイナリが存在すれば絶対パス、なければ None)。"""
    return shutil.which(name)


# ── ffmpeg 前処理 (RPi5 ホスト側) ─────────────────────────────────────────


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


# ── バックエンド: main_pc / rpi5 ─────────────────────────────────────────


async def _play_via_main_pc(wav_bytes: bytes) -> bool:
    """メイン PC のスピーカーで WAV を再生する (mpv / ffplay 経由)。

    実装が走るマシン (RPi5 か別 PC) の音声出力デバイスで mpv / ffplay 再生を試みる。

    Returns:
        再生プロセスが exit code 0 で終わったら True、それ以外 False。
    """
    if not wav_bytes:
        return False
    bin_path = _which("mpv") or _which("ffplay")
    if bin_path is None:
        logger.warning("tts_sbv2._play_via_main_pc: neither mpv nor ffplay found in PATH")
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
        logger.warning("tts_sbv2._play_via_rpi5: not on Linux ({}); skipping", sys.platform)
        return False
    bin_path = _which("aplay") or _which("paplay")
    if bin_path is None:
        logger.warning("tts_sbv2._play_via_rpi5: neither aplay nor paplay found in PATH")
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


# ── バックエンド: tapo_speaker (go2rtc 経由) ─────────────────────────────


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


async def _play_via_go2rtc(wav_bytes: bytes) -> bool:
    """go2rtc REST API 経由で Tapo C210 スピーカーに WAV を流す。

    GO2RTC_ENABLED=1/true/yes でないと no-op (False)。
    ffmpeg 前処理 → 一時 WAV → POST /api/streams?dst=...&src=ffmpeg:... を実行。

    Returns:
        - GO2RTC_ENABLED が偽 → False
        - 空 bytes → False
        - ffmpeg / HTTP 失敗 → False (silent fail)
        - 成功 → True
    """
    if not wav_bytes:
        return False
    if not _is_go2rtc_enabled():
        logger.debug("tts_sbv2._play_via_go2rtc: GO2RTC_ENABLED not set, skipping")
        return False

    # SBV2 出力を tmp に保存 → ffmpeg 前処理 → 結果を go2rtc に投げる
    src_path = _write_tmp_wav(wav_bytes)
    pre_path: str | None = None
    try:
        pre_path = await _preprocess_wav_with_ffmpeg(src_path)
        if pre_path is None:
            logger.warning("tts_sbv2._play_via_go2rtc: ffmpeg preprocess failed")
            return False

        url = _build_go2rtc_url(pre_path)
        timeout = aiohttp.ClientTimeout(total=_get_timeout())
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url) as resp:
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
    finally:
        for p in (src_path, pre_path):
            if not p:
                continue
            try:
                os.unlink(p)
            except OSError:
                pass


_BACKENDS: dict[str, Any] = {
    "tapo_speaker": _play_via_go2rtc,
    "main_pc": _play_via_main_pc,
    "rpi5": _play_via_rpi5,
}


# ── 公開 API: play_with_fallback() ────────────────────────────────────────


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
        - success=False なら played_via は理由文字列 ("empty_text" / "no_audio" / "all_failed")
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
