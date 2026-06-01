"""Phase I 後処理スクリプトのテスト (実 DB は触らず tmp_path の temp DB を使う)。

mutation 対応 (必須4):
    - --dry-run 既定を False にする → test_cleanup_dry_run_does_not_modify_db が fail
    - backup_db 呼び出しを消す → test_cleanup_apply_supersedes_and_backs_up が fail
    - denylist 判定 (Path A/B) を消す → test_cleanup_apply_supersedes_and_backs_up が fail
    - allowlist/hedge keep ガードを消す → test_classify_allowlisted_quote_kept /
      test_classify_hedged_kept が fail
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "dev"
    / "cleanup_observations_stt_hallucination.py"
)
_spec = importlib.util.spec_from_file_location("cleanup_observations_stt_hallucination", _SCRIPT)
assert _spec and _spec.loader
cleanup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cleanup)


_COMPANION = "カイニット"

# (id, content, kind) → expected action ("supersede" | "keep" | "review-manual")
_SEED = [
    # quoted denylist phrase attributed to companion → supersede
    ("h1", "カイニットが「いい」と言って、幸せそうな様子を見せた。私も嬉しくなった。", "observation", "supersede"),
    ("h2", "カイニットと「はい」という短いやり取りをした。会話が続いて嬉しい。", "observation", "supersede"),
    # allowlisted quote → keep (allowlist precedence)
    ("a1", "カイニットが「ありがとう」と言ってくれた。", "observation", "keep"),
    # hedged / already-recognized-as-mishearing → keep
    ("g1", "カイニットが「はい」と言った気がするけど空耳かもしれない。", "observation", "keep"),
    # plain conversation summary, no denylist quote → keep
    ("c1", "カイニットと今日の天気について話した。", "conversation", "keep"),
    # closing phrase not in denylist → review-manual (kept, not auto-superseded)
    ("r1", "了解。おやすみなさい。", "observation", "review-manual"),
]


def _seed_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute(
        """CREATE TABLE observations (
            id TEXT PRIMARY KEY, content TEXT NOT NULL, timestamp TEXT NOT NULL,
            date TEXT NOT NULL, time TEXT NOT NULL, direction TEXT NOT NULL DEFAULT 'unknown',
            kind TEXT NOT NULL DEFAULT 'observation', emotion TEXT NOT NULL DEFAULT 'neutral',
            image_path TEXT, image_data TEXT, importance REAL NOT NULL DEFAULT 1.0,
            superseded_by TEXT)"""
    )
    for i, (rid, content, kind, _exp) in enumerate(_SEED):
        con.execute(
            "INSERT INTO observations (id, content, timestamp, date, time, kind) "
            "VALUES (?,?,?,?,?,?)",
            (rid, content, f"2026-05-31T00:00:0{i}", "2026-05-31", f"00:00:0{i}", kind),
        )
    con.commit()
    con.close()


def _seed_clean_db(path: Path) -> None:
    """No hallucination candidates at all (for the graceful no-op test)."""
    con = sqlite3.connect(path)
    con.execute(
        """CREATE TABLE observations (
            id TEXT PRIMARY KEY, content TEXT NOT NULL, timestamp TEXT NOT NULL,
            date TEXT NOT NULL, time TEXT NOT NULL, direction TEXT NOT NULL DEFAULT 'unknown',
            kind TEXT NOT NULL DEFAULT 'observation', emotion TEXT NOT NULL DEFAULT 'neutral',
            image_path TEXT, image_data TEXT, importance REAL NOT NULL DEFAULT 1.0,
            superseded_by TEXT)"""
    )
    con.execute(
        "INSERT INTO observations (id, content, timestamp, date, time, kind) VALUES (?,?,?,?,?,?)",
        ("c1", "カイニットと今日の天気について話した。", "2026-05-31T00:00:00", "2026-05-31", "00:00:00", "conversation"),
    )
    con.commit()
    con.close()


def _superseded_of(path: Path, rid: str) -> str | None:
    con = sqlite3.connect(path)
    try:
        row = con.execute("SELECT superseded_by FROM observations WHERE id=?", (rid,)).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def _backups(path: Path) -> list[Path]:
    return list(path.parent.glob(f"{path.name}.bak_stt_hallucination_*"))


def _run(db: Path, *extra: str) -> int:
    return cleanup.main(["--db", str(db), "--companion", _COMPANION, *extra])


# ── classify_row 単体 (純関数) ───────────────────────────────────────────────
def test_classify_row_purity():
    # 同一入力で副作用なく決定的に同じ出力 (I/O/env/DB 非依存)
    kwargs = dict(content="カイニットが「いい」と言って嬉しかった。", kind="observation", companion_name=_COMPANION)
    first = cleanup.classify_row(**kwargs)
    second = cleanup.classify_row(**kwargs)
    assert first == second == ("supersede", "stt-hallucination-quote")


def test_classify_quoted_hallucination_superseded():
    action, reason = cleanup.classify_row(
        content="カイニットが「いい」と言って、幸せそうな様子を見せた。", kind="observation", companion_name=_COMPANION
    )
    assert action == "supersede" and reason == "stt-hallucination-quote"


def test_classify_allowlisted_kept():
    # Bare "ありがとう" is a prefix of denylist "ありがとうございました"; only the
    # allowlist guard (rule 1) keeps it from being flagged as standalone (load-bearing).
    action, reason = cleanup.classify_row(
        content="ありがとう", kind="conversation", companion_name=_COMPANION
    )
    assert action == "keep" and reason == "allowlisted"


def test_classify_allowlisted_quote_kept():
    # An allowlisted quote (not a denylist phrase) is also kept.
    action, reason = cleanup.classify_row(
        content="カイニットが「ありがとう」と言ってくれた。", kind="observation", companion_name=_COMPANION
    )
    assert action == "keep"


def test_classify_hedged_kept():
    action, reason = cleanup.classify_row(
        content="カイニットが「はい」と言った気がするけど空耳かもしれない。",
        kind="observation",
        companion_name=_COMPANION,
    )
    assert action == "keep" and reason == "hedged"


def test_classify_plain_conversation_kept():
    action, reason = cleanup.classify_row(
        content="カイニットと今日の天気について話した。", kind="conversation", companion_name=_COMPANION
    )
    assert action == "keep" and reason == "no-hallucination"


def test_classify_closing_phrase_review_manual():
    action, reason = cleanup.classify_row(
        content="了解。おやすみなさい。", kind="observation", companion_name=_COMPANION
    )
    assert action == "review-manual" and reason == "closing-phrase"


# ── スクリプト統合 (tmp DB) ──────────────────────────────────────────────────
def test_cleanup_dry_run_does_not_modify_db(tmp_path):
    db = tmp_path / "obs.db"
    _seed_db(db)
    rc = _run(db)  # default dry-run
    assert rc == 0
    # candidates not superseded; no backup created
    assert _superseded_of(db, "h1") is None
    assert _superseded_of(db, "h2") is None
    assert _backups(db) == []


def test_cleanup_apply_supersedes_and_backs_up(tmp_path):
    db = tmp_path / "obs.db"
    _seed_db(db)
    rc = _run(db, "--apply")
    assert rc == 0
    # the two quoted-hallucination rows are superseded
    assert _superseded_of(db, "h1") == cleanup._SUPERSEDE_SENTINEL
    assert _superseded_of(db, "h2") == cleanup._SUPERSEDE_SENTINEL
    # kept rows untouched
    assert _superseded_of(db, "a1") is None
    assert _superseded_of(db, "g1") is None
    assert _superseded_of(db, "c1") is None
    assert _superseded_of(db, "r1") is None  # review-manual kept
    # backup exists, is valid SQLite, and holds PRE-change state (h1 NULL)
    baks = _backups(db)
    assert len(baks) == 1
    bcon = sqlite3.connect(baks[0])
    try:
        pre = bcon.execute("SELECT superseded_by FROM observations WHERE id='h1'").fetchone()
        assert pre[0] is None
    finally:
        bcon.close()


def test_cleanup_preserves_short_but_meaningful(tmp_path):
    # false-positive 抑止: allowlisted quote + hedged row survive --apply
    db = tmp_path / "obs.db"
    _seed_db(db)
    _run(db, "--apply")
    assert _superseded_of(db, "a1") is None  # allowlisted quote survives
    assert _superseded_of(db, "g1") is None  # hedged row survives


def test_cleanup_zero_candidates_no_backup_no_write(tmp_path):
    db = tmp_path / "obs.db"
    _seed_clean_db(db)
    rc = _run(db, "--apply")
    assert rc == 0
    assert _superseded_of(db, "c1") is None
    assert _backups(db) == []  # graceful: no backup when nothing to clean


def test_cleanup_handles_empty_db(tmp_path):
    db = tmp_path / "obs.db"
    # empty table (no rows)
    con = sqlite3.connect(db)
    con.execute(
        """CREATE TABLE observations (
            id TEXT PRIMARY KEY, content TEXT NOT NULL, timestamp TEXT NOT NULL,
            date TEXT NOT NULL, time TEXT NOT NULL, direction TEXT NOT NULL DEFAULT 'unknown',
            kind TEXT NOT NULL DEFAULT 'observation', emotion TEXT NOT NULL DEFAULT 'neutral',
            image_path TEXT, image_data TEXT, importance REAL NOT NULL DEFAULT 1.0,
            superseded_by TEXT)"""
    )
    con.commit()
    con.close()
    assert _run(db, "--apply") == 0
    assert _backups(db) == []


def test_cleanup_missing_db_returns_error(tmp_path):
    assert _run(tmp_path / "nope.db") == 1


def test_cleanup_remove_mode_deletes(tmp_path):
    db = tmp_path / "obs.db"
    _seed_db(db)
    _run(db, "--apply", "--mode", "remove")
    con = sqlite3.connect(db)
    try:
        ids = {r[0] for r in con.execute("SELECT id FROM observations").fetchall()}
    finally:
        con.close()
    assert "h1" not in ids and "h2" not in ids  # hard-deleted
    assert {"a1", "g1", "c1", "r1"} <= ids  # kept rows remain
