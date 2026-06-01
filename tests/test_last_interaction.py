"""Phase E: last_interaction persistence tests.

mutation 対応:
    - update の永続化を skip → test_update_persists_and_load が fail
    - ISO→epoch 復元を破壊 → test_startup_restore_seeds_value が fail
    - kind 検証を削除 → test_kind_recorded が fail
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from familiar_agent.emotion.last_interaction import LastInteraction

_T = 1_700_000_000.0


def test_update_persists_and_load(tmp_path):
    p = tmp_path / "li.json"
    LastInteraction(path=p).update("stt", now=_T)
    restored = LastInteraction(path=p).load()
    assert restored == pytest.approx(_T, abs=1.0)


def test_load_missing_returns_none(tmp_path):
    assert LastInteraction(path=tmp_path / "nope.json").load() is None


def test_kind_recorded(tmp_path):
    p = tmp_path / "li.json"
    li = LastInteraction(path=p)
    li.update("greeting", now=_T)
    assert li.kind() == "greeting"
    li.update("bogus", now=_T)  # invalid kind → "chat"
    assert li.kind() == "chat"


def test_startup_restore_seeds_value(tmp_path):
    p = tmp_path / "li.json"
    p.write_text(
        json.dumps({"timestamp": datetime.fromtimestamp(_T).isoformat(), "kind": "chat"})
    )
    assert LastInteraction(path=p).load() == pytest.approx(_T, abs=1.0)
