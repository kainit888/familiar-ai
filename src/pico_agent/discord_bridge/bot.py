"""Discord Bot 本体スケルトン (設計書 v4.0 第 7-1 / 第 13 章)。

Phase D 着手時に discord.py で本実装する。Phase C-1 完了時点では
インターフェース定義のみで、actual な Discord 接続は一切行わない。

設計方針:
    - OWNER_ID と GUILD_ID で発話相手を制限 (boundary レイヤー、設計書 13-1)
    - Bot Token は load_secret() 経由で取得 (Phase H で本実装、Phase C-1 では
      環境変数 DISCORD_BOT_TOKEN を読む暫定実装)
    - on_message / on_voice_state_update を ReAct loop に橋渡し
    - intents は MESSAGE_CONTENT + GUILD_VOICE_STATES を最低限有効化
"""

from __future__ import annotations

import os
from typing import Any, Awaitable, Callable, Optional

from loguru import logger

from .availability import DiscordDisabledError, is_discord_available


def _load_bot_token() -> Optional[str]:
    """Discord Bot Token を取得。

    Phase D 本実装時は load_secret() 経由に切替えるが、Phase C-1 では
    環境変数 DISCORD_TOKEN を読む暫定実装。
    discord.py 標準慣習に合わせて DISCORD_TOKEN を優先、後方互換で
    DISCORD_BOT_TOKEN もフォールバック。両方未設定なら None。
    """
    raw = os.environ.get("DISCORD_TOKEN", "").strip()
    if not raw:
        raw = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    return raw or None


def _load_owner_id() -> Optional[int]:
    """OWNER (カイニット) の Discord ユーザー ID を取得 (環境変数 DISCORD_OWNER_ID)。"""
    raw = os.environ.get("DISCORD_OWNER_ID", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        logger.warning("discord_bridge: invalid DISCORD_OWNER_ID={!r}", raw)
        return None


def _load_guild_id() -> Optional[int]:
    """ピコが応答する Guild (Discord サーバ) ID を取得 (環境変数 DISCORD_GUILD_ID)。"""
    raw = os.environ.get("DISCORD_GUILD_ID", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        logger.warning("discord_bridge: invalid DISCORD_GUILD_ID={!r}", raw)
        return None


class PicoBot:
    """Discord Bot 本体 (Phase D スケルトン、実 client は未起動)。

    Phase D 着手時に discord.Bot を継承する形に書き換える。Phase C-1 完了
    時点では「呼ばれたら DiscordDisabledError か no-op」の安全な動作のみ
    実装する。

    Args:
        on_text_message: テキストメッセージを受け取った時に呼ぶ async callback。
            シグネチャ: ``async def on_text(user_id: str, content: str) -> str``
            戻り値は応答テキスト (空文字なら応答しない)。
        on_voice_speech: VC で発話を検出した時に呼ぶ async callback。
            シグネチャ: ``async def on_voice(user_id: str, transcript: str) -> str``
    """

    def __init__(
        self,
        on_text_message: Optional[Callable[[str, str], Awaitable[str]]] = None,
        on_voice_speech: Optional[Callable[[str, str], Awaitable[str]]] = None,
    ) -> None:
        self.on_text_message = on_text_message
        self.on_voice_speech = on_voice_speech
        self.token = _load_bot_token()
        self.owner_id = _load_owner_id()
        self.guild_id = _load_guild_id()
        self._is_running = False
        self._client: Any = None  # discord.Bot インスタンス (Phase D で設定)

    @property
    def is_configured(self) -> bool:
        """Bot Token / OWNER_ID / GUILD_ID が揃っているかを判定。"""
        return all((self.token, self.owner_id, self.guild_id))

    @property
    def is_running(self) -> bool:
        """Bot が起動中かどうか (Phase C-1 では常に False)。"""
        return self._is_running

    async def start(self) -> None:
        """Discord Bot を起動する。

        Phase C-1 完了時点では discord.py 未インストール or 設定欠落で
        DiscordDisabledError を投げる。Phase D 着手時に discord.Client.start()
        を呼ぶ実装に書き換える。

        Raises:
            DiscordDisabledError: discord.py が未インストール、または
                token/owner_id/guild_id が未設定の場合。
        """
        if not is_discord_available():
            raise DiscordDisabledError(
                "discord.py is not installed; install via `uv add discord.py` "
                "in Phase D"
            )
        if not self.is_configured:
            raise DiscordDisabledError(
                "DISCORD_BOT_TOKEN / DISCORD_OWNER_ID / DISCORD_GUILD_ID is "
                "not set in environment"
            )
        # Phase D: discord.Bot を初期化して start
        raise DiscordDisabledError(
            "PicoBot.start() implementation pending (Phase D)"
        )

    async def stop(self) -> None:
        """Bot を停止する (Phase C-1 では no-op)。"""
        self._is_running = False
        self._client = None
        logger.info("discord_bridge.PicoBot: stop() called (Phase D pending)")

    async def send_message(self, channel_id: int, content: str) -> bool:
        """テキストメッセージを Discord チャンネルに送信する (Phase C-1 では DiscordDisabledError)。

        Args:
            channel_id: 送信先 Discord チャンネル ID。
            content: 送信メッセージ本文。

        Returns:
            送信成功なら True。Phase C-1 では DiscordDisabledError。

        Raises:
            DiscordDisabledError: 常に発火 (Phase D で本実装)。
        """
        raise DiscordDisabledError(
            "PicoBot.send_message() implementation pending (Phase D)"
        )
