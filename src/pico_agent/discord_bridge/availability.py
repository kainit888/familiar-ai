"""discord.py の動的可用性チェック。

Phase D 着手前は discord.py を `uv add` しないため、import 失敗時に
明示的なエラーを返す仕組みを用意する。Phase D で discord.py が
追加されたら is_discord_available() が True を返すようになる。
"""

from __future__ import annotations

# 直近の `is_discord_available()` 試行で捕捉した ImportError 等の `repr(e)` を保持する
# モジュールグローバル。値の更新は `is_discord_available()` のみが行い、参照は
# `last_import_error()` 経由を想定。Phase C-1 時点では discord.py 未インストールで
# 失敗するため、ここに最後の失敗理由が残る。Phase D 以降は成功すれば None になる。
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
    """直近の `is_discord_available()` 試行で発生した import error 表現を返す。

    主にデバッグ・診断用 (起動時ログ・/status コマンドへの埋め込み等)。
    プロダクション制御フローでは使わないこと: 値は最新の `is_discord_available()`
    呼び出しに依存するため、競合条件で見たいエラーと別物になる可能性がある。
    制御判定には `is_discord_available()` の戻り値を直接使う。
    """
    return _DISCORD_PY_IMPORT_ERROR


class DiscordDisabledError(RuntimeError):
    """discord.py 未インストール状態で Discord 機能を呼び出した時の例外。

    Phase D 着手前 (Phase C-1 完了時点) は基本的にこれが発火する。
    呼び出し側は `try/except DiscordDisabledError` で握り潰すか、
    `is_discord_available()` で事前判定すること。
    """

    def __init__(self, message: str = "discord.py is not installed (Phase D pending)") -> None:
        super().__init__(message)
