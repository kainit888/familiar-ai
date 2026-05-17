"""discord.py の動的可用性チェック。

Phase D 着手前は discord.py を `uv add` しないため、import 失敗時に
明示的なエラーを返す仕組みを用意する。Phase D で discord.py が
追加されたら is_discord_available() が True を返すようになる。
"""

from __future__ import annotations

_DISCORD_PY_IMPORT_ERROR: str | None = None


def is_discord_available() -> bool:
    """discord.py が import 可能かを判定する (キャッシュなし、毎回 import 試行)。

    Phase C-1 完了時点では discord.py 未インストールなので False を返す想定。
    Phase D で `uv add discord.py` 後に True になる。
    """
    global _DISCORD_PY_IMPORT_ERROR
    try:
        import discord  # type: ignore  # noqa: F401
        _DISCORD_PY_IMPORT_ERROR = None
        return True
    except Exception as e:  # noqa: BLE001
        _DISCORD_PY_IMPORT_ERROR = repr(e)
        return False


def last_import_error() -> str | None:
    """直近の is_discord_available() 試行で発生した import error 表現。"""
    return _DISCORD_PY_IMPORT_ERROR


class DiscordDisabledError(RuntimeError):
    """discord.py 未インストール状態で Discord 機能を呼び出した時の例外。

    Phase D 着手前 (Phase C-1 完了時点) は基本的にこれが発火する。
    呼び出し側は `try/except DiscordDisabledError` で握り潰すか、
    `is_discord_available()` で事前判定すること。
    """

    def __init__(self, message: str = "discord.py is not installed (Phase D pending)") -> None:
        super().__init__(message)
