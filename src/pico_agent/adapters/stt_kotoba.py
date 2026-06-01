"""STT adapter (Kotoba-Whisper) - 設計書 v4.0 第 7-3 章 / v4.2 第 14-4 章。

メイン PC で稼働中の ``whisper_server.py`` (デフォルト
192.168.10.104:8765) の /transcribe エンドポイントに音声を POST して
書き起こしテキストを取得する。

公開 I/F (設計書 7-3 章 + v4.2 14-4 章、Phase C-4 で固定):
    async def transcribe(audio_bytes: bytes, sample_rate: int = 16000) -> str
        "1 発話 WAV/PCM を投げて書き起こしテキストを得る"

    def start_rtsp_subscription(
        on_speech: Callable[[str], Awaitable[None]],
        rtsp_url: str | None = None,
        *,
        vad_threshold: float = 0.5,
        min_silence_ms: int = 500,
        chunk_duration_ms: int = 30,
    ) -> asyncio.Task
        "Tapo C210 RTSP 音声トラックを常駐購読し、無音区間で区切って
         1 発話 = 1 transcribe 呼び出し → on_speech(text)"

TTS との対比 (Phase C-4 設計時の整理):
    - TTS は「テキストを音声方向へ分解」: 1 文を 30 字ウィンドウで句読点優先 split
      → 順次合成 → Tapo C210 へストリーミング再生 (短いユニット派)。
    - STT は「音声をテキスト方向へ統合」: 連続 RTSP 音声 → 無音区間で切る
      → 1 発話 (PCM) を WAV にラップ → Kotoba-Whisper に 1 リクエスト
      → 1 書き起こしを on_speech callback へ流す (まとまり派)。
    - 方向は逆だが、共通理念は「意味のまとまりで区切って LLM-friendly な単位にする」。
      30 字 (TTS) と無音区切り (STT) はどちらも「LLM が扱いやすい 1 文単位」。

エラーハンドリング方針 (絶対遵守):
    - 例外を raise せず silent fail + logger.warning
    - transcribe() 失敗時は空文字を返す
    - start_rtsp_subscription() は依存欠落 (ffmpeg なし / URL 無) なら no-op タスクを返す
    - ffmpeg 異常終了は STT_FFMPEG_RESTART_BACKOFF_SEC 待ってから再接続

VAD バックエンド (Phase C-4 で確定):
    - ffmpeg silencedetect filter を採用 (uv add 不要、aarch64 wheel 不要)
    - 1 ffmpeg プロセスから 2 出力をフォーク:
        * `-map 0:a -ac 1 -ar 16000 -f s16le pcm_s16le pipe:1`: stdout に PCM
        * `-map 0:a -af silencedetect=... -f null /dev/null`: stderr に silence_start/end
    - silencedetect の `silence_end: T | silence_duration: D` で
      「[T-D, T] が無音」と判定 → 直前の発話セグメントを flush
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import shutil
import wave
from collections.abc import Awaitable, Callable
from typing import TypedDict
from urllib.parse import quote

import aiohttp
from loguru import logger

from pico_agent.stt_hallucination_filter import is_whisper_hallucination

# Phase C-5.5 調査用: 標準 logging を loguru と並行発行 (familiar_agent/main.py の
# setup_logging が loguru sink を設定していないため、loguru 出力が app.log に
# 届かない疑い。実機ログで切り分けるための一時マーカー)。
import logging as _stdlogging
_stdlog = _stdlogging.getLogger(__name__)


# ── 型エイリアス ──────────────────────────────────────────────────────────
class _SilenceEvent(TypedDict):
    """ffmpeg silencedetect filter の 1 イベントを表す内部 dict。

    Attributes:
        event: ``"start"`` (無音開始) または ``"end"`` (無音終了)。
        time: イベント発生時刻 (秒、ffmpeg の入力時刻軸)。
        duration: ``"end"`` 時のみ silence_duration 値 (秒)。
            ``"start"`` または duration 欠落時は ``None``。
    """

    event: str
    time: float
    duration: float | None

# ── 設定 (環境変数で上書き可能、ハードコード禁止) ────────────────────────────
_DEFAULT_BASE_URL = "http://192.168.10.104:8765/transcribe"
_DEFAULT_TIMEOUT_SEC = 60.0

# Phase C-4 RTSP 購読 (planner 確定デフォルト)
_DEFAULT_VAD_BACKEND = "ffmpeg"
_DEFAULT_VAD_NOISE_DB = "-30dB"
_DEFAULT_VAD_MIN_SILENCE_SEC = 0.5
_DEFAULT_VAD_MAX_SEGMENT_SEC = 15.0
_DEFAULT_VAD_MIN_SEGMENT_SEC = 0.3
_DEFAULT_FFMPEG_RESTART_BACKOFF_SEC = 5.0
_DEFAULT_CAMERA_RTSP_PATH = "stream1"
_DEFAULT_RTSP_PORT = 554
_PCM_SAMPLE_RATE = 16000
_PCM_BYTES_PER_SEC = _PCM_SAMPLE_RATE * 2  # mono s16le

# silencedetect 出力パース用 (stderr に出る)
# 例: [silencedetect @ 0x...] silence_start: 2.345
# 例: [silencedetect @ 0x...] silence_end: 5.678 | silence_duration: 3.333
_SILENCE_RE = re.compile(
    r"silence_(start|end):\s*([0-9.]+)(?:\s*\|\s*silence_duration:\s*([0-9.]+))?"
)


# ── 環境変数アクセサ ──────────────────────────────────────────────────────


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


def _get_vad_backend() -> str:
    """環境変数 STT_VAD_BACKEND で VAD 実装を切り替え (default: ffmpeg)。"""
    return os.environ.get("STT_VAD_BACKEND", _DEFAULT_VAD_BACKEND).strip().lower() or _DEFAULT_VAD_BACKEND


def _get_vad_noise_db() -> str:
    """silencedetect の noise 閾値 (例: '-30dB')。"""
    raw = os.environ.get("STT_VAD_NOISE_DB", "").strip()
    return raw or _DEFAULT_VAD_NOISE_DB


def _get_vad_min_silence_sec() -> float:
    """silencedetect の d= 引数 (これより長い無音で発話終端と判定)。"""
    raw = os.environ.get("STT_VAD_MIN_SILENCE_SEC", "")
    try:
        return float(raw) if raw else _DEFAULT_VAD_MIN_SILENCE_SEC
    except ValueError:
        return _DEFAULT_VAD_MIN_SILENCE_SEC


def _get_vad_max_segment_sec() -> float:
    """1 発話セグメントの上限秒。超えたら強制 flush して whisper へ。"""
    raw = os.environ.get("STT_VAD_MAX_SEGMENT_SEC", "")
    try:
        return float(raw) if raw else _DEFAULT_VAD_MAX_SEGMENT_SEC
    except ValueError:
        return _DEFAULT_VAD_MAX_SEGMENT_SEC


def _get_vad_min_segment_sec() -> float:
    """これ未満のセグメントは捨てる (短すぎる音はノイズ扱い)。"""
    raw = os.environ.get("STT_VAD_MIN_SEGMENT_SEC", "")
    try:
        return float(raw) if raw else _DEFAULT_VAD_MIN_SEGMENT_SEC
    except ValueError:
        return _DEFAULT_VAD_MIN_SEGMENT_SEC


def _get_ffmpeg_restart_backoff_sec() -> float:
    """ffmpeg 異常終了時の再起動待機 (秒)。"""
    raw = os.environ.get("STT_FFMPEG_RESTART_BACKOFF_SEC", "")
    try:
        return float(raw) if raw else _DEFAULT_FFMPEG_RESTART_BACKOFF_SEC
    except ValueError:
        return _DEFAULT_FFMPEG_RESTART_BACKOFF_SEC


# ── RTSP URL 組み立て / マスク ─────────────────────────────────────────────


def _get_rtsp_url() -> str | None:
    """STT_RTSP_URL が設定されていればそれを優先、未設定なら CAMERA_* から組み立て。

    優先順:
        1. ``STT_RTSP_URL`` (空白除去後に値があればそのまま返す)
        2. ``CAMERA_HOST`` + ``CAMERA_USERNAME`` + ``CAMERA_PASSWORD`` から
           ``rtsp://user:pass@host:554/stream1`` を組み立て
        3. いずれも揃わなければ ``None``

    パスワードはここでは URL エンコードしておく (記号入りパスワード対策)。
    """
    raw = os.environ.get("STT_RTSP_URL", "").strip()
    if raw:
        return raw

    host = os.environ.get("CAMERA_HOST", "").strip()
    user = os.environ.get("CAMERA_USERNAME", "").strip()
    password = os.environ.get("CAMERA_PASSWORD", "").strip()
    if not host or not user or not password:
        return None

    user_enc = quote(user, safe="")
    pass_enc = quote(password, safe="")
    path = _DEFAULT_CAMERA_RTSP_PATH
    return f"rtsp://{user_enc}:{pass_enc}@{host}:{_DEFAULT_RTSP_PORT}/{path}"


def _mask_rtsp_url(url: str) -> str:
    """ログ出力用に rtsp://user:pass@host:port/path を rtsp://***@host:port/path にマスク。

    認証情報が無い URL はそのまま返す (例: rtsp://example/stream)。
    """
    try:
        if "@" not in url:
            return url
        scheme_sep = url.find("://")
        if scheme_sep < 0:
            return url
        scheme = url[: scheme_sep + 3]
        rest = url[scheme_sep + 3 :]
        at_idx = rest.rfind("@")
        if at_idx < 0:
            return url
        host_and_path = rest[at_idx + 1 :]
        return f"{scheme}***@{host_and_path}"
    except Exception:
        return url


# ── 依存ライブラリ存在判定 ──────────────────────────────────────────────────


def _ffmpeg_available() -> bool:
    """ffmpeg バイナリが PATH にあるかを判定 (RTSP demux + 変換に必要)。"""
    return shutil.which("ffmpeg") is not None


def _vad_backend_available(backend: str) -> bool:
    """指定 VAD バックエンドが利用可能か判定。

    現状サポート:
        - ``ffmpeg``: ffmpeg バイナリの存在のみで判定 (silencedetect は標準フィルタ)
        - ``disabled`` / 不明な値: 常に False (no-op に倒す)
    """
    if backend == "ffmpeg":
        return _ffmpeg_available()
    return False


def _silero_vad_available() -> bool:
    """後方互換用の薄いラッパ。

    Phase C-1 のスケルトン段階で `_silero_vad_available()` を直接 monkeypatch する
    テストが存在するため、import 可能・呼び出し可能なまま残す。本実装では silero は
    使わないので、現在の VAD バックエンド (default: ffmpeg) の可用性を返す。
    """
    return _vad_backend_available(_get_vad_backend())


# ── transcribe (HTTP POST、Phase C-1 から継続) ─────────────────────────────


async def transcribe(audio_bytes: bytes, sample_rate: int = 16000) -> str:
    """音声 bytes を Kotoba-Whisper に投げて書き起こしテキストを返す。

    Args:
        audio_bytes: WAV (PCM16) または OGG/Opus 等の音声 bytes。
        sample_rate: サンプリングレート (whisper_server 側でリサンプルされる
            ためメタデータ扱い、未送信のサーバ実装にも対応)。

    Returns:
        書き起こしテキスト。失敗時 / 空入力時は空文字 (例外は投げない)。
    """
    _stdlog.info(
        "stt_kotoba.transcribe: ENTER bytes=%d sr=%d",
        len(audio_bytes) if audio_bytes else 0,
        sample_rate,
    )
    if not audio_bytes:
        logger.warning("stt_kotoba.transcribe: empty audio_bytes")
        _stdlog.info("stt_kotoba.transcribe: EXIT error=empty_audio_bytes")
        return ""

    url = _get_base_url()
    timeout = aiohttp.ClientTimeout(total=_get_timeout())

    form = aiohttp.FormData()
    form.add_field(
        "audio",
        audio_bytes,
        filename="audio.wav",
        content_type="audio/wav",
    )
    form.add_field("sample_rate", str(int(sample_rate)))

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            _stdlog.info("stt_kotoba.transcribe: POST %s", url)
            async with session.post(url, data=form) as resp:
                content_type = resp.headers.get("Content-Type", "")
                _stdlog.info(
                    "stt_kotoba.transcribe: status=%d ct=%s",
                    resp.status,
                    content_type,
                )
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(
                        "stt_kotoba: HTTP {} from {} body={!r}",
                        resp.status,
                        url,
                        body[:200],
                    )
                    _stdlog.info(
                        "stt_kotoba.transcribe: EXIT error=http_%d", resp.status
                    )
                    return ""
                # whisper_server は JSON {"text": "..."} を返す想定。
                # plain text を返す実装にも対応するため両方試す。
                if "json" in content_type.lower():
                    try:
                        data = await resp.json()
                    except Exception as e:
                        logger.warning("stt_kotoba: JSON decode failed: {}", e)
                        _stdlog.info(
                            "stt_kotoba.transcribe: EXIT error=json_decode_failed"
                        )
                        return ""
                    text = data.get("text", "") if isinstance(data, dict) else ""
                    result = str(text).strip()
                    _stdlog.info(
                        "stt_kotoba.transcribe: EXIT text_len=%d",
                        len(result) if result else 0,
                    )
                    return result
                # text/plain fallback。
                text = await resp.text()
                result = text.strip()
                _stdlog.info(
                    "stt_kotoba.transcribe: EXIT text_len=%d",
                    len(result) if result else 0,
                )
                return result
    except Exception as e:
        logger.warning("stt_kotoba.transcribe: request failed: {}", e)
        _stdlog.info("stt_kotoba.transcribe: EXIT error=%s", e)
        return ""


# ── v4.2 14-4: Tapo RTSP 音声トラック購読 (Phase C-4 本実装) ────────────────


async def _noop_subscription_loop(reason: str) -> None:
    """依存が揃っていない時の no-op ループ (起動ログだけ出して即終了)。"""
    logger.warning(
        "stt_kotoba.start_rtsp_subscription: skipped ({}); "
        "STT live capture disabled until dependencies are configured",
        reason,
    )


def _build_ffmpeg_rtsp_cmd(
    rtsp_url: str,
    *,
    noise_db: str,
    min_silence_sec: float,
) -> list[str]:
    """ffmpeg を 1 入力 2 出力で起動するコマンドを組み立てる。

    - 出力 1 (stdout): PCM s16le 16kHz mono、後段で発話セグメント切り出し
    - 出力 2 (/dev/null): silencedetect filter、stderr に silence_start/end を出す

    Phase C-4 確定:
        - ``-loglevel info`` 必須 (warning だと silencedetect 出力が出ない)
        - ``-rtsp_transport tcp`` で UDP より安定
        - ``-timeout 5000000`` (μs) で RTSP demuxer の socket TCP I/O timeout 5 秒

    Note:
        旧 ``-stimeout`` は ffmpeg 5.0 で削除された (RTSP demuxer の旧オプション)。
        ffmpeg 7.x では ``-timeout`` が同義の置き換え。`-stimeout` を渡すと
        "Unrecognized option 'stimeout'" で即終了する (Pi5 ffmpeg 7.1.3 で実証)。
    """
    return [
        "ffmpeg",
        "-nostdin",
        "-loglevel",
        "info",
        "-rtsp_transport",
        "tcp",
        "-timeout",
        "5000000",
        "-i",
        rtsp_url,
        # 出力 1: PCM を stdout へ
        "-map",
        "0:a",
        "-ac",
        "1",
        "-ar",
        str(_PCM_SAMPLE_RATE),
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "pipe:1",
        # 出力 2: silencedetect (結果は stderr に出る、null sink)
        "-map",
        "0:a",
        "-af",
        f"silencedetect=noise={noise_db}:d={min_silence_sec}",
        "-f",
        "null",
        "/dev/null",
    ]


def _parse_silencedetect_line(line: str) -> _SilenceEvent | None:
    """ffmpeg stderr 1 行を見て silence_start / silence_end を抽出。

    Returns:
        マッチした場合: ``_SilenceEvent`` TypedDict
        (``{"event": "start"|"end", "time": float, "duration": float|None}``)。
        マッチしなければ None。
    """
    if not line:
        return None
    m = _SILENCE_RE.search(line)
    if not m:
        return None
    event = m.group(1)  # "start" or "end"
    try:
        t = float(m.group(2))
    except (TypeError, ValueError):
        return None
    duration: float | None = None
    if m.group(3):
        try:
            duration = float(m.group(3))
        except ValueError:
            duration = None
    return {"event": event, "time": t, "duration": duration}


def _wrap_pcm_to_wav(pcm_bytes: bytes, sample_rate: int = _PCM_SAMPLE_RATE) -> bytes:
    """PCM s16le mono bytes を WAV (RIFF) bytes にラップ。

    標準ライブラリ ``wave`` + ``io.BytesIO`` で 44-byte WAV ヘッダを書く。
    Kotoba-Whisper の /transcribe エンドポイントは WAV を期待。
    """
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # s16
        wf.setframerate(int(sample_rate))
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


# ── サブスクリプションループ本体 (内部ヘルパ + 公開 API) ───────────────────


async def _emit_segment(
    pcm_buffer: bytearray,
    on_speech: Callable[[str], Awaitable[None]],
    *,
    min_segment_sec: float,
) -> None:
    """蓄積した PCM バッファ 1 発話を whisper へ流し on_speech() を呼ぶ。

    短すぎる (min_segment_sec 未満) セグメントは捨てる。
    transcribe / on_speech の例外は silent fail (raise しない)。
    """
    if not pcm_buffer:
        return
    duration_sec = len(pcm_buffer) / _PCM_BYTES_PER_SEC
    if duration_sec < min_segment_sec:
        logger.debug(
            "stt_kotoba.subscription: segment too short ({:.2f}s < {:.2f}s), dropped",
            duration_sec,
            min_segment_sec,
        )
        return
    wav_bytes = _wrap_pcm_to_wav(bytes(pcm_buffer), sample_rate=_PCM_SAMPLE_RATE)
    try:
        text = await transcribe(wav_bytes, sample_rate=_PCM_SAMPLE_RATE)
    except Exception as e:
        logger.warning("stt_kotoba.subscription: transcribe failed: {}", e)
        return
    if not text:
        return
    if is_whisper_hallucination(text):
        logger.debug(
            "stt_kotoba: dropped hallucination text={!r} len={}", text, len(text)
        )
        return
    # 通過した発話を DEBUG で記録 (将来の grounding / 認識履歴用。INFO は運用ログ汚染)。
    logger.debug("stt_kotoba: transcribed text={!r} len={}", text, len(text))
    try:
        await on_speech(text)
    except Exception as e:
        logger.warning("stt_kotoba.subscription: on_speech callback failed: {}", e)


async def _pcm_reader(
    stdout: asyncio.StreamReader,
    pcm_buffer: bytearray,
    on_speech: Callable[[str], Awaitable[None]],
    *,
    max_segment_sec: float,
    min_segment_sec: float,
    flush_event: asyncio.Event,
) -> None:
    """ffmpeg stdout から PCM を読み続け、無音通知 (flush_event) でセグメントを emit。

    max_segment_sec 超過は強制 flush。EOF (ffmpeg 終了) で抜ける。
    """
    max_bytes = int(max_segment_sec * _PCM_BYTES_PER_SEC)
    chunk_size = 4096
    while True:
        try:
            chunk = await stdout.read(chunk_size)
        except Exception as e:
            logger.warning("stt_kotoba.subscription: pcm read failed: {}", e)
            break
        if not chunk:
            break  # ffmpeg EOF
        pcm_buffer.extend(chunk)
        if len(pcm_buffer) >= max_bytes:
            logger.debug(
                "stt_kotoba.subscription: max segment reached ({}B), force flush",
                len(pcm_buffer),
            )
            await _emit_segment(pcm_buffer, on_speech, min_segment_sec=min_segment_sec)
            pcm_buffer.clear()
        if flush_event.is_set():
            flush_event.clear()
            await _emit_segment(pcm_buffer, on_speech, min_segment_sec=min_segment_sec)
            pcm_buffer.clear()


async def _stderr_reader(
    stderr: asyncio.StreamReader,
    flush_event: asyncio.Event,
) -> None:
    """ffmpeg stderr を行単位で読み、silence_end を見たら flush_event をセット。

    silence_end は「無音区間が終わった (= 発話が始まった可能性)」イベントだが、
    その直前 (silence_start 〜 silence_end) は無音なので、ここで「直前の発話を
    完了させて流す」タイミングとして扱う。
    """
    while True:
        try:
            line_bytes = await stderr.readline()
        except Exception as e:
            logger.warning("stt_kotoba.subscription: stderr read failed: {}", e)
            break
        if not line_bytes:
            break  # ffmpeg EOF
        try:
            line = line_bytes.decode("utf-8", errors="replace")
        except Exception:
            continue
        parsed = _parse_silencedetect_line(line)
        if not parsed:
            continue
        # silence_end = 無音区間の終わり = 発話が再開した瞬間。
        # この時点で「直前まで貯めた PCM = 1 発話」とみなして flush 指示を出す。
        if parsed["event"] == "end":
            flush_event.set()


async def _run_one_ffmpeg_session(
    rtsp_url: str,
    on_speech: Callable[[str], Awaitable[None]],
    *,
    noise_db: str,
    min_silence_sec: float,
    max_segment_sec: float,
    min_segment_sec: float,
) -> None:
    """ffmpeg を 1 回起動 → PCM 受信 + silencedetect 受信 → セグメント切り出し。

    ffmpeg が EOF や異常終了したら戻る (再起動は呼び出し側ループの責務)。
    asyncio.CancelledError は ffmpeg を terminate してから再 raise する。
    """
    cmd = _build_ffmpeg_rtsp_cmd(
        rtsp_url, noise_db=noise_db, min_silence_sec=min_silence_sec
    )
    logger.info(
        "stt_kotoba.subscription: ffmpeg start url={} noise={} min_silence={:.2f}s",
        _mask_rtsp_url(rtsp_url),
        noise_db,
        min_silence_sec,
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:
        logger.warning("stt_kotoba.subscription: ffmpeg spawn failed: {}", e)
        return

    pcm_buffer = bytearray()
    flush_event = asyncio.Event()
    pcm_task: asyncio.Task[None] | None = None
    err_task: asyncio.Task[None] | None = None
    try:
        assert proc.stdout is not None and proc.stderr is not None
        pcm_task = asyncio.create_task(
            _pcm_reader(
                proc.stdout,
                pcm_buffer,
                on_speech,
                max_segment_sec=max_segment_sec,
                min_segment_sec=min_segment_sec,
                flush_event=flush_event,
            )
        )
        err_task = asyncio.create_task(_stderr_reader(proc.stderr, flush_event))
        # どちらかが EOF で抜けたら ffmpeg は終わり
        await asyncio.wait(
            {pcm_task, err_task}, return_when=asyncio.FIRST_COMPLETED
        )
    except asyncio.CancelledError:
        logger.info("stt_kotoba.subscription: cancelled, terminating ffmpeg")
        raise
    finally:
        # ffmpeg を確実に止める
        try:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
        except Exception as e:
            logger.warning("stt_kotoba.subscription: ffmpeg cleanup failed: {}", e)
        for t in (pcm_task, err_task):
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        # 終了時に残った PCM を最後に flush
        if pcm_buffer:
            await _emit_segment(
                pcm_buffer, on_speech, min_segment_sec=min_segment_sec
            )


async def _subscription_loop(
    rtsp_url: str,
    on_speech: Callable[[str], Awaitable[None]],
    *,
    noise_db: str,
    min_silence_sec: float,
    max_segment_sec: float,
    min_segment_sec: float,
    restart_backoff_sec: float,
) -> None:
    """無限ループで ffmpeg を起動・再起動するトップレベルループ。

    cancel が呼ばれたら無限ループを抜ける。それ以外の異常終了は
    restart_backoff_sec 待ってから再接続。
    """
    while True:
        try:
            await _run_one_ffmpeg_session(
                rtsp_url,
                on_speech,
                noise_db=noise_db,
                min_silence_sec=min_silence_sec,
                max_segment_sec=max_segment_sec,
                min_segment_sec=min_segment_sec,
            )
        except asyncio.CancelledError:
            logger.info("stt_kotoba.subscription: loop cancelled, exiting")
            raise
        except Exception as e:
            logger.warning(
                "stt_kotoba.subscription: ffmpeg session crashed: {}", e
            )
        logger.info(
            "stt_kotoba.subscription: ffmpeg ended, restarting in {:.1f}s",
            restart_backoff_sec,
        )
        try:
            await asyncio.sleep(restart_backoff_sec)
        except asyncio.CancelledError:
            raise


async def start_rtsp_subscription(
    on_speech: Callable[[str], Awaitable[None]],
    rtsp_url: str | None = None,
    *,
    vad_threshold: float = 0.5,
    min_silence_ms: int = 500,
    chunk_duration_ms: int = 30,
) -> asyncio.Task[None]:
    """Tapo C210 RTSP 音声トラックを連続購読し、発話単位で transcribe を呼ぶ常駐タスクを起動。

    Phase C-4 で本実装。ffmpeg silencedetect を VAD として使い、無音区間で
    発話を区切る。後方互換のため引数シグネチャは Phase C-1 と同一。
    内部動作は **環境変数** (STT_VAD_* / STT_FFMPEG_*) で調整する:

    - ``vad_threshold`` / ``chunk_duration_ms``: silero VAD 想定の名残、現実装では未使用
      (silencedetect は dB ベース)。将来 silero に切替えるときの予約パラメータ。
    - ``min_silence_ms``: STT_VAD_MIN_SILENCE_SEC 未設定時のみ参照する後方互換引数。
      環境変数が設定されていればそちらが優先。

    Args:
        on_speech: 1 発話の書き起こしテキストを受け取る async callback。
        rtsp_url: 完全な RTSP URL。未指定なら _get_rtsp_url() で組み立て。
        vad_threshold: (将来用) silero VAD 閾値。現実装では未使用。
        min_silence_ms: STT_VAD_MIN_SILENCE_SEC env 未設定時のみ使う後方互換引数。
        chunk_duration_ms: (将来用) silero VAD 用 chunk 長。現実装では未使用。

    Returns:
        起動した `asyncio.Task`。停止は ``task.cancel()`` で。
        依存未満の場合も Task を返す (即終了する no-op タスク)。
    """
    # 引数で渡されなければ env から組み立て
    url = rtsp_url if rtsp_url is not None else _get_rtsp_url()
    if not url:
        return asyncio.create_task(
            _noop_subscription_loop("STT_RTSP_URL not set and CAMERA_* incomplete")
        )

    backend = _get_vad_backend()
    if backend == "disabled":
        return asyncio.create_task(_noop_subscription_loop("STT_VAD_BACKEND=disabled"))
    if not _vad_backend_available(backend):
        return asyncio.create_task(
            _noop_subscription_loop(f"VAD backend '{backend}' unavailable (ffmpeg missing?)")
        )

    # env 未設定時のみ引数値を採用 (後方互換)
    if "STT_VAD_MIN_SILENCE_SEC" in os.environ:
        min_silence_sec = _get_vad_min_silence_sec()
    else:
        min_silence_sec = max(min_silence_ms / 1000.0, 0.05)

    noise_db = _get_vad_noise_db()
    max_segment_sec = _get_vad_max_segment_sec()
    min_segment_sec = _get_vad_min_segment_sec()
    restart_backoff_sec = _get_ffmpeg_restart_backoff_sec()

    # 参照だけ残して static analyzer 警告抑止 (silero へ移行する際の予約引数)
    _ = vad_threshold, chunk_duration_ms

    logger.info(
        "stt_kotoba.start_rtsp_subscription: starting url={} backend={} "
        "noise_db={} min_silence_sec={:.2f} max_segment_sec={:.2f}",
        _mask_rtsp_url(url),
        backend,
        noise_db,
        min_silence_sec,
        max_segment_sec,
    )
    return asyncio.create_task(
        _subscription_loop(
            url,
            on_speech,
            noise_db=noise_db,
            min_silence_sec=min_silence_sec,
            max_segment_sec=max_segment_sec,
            min_segment_sec=min_segment_sec,
            restart_backoff_sec=restart_backoff_sec,
        )
    )
