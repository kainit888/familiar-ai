#!/usr/bin/env python3
"""手動 TTS 再生検証スクリプト。

LLM 呼び出しを完全にスキップして固定テキスト or 引数テキストを
``speak_to_tapo`` (voice_chat.py 内) で再生し、デバッグログから
``_wait_for_playback_done`` の挙動を観測する。

使い方:
    cd /home/pico/pico_v3
    set -a; source .env; set +a
    LOGURU_LEVEL=DEBUG uv run python scripts/manual_tts_playback_check.py
    # 別テキスト
    LOGURU_LEVEL=DEBUG uv run python scripts/manual_tts_playback_check.py --text "別文" --max-chars 20
    # 分割結果だけ確認
    uv run python scripts/manual_tts_playback_check.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from voice_chat import _split_text_for_tts, speak_to_tapo  # noqa: E402

_DEFAULT_TEXT = "うん、元気だよ！カイニットは？ 最近何か面白いことあった？"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Manual TTS playback check")
    p.add_argument("--text", default=_DEFAULT_TEXT, help="再生テキスト")
    p.add_argument(
        "--max-chars",
        type=int,
        default=None,
        help="TTS_CHUNK_MAX_CHARS 上書き (未指定なら env 値)",
    )
    p.add_argument("--log-level", default=None, help="LOGURU_LEVEL 上書き")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="分割結果のみ表示して exit (POST しない)",
    )
    return p.parse_args()


def _setup_logging(level: str) -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format="{time:HH:mm:ss.SSS} | {level} | {message} | {extra}",
    )


async def _run(text: str) -> bool:
    tts_base_url = os.environ.get("TTS_BASE_URL", "")
    tts_model = os.environ.get("TTS_MODEL_NAME", "jvnv-F1-jp")
    go2rtc_base_url = os.environ.get("GO2RTC_BASE_URL", "")
    stream_name = os.environ.get("TAPO_STREAM_NAME", "tapo_c210")

    missing = [
        name for name, value in [
            ("TTS_BASE_URL", tts_base_url),
            ("GO2RTC_BASE_URL", go2rtc_base_url),
        ] if not value
    ]
    if missing:
        logger.error("missing required env: {}", ", ".join(missing))
        return False

    return await speak_to_tapo(
        text,
        tts_base_url=tts_base_url,
        tts_model=tts_model,
        go2rtc_base_url=go2rtc_base_url,
        stream_name=stream_name,
    )


def main() -> int:
    args = _parse_args()
    load_dotenv()

    if args.log_level:
        os.environ["LOGURU_LEVEL"] = args.log_level
    os.environ.setdefault("LOGURU_LEVEL", "DEBUG")
    _setup_logging(os.environ["LOGURU_LEVEL"])

    if args.max_chars is not None:
        os.environ["TTS_CHUNK_MAX_CHARS"] = str(args.max_chars)

    max_chars_for_split = int(os.environ.get("TTS_CHUNK_MAX_CHARS", "30"))
    chunks = _split_text_for_tts(args.text, max_chars=max_chars_for_split)
    logger.info("split: {} chunks: {}", len(chunks), chunks)

    if args.dry_run:
        return 0

    ok = asyncio.run(_run(args.text))
    logger.info("speak_to_tapo all_ok={}", ok)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
