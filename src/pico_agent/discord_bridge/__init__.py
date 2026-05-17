"""ピコ独自 Discord 統合 (設計書 v4.0 第 7-1 / 第 13 章) - Phase D スケルトン。

Phase D 着手時に本実装。Phase C-1 完了時点 (2026-05-18 夜間) では
**骨組み + テスト + 設計メモ** のみ。実 Discord 接続は OWNER 設定や
Bot Token が揃ってから (overnight_task.md タスク 4 参照)。

公開シンボル:
    PicoBot: Discord クライアント (実装時は discord.py を内部利用)
    TextChannelHandler: テキストチャンネルの on_message ハンドラ
    VoiceChannelListener: 音声チャンネル常駐リスナ
    DiscordDisabledError: discord.py 未インストール時に発火する明示例外
    is_discord_available(): discord.py が import 可能かを判定
"""

from .availability import DiscordDisabledError, is_discord_available
from .bot import PicoBot
from .text_channel import TextChannelHandler
from .voice_channel import VoiceChannelListener

__all__ = [
    "PicoBot",
    "TextChannelHandler",
    "VoiceChannelListener",
    "DiscordDisabledError",
    "is_discord_available",
]
