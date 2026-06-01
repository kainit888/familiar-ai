"""Phase D-1: Discord 自発 post tool — ピコが text channel へ自分から投稿する経路。

Phase F の Q5(d) で「最終的に Discord から出力できたら嬉しい」とされ後回しにされた分。
ピコは heartbeat / idle turn 中に自分の judgment で ``post_to_discord`` を呼び、調べた
こと・学んだこと・ふと思ったことを Discord に共有できる (Tapo スピーカーの Phase E
heartbeat と並ぶもう一つの出力経路)。呼ぶか呼ばないかがピコの autonomy。

Phase F ``search_web`` (tools/web_search.py) と同型のツール:
    - tool 登録は ``available()`` で gate (token/guild/channel 未設定なら不可視)
    - mock seam ``_BOT_OVERRIDE`` で実 Discord 接続なしにテスト
    - 失敗 (token 欠落 / 接続失敗 / channel 不在 / 送信例外) は全て graceful no-op、
      例外を投げず文字列を返すだけ (ピコは黙る)

二層分離:
    Discord 実装は ``pico_agent.discord_bridge`` (既存スケルトン) に閉じており、本
    ツールは familiar_agent→pico_agent の許可方向で ``PicoBot`` を import するのみ。
    pico_agent は無変更。VC (voice_channel) には一切触れない (Phase D-1 範囲外)。

注意 (実機運用): Discord bot は現状どこからも起動されていない。本ツールが初回 post
時に ``PicoBot`` を 1 つ lazy 起動する。これに伴い既存の受動応答 (on_message) も
同時に live 化する (カイニット承認済の挙動)。
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from loguru import logger

from .._i18n import _t

_DEFAULT_COOLDOWN_S = 600.0  # post 間の最小間隔 (turn 頻度とは独立、spam 抑止)
_DEFAULT_CONNECT_TIMEOUT_S = 15.0  # 初回 lazy 起動時の on_ready 待ち上限

# Mock seam (tests がここに fake PicoBot を入れる) + lazy bot singleton。
_BOT_OVERRIDE: Any | None = None
_BOT_SINGLETON: Any | None = None


# ── 環境変数アクセサ (web_search.py と同型、env-direct) ───────────────────────
def _post_channel_id() -> int | None:
    raw = os.environ.get("DISCORD_POST_CHANNEL_ID", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        logger.warning("discord_post: invalid DISCORD_POST_CHANNEL_ID={!r}", raw)
        return None


def _post_cooldown_s() -> float:
    raw = os.environ.get("DISCORD_POST_COOLDOWN_S", "").strip()
    try:
        return float(raw) if raw else _DEFAULT_COOLDOWN_S
    except ValueError:
        return _DEFAULT_COOLDOWN_S


def _connect_timeout_s() -> float:
    raw = os.environ.get("DISCORD_POST_CONNECT_TIMEOUT_S", "").strip()
    try:
        return float(raw) if raw else _DEFAULT_CONNECT_TIMEOUT_S
    except ValueError:
        return _DEFAULT_CONNECT_TIMEOUT_S


def _discord_configured() -> bool:
    """discord.py 利用可 ∧ token/owner/guild 設定済 ∧ post channel 設定済 か。"""
    try:
        from pico_agent.discord_bridge import PicoBot, is_discord_available

        if not is_discord_available():
            return False
        if _post_channel_id() is None:
            return False
        return bool(PicoBot().is_configured)
    except Exception:
        return False


async def _get_bot() -> Any | None:
    """投稿に使う PicoBot を返す (override 優先、無ければ lazy 起動)。失敗時 None。"""
    global _BOT_SINGLETON
    if _BOT_OVERRIDE is not None:
        return _BOT_OVERRIDE
    try:
        from pico_agent.discord_bridge import PicoBot, is_discord_available

        if not is_discord_available():
            return None
        if _BOT_SINGLETON is None:
            _BOT_SINGLETON = PicoBot()
        bot = _BOT_SINGLETON
        if not getattr(bot, "is_running", False):
            await bot.start_in_background()
            # on_ready (= send 可能) まで bounded に待つ。長時間沈黙後の post は
            # cache 未ヒットで fetch_channel (REST) になるため接続を待つ価値がある。
            timeout = _connect_timeout_s()
            waited = 0.0
            while waited < timeout and not getattr(bot, "is_running", False):
                await asyncio.sleep(0.25)
                waited += 0.25
        return bot
    except Exception as e:
        logger.warning("discord_post: failed to obtain bot: {}", e)
        return None


class DiscordPostTool:
    """post_to_discord tool — ピコが text channel へ自発投稿する。"""

    def __init__(self) -> None:
        self._last_post_monotonic: float | None = None

    @staticmethod
    def available() -> bool:
        """token/guild/channel が揃い discord.py が使えるときだけ True (tool を広告)。"""
        return _discord_configured()

    def get_tool_definitions(self) -> list[dict]:
        return [
            {
                "name": "post_to_discord",
                "description": _t("discord_post_tool_desc"),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "message": {
                            "type": "string",
                            "description": (
                                "The message to post, as plain text in your own voice. "
                                "Include a link inline if relevant."
                            ),
                        }
                    },
                    "required": ["message"],
                },
            }
        ]

    def _cooldown_ok(self) -> bool:
        if self._last_post_monotonic is None:
            return True
        return (time.monotonic() - self._last_post_monotonic) >= _post_cooldown_s()

    def _mark_posted(self) -> None:
        self._last_post_monotonic = time.monotonic()

    async def call(self, tool_name: str, tool_input: dict) -> tuple[str, None]:
        if tool_name != "post_to_discord":
            return f"Unknown tool: {tool_name}", None
        message = (tool_input.get("message") or "").strip()
        if not message:
            return "No message provided.", None
        if not self._cooldown_ok():
            return _t("discord_post_cooldown_notice"), None
        channel_id = _post_channel_id()
        if channel_id is None:
            return "(discord posting unavailable)", None
        bot = await _get_bot()
        if bot is None:
            return "(discord posting unavailable)", None
        try:
            ok = await bot.send_message(channel_id, message)
        except Exception as e:  # never let a post break the turn
            logger.warning("discord_post: send failed: {}", e)
            return "(discord post failed)", None
        if ok:
            self._mark_posted()  # only on success → a failed post stays eligible
            return "(posted)", None
        return "(discord post failed)", None
