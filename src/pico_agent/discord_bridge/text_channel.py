"""Discord テキストチャンネル on_message ハンドラ (設計書 v4.0 第 13-1 章)。

Phase D 実装版: discord.Message → IncomingMessage の変換関数を追加。
boundary 判定ロジックは Phase C-1 のままで、純粋関数として再利用可能。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from loguru import logger


@dataclass
class IncomingMessage:
    """discord.Message から抽出した最小限のメタ情報。

    Phase C-1 では discord.py 非依存のテストを書くために dataclass で抽象化。
    Phase D 着手時に discord.Message → IncomingMessage の変換関数を追加する。
    """

    author_id: int
    author_is_bot: bool
    guild_id: Optional[int]
    channel_id: int
    content: str


class TextChannelHandler:
    """on_message ハンドラ (boundary 通過後に ReAct loop へ流す)。

    設計書 13-1:
        ```python
        if msg.author.bot or msg.author.id != OWNER_ID and msg.guild_id != GUILD_ID:
            return
        ```
    つまり「bot は無視 / OWNER 以外は GUILD 内発言のみ通す」というポリシー。

    Args:
        owner_id: ピコの OWNER (カイニット) の Discord ユーザー ID。
        guild_id: OWNER 以外も応答する Guild ID。
        on_input: 入力をふるい落とさず ReAct loop に渡す async callback。
            シグネチャ: ``async def on_input(user_id: str, content: str) -> str``
    """

    def __init__(
        self,
        owner_id: Optional[int],
        guild_id: Optional[int],
        on_input: Optional[Callable[[str, str], Awaitable[str]]] = None,
    ) -> None:
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.on_input = on_input

    def should_handle(self, msg: IncomingMessage) -> bool:
        """boundary 判定: このメッセージに応答するかどうか。

        ルール (設計書 13-1):
            1. bot からのメッセージは無視
            2. OWNER (owner_id) からのメッセージは常に応答
            3. OWNER 以外は GUILD_ID 内のチャンネル発言のみ応答
            4. owner_id / guild_id が未設定なら全て無視 (安全側)
        """
        if msg.author_is_bot:
            return False
        if self.owner_id is None and self.guild_id is None:
            # 設定未満 → 安全側で無視
            logger.debug(
                "text_channel: ignoring msg (no owner_id/guild_id configured)"
            )
            return False
        if self.owner_id is not None and msg.author_id == self.owner_id:
            return True
        if self.guild_id is not None and msg.guild_id == self.guild_id:
            return True
        return False

    async def handle(self, msg: IncomingMessage) -> Optional[str]:
        """boundary 通過したら ReAct loop に流して応答 text を返す。

        Returns:
            応答テキスト (送信は呼び出し側責務)。フィルタアウトされたら None。
        """
        if not self.should_handle(msg):
            return None
        if self.on_input is None:
            logger.warning(
                "text_channel.handle: on_input callback not configured"
            )
            return None
        return await self.on_input(str(msg.author_id), msg.content)


def incoming_message_from_discord(msg: Any) -> IncomingMessage:
    """discord.Message → IncomingMessage 変換 (Phase D 実装版)。

    discord.py を import 依存にしないため、duck-typing で必要属性を抽出する。
    呼び出し側 (PicoBot.on_message) は msg が discord.Message であることを
    前提とするが、本関数は型に縛られない (テスト時は MagicMock でも動く)。

    Args:
        msg: discord.Message 相当オブジェクト。以下の属性を持つ前提:
            - author.id (int)
            - author.bot (bool)
            - guild.id (int or None)
            - channel.id (int)
            - content (str)

    Returns:
        IncomingMessage dataclass。

    Raises:
        AttributeError: 必要な属性が欠けている場合。
    """
    author_id = int(msg.author.id)
    author_is_bot = bool(getattr(msg.author, "bot", False))
    guild = getattr(msg, "guild", None)
    guild_id = int(guild.id) if guild is not None else None
    channel_id = int(msg.channel.id)
    content = str(getattr(msg, "content", "") or "")

    return IncomingMessage(
        author_id=author_id,
        author_is_bot=author_is_bot,
        guild_id=guild_id,
        channel_id=channel_id,
        content=content,
    )
