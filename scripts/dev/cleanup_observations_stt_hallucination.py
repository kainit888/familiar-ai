#!/usr/bin/env python3
"""Phase I 後処理: STT 幻聴 (C-13) 由来の誤記憶 observation を監査/除去する。

Phase C-13 / C-13a (commits 1717a12 + cc64cf5) で Whisper 幻聴フィルタが両側
(Pi5 stt_hallucination_filter + メイン PC whisper_server) に入る前に、無音/雑音を
定型句 (「ありがとうございました」「はい」「いい」等) と誤認識した発話が
observations.db に誤記憶として残り得る。本スクリプトはそれを **精査 (dry-run 既定)**
し、明確な幻聴由来のみ除去する。B1 (cleanup_observations_problem1.py) と同じ
dry-run + apply + WAL-safe backup パターン。

設計思想:
    - 判定は保守的 (false-negative 寄り)。allowlist の語、hedge 済み
      (「空耳かもしれない」等)、denylist に無い締め言葉 (「おやすみなさい」) は **keep**。
    - 検出は live filter (`pico_agent.stt_hallucination_filter`) の denylist/allowlist/
      正規化を **import** して単一ソース化 (コピーして drift させない)。dev スクリプト
      からの read-only import は runtime ではないため二層分離に反しない。
    - 実 DB の誤記憶は幻聴句が `「…」` 引用で物語に埋め込まれた形 (例
      「カイニットが『いい』と言って…」) が主。よって live filter をそのまま発話全体に
      かけるのではなく、引用句抽出 (Path A) + 短い単発幻聴 (Path B) で検出する。
    - 既定アクションは **supersede** (`superseded_by` マーク)。observations は
      obs_embeddings / memory_links から ON DELETE CASCADE で参照されるため hard DELETE は
      破壊的。recall は `superseded_by IS NULL` で除外するので、supersede でピコの
      アクティブ記憶からは消える (埋め込み/リンクは保持)。--mode remove で削除も可。
    - 既定 --dry-run。実変更には --apply。--apply は変更前に SQLite backup API で
      WAL-safe スナップショットを取る (cp は WAL 未反映を取りこぼすため使わない)。
    - 候補 0 件なら --apply でも no-op (backup を作らない)。冪等
      (superseded 済みは再走査対象外)。

範囲外 (本スクリプトは触らない):
    - vision 由来 identity 誤認 (B1 で is_whisper_hallucination 範囲外として処理済)。
    - self_narrative.jsonl / web_knowledge (別系統)。
    - denylist に無い締め言葉 → review-manual として報告のみ (自動では触らない)。

使い方:
    uv run python scripts/dev/cleanup_observations_stt_hallucination.py            # dry-run
    uv run python scripts/dev/cleanup_observations_stt_hallucination.py --apply    # supersede 実行 (backup 付き)
    uv run python scripts/dev/cleanup_observations_stt_hallucination.py --apply --mode remove
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import sqlite3
import sys

# live filter を単一ソースとして import (denylist/allowlist/正規化を共有、drift 防止)。
from pico_agent.stt_hallucination_filter import (
    _ALLOWLIST_NORM,
    _CONTAIN_MIN_PHRASE_LEN,
    _DENYLIST_NORM,
    _SHORT_CONTAIN_MAX,
    _SHORT_CONTAIN_MIN,
    _normalize,
)

_DEFAULT_DB = os.path.expanduser("~/.familiar_ai/observations.db")
_DEFAULT_COMPANION = "カイニット"
_SUPERSEDE_SENTINEL = "stt-hallucination-cleanup"

# 引用句抽出 (「…」/『…』)。幻聴句は引用で物語に埋め込まれる。
_QUOTE_RE = re.compile(r"[「『]([^」』]{1,40})[」』]")
# hedge / 不確実性 (既に空耳と気づいている記述は触らない、bias-to-keep)。
_UNCERTAINTY_RE = re.compile(r"かもしれない|のかな|だろうか|聞き間違い|空耳|気のせい")
# denylist に無いが締め言葉として手動レビューに回す候補 (自動除去しない)。
_CLOSING_HINT_RE = re.compile(r"おやすみなさい|おやすみ|また明日|さようなら")


def _extract_quotes(content: str) -> list[str]:
    return _QUOTE_RE.findall(content)


def _is_standalone_hallucination(norm: str) -> bool:
    """正規化済み文字列が単独の幻聴句か (live filter rule 3-6 を toggle 抜きで再現)。

    allowlist は呼び出し側で先に除外する前提。ここでは denylist 完全一致 + 短文
    (5-15 字) の prefix / 署名長包含のみ判定する。
    """
    if not norm:
        return False
    if norm in _DENYLIST_NORM:
        return True
    if _SHORT_CONTAIN_MIN <= len(norm) <= _SHORT_CONTAIN_MAX:
        if any(d.startswith(norm) for d in _DENYLIST_NORM):
            return True
        if any(len(d) >= _CONTAIN_MIN_PHRASE_LEN and d in norm for d in _DENYLIST_NORM):
            return True
    return False


def classify_row(*, content: str, kind: str, companion_name: str) -> tuple[str, str]:
    """(action, reason) を返す純関数。action は "supersede" | "review-manual" | "keep"。

    保守的順序 (先勝ち):
        1. content 全体が allowlist 完全一致      → keep (allowlisted)
        2. hedge / 不確実性を表現している          → keep (hedged)
        3. companion が denylist 句を引用している  → supersede (stt-hallucination-quote)
        4. content 全体が単独の短い幻聴句          → supersede (stt-hallucination-standalone)
        5. denylist 外の締め言葉                   → review-manual (closing-phrase, 自動では触らない)
        6. それ以外                                → keep (no-hallucination)

    I/O・env・DB に触れない (env トグルは cleanup では無視し常に分類する)。
    """
    norm_full = _normalize(content)
    if norm_full and norm_full in _ALLOWLIST_NORM:
        return "keep", "allowlisted"
    if _UNCERTAINTY_RE.search(content):
        return "keep", "hedged"
    # Path A: companion が denylist 句を引用 (allowlist 句は除外)
    if companion_name and companion_name in content:
        for quote in _extract_quotes(content):
            nq = _normalize(quote)
            if not nq or nq in _ALLOWLIST_NORM:
                continue
            if nq in _DENYLIST_NORM:
                return "supersede", "stt-hallucination-quote"
    # Path B: content 全体が単独の短い幻聴句
    if _is_standalone_hallucination(norm_full):
        return "supersede", "stt-hallucination-standalone"
    # 締め言葉 (denylist 外) は手動レビューへ
    if _CLOSING_HINT_RE.search(content):
        return "review-manual", "closing-phrase"
    return "keep", "no-hallucination"


def backup_db(db_path: str, date_str: str) -> str:
    """変更前に SQLite online backup API で WAL-safe スナップショットを作る。

    cp と違い未 checkpoint の WAL ページも含む完全スナップショットになる。
    """
    dest = f"{db_path}.bak_stt_hallucination_{date_str}"
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
        description="Phase I: STT 幻聴由来の誤記憶 observation の監査/除去 (dry-run 既定)",
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
        help="実際に supersede/remove を実行する (--dry-run を無効化)",
    )
    parser.add_argument(
        "--mode",
        choices=("supersede", "remove"),
        default="supersede",
        help="supersede=superseded_by マーク (既定, 非破壊) / remove=行削除 (CASCADE 破壊的)",
    )
    parser.add_argument(
        "--companion",
        default=os.environ.get("COMPANION_NAME", _DEFAULT_COMPANION),
        help=f"引用元 companion 名 (既定: env COMPANION_NAME or {_DEFAULT_COMPANION})",
    )
    parser.add_argument(
        "--backup-suffix",
        default=datetime.date.today().isoformat(),
        help="backup ファイル名の日付サフィックス (既定: 今日)",
    )
    return parser.parse_args(argv)


def _fetch_rows(con: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """アクティブ (superseded_by IS NULL) な (id, content, kind) を取得。"""
    cur = con.execute(
        "SELECT id, content, kind FROM observations "
        "WHERE superseded_by IS NULL ORDER BY timestamp"
    )
    out: list[tuple[str, str, str]] = []
    for r in cur.fetchall():
        out.append(
            (
                str(r[0]),
                str(r[1]) if r[1] is not None else "",
                str(r[2]) if r[2] is not None else "observation",
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
        review: list[tuple[str, str, str]] = []  # (id, content, reason)
        reasons: dict[str, int] = {}
        for rid, content, kind in rows:
            action, reason = classify_row(
                content=content, kind=kind, companion_name=args.companion
            )
            reasons[reason] = reasons.get(reason, 0) + 1
            if action == "supersede":
                candidates.append((rid, content, reason))
            elif action == "review-manual":
                review.append((rid, content, reason))

        print(f"DB: {db_path}")
        print(f"companion: {args.companion!r}  mode: {args.mode}")
        print(f"observations active (superseded_by IS NULL): {total}")
        for reason, n in sorted(reasons.items()):
            print(f"  {reason}: {n}")
        print(f"candidates (auto-supersede): {len(candidates)}")
        print(f"review-manual (kept, surfaced for owner): {len(review)}")
        print("-" * 72)
        for rid, content, reason in candidates[:10]:
            snippet = content[:60].replace("\n", "\\n")
            print(f"[{reason}] id={rid}")
            print(f"    {snippet!r}")
        for rid, content, reason in review[:10]:
            snippet = content[:60].replace("\n", "\\n")
            print(f"[{reason}] (review, kept) id={rid}")
            print(f"    {snippet!r}")

        if args.dry_run:
            print("-" * 72)
            print("[dry-run] no writes. Re-run with --apply to modify the DB.")
            return 0

        if not candidates:
            print("-" * 72)
            print("[no candidates] nothing to clean; no backup, no writes.")
            return 0

        bak = backup_db(db_path, args.backup_suffix)
        print("-" * 72)
        print(f"[backup] {bak}")
        if args.mode == "remove":
            print("[warning] mode=remove hard-DELETEs rows; obs_embeddings / memory_links "
                  "cascade. superseded (default) is non-destructive.")

        changed = 0
        for rid, _content, _reason in candidates:
            if args.mode == "remove":
                con.execute("DELETE FROM observations WHERE id = ?", (rid,))
            else:
                con.execute(
                    "UPDATE observations SET superseded_by = ? WHERE id = ?",
                    (_SUPERSEDE_SENTINEL, rid),
                )
            changed += 1
        con.commit()

        after = len(_fetch_rows(con))
        print(f"[apply] mode={args.mode} changed {changed} rows")
        print(f"observations active: before={total} after={after}")
        print(f"[backup] saved at {bak}")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
