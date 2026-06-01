"""Phase G: identity injection into the see() result (agent.py side).

mutation 対応 (必須4 のうち本ファイルが担う 1):
    - confidence/identity の prompt 注入を skip (append しない) → test_confidence_passed_to_prompt
      / test_see_result_appended_with_likeness_when_match が fail

agent の重い run ループは回さず、Phase G が追加した
``EmbodiedAgent._maybe_annotate_identity`` (gate) と ``_annotate_identity`` を直接叩く。
"""

from __future__ import annotations

import pytest

from familiar_agent.agent import EmbodiedAgent
from familiar_agent.config import AgentConfig
from pico_agent.adapters import face_recognition as fr


class _FakeMatch:
    def __init__(self, name="kainit", confidence=0.82):
        self.name = name
        self.confidence = confidence


@pytest.fixture(autouse=True)
def _en_lang(monkeypatch):
    # deterministic locale for substring assertions
    monkeypatch.setattr("familiar_agent._i18n._LANG", "en")
    monkeypatch.setattr(fr, "_RECOGNIZER_OVERRIDE", None)
    monkeypatch.setattr(fr, "_ENCODINGS_OVERRIDE", None)
    yield


def _agent(*, enabled: bool) -> EmbodiedAgent:
    agent = EmbodiedAgent.__new__(EmbodiedAgent)
    agent.config = AgentConfig(face_recognition_enabled=enabled)
    return agent


def test_see_result_appended_with_likeness_when_match(monkeypatch):
    monkeypatch.setattr(fr, "recognize_b64", lambda *a, **k: _FakeMatch(confidence=0.82))
    agent = _agent(enabled=True)
    out = agent._maybe_annotate_identity("see", "You see the current view.", "B64IMG")
    assert "You see the current view." in out
    assert "kainit" in out
    assert "Face recognition" in out


def test_confidence_passed_to_prompt(monkeypatch):
    monkeypatch.setattr(fr, "recognize_b64", lambda *a, **k: _FakeMatch(confidence=0.82))
    agent = _agent(enabled=True)
    out = agent._maybe_annotate_identity("see", "view", "B64IMG")
    assert "0.82" in out  # confidence reaches the prompt verbatim


def test_see_result_unchanged_when_flag_off(monkeypatch):
    # would match, but the flag gates it off → byte-identical (Problem-1 invariant)
    monkeypatch.setattr(fr, "recognize_b64", lambda *a, **k: _FakeMatch())
    agent = _agent(enabled=False)
    base = "You see the current view."
    assert agent._maybe_annotate_identity("see", base, "B64IMG") == base


def test_see_result_unchanged_when_no_match(monkeypatch):
    monkeypatch.setattr(fr, "recognize_b64", lambda *a, **k: None)
    agent = _agent(enabled=True)
    base = "You see the current view."
    assert agent._maybe_annotate_identity("see", base, "B64IMG") == base


def test_non_see_tool_never_triggers_recognition(monkeypatch):
    called = {"n": 0}

    def _spy(*a, **k):
        called["n"] += 1
        return _FakeMatch()

    monkeypatch.setattr(fr, "recognize_b64", _spy)
    agent = _agent(enabled=True)
    base = "walked left"
    assert agent._maybe_annotate_identity("walk", base, "B64IMG") == base
    assert called["n"] == 0


def test_likeness_note_is_non_asserting(monkeypatch):
    monkeypatch.setattr(fr, "recognize_b64", lambda *a, **k: _FakeMatch())
    agent = _agent(enabled=True)
    out = agent._maybe_annotate_identity("see", "view", "B64IMG").lower()
    # autonomy guard: must hedge, never hard-assert identity
    assert "judge their identity yourself" in out or "do not hard-assert" in out


def test_annotation_graceful_when_adapter_raises(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(fr, "recognize_b64", _boom)
    agent = _agent(enabled=True)
    base = "You see the current view."
    # any adapter failure must not break vision
    assert agent._maybe_annotate_identity("see", base, "B64IMG") == base


def test_vision_event_includes_identity_metadata(monkeypatch):
    # integration-ish: a match yields both name and confidence in the see text
    monkeypatch.setattr(fr, "recognize_b64", lambda *a, **k: _FakeMatch(confidence=0.91))
    agent = _agent(enabled=True)
    out = agent._maybe_annotate_identity("see", "You see the current view.", "B64IMG")
    assert "kainit" in out and "0.91" in out
