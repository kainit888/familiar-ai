"""ピコ音声会話スクリプト (TUI 経由なし、Tapo + EmbodiedAgent 直結)。

パイプライン:
    Tapo C210 マイク (RTSP)
      → ffmpeg で 16kHz mono WAV 録音
      → Kotoba-Whisper STT (audio= フィールド POST)
      → familiar_agent.agent.EmbodiedAgent (ピコのキャラ/記憶反映)
      → SBV2 TTS
      → go2rtc (#audio=pcma_pico カスタムテンプレート)
      → Tapo C210 スピーカー

使い方:
    cd /home/pico/pico_v3
    set -a; source .env; set +a
    uv run python voice_chat.py

機密情報 (RTSP URL のパスワード等) は .env から取得。ハードコード禁止。
adapter (src/pico_agent/adapters/stt_kotoba.py) は ``file=`` で送る誤実装が
残っているため、本スクリプト内では aiohttp で直接 ``audio=`` フィールドを
POST して回避する (adapter 修正は別タスク)。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import traceback
from urllib.parse import quote

import aiohttp
from dotenv import load_dotenv
from loguru import logger

from familiar_agent.agent import EmbodiedAgent
from familiar_agent.config import AgentConfig


DEFAULT_RECORD_SECONDS = 5.0
DEFAULT_STT_TIMEOUT = 30.0
DEFAULT_TTS_TIMEOUT = 60.0
DEFAULT_TTS_CHUNK_MAX_CHARS = 30
DEFAULT_TTS_CHUNK_DELAY_MS = 0
DEFAULT_TTS_PLAYBACK_STABLE_THRESHOLD = 3
DEFAULT_TTS_PLAYBACK_MAX_WAIT_MS = 10000
DEFAULT_TTS_PLAYBACK_POLL_INTERVAL_MS = 100
DEFAULT_TTS_PLAYBACK_GRACE_MS = 800
DEFAULT_TTS_PLAYBACK_FINAL_GRACE_MS = 1200
_TTS_PUNCT = "。、！？!?.,"


def _extract_consumers(payload: object, stream_name: str) -> list[dict] | None:
    """go2rtc レスポンス (バージョン依存) から該当 stream の consumers を取り出す。"""
    if not isinstance(payload, dict):
        return None

    direct = payload.get("consumers")
    if isinstance(direct, list):
        return [c for c in direct if isinstance(c, dict)]

    streams = payload.get("streams")
    if isinstance(streams, dict):
        entry = streams.get(stream_name)
        if isinstance(entry, dict):
            inner = entry.get("consumers")
            if isinstance(inner, list):
                return [c for c in inner if isinstance(c, dict)]

    entry = payload.get(stream_name)
    if isinstance(entry, dict):
        inner = entry.get("consumers")
        if isinstance(inner, list):
            return [c for c in inner if isinstance(c, dict)]

    return None


def _extract_consumers_with_pattern(
    payload: object, stream_name: str
) -> tuple[list[dict] | None, str]:
    """``_extract_consumers`` と同じだが、ヒットしたパターン名も返す (debug 用)。

    pattern: "direct" | "streams_map" | "top_level_map" | "missing" | "invalid_root"
    """
    if not isinstance(payload, dict):
        return None, "invalid_root"
    if "consumers" in payload:
        c = payload.get("consumers")
        if isinstance(c, list):
            return c, "direct"
        return None, "direct_invalid"
    streams = payload.get("streams")
    if isinstance(streams, dict) and stream_name in streams:
        inner = streams[stream_name]
        if isinstance(inner, dict):
            c = inner.get("consumers")
            if isinstance(c, list):
                return c, "streams_map"
            return None, "streams_map_invalid"
    inner = payload.get(stream_name)
    if isinstance(inner, dict):
        c = inner.get("consumers")
        if isinstance(c, list):
            return c, "top_level_map"
        return None, "top_level_map_invalid"
    return None, "missing"


def _sum_sender_bytes(consumers: list[dict]) -> int:
    """全 consumer の senders[i].bytes を合計。bytes が無い/int で無い sender は 0 扱い。"""
    total = 0
    for consumer in consumers:
        if not isinstance(consumer, dict):
            continue
        senders = consumer.get("senders")
        if not isinstance(senders, list):
            continue
        for sender in senders:
            if not isinstance(sender, dict):
                continue
            value = sender.get("bytes")
            # bool は int のサブクラスのため明示的に除外
            if isinstance(value, int) and not isinstance(value, bool):
                total += value
    return total


async def _wait_for_playback_done(
    session: aiohttp.ClientSession,
    *,
    go2rtc_base_url: str,
    stream_name: str,
    max_wait_ms: int,
    stable_threshold: int,
    poll_interval_ms: int,
) -> tuple[bool, str]:
    """go2rtc /api/streams?src=<stream_name> を GET ポーリングして再生完了を待つ。

    Returns:
        ``(done, reason)`` のタプル。

        - ``done``: ``True`` なら「これ以上待つ必要なし」、``False`` は timeout (= 強制終了)。
        - ``reason``: 終了理由を表す文字列。以下のいずれか:

          * ``"timeout"``         — ``max_wait_ms`` 経過 (done=False)
          * ``"http_error"``      — go2rtc が HTTP non-200 を返した
          * ``"exception"``       — リクエストが例外を投げた
          * ``"consumers_none"``  — レスポンスから consumers を抽出できなかった
          * ``"consumers_empty"`` — consumers リストが空 = 再生完了
          * ``"stable"``          — bytes が stable_threshold 回連続不変 = 完了
    """
    api_url = (
        f"{go2rtc_base_url.rstrip('/')}/api/streams"
        f"?src={quote(stream_name)}"
    )
    # チャンク POST 直後は consumer がまだ立っていない可能性があるので少しだけ待つ
    initial_sleep_ms = min(poll_interval_ms, 200)
    await asyncio.sleep(initial_sleep_ms / 1000.0)

    start_time = time.monotonic()
    deadline = start_time + max_wait_ms / 1000.0
    get_timeout = aiohttp.ClientTimeout(total=2.0)
    prev_bytes: int | None = None
    stable_count = 0
    iter_n = 0

    logger.debug(
        "wait[start] stream={} max_wait_ms={} stable_threshold={} "
        "poll_interval_ms={} initial_sleep_ms={}",
        stream_name,
        max_wait_ms,
        stable_threshold,
        poll_interval_ms,
        initial_sleep_ms,
    )

    while True:
        iter_n += 1
        elapsed_ms = (time.monotonic() - start_time) * 1000.0
        if time.monotonic() >= deadline:
            logger.debug(
                "wait[end] reason=timeout stream={} iter={} elapsed_ms={:.0f}",
                stream_name,
                iter_n,
                elapsed_ms,
            )
            return (False, "timeout")
        try:
            async with session.get(api_url, timeout=get_timeout) as resp:
                if resp.status != 200:
                    logger.warning(
                        "_wait_for_playback_done: HTTP {} from {}",
                        resp.status,
                        api_url,
                    )
                    logger.debug(
                        "wait[end] reason=http_error stream={} status={} iter={}",
                        stream_name,
                        resp.status,
                        iter_n,
                    )
                    return (True, "http_error")
                payload = await resp.json()
        except Exception as e:
            logger.warning("_wait_for_playback_done: request failed: {}", e)
            logger.debug(
                "wait[end] reason=exception stream={} err={!r} iter={}",
                stream_name,
                e,
                iter_n,
            )
            return (True, "exception")

        consumers, pattern = _extract_consumers_with_pattern(payload, stream_name)
        elapsed_ms = (time.monotonic() - start_time) * 1000.0
        if consumers is None:
            logger.debug(
                "wait[end] reason=consumers_none stream={} pattern={} iter={} "
                "elapsed_ms={:.0f}",
                stream_name,
                pattern,
                iter_n,
                elapsed_ms,
            )
            return (True, "consumers_none")
        if len(consumers) == 0:
            logger.debug(
                "wait[end] reason=consumers_empty stream={} pattern={} iter={} "
                "elapsed_ms={:.0f}",
                stream_name,
                pattern,
                iter_n,
                elapsed_ms,
            )
            return (True, "consumers_empty")

        current = _sum_sender_bytes(consumers)
        if prev_bytes is not None and current == prev_bytes:
            stable_count += 1
            if stable_count >= stable_threshold:
                logger.debug(
                    "wait[end] reason=stable stream={} bytes={} iter={} "
                    "elapsed_ms={:.0f}",
                    stream_name,
                    prev_bytes,
                    iter_n,
                    elapsed_ms,
                )
                return (True, "stable")
        else:
            stable_count = 0
        logger.debug(
            "wait[iter={}] stream={} pattern={} consumers_len={} current_bytes={} "
            "prev_bytes={} stable_count={}/{} elapsed_ms={:.0f}",
            iter_n,
            stream_name,
            pattern,
            len(consumers),
            current,
            prev_bytes,
            stable_count,
            stable_threshold,
            elapsed_ms,
        )
        prev_bytes = current

        await asyncio.sleep(poll_interval_ms / 1000.0)


def _split_text_for_tts(text: str, *, max_chars: int) -> list[str]:
    """テキストを TTS チャンクに分割する。

    1. ``_TTS_PUNCT`` の各文字で区切り (句読点はチャンクの末尾に残す)。
    2. 各チャンクが ``max_chars`` を超える場合は ``max_chars`` 単位で再分割。
    3. 空白行は除外。

    SBV2 サーバが長文で 4XX を返す問題への対策 (v4.2 Phase C-3 設計)。
    """
    if not text or not text.strip() or max_chars <= 0:
        return []

    primary: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in _TTS_PUNCT:
            primary.append(buf)
            buf = ""
    if buf:
        primary.append(buf)

    out: list[str] = []
    for chunk in primary:
        chunk = chunk.strip()
        if not chunk:
            continue
        if len(chunk) <= max_chars:
            out.append(chunk)
            continue
        for i in range(0, len(chunk), max_chars):
            piece = chunk[i : i + max_chars].strip()
            if piece:
                out.append(piece)
    return out


async def record_from_tapo(rtsp_url: str, duration: float = DEFAULT_RECORD_SECONDS) -> bytes:
    """Tapo C210 の RTSP から ffmpeg で 16kHz mono WAV を録音し bytes で返す。

    失敗時 (ffmpeg がコード != 0 / 例外) は空 bytes を返す。
    """
    if not rtsp_url:
        logger.warning("record_from_tapo: empty rtsp_url")
        return b""

    cmd = [
        "ffmpeg",
        "-rtsp_transport",
        "tcp",
        "-i",
        rtsp_url,
        "-t",
        str(duration),
        "-ar",
        "16000",
        "-ac",
        "1",
        "-f",
        "wav",
        "-y",
        "pipe:1",
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
    except FileNotFoundError:
        logger.error("ffmpeg not found in PATH")
        return b""
    except Exception as e:
        logger.warning("record_from_tapo: ffmpeg invocation failed: {}", e)
        return b""

    if proc.returncode != 0:
        logger.warning(
            "record_from_tapo: ffmpeg exited {} stderr={!r}",
            proc.returncode,
            stderr.decode("utf-8", errors="ignore")[:300],
        )
        return b""

    return stdout


async def transcribe(audio_bytes: bytes, stt_url: str) -> str:
    """Kotoba-Whisper サーバへ ``audio=`` フィールドで POST し転写テキストを返す。

    空入力 / HTTP 非 200 / JSON 不正時は空文字を返す (例外を投げない)。
    adapter の ``file=`` バグ回避のため、本関数は直接 aiohttp.FormData を組み立てる。
    """
    if not audio_bytes:
        logger.warning("transcribe: empty audio_bytes")
        return ""
    if not stt_url:
        logger.warning("transcribe: empty stt_url")
        return ""

    form = aiohttp.FormData()
    form.add_field(
        "audio",
        audio_bytes,
        filename="audio.wav",
        content_type="audio/wav",
    )

    timeout = aiohttp.ClientTimeout(total=DEFAULT_STT_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(stt_url, data=form) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(
                        "transcribe: HTTP {} from {} body={!r}",
                        resp.status,
                        stt_url,
                        body[:200],
                    )
                    return ""
                payload = await resp.json()
                text = (payload or {}).get("text", "") if isinstance(payload, dict) else ""
                return text.strip() if isinstance(text, str) else ""
    except Exception as e:
        logger.warning("transcribe: request failed: {}", e)
        return ""


async def speak_to_tapo(
    text: str,
    *,
    tts_base_url: str,
    tts_model: str,
    go2rtc_base_url: str,
    stream_name: str,
) -> bool:
    """ピコの応答テキストを go2rtc 経由で Tapo スピーカーに再生させる。

    SBV2 サーバが長文で 4XX を返す対策として、``TTS_CHUNK_MAX_CHARS``
    (デフォルト 30) 文字以下になるよう句読点で分割し、各チャンクを順次 POST。
    チャンク間は ``TTS_CHUNK_DELAY_MS`` (デフォルト 0) ms 待機。途中のチャンクが
    失敗しても残りは送信を続け、全チャンク成功時のみ True を返す。
    """
    if not text or not text.strip():
        logger.warning("speak_to_tapo: empty text")
        return False
    if not (tts_base_url and tts_model and go2rtc_base_url and stream_name):
        logger.warning("speak_to_tapo: missing TTS/go2rtc config")
        return False

    try:
        max_chars = int(os.environ.get("TTS_CHUNK_MAX_CHARS", str(DEFAULT_TTS_CHUNK_MAX_CHARS)))
    except ValueError:
        max_chars = DEFAULT_TTS_CHUNK_MAX_CHARS
    try:
        delay_ms = int(os.environ.get("TTS_CHUNK_DELAY_MS", str(DEFAULT_TTS_CHUNK_DELAY_MS)))
    except ValueError:
        delay_ms = DEFAULT_TTS_CHUNK_DELAY_MS
    try:
        stable_threshold = int(
            os.environ.get(
                "TTS_PLAYBACK_STABLE_THRESHOLD",
                str(DEFAULT_TTS_PLAYBACK_STABLE_THRESHOLD),
            )
        )
    except ValueError:
        stable_threshold = DEFAULT_TTS_PLAYBACK_STABLE_THRESHOLD
    try:
        max_wait_ms = int(
            os.environ.get(
                "TTS_PLAYBACK_MAX_WAIT_MS",
                str(DEFAULT_TTS_PLAYBACK_MAX_WAIT_MS),
            )
        )
    except ValueError:
        max_wait_ms = DEFAULT_TTS_PLAYBACK_MAX_WAIT_MS
    try:
        poll_interval_ms = int(
            os.environ.get(
                "TTS_PLAYBACK_POLL_INTERVAL_MS",
                str(DEFAULT_TTS_PLAYBACK_POLL_INTERVAL_MS),
            )
        )
    except ValueError:
        poll_interval_ms = DEFAULT_TTS_PLAYBACK_POLL_INTERVAL_MS
    try:
        grace_ms = int(
            os.environ.get(
                "TTS_PLAYBACK_GRACE_MS",
                str(DEFAULT_TTS_PLAYBACK_GRACE_MS),
            )
        )
    except ValueError:
        grace_ms = DEFAULT_TTS_PLAYBACK_GRACE_MS
    try:
        final_grace_ms = int(
            os.environ.get(
                "TTS_PLAYBACK_FINAL_GRACE_MS",
                str(DEFAULT_TTS_PLAYBACK_FINAL_GRACE_MS),
            )
        )
    except ValueError:
        final_grace_ms = DEFAULT_TTS_PLAYBACK_FINAL_GRACE_MS

    chunks = _split_text_for_tts(text, max_chars=max_chars)
    if not chunks:
        logger.warning("speak_to_tapo: text produced no speakable chunks")
        return False

    timeout = aiohttp.ClientTimeout(total=DEFAULT_TTS_TIMEOUT)
    all_ok = True
    last_reason: str | None = None
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for index, chunk in enumerate(chunks):
                with logger.contextualize(chunk_index=index, chunk_total=len(chunks)):
                    voice_url = (
                        f"{tts_base_url.rstrip('/')}/voice"
                        f"?text={quote(chunk)}&model_name={quote(tts_model)}"
                    )
                    src = f"ffmpeg:{voice_url}#audio=pcma_pico#input=file"
                    api_url = (
                        f"{go2rtc_base_url.rstrip('/')}/api/streams"
                        f"?dst={quote(stream_name)}&src={quote(src)}"
                    )
                    logger.debug(
                        "speak[chunk] text={!r} api_url={}",
                        chunk[:20],
                        api_url,
                    )
                    chunk_post_ok = False
                    try:
                        async with session.post(api_url) as resp:
                            if resp.status >= 400:
                                body = await resp.text()
                                logger.warning(
                                    "speak_to_tapo: chunk {}/{} HTTP {} body={!r} text={!r}",
                                    index + 1,
                                    len(chunks),
                                    resp.status,
                                    body[:200],
                                    chunk,
                                )
                                all_ok = False
                            else:
                                chunk_post_ok = True
                            logger.debug(
                                "speak[chunk] post_status={} chunk_post_ok={}",
                                resp.status,
                                chunk_post_ok,
                            )
                    except Exception as e:
                        logger.warning(
                            "speak_to_tapo: chunk {}/{} request failed: {} text={!r}",
                            index + 1,
                            len(chunks),
                            e,
                            chunk,
                        )
                        all_ok = False

                    if chunk_post_ok:
                        done, reason = await _wait_for_playback_done(
                            session,
                            go2rtc_base_url=go2rtc_base_url,
                            stream_name=stream_name,
                            max_wait_ms=max_wait_ms,
                            stable_threshold=stable_threshold,
                            poll_interval_ms=poll_interval_ms,
                        )
                        last_reason = reason
                        if reason == "stable" and grace_ms > 0:
                            await asyncio.sleep(grace_ms / 1000.0)
                        logger.debug(
                            "speak[chunk] wait_done={} reason={}", done, reason
                        )
                        if not done:
                            logger.warning(
                                "speak_to_tapo: chunk {}/{} playback wait timed out",
                                index + 1,
                                len(chunks),
                            )

                    if delay_ms > 0 and index < len(chunks) - 1:
                        logger.debug("speak[chunk] inter_chunk_sleep_ms={}", delay_ms)
                        await asyncio.sleep(delay_ms / 1000.0)
            if last_reason == "stable" and final_grace_ms > 0:
                logger.debug("speak[final_grace] sleep_ms={}", final_grace_ms)
                await asyncio.sleep(final_grace_ms / 1000.0)
    except Exception as e:
        logger.warning("speak_to_tapo: session failed: {}", e)
        return False

    return all_ok


async def _wait_for_embedding(agent: EmbodiedAgent) -> None:
    """ピコの embedding load が終わるまでブロックする (進捗を print)。"""
    if agent.is_embedding_ready:
        return
    start = time.time()
    while not agent.is_embedding_ready:
        elapsed = int(time.time() - start)
        print(f"\r  initializing embeddings... ({elapsed}s)", end="", flush=True)
        await asyncio.sleep(0.5)
    print(f"\r  initialized ({int(time.time() - start)}s)" + " " * 20)


async def _conversation_loop(
    agent: EmbodiedAgent,
    *,
    rtsp_url: str,
    stt_url: str,
    tts_base_url: str,
    tts_model: str,
    go2rtc_base_url: str,
    stream_name: str,
    record_seconds: float,
) -> None:
    """Enter キーで 1 ターン録音→STT→agent→TTS を回すメインループ。"""
    loop = asyncio.get_event_loop()
    print("\n  Press Enter to talk (Ctrl+C to quit).")

    while True:
        await loop.run_in_executor(None, input, "\n> ready, press Enter to record: ")

        print(f"  🎤 recording {record_seconds:.1f}s from Tapo...")
        wav = await record_from_tapo(rtsp_url, duration=record_seconds)
        if not wav:
            logger.warning("conversation_loop: no audio recorded, skipping")
            continue
        print(f"  ✓ recorded {len(wav)} bytes")

        text = await transcribe(wav, stt_url)
        if not text or len(text.strip()) < 1:
            logger.warning("conversation_loop: empty/short transcript, skipping")
            continue
        print(f"  📝 you: {text}")

        try:
            response = await agent.run(user_input=text)
        except Exception as e:
            logger.error("agent.run failed: {}", e)
            traceback.print_exc()
            continue

        if not response or not response.strip():
            logger.warning("conversation_loop: empty response from agent")
            continue
        print(f"  🤖 pico: {response}")

        ok = await speak_to_tapo(
            response,
            tts_base_url=tts_base_url,
            tts_model=tts_model,
            go2rtc_base_url=go2rtc_base_url,
            stream_name=stream_name,
        )
        if not ok:
            logger.warning("conversation_loop: TTS playback failed")
        else:
            print("  🔊 played back via Tapo")


async def _amain() -> None:
    load_dotenv()

    log_level = os.environ.get("LOGURU_LEVEL", "INFO")
    logger.remove()
    logger.add(
        sys.stderr,
        level=log_level,
        format="{time:HH:mm:ss.SSS} | {level} | {message} | {extra}",
    )

    rtsp_url = os.environ.get("STT_RTSP_URL", "")
    stt_url = os.environ.get("STT_BASE_URL", "")
    tts_base_url = os.environ.get("TTS_BASE_URL", "")
    tts_model = os.environ.get("TTS_MODEL_NAME", "jvnv-F1-jp")
    go2rtc_base_url = os.environ.get("GO2RTC_BASE_URL", "")
    stream_name = os.environ.get("TAPO_STREAM_NAME", "tapo_c210")

    missing = [
        name
        for name, value in [
            ("STT_RTSP_URL", rtsp_url),
            ("STT_BASE_URL", stt_url),
            ("TTS_BASE_URL", tts_base_url),
            ("GO2RTC_BASE_URL", go2rtc_base_url),
        ]
        if not value
    ]
    if missing:
        print(f"Error: missing required env vars: {', '.join(missing)}", file=sys.stderr)
        print("  Configure them in .env and re-run.", file=sys.stderr)
        sys.exit(1)

    record_seconds = float(os.environ.get("VOICE_CHAT_RECORD_SECONDS", DEFAULT_RECORD_SECONDS))

    print("=" * 60)
    print("  ピコ voice chat (Tapo mic → STT → EmbodiedAgent → TTS → Tapo speaker)")
    print("=" * 60)

    config = AgentConfig()
    agent = EmbodiedAgent(config)

    await _wait_for_embedding(agent)

    try:
        await _conversation_loop(
            agent,
            rtsp_url=rtsp_url,
            stt_url=stt_url,
            tts_base_url=tts_base_url,
            tts_model=tts_model,
            go2rtc_base_url=go2rtc_base_url,
            stream_name=stream_name,
            record_seconds=record_seconds,
        )
    except (KeyboardInterrupt, EOFError):
        print("\n  bye.")


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        print("\n  bye.")


if __name__ == "__main__":
    main()
