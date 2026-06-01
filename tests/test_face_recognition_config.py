"""Phase G: config wiring + Problem-1 invariant (conditional warning).

mutation 対応 (必須4 のうち本ファイルが担う 1):
    - resolve_tom_default_person を companion_name フォールバックに戻す (Problem-1 退行)
      → test_resolve_always_returns_label_never_companion_name が fail
"""

from __future__ import annotations

import logging

import numpy as np
import pytest

from familiar_agent.config import AgentConfig
from pico_agent.adapters import face_recognition as fr


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    for k in (
        "FACE_RECOGNITION_ENABLED",
        "FACE_RECOGNITION_TOLERANCE",
        "FACE_ENCODINGS_DIR",
        "FACE_SAMPLES_DIR",
        "TOM_DEFAULT_PERSON_LABEL",
        "COMPANION_NAME",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(fr, "_RECOGNIZER_OVERRIDE", None)
    monkeypatch.setattr(fr, "_RECOGNIZER_SINGLETON", None)
    monkeypatch.setattr(fr, "_RECOGNIZER_TRIED", False)
    monkeypatch.setattr(fr, "_ENCODINGS_OVERRIDE", None)
    monkeypatch.setattr(fr, "_ENCODINGS_CACHE", None)
    monkeypatch.setattr(fr, "_WARNED_ONCE", False)
    yield


def _make_available(monkeypatch):
    monkeypatch.setattr(fr, "_RECOGNIZER_OVERRIDE", object())
    monkeypatch.setattr(fr, "_ENCODINGS_OVERRIDE", {"kainit": np.zeros(8)})


# ── config fields ────────────────────────────────────────────────────────────
def test_face_tolerance_default_and_env_override(monkeypatch):
    assert AgentConfig().face_recognition_tolerance == pytest.approx(0.6)
    monkeypatch.setenv("FACE_RECOGNITION_TOLERANCE", "0.45")
    assert AgentConfig().face_recognition_tolerance == pytest.approx(0.45)


def test_face_encodings_dir_default_and_env(monkeypatch, tmp_path):
    assert AgentConfig().face_encodings_dir.endswith("/.familiar_ai/face_encodings")
    monkeypatch.setenv("FACE_ENCODINGS_DIR", str(tmp_path))
    assert AgentConfig().face_encodings_dir == str(tmp_path)


def test_face_recognition_disabled_by_default():
    assert AgentConfig().face_recognition_enabled is False


# ── resolve_tom_default_person (Problem-1 整合) ───────────────────────────────
def test_face_recognition_disabled_uses_unknown_person():
    cfg = AgentConfig(face_recognition_enabled=False)
    assert cfg.resolve_tom_default_person() == "unknown_person"


def test_resolve_warns_only_when_recognizer_unavailable(caplog):
    cfg = AgentConfig(face_recognition_enabled=True)  # no recognizer available
    with caplog.at_level(logging.WARNING, logger="familiar_agent.config"):
        label = cfg.resolve_tom_default_person()
    assert label == "unknown_person"
    assert any("no face recognizer is available" in r.message for r in caplog.records)


def test_resolve_no_warn_when_recognizer_available(monkeypatch, caplog):
    _make_available(monkeypatch)
    cfg = AgentConfig(face_recognition_enabled=True)
    with caplog.at_level(logging.WARNING, logger="familiar_agent.config"):
        label = cfg.resolve_tom_default_person()
    assert label == "unknown_person"
    assert not any("face recognizer" in r.message for r in caplog.records)


def test_resolve_always_returns_label_never_companion_name(monkeypatch):
    # Problem-1 invariant: even with the flag on, never fall back to companion_name.
    monkeypatch.setenv("COMPANION_NAME", "カイニット")
    monkeypatch.setenv("TOM_DEFAULT_PERSON_LABEL", "unknown_person")
    cfg = AgentConfig(face_recognition_enabled=True)
    resolved = cfg.resolve_tom_default_person()
    assert resolved == "unknown_person"
    assert resolved != cfg.companion_name
