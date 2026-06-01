#!/usr/bin/env python3
"""Problem-1 後処理: vision 由来の identity 誤認 observation を監査/是正する。

Problem-1 修正 (commit e59abba) で ToM の default_person を companion_name から
切り離す前に作られた observations.db の行には、カメラに映った人物を無条件で
companion (例「カイニット」) と決め打ちした vision 由来の誤認が混じり得る。
本スクリプトはそれを **精査 (dry-run 既定)** し、明確な誤認のみ是正する。

設計思想:
    - 判定は保守的。不確実性を既に表現している行 (「誰か（…かもしれない）」) や、
      同一ターンで STT 確認がある行 (「…と確認してくる」) は **keep**。hedge 済み
      の記憶を誤って書き換えない。
    - 既定アクションは **rewrite** (companion 名を中立ラベルへ上書き)。observations は
      obs_embeddings / memory_links から ON DELETE CASCADE で参照されるため、削除は
      破壊的。rewrite は記憶の存在・重要度・リンク・埋め込みを保ったまま誤 identity
      だけ消す。--mode remove で削除も可能 (CASCADE 警告を表示)。
    - 既定は --dry-run (DB を一切変更しない)。実変更には --apply が必須。
    - --apply は変更前に **SQLite backup API** で WAL-safe スナップショットを取る
      (cp は WAL 未反映ページを取りこぼすため使わない)。
    - 冪等: rewrite 後は companion 名が消えるので再実行で再ヒットしない。

範囲外 (本スクリプトは触らない):
    - STT 幻聴 (C-13) 由来の誤記憶 (例「カイニットが『いい』と言って」) — vision 由来
      でないため keep 判定。別問題ストリーム。
    - self_narrative.jsonl (別系統)。

使い方:
    uv run python scripts/dev/cleanup_observations_problem1.py            # dry-run
    uv run python scripts/dev/cleanup_observations_problem1.py --apply    # rewrite 実行 (backup 付き)
    uv run python scripts/dev/cleanup_observations_problem1.py --apply --mode remove
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import sqlite3
import sys

_DEFAULT_DB = os.path.expanduser("~/.familiar_ai/observations.db")
_DEFAULT_COMPANION = "カイニット"
_DEFAULT_LABEL = "誰か"

# vision 由来マーカー (image_path が無い視覚ログを拾う)。
_VISION_MARKER_RE = re.compile(r"視覚ログ|カメラ.{0,6}見|周りを見")
# STT 確認 / 会話交換マーカー (companion が話した/確認したと引用されている)。
_STT_EXCHANGE_RE = re.compile(r"」と(言|確認|聞|話|尋|返)|と確認|と聞か|聞こえ")
# 不確実性マーカー (既に Problem-1 修正後の理想形 = 断定していない)。
_UNCERTAINTY_RE = re.compile(r"かもしれない|かな[?？]|のかな|だろうか|誰か[（(]")


def _is_vision_derived(content: str, image_path: str | None) -> bool:
    if image_path is not None and str(image_path).strip():
        return True
    return bool(_VISION_MARKER_RE.search(content))


def classify_row(
    *, content: str, image_path: str | None, kind: str, companion_name: str
) -> tuple[str, str]:
    """(action, reason) を返す。action は "rewrite" | "remove" | "keep"。

    保守的順序 (先勝ち、各ガードは独立してテスト可能):
        1. 非 vision 由来          → keep
        2. companion を断定していない → keep
        3. 不確実性を表現している  → keep (hedge 済みは触らない)
        4. STT 確認がある          → keep
        5. それ以外                → rewrite (vision 由来の identity 断定)

    返す action は推奨ラベル "rewrite"。呼び出し側が --mode remove のとき remove へ写像。
    """
    if not _is_vision_derived(content, image_path):
        return "keep", "not-vision"
    if companion_name not in content:
        return "keep", "no-companion"
    if _UNCERTAINTY_RE.search(content):
        return "keep", "uncertainty"
    if kind == "conversation" or _STT_EXCHANGE_RE.search(content):
        return "keep", "stt-confirmed"
    return "rewrite", "vision-identity-misid"


def rewrite_content(content: str, companion_name: str, label: str) -> str:
    """companion 名を中立ラベルへ置換 (記憶は残し identity 断定だけ消す)。"""
    return content.replace(companion_name, label)


def backup_db(db_path: str, date_str: str) -> str:
    """変更前に SQLite online backup API で WAL-safe スナップショットを作る。

    cp と違い未 checkpoint の WAL ページも含む完全スナップショットになる。
    """
    dest = f"{db_path}.bak_problem1_{date_str}"
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(dest)
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()
    return dest


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Problem-1: vision 由来 identity 誤認 observation の監査/是正 (dry-run 既定)",
    )
    parser.add_argument("--db", default=_DEFAULT_DB, help=f"対象 SQLite DB (既定: {_DEFAULT_DB})")
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="候補を列挙するのみ、DB は変更しない (既定: True)",
    )
    parser.add_argument(
        "--apply",
        dest="dry_run",
        action="store_false",
        help="実際に rewrite/remove を実行する (--dry-run を無効化)",
    )
    parser.add_argument(
        "--mode",
        choices=("rewrite", "remove"),
        default="rewrite",
        help="rewrite=companion 名を中立ラベルへ上書き (既定, 非破壊) / remove=行を削除 (CASCADE 破壊的)",
    )
    parser.add_argument(
        "--companion",
        default=os.environ.get("COMPANION_NAME", _DEFAULT_COMPANION),
        help=f"識別対象の companion 名 (既定: env COMPANION_NAME or {_DEFAULT_COMPANION})",
    )
    parser.add_argument("--label", default=_DEFAULT_LABEL, help=f"中立ラベル (既定: {_DEFAULT_LABEL})")
    parser.add_argument(
        "--backup-suffix",
        default=datetime.date.today().isoformat(),
        help="backup ファイル名の日付サフィックス (既定: 今日)",
    )
    return parser.parse_args(argv)


def _fetch_rows(con: sqlite3.Connection) -> list[tuple[str, str, str | None, str, str]]:
    """(id, content, image_path, kind, date) 全行を取得。"""
    cur = con.execute(
        "SELECT id, content, image_path, kind, date FROM observations ORDER BY timestamp"
    )
    out: list[tuple[str, str, str | None, str, str]] = []
    for r in cur.fetchall():
        out.append(
            (
                str(r[0]),
                str(r[1]) if r[1] is not None else "",
                (str(r[2]) if r[2] is not None else None),
                str(r[3]) if r[3] is not None else "observation",
                str(r[4]) if r[4] is not None else "",
            )
        )
    return out


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    db_path = os.path.expanduser(args.db)
    if not os.path.exists(db_path):
        print(f"[error] DB not found: {db_path}", file=sys.stderr)
        return 1

    con = sqlite3.connect(db_path)
    try:
        rows = _fetch_rows(con)
        total = len(rows)
        candidates: list[tuple[str, str, str]] = []  # (id, content, reason)
        reasons: dict[str, int] = {}
        for rid, content, image_path, kind, _date in rows:
            action, reason = classify_row(
                content=content,
                image_path=image_path,
                kind=kind,
                companion_name=args.companion,
            )
            reasons[reason] = reasons.get(reason, 0) + 1
            if action == "rewrite":
                candidates.append((rid, content, reason))

        print(f"DB: {db_path}")
        print(f"companion: {args.companion!r}  mode: {args.mode}")
        print(f"observations total: {total}")
        for reason, n in sorted(reasons.items()):
            print(f"  {reason}: {n}")
        print(f"candidates (vision-identity-misid): {len(candidates)}")
        print("-" * 72)
        for rid, content, reason in candidates[:10]:
            snippet = content[:60].replace("\n", "\\n")
            print(f"[{reason}] id={rid}")
            print(f"    {snippet!r}")

        if args.dry_run:
            print("-" * 72)
            print("[dry-run] no writes. Re-run with --apply to modify the DB.")
            return 0

        bak = backup_db(db_path, args.backup_suffix)
        print("-" * 72)
        print(f"[backup] {bak}")

        changed = 0
        for rid, content, _reason in candidates:
            if args.mode == "remove":
                con.execute("DELETE FROM observations WHERE id = ?", (rid,))
            else:
                new_content = rewrite_content(content, args.companion, args.label)
                con.execute(
                    "UPDATE observations SET content = ? WHERE id = ?", (new_content, rid)
                )
            changed += 1
        con.commit()

        after = len(_fetch_rows(con))
        print(f"[apply] mode={args.mode} changed {changed} rows")
        print(f"observations count: before={total} after={after}")
        print(f"[backup] saved at {bak}")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
