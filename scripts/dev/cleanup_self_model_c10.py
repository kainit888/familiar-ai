#!/usr/bin/env python3
"""Phase C-10: 既存 DB の汚染 self_model 行をクリーンアップする。

`_update_self_model` (familiar_agent/agent.py) が utility backend
(qwen2.5:1.5b) の verbatim 反射 / ラベルリーク / 異言語混入をそのまま
kind='self_model' で保存してしまった既存行を、`self_model_filter` の
`is_contaminated_existing_row` で判定し削除する。

判定ロジックの**単一の真実源**は `pico_agent.self_model_filter`。本スクリプトは
SQL の実行のみを担い、汚染判定は一切ここに書かない (保存前フィルタと既存行
クリーンアップが乖離しないようにするため)。

安全設計:
    - DROP は使わない。kind='self_model' かつ汚染判定 True の行を id 指定で
      DELETE するのみ。
    - 既定は --dry-run (削除候補を列挙するのみ、DB は変更しない)。
    - 実削除には明示の --apply が必要。
    - フィルタ通過行 (正常な self_model) は残す → **冪等**。再実行しても
      削除済み行は存在しないので no-op (削除 0 件)。

バックアップ手順 (実行前に必須):
    cp ~/.familiar_ai/observations.db ~/.familiar_ai/observations.db.bak_c10

使い方:
    # 削除候補の確認 (DB 変更なし)
    uv run python scripts/dev/cleanup_self_model_c10.py --dry-run
    # 実削除 (バックアップ後に)
    uv run python scripts/dev/cleanup_self_model_c10.py --apply
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

# scripts/dev/ から src/ をパスに追加 (uv run でも素の python でも動くように)
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from pico_agent.self_model_filter import (  # noqa: E402
    contamination_reason,
    is_contaminated_existing_row,
)

_DEFAULT_DB = os.path.expanduser("~/.familiar_ai/observations.db")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Phase C-10: 汚染 self_model 行のクリーンアップ (DELETE のみ、DROP 禁止)",
    )
    parser.add_argument(
        "--db",
        default=_DEFAULT_DB,
        help=f"対象 SQLite DB パス (既定: {_DEFAULT_DB})",
    )
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="削除候補を列挙するのみ (既定: True)",
    )
    parser.add_argument(
        "--apply",
        dest="dry_run",
        action="store_false",
        help="実際に DELETE を実行する (--dry-run を無効化)",
    )
    return parser.parse_args(argv)


def _fetch_self_model_rows(con: sqlite3.Connection) -> list[tuple[str, str]]:
    """kind='self_model' の (id, content) 全行を取得する。"""
    cur = con.execute(
        "SELECT id, content FROM observations WHERE kind = 'self_model' ORDER BY timestamp"
    )
    return [(str(r[0]), str(r[1]) if r[1] is not None else "") for r in cur.fetchall()]


def _delete_rows(con: sqlite3.Connection, ids: list[str]) -> int:
    """指定 id の self_model 行を DELETE する。削除件数を返す。"""
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    cur = con.execute(
        f"DELETE FROM observations WHERE kind = 'self_model' AND id IN ({placeholders})",
        ids,
    )
    con.commit()
    return cur.rowcount


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    db_path = os.path.expanduser(args.db)
    if not os.path.exists(db_path):
        print(f"[error] DB not found: {db_path}", file=sys.stderr)
        return 1

    con = sqlite3.connect(db_path)
    try:
        rows = _fetch_self_model_rows(con)
        total = len(rows)
        contaminated = [
            (rid, content, contamination_reason(content))
            for rid, content in rows
            if is_contaminated_existing_row(content)
        ]

        print(f"DB: {db_path}")
        print(f"self_model total rows: {total}")
        print(f"contaminated (delete candidates): {len(contaminated)}")
        print(f"will keep: {total - len(contaminated)}")
        print("-" * 72)
        for rid, content, reason in contaminated:
            snippet = content[:60].replace("\n", "\\n")
            print(f"[{reason}] id={rid}")
            print(f"    {snippet!r}")

        if args.dry_run:
            print("-" * 72)
            print("[dry-run] no rows deleted. Re-run with --apply to delete.")
            return 0

        ids = [rid for rid, _, _ in contaminated]
        before = total
        deleted = _delete_rows(con, ids)
        after = len(_fetch_self_model_rows(con))
        print("-" * 72)
        print(f"[apply] deleted {deleted} rows")
        print(f"self_model count: before={before} after={after}")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
