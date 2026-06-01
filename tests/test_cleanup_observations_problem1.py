"""Problem-1 後処理スクリプトのテスト (実 DB は触らず tmp_path の temp DB を使う)。

mutation 対応:
    - classify の uncertainty/stt-confirmed keep を消す → keep 系テストが fail
    - backup_db 呼び出しを消す → test_cleanup_creates_backup_before_modify が fail
    - --dry-run 既定を False にする → test_cleanup_dry_run_does_not_modify_db が fail
    - vision 判定を消す (全行候補化) → test_cleanup_preserves_* が fail
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "dev"
    / "cleanup_observations_problem1.py"
)
_spec = importlib.util.spec_from_file_location("cleanup_observations_problem1", _SCRIPT)
assert _spec and _spec.loader
cleanup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cleanup)


_COMPANION = "カイニット"

# (id, content, image_path, kind) → expected action
_SEED = [
    # V: vision hard-assertion (image + companion, no hedge/STT) → rewrite
    ("v1", "視覚ログ カイニットが目の前にいる。", "/x.jpg", "observation", "rewrite"),
    # S: STT-confirmed (companion quoted) → keep
    ("s1", "カイニットが「聞こえてる？」と確認してくる", "/y.jpg", "observation", "keep"),
    # U: uncertainty already expressed → keep
    ("u1", "視覚ログ 誰か（カイニットかもしれない）が見ている", None, "observation", "keep"),
    # C: plain conversation → keep (not vision-derived)
    ("c1", "カイニットと今日の天気を話した", None, "conversation", "keep"),
    # O: non-vision observation w/ companion, no STT/uncertainty marker → keep.
    # Its ONLY keep-path is the vision guard (kind!=conversation, no hedge/STT),
    # so removing that guard turns it into a rewrite candidate (guards M4).
    ("o1", "カイニットの誕生日を祝った", None, "observation", "keep"),
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
    for i, (rid, content, image_path, kind, _exp) in enumerate(_SEED):
        con.execute(
            "INSERT INTO observations (id, content, timestamp, date, time, kind, image_path) "
            "VALUES (?,?,?,?,?,?,?)",
            (rid, content, f"2026-05-31T00:00:0{i}", "2026-05-31", f"00:00:0{i}", kind, image_path),
        )
    con.commit()
    con.close()


def _content_of(path: Path, rid: str) -> str | None:
    con = sqlite3.connect(path)
    try:
        row = con.execute("SELECT content FROM observations WHERE id=?", (rid,)).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def _run(db: Path, *extra: str) -> int:
    return cleanup.main(["--db", str(db), "--companion", _COMPANION, *extra])


# ── classify_row 単体 ──────────────────────────────────────────────────────────


def test_classify_row_cases():
    for rid, content, image_path, kind, expected in _SEED:
        action, _reason = cleanup.classify_row(
            content=content, image_path=image_path, kind=kind, companion_name=_COMPANION
        )
        got = "keep" if action == "keep" else "rewrite"
        assert got == expected, f"{rid}: expected {expected}, got {action}"


def test_classify_non_vision_kept_via_vision_guard():
    # o1's only keep-path is the vision guard → asserting reason catches removal
    # of that guard (which would make every companion-mentioning row a candidate).
    action, reason = cleanup.classify_row(
        content="カイニットの誕生日を祝った",
        image_path=None,
        kind="observation",
        companion_name=_COMPANION,
    )
    assert action == "keep"
    assert reason == "not-vision"


# ── dry-run / apply / backup ───────────────────────────────────────────────────


def test_cleanup_dry_run_does_not_modify_db(tmp_path):
    db = tmp_path / "obs.db"
    _seed_db(db)
    before = {rid: _content_of(db, rid) for rid, *_ in _SEED}
    rc = _run(db)  # dry-run is default
    assert rc == 0
    after = {rid: _content_of(db, rid) for rid, *_ in _SEED}
    assert before == after
    assert not list(tmp_path.glob("obs.db.bak_problem1_*"))  # no backup in dry-run


def test_cleanup_apply_rewrites_vision_only_identity(tmp_path):
    db = tmp_path / "obs.db"
    _seed_db(db)
    rc = _run(db, "--apply", "--mode", "rewrite", "--backup-suffix", "2026-06-01")
    assert rc == 0
    # V rewritten: companion name replaced by label "誰か"
    v = _content_of(db, "v1")
    assert _COMPANION not in v
    assert "誰か" in v


def test_cleanup_apply_remove_mode_deletes_candidate(tmp_path):
    db = tmp_path / "obs.db"
    _seed_db(db)
    rc = _run(db, "--apply", "--mode", "remove", "--backup-suffix", "2026-06-01")
    assert rc == 0
    assert _content_of(db, "v1") is None  # removed
    assert _content_of(db, "s1") is not None  # others kept


def test_cleanup_preserves_stt_confirmed_identity(tmp_path):
    db = tmp_path / "obs.db"
    _seed_db(db)
    _run(db, "--apply", "--backup-suffix", "2026-06-01")
    # STT-confirmed and uncertainty and conversation rows keep カイニット
    for rid in ("s1", "u1", "c1"):
        assert _COMPANION in _content_of(db, rid)


def test_cleanup_creates_backup_before_modify(tmp_path):
    db = tmp_path / "obs.db"
    _seed_db(db)
    _run(db, "--apply", "--backup-suffix", "2026-06-01")
    bak = tmp_path / "obs.db.bak_problem1_2026-06-01"
    assert bak.exists()
    # backup is valid SQLite AND holds the PRE-change V content (still カイニット)
    con = sqlite3.connect(bak)
    try:
        row = con.execute("SELECT content FROM observations WHERE id='v1'").fetchone()
        assert row is not None
        assert _COMPANION in row[0]  # backup captured state before the rewrite
    finally:
        con.close()
