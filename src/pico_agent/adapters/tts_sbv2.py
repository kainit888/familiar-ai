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

go2rtc 連携 (v5 14-5-5 / 14-5-12 確定、Phase C-7 で HTTP pull 方式へ修正):
    - POST {GO2RTC_BASE_URL}/api/streams?dst={TAPO_STREAM_NAME}&src=<URL-encoded ffmpeg URL>
    - src 形式: ffmpeg:<wav_url>#audio=pcma#input=file
    - <wav_url> は Pi 側 WAV 配信サーバの HTTP URL
      (例 http://192.168.10.109:50021/xxxx.wav)。go2rtc (メイン PC) が
      このローカルパスではなく HTTP で WAV を pull する (Phase C-7)。
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
import wave
from pathlib import Path
from typing import Literal, TypeAlias
from urllib.parse import quote, urlencode

import aiohttp
from aiohttp import web
from loguru import logger

# Phase C-5.5 調査用: 標準 logging を loguru と並行発行 (familiar_agent/main.py の
# setup_logging が loguru sink を設定していないため、loguru 出力が app.log に
# 届かない疑い。実機ログで切り分けるための一時マーカー)。
import logging as _stdlogging
_stdlog = _stdlogging.getLogger(__name__)

# ── 型エイリアス ──────────────────────────────────────────────────────────
# 感情 dict は ``{"valence": 0.0-1.0, "arousal": 0.0-1.0, ...}`` 形式。
# Phase E (感情 3 値実装) で具体的なキーを TypedDict 化する想定。
EmotionDict: TypeAlias = dict[str, float]

# speak() の target 値域 (v5 で用途別 3 経路に再構成、14-5-11 節)。
Target: TypeAlias = Literal["tapo_speaker", "discord_vc", "obs_audio"]

# ── 設定 (環境変数で上書き可能、ハードコード禁止) ────────────────────────────
_DEFAULT_BASE_URL = "http://192.168.10.104:5000"
_DEFAULT_TIMEOUT_SEC = 60.0
_DEFAULT_GO2RTC_BASE_URL = "http://192.168.10.104:1984"
_DEFAULT_TAPO_STREAM_NAME = "tapo_c210"
_DEFAULT_TTS_MODEL_NAME = "jvnv-F1-jp"
_DEFAULT_TTS_VOLUME = 0.5
_DEFAULT_TTS_PRE_RESAMPLE = 16000
_DEFAULT_TTS_TAIL_SILENCE = 0.5
_DEFAULT_TTS_CHUNK_MAX_CHARS = 30
_DEFAULT_TTS_CHUNK_DELAY_MS = 0

# go2rtc は ffmpeg:<url>#input=file で WAV を **HTTP pull** する (Phase C-7)。
# Pi 上の WAV を go2rtc (メイン PC) から取りに来させるため、Pi 側で小さな
# HTTP 配信サーバを立てて WAV を公開する。以下はそのパラメータ。
_DEFAULT_TTS_SERVE_PORT = 50021
_DEFAULT_TTS_PI_SELF_IP = "192.168.10.109"
_DEFAULT_TTS_DELETE_DELAY_SEC = 30.0   # 配信 WAV 遅延削除秒の下限 (floor、go2rtc pull 猶予)
# Phase C-8.1: 削除 delay を再生時間ベースで動的算出する係数。長文 TTS で
# go2rtc が pull/再生し終える前に WAV を消してしまう「not found」レースを防ぐ。
_DEFAULT_TTS_DELETE_SAFETY = 1.5       # 再生時間に掛ける安全係数 (TTS_DELETE_SAFETY)
_DEFAULT_TTS_DELETE_MARGIN_SEC = 10.0  # 上乗せ固定マージン秒 (TTS_DELETE_MARGIN_SEC)

# 暖機ファイル (カイニット指定)
_WARMUP_WAV_PATH = Path("/tmp/pico_v3_warmup.wav")
_WARMUP_TEXT = "ん"
_WARMUP_DONE: bool = False

# Pi 側 WAV 配信サーバ (Phase C-7、go2rtc HTTP pull 用)。
# lazy init + singleton: 初回 speak() で起動し、以後は使い回す。
_serve_runner: web.AppRunner | None = None
_serve_dir: Path | None = None
_serve_lock = asyncio.Lock()

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


def _get_serve_port() -> int:
    """Pi 側 WAV 配信サーバの待受ポートを取得 (環境変数 TTS_SERVE_PORT)。"""
    raw = os.environ.get("TTS_SERVE_PORT", "")
    try:
        return int(raw) if raw else _DEFAULT_TTS_SERVE_PORT
    except ValueError:
        return _DEFAULT_TTS_SERVE_PORT


def _get_pi_self_ip() -> str:
    """go2rtc が WAV を取りに来る Pi 自身の LAN IP を取得 (環境変数 TTS_PI_SELF_IP)。"""
    return os.environ.get("TTS_PI_SELF_IP", _DEFAULT_TTS_PI_SELF_IP)


def _get_delete_delay_sec() -> float:
    """配信 WAV の遅延削除秒数の下限 (floor) を取得 (環境変数 TTS_DELETE_DELAY_SEC)。"""
    raw = os.environ.get("TTS_DELETE_DELAY_SEC", "")
    try:
        return float(raw) if raw else _DEFAULT_TTS_DELETE_DELAY_SEC
    except ValueError:
        return _DEFAULT_TTS_DELETE_DELAY_SEC


def _get_delete_safety() -> float:
    """削除 delay の安全係数を取得 (環境変数 TTS_DELETE_SAFETY、既定 1.5)。"""
    raw = os.environ.get("TTS_DELETE_SAFETY", "")
    try:
        return float(raw) if raw else _DEFAULT_TTS_DELETE_SAFETY
    except ValueError:
        return _DEFAULT_TTS_DELETE_SAFETY


def _get_delete_margin_sec() -> float:
    """削除 delay に上乗せする固定マージン秒を取得 (環境変数 TTS_DELETE_MARGIN_SEC、既定 10.0)。"""
    raw = os.environ.get("TTS_DELETE_MARGIN_SEC", "")
    try:
        return float(raw) if raw else _DEFAULT_TTS_DELETE_MARGIN_SEC
    except ValueError:
        return _DEFAULT_TTS_DELETE_MARGIN_SEC


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


# ── SBV2 WAV パーツ取得 (旧 speak() の本体を helper 化、Phase C-8 で list 化) ──


async def _fetch_wav_parts(
    text: str,
    speaker_id: int,
    emotion: EmotionDict | None,
) -> list[bytes]:
    """テキストを 30 文字分割 → SBV2 GET → 各チャンクの WAV bytes を list で返す (silent fail)。

    Phase C-8: 旧 ``_fetch_wav_bytes`` は複数チャンクを ``b"".join()`` でバイト連結して
    いたが、これだと WAV ヘッダの data サイズが第1チャンク分しか宣言されず、ffmpeg が
    1個目だけ読んで打ち切る (実機 ffprobe で確認)。バイト連結をやめ、各チャンクの WAV を
    list で返して呼び出し側で concat demuxer により結合する。

    Args:
        text: 読み上げ対象テキスト (空 / 空白のみは呼び出し側で除外済の前提)。
        speaker_id: SBV2 サーバ側の speaker ID (デフォルト 0)。
        emotion: ``{"valence": 0.0-1.0, "arousal": 0.0-1.0}`` 形式の感情 dict。

    Returns:
        各チャンクの WAV bytes の list。空チャンク (b"") は除外する (部分再生)。
        全滅 / 空入力 / session 例外時は空 list ``[]`` を返す (例外は投げない)。
    """
    chunks = _split_chunks(text.strip())
    if not chunks:
        return []

    logger.debug(
        "tts_sbv2._fetch_wav_parts: text={!r} chunks={} speaker_id={}",
        text[:40],
        len(chunks),
        speaker_id,
    )

    timeout = aiohttp.ClientTimeout(total=_get_timeout())
    parts: list[bytes] = []
    chunk_delay_ms = _get_chunk_delay_ms()
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for i, chunk in enumerate(chunks):
                query = _build_query(chunk, speaker_id, emotion)
                part = await _fetch_one_chunk(session, chunk, query)
                if part:
                    parts.append(part)
                if chunk_delay_ms > 0 and i < len(chunks) - 1:
                    await asyncio.sleep(chunk_delay_ms / 1000.0)
    except Exception as e:
        logger.warning("tts_sbv2._fetch_wav_parts: session-level failure: {}", e)
        return []

    if not parts:
        logger.warning("tts_sbv2._fetch_wav_parts: empty WAV bytes after all chunks")
        return []

    return parts


# ── Pi 側 WAV HTTP 配信サーバ (Phase C-7、go2rtc HTTP pull 用) ─────────────


async def _serve_handler(request: web.Request) -> web.StreamResponse:
    """配信 dir 内の <name>.wav を返す。トラバーサル / 非 wav は 404。"""
    name = Path(request.match_info["name"]).name  # トラバーサル無効化
    if not name.endswith(".wav"):
        return web.Response(status=404)
    if _serve_dir is None:
        return web.Response(status=404)
    target = _serve_dir / name
    if not target.is_file():
        return web.Response(status=404)
    return web.FileResponse(path=target)


async def _ensure_http_server() -> tuple[Path, int]:
    """配信サーバを冪等起動し (root_dir, port) を返す。

    すでに起動済みなら既存の配信 dir と現在の待受ポートを返す。
    未起動なら一時 dir を作り、aiohttp.web で 0.0.0.0:<port> に listen する。
    """
    global _serve_runner, _serve_dir
    async with _serve_lock:
        if _serve_runner is not None and _serve_dir is not None:
            return _serve_dir, _get_serve_port()
        _serve_dir = Path(tempfile.mkdtemp(prefix="pico_tts_serve_"))
        app = web.Application()
        app.router.add_get("/{name}", _serve_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host="0.0.0.0", port=_get_serve_port())
        await site.start()
        _serve_runner = runner
        logger.info("tts_sbv2: WAV serve server up on 0.0.0.0:{}", _get_serve_port())
        return _serve_dir, _get_serve_port()


# ── 一時 WAV / ffmpeg 前処理 ──────────────────────────────────────────────


def _write_tmp_wav(wav_bytes: bytes) -> str:
    """WAV bytes を一時ファイルに書き出してパスを返す。"""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(wav_bytes)
        return f.name


def _write_tmp_wavs(parts: list[bytes]) -> list[str]:
    """複数の WAV bytes をそれぞれ一時ファイルに書き出してパス list を返す (Phase C-8)。"""
    return [_write_tmp_wav(p) for p in parts]


def _which(name: str) -> str | None:
    """shutil.which のラッパ (path にバイナリが存在すれば絶対パス、なければ None)。"""
    return shutil.which(name)


def _build_af_filter() -> str:
    """ffmpeg ``-af`` フィルタ文字列を組み立てる。

    ``volume=<TTS_VOLUME>,apad=pad_dur=<TTS_TAIL_SILENCE>``。
    Phase C-8 で concat 経路と共有するため切り出した (パラメータ値は不変)。
    """
    return f"volume={_get_tts_volume()},apad=pad_dur={_get_tts_tail_silence()}"


def _write_concat_list(src_paths: list[str]) -> str:
    """concat demuxer 用のリストファイルを書き出してパスを返す (Phase C-8)。

    各 path を絶対パス化し、ffmpeg concat demuxer の ``file '<path>'`` 形式で 1 行ずつ
    書く。path 内のシングルクォートは ``'\\''`` でエスケープする。
    echo/redirect ではなく Python の tempfile API で書く。
    """
    lines: list[str] = []
    for p in src_paths:
        abs_path = os.path.abspath(p)
        escaped = abs_path.replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    with tempfile.NamedTemporaryFile(
        suffix=".txt", delete=False, mode="w", encoding="utf-8"
    ) as f:
        f.write("\n".join(lines) + "\n")
        return f.name


def _build_concat_ffmpeg_args(list_path: str, dst: str) -> list[str]:
    """concat demuxer + 前処理 (音量・PCMA 用 16k 単 ch) の ffmpeg 引数を組み立てる。

    生成コマンド:
        ffmpeg -y -loglevel error -f concat -safe 0 -i <list_path> \
          -af "volume=<TTS_VOLUME>,apad=pad_dur=<TTS_TAIL_SILENCE>" \
          -ar <TTS_PRE_RESAMPLE> -ac 1 -f wav <dst>
    """
    return [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        list_path,
        "-af",
        _build_af_filter(),
        "-ar",
        str(_get_tts_pre_resample()),
        "-ac",
        "1",
        "-f",
        "wav",
        dst,
    ]


async def _concat_and_preprocess(
    src_paths: list[str], out_dir: Path | None = None
) -> str | None:
    """複数の SBV2 生 WAV を concat demuxer で結合 + 前処理して新ファイルパスを返す (Phase C-8)。

    旧 ``_preprocess_wav_with_ffmpeg`` (単一 WAV 前処理) を置き換える。バイト連結ではなく
    ffmpeg concat demuxer で結合することで、全チャンクの音声が欠けずに再生される。

    Args:
        src_paths: 結合対象 (SBV2 生 WAV) の絶対パス list。
        out_dir: 出力先ディレクトリ。指定時はその dir 内に WAV を作る
            (Phase C-7: go2rtc HTTP pull 用の配信 dir)。``None`` なら
            従来通り tempfile デフォルト位置に作る (後方互換)。

    Returns:
        結合・前処理済み WAV の絶対パス。ffmpeg 失敗時 / バイナリ無し / 入力空なら ``None``。
    """
    ffmpeg = _which("ffmpeg")
    if ffmpeg is None:
        logger.warning("tts_sbv2._concat_and_preprocess: ffmpeg not found in PATH")
        return None
    if not src_paths:
        logger.warning("tts_sbv2._concat_and_preprocess: no source WAV paths given")
        return None

    list_path = _write_concat_list(src_paths)
    if out_dir is not None:
        dst_path = tempfile.NamedTemporaryFile(
            suffix=".wav", delete=False, dir=str(out_dir)
        ).name
    else:
        dst_path = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
    args = _build_concat_ffmpeg_args(list_path, dst_path)
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
                "tts_sbv2._concat_and_preprocess: ffmpeg rc={} stderr={!r}",
                proc.returncode,
                stderr.decode("utf-8", errors="replace")[:200] if stderr else "",
            )
            _unlink_quiet(dst_path)
            return None
        return dst_path
    except Exception as e:
        logger.warning("tts_sbv2._concat_and_preprocess: exec failed: {}", e)
        _unlink_quiet(dst_path)
        return None
    finally:
        _unlink_quiet(list_path)


# ── go2rtc HTTP API POST (v5 14-5-5 / 14-5-12) ────────────────────────────


def _build_go2rtc_url(wav_url: str) -> str:
    """go2rtc POST URL を組み立てる (Phase C-7: HTTP pull 方式)。

    形式:
        {GO2RTC_BASE_URL}/api/streams?dst={TAPO_STREAM_NAME}&src=<URL-encoded ffmpeg src>

    src 形式:
        ffmpeg:<wav_url>#audio=pcma#input=file

    ``wav_url`` は go2rtc (メイン PC) が取りに来る Pi 側配信サーバの HTTP URL
    (例 ``http://192.168.10.109:50021/xxxx.wav``)。``#input=file`` は維持必須。
    """
    base_url = _get_go2rtc_base_url()
    stream = _get_tapo_stream_name()
    src = f"ffmpeg:{wav_url}#audio=pcma#input=file"
    # # と : を含むので quote(safe="") で完全エンコード
    src_encoded = quote(src, safe="")
    return f"{base_url}/api/streams?dst={quote(stream, safe='')}&src={src_encoded}"


async def _post_to_go2rtc(wav_url: str) -> bool:
    """配信 WAV の HTTP URL を go2rtc HTTP API へ POST する (silent fail)。

    v5 14-5-5 仕様 (Phase C-7 で src をローカルパスから HTTP URL へ変更):
        POST {GO2RTC_BASE_URL}/api/streams?dst={TAPO_STREAM_NAME}&src=ffmpeg:...
        - body 空、Authorization なし、LAN 内認証なし
        - Pi 側に go2rtc バイナリを置かない (HTTP API 単一経路)
        - ``wav_url`` は go2rtc が取りに来る Pi 側配信サーバの HTTP URL

    Returns:
        成功時 True、HTTP / ネットワーク失敗時 False (例外は投げない)。
    """
    url = _build_go2rtc_url(wav_url)
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


# ── 一時ファイル削除 (Phase C-7: 配信 WAV は遅延削除) ─────────────────────


def _unlink_quiet(path: str) -> None:
    """ファイルを削除する。存在しない / 権限エラーは握りつぶす。"""
    try:
        os.unlink(path)
    except OSError:
        pass


async def _delayed_unlink(path: str, delay: float) -> None:
    """delay 秒待ってから path を削除する (go2rtc が pull し終える猶予)。"""
    await asyncio.sleep(delay)
    _unlink_quiet(path)


def _wav_duration_sec(path: str) -> float | None:
    """WAV ファイルの再生時間 (秒) を標準 ``wave`` で求める (silent fail)。

    ``getnframes() / getframerate()`` で算出する。ファイルが読めない / WAV で
    ない / framerate が 0 など壊れている場合は ``None`` を返す (例外は投げない)。
    """
    try:
        with wave.open(path, "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
        if rate <= 0:
            return None
        return frames / float(rate)
    except (OSError, wave.Error, EOFError) as e:
        logger.debug("tts_sbv2._wav_duration_sec: cannot read {!r}: {}", path, e)
        return None


def _compute_delete_delay(pre_path: str) -> float:
    """配信 WAV の遅延削除 delay を再生時間ベースで動的算出する (Phase C-8.1)。

    長文 TTS は連結後の再生時間が固定 floor (TTS_DELETE_DELAY_SEC=30) を超えて
    しまい、go2rtc が pull / 再生し終える前に WAV が消えて「not found」レースに
    なる。再生時間に安全係数 + マージンを掛けた値と floor の大きい方を採用する。

    Args:
        pre_path: 連結・前処理済み配信 WAV の絶対パス。

    Returns:
        ``floor`` (= ``_get_delete_delay_sec()``) を下限とした削除 delay 秒。
        WAV 長が読めない場合は安全側に倒して ``floor`` を返す。
    """
    dur = _wav_duration_sec(pre_path)
    floor = _get_delete_delay_sec()
    if dur is None:
        return floor
    return max(floor, dur * _get_delete_safety() + _get_delete_margin_sec())


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
    _stdlog.info(
        "tts_sbv2.speak: ENTER target=%s text_len=%d",
        target,
        len(text) if text else 0,
    )
    if not text or not text.strip():
        logger.warning("tts_sbv2.speak: empty text")
        _stdlog.info("tts_sbv2.speak: EXIT empty_text")
        return
    if target not in _VALID_TARGETS:
        logger.warning("tts_sbv2.speak: unknown target {!r}, returning silently", target)
        _stdlog.info("tts_sbv2.speak: EXIT invalid_target=%s", target)
        return

    if target == "discord_vc":
        logger.warning("tts_sbv2.speak: target=discord_vc is not implemented yet (Phase D)")
        _stdlog.info("tts_sbv2.speak: EXIT stub_discord_vc")
        return
    if target == "obs_audio":
        logger.warning("tts_sbv2.speak: target=obs_audio is not implemented yet (Phase K)")
        _stdlog.info("tts_sbv2.speak: EXIT stub_obs_audio")
        return

    # target == "tapo_speaker": go2rtc HTTP API 経由で Tapo C210 へ送出
    await _warmup_once()

    # Phase C-8: 各チャンクの WAV を list で取得 (バイト連結しない)。
    parts = await _fetch_wav_parts(text, speaker_id, emotion)
    if not parts:
        # _fetch_wav_parts 内部で warning 済
        _stdlog.info("tts_sbv2.speak: EXIT sbv2_failed")
        return

    src_paths = _write_tmp_wavs(parts)
    pre_path: str | None = None
    try:
        # Phase C-7: 配信サーバを lazy 起動し、ffmpeg 出力をその配信 dir に置く。
        serve_dir, port = await _ensure_http_server()
        # Phase C-8: concat demuxer で全チャンクを結合 + 前処理 (途中切れ修正)。
        pre_path = await _concat_and_preprocess(src_paths, out_dir=serve_dir)
        if pre_path is None:
            # _concat_and_preprocess 内部で warning 済
            _stdlog.info("tts_sbv2.speak: EXIT ffmpeg_failed")
            return

        # go2rtc (メイン PC) が取りに来る Pi 側 HTTP URL を組み立てて POST。
        name = Path(pre_path).name
        wav_url = f"http://{_get_pi_self_ip()}:{port}/{name}"
        ok = await _post_to_go2rtc(wav_url)
        if not ok:
            # _post_to_go2rtc 内部で warning 済
            _stdlog.info("tts_sbv2.speak: EXIT go2rtc_post_failed")
            return

        logger.info(
            "tts_sbv2.speak: played via tapo_speaker (text={!r})",
            text[:40],
        )
        _stdlog.info("tts_sbv2.speak: EXIT ok")
    finally:
        for sp in src_paths:
            _unlink_quiet(sp)  # SBV2 生 WAV 群は即削除
        if pre_path:  # 配信 WAV は go2rtc が pull し終えるまで遅延削除
            # Phase C-8.1: 固定 30s ではなく再生時間ベースの動的 delay。長文で
            # go2rtc が pull/再生し終える前に消す「not found」レースを防ぐ。
            asyncio.create_task(_delayed_unlink(pre_path, _compute_delete_delay(pre_path)))
