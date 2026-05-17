"""Discord Bot 本体 (設計書 v4.0 第 7-1 / 第 13 章) - Phase D 実装版。

discord.py を **lazy import** することで、未インストール環境でもモジュールが
import 可能。実 client は :py:meth:`PicoBot.start` 時に初期化される。

設計方針:
    - OWNER_ID と GUILD_ID で発話相手を制限 (boundary レイヤー、設計書 13-1)
    - Bot Token は環境変数 DISCORD_TOKEN を読む (Phase H で load_secret() 移行)
    - on_message / on_voice_state_update を ReAct loop に橋渡し
    - intents は MESSAGE_CONTENT + GUILD_VOICE_STATES を最低限有効化

⚠️ 外出期間タスク C 制約:
    - 実 Discord 接続は **mock テストまで**
    - 本番接続テストは帰宅後 (2026-05-21 以降)
    - pico_v3.service systemd 設定は実装しない
"""

from __future__ import annotations

import asyncio
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


def _build_intents() -> Any:
    """discord.Intents をピコ用に組み立てる (MESSAGE_CONTENT + GUILDS + GUILD_VOICE_STATES)。

    discord.py を lazy import するため、import 失敗時は DiscordDisabledError。

    Raises:
        DiscordDisabledError: discord.py が import できない場合。
    """
    if not is_discord_available():
        raise DiscordDisabledError(
            "discord.py is not installed; cannot build intents"
        )
    import discord  # type: ignore

    intents = discord.Intents.default()
    intents.message_content = True  # MESSAGE CONTENT INTENT (Privileged)
    intents.guilds = True
    intents.voice_states = True
    intents.members = True  # OWNER 判定のため
    return intents


def _build_client(intents: Any, on_ready: Callable[[], Awaitable[None]]) -> Any:
    """discord.Client インスタンスを生成し、on_ready ハンドラを登録する。

    Args:
        intents: :func:`_build_intents` の戻り値。
        on_ready: クライアントが Discord に接続した時に呼ばれる async callback。

    Returns:
        discord.Client インスタンス (event handler 登録済み)。

    Raises:
        DiscordDisabledError: discord.py が import できない場合。
    """
    if not is_discord_available():
        raise DiscordDisabledError(
            "discord.py is not installed; cannot build client"
        )
    import discord  # type: ignore

    client = discord.Client(intents=intents)

    @client.event  # type: ignore[misc]
    async def on_ready() -> None:  # noqa: D401
        """Discord 接続完了時のハンドラ (内部実装)。"""
        await on_ready_handler()

    async def on_ready_handler() -> None:
        await on_ready()

    return client


class PicoBot:
    """Discord Bot 本体 (Phase D 実装版)。

    discord.py の :class:`discord.Client` を **保有** する (継承ではなく合成)。
    実 client は :py:meth:`start` 時に :func:`_build_client` で生成。

    Args:
        on_text_message: テキストメッセージを受け取った時に呼ぶ async callback。
            シグネチャ: ``async def on_text(user_id: str, content: str) -> str``
            戻り値は応答テキスト (空文字なら応答しない)。
        on_voice_speech: VC で発話を検出した時に呼ぶ async callback。
            シグネチャ: ``async def on_voice(user_id: str, transcript: str) -> str``
        text_channel_handler: テキストチャンネル受信時の boundary 判定 + ハンドラ。
            :class:`pico_agent.discord_bridge.text_channel.TextChannelHandler`。
            未指定なら on_text_message から自動構築。
    """

    def __init__(
        self,
        on_text_message: Optional[Callable[[str, str], Awaitable[str]]] = None,
        on_voice_speech: Optional[Callable[[str, str], Awaitable[str]]] = None,
        text_channel_handler: Any = None,
    ) -> None:
        self.on_text_message = on_text_message
        self.on_voice_speech = on_voice_speech
        self.token = _load_bot_token()
        self.owner_id = _load_owner_id()
        self.guild_id = _load_guild_id()
        self._is_running = False
        self._client: Any = None  # discord.Client インスタンス (start 時に設定)
        self._text_channel_handler = text_channel_handler
        self._start_task: asyncio.Task | None = None

    @property
    def is_configured(self) -> bool:
        """Bot Token / OWNER_ID / GUILD_ID が揃っているかを判定。"""
        return all((self.token, self.owner_id, self.guild_id))

    @property
    def is_running(self) -> bool:
        """Bot が起動中かどうか。"""
        return self._is_running

    @property
    def client(self) -> Any:
        """内部の discord.Client インスタンス (start 後に non-None)。"""
        return self._client

    def _ensure_text_handler(self) -> Any:
        """text_channel_handler が未設定なら on_text_message から自動構築する。"""
        if self._text_channel_handler is not None:
            return self._text_channel_handler
        from .text_channel import TextChannelHandler

        self._text_channel_handler = TextChannelHandler(
            owner_id=self.owner_id,
            guild_id=self.guild_id,
            on_input=self.on_text_message,
        )
        return self._text_channel_handler

    def _attach_event_handlers(self, client: Any) -> None:
        """discord.Client に on_ready / on_message のハンドラを登録する。

        Args:
            client: discord.Client インスタンス。
        """
        from .text_channel import incoming_message_from_discord

        bot_self = self

        @client.event  # type: ignore[misc]
        async def on_ready() -> None:  # noqa: D401
            bot_self._is_running = True
            user = client.user
            logger.info(
                "discord_bridge.PicoBot: connected as {} (id={})",
                getattr(user, "name", "<unknown>"),
                getattr(user, "id", "<unknown>"),
            )

        @client.event  # type: ignore[misc]
        async def on_message(msg: Any) -> None:  # noqa: D401
            try:
                incoming = incoming_message_from_discord(msg)
            except Exception as exc:  # noqa: BLE001
                logger.warning("on_message conversion failed: {}", exc)
                return
            handler = bot_self._ensure_text_handler()
            response = await handler.handle(incoming)
            if response:
                try:
                    await msg.channel.send(response)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("on_message reply send failed: {}", exc)

    async def start(self) -> None:
        """Discord Bot を起動する。

        discord.py 未インストール or 設定欠落で DiscordDisabledError。
        正常時は :py:meth:`discord.Client.start` を await する (blocking)。

        Raises:
            DiscordDisabledError: discord.py が未インストール、または
                token/owner_id/guild_id が未設定の場合。
        """
        if not is_discord_available():
            raise DiscordDisabledError(
                "discord.py is not installed; install via `uv add discord.py`"
            )
        if not self.is_configured:
            raise DiscordDisabledError(
                "DISCORD_TOKEN / DISCORD_OWNER_ID / DISCORD_GUILD_ID is "
                "not set in environment"
            )

        intents = _build_intents()
        import discord  # type: ignore

        self._client = discord.Client(intents=intents)
        self._attach_event_handlers(self._client)

        logger.info(
            "discord_bridge.PicoBot: starting (owner_id={}, guild_id={})",
            self.owner_id,
            self.guild_id,
        )
        try:
            await self._client.start(self.token)
        finally:
            self._is_running = False

    async def start_in_background(self) -> "asyncio.Task[None]":
        """start() を background task として起動する (テスト/対話用)。

        Returns:
            起動した asyncio.Task。停止は :py:meth:`stop` または task.cancel()。
        """
        if self._start_task is not None and not self._start_task.done():
            logger.warning("discord_bridge.PicoBot: already running, skipping")
            return self._start_task
        self._start_task = asyncio.create_task(self.start())
        return self._start_task

    async def stop(self) -> None:
        """Bot を停止する。

        実 client があれば close() を await。background task があれば cancel。
        """
        if self._client is not None:
            try:
                await self._client.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("discord_bridge.PicoBot: close failed: {}", exc)
        if self._start_task is not None and not self._start_task.done():
            self._start_task.cancel()
            try:
                await self._start_task
            except (asyncio.CancelledError, Exception):
                pass
        self._is_running = False
        self._client = None
        self._start_task = None
        logger.info("discord_bridge.PicoBot: stopped")

    async def send_message(self, channel_id: int, content: str) -> bool:
        """テキストメッセージを Discord チャンネルに送信する。

        Args:
            channel_id: 送信先 Discord チャンネル ID。
            content: 送信メッセージ本文 (空文字は送信しない)。

        Returns:
            送信成功なら True、未起動/チャンネル不在/送信失敗なら False。

        Raises:
            DiscordDisabledError: client が未起動 (start() 前)。
        """
        if self._client is None:
            raise DiscordDisabledError(
                "PicoBot client is not started; call start() first"
            )
        if not content or not content.strip():
            logger.warning("PicoBot.send_message: empty content, skipping")
            return False
        try:
            channel = self._client.get_channel(channel_id)
            if channel is None:
                # キャッシュにない時は fetch_channel
                channel = await self._client.fetch_channel(channel_id)
            await channel.send(content)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "PicoBot.send_message: failed channel_id={} err={}",
                channel_id,
                exc,
            )
            return False
