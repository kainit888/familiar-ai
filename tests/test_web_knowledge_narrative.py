"""Phase H: web_knowledge → self_narrative 統合テスト (全 mock; 実 LLM/API なし)。

mutation 対応 (必須4):
    - 統合関数を no-op → test_integration_writes_web_knowledge_entry が fail
    - locale 文面 (self_narrative_web_prompt) を空 → test_web_prompt_locale_is_permissive が fail
    - judgment 経路を bypass (空応答でも write+mark) → test_empty_completion_writes_nothing が fail
    - graceful try/except を無効化 → test_backend_error_is_graceful が fail
"""

from __future__ import annotations

import pytest

from familiar_agent.agent import EmbodiedAgent
from familiar_agent.config import AgentConfig
from familiar_agent.self_narrative import SelfNarrative
from familiar_agent.tools.memory import ObservationMemory
from familiar_agent.tools.web_search import Source, WebKnowledge
from familiar_agent.web_knowledge_ledger import WebKnowledgeLedger


# ── fakes ────────────────────────────────────────────────────────────────────
class FakeBackend:
    def __init__(self, reply="今日は犬の品種について調べた。", *, delay=0.0, raises=None):
        self._reply = reply
        self._delay = delay
        self._raises = raises
        self.calls: list[str] = []

    async def complete(self, prompt, max_tokens=120, **kw):
        self.calls.append(prompt)
        if self._delay:
            import asyncio

            await asyncio.sleep(self._delay)
        if self._raises is not None:
            raise self._raises
        return self._reply


def _seed_web_knowledge(mem, query="dog breeds", summary="Dogs have 400+ breeds."):
    wk = WebKnowledge(query, summary, [Source("AKC", "https://vertexaisearch.example/x")])
    mem.save(content=wk.to_json(), kind="web_knowledge")
    return query


def _agent(tmp_path, *, reply="今日は犬の品種について調べた。", delay=0.0, raises=None, timeout=5.0):
    agent = EmbodiedAgent.__new__(EmbodiedAgent)
    agent._memory = ObservationMemory(db_path=str(tmp_path / "obs.db"))
    agent._utility_backend = FakeBackend(reply=reply, delay=delay, raises=raises)
    agent._self_narrative = SelfNarrative(path=tmp_path / "sn.jsonl")
    agent._web_knowledge_ledger = WebKnowledgeLedger(path=tmp_path / "ledger.json")
    agent._turn_count = 1
    agent.config = AgentConfig(utility_timeout_s=timeout)
    agent._decayed_mood = lambda: ("curious", 0.5)
    return agent


# ── Group 1: ledger / dedup ──────────────────────────────────────────────────
def test_ledger_fresh_returns_not_seen(tmp_path):
    assert WebKnowledgeLedger(path=tmp_path / "l.json").seen("q") is False


def test_ledger_mark_then_seen(tmp_path):
    led = WebKnowledgeLedger(path=tmp_path / "l.json")
    led.mark("q")
    assert led.seen("q") is True


def test_ledger_persists_across_instances(tmp_path):
    p = tmp_path / "l.json"
    WebKnowledgeLedger(path=p).mark("dog breeds")
    assert WebKnowledgeLedger(path=p).seen("dog breeds") is True


def test_ledger_missing_file_does_not_crash(tmp_path):
    led = WebKnowledgeLedger(path=tmp_path / "nope" / "deep" / "l.json")
    assert led.seen("q") is False  # no raise


# ── Group 2: integration writes a narrative entry ─────────────────────────────
@pytest.mark.asyncio
async def test_integration_writes_web_knowledge_entry(tmp_path):
    agent = _agent(tmp_path, reply="今日は犬の品種を学んだ。")
    _seed_web_knowledge(agent._memory)
    await agent._maybe_integrate_web_knowledge()
    entries = agent._self_narrative.read_recent(5)
    assert len(entries) == 1
    assert entries[-1].trigger == "web_knowledge"
    assert entries[-1].text == "今日は犬の品種を学んだ。"


@pytest.mark.asyncio
async def test_integration_uses_decayed_mood(tmp_path):
    agent = _agent(tmp_path)
    _seed_web_knowledge(agent._memory)
    await agent._maybe_integrate_web_knowledge()
    assert agent._self_narrative.read_recent(1)[-1].mood == "curious"


@pytest.mark.asyncio
async def test_integration_marks_query_in_ledger(tmp_path):
    agent = _agent(tmp_path)
    q = _seed_web_knowledge(agent._memory, query="cat facts")
    await agent._maybe_integrate_web_knowledge()
    assert agent._web_knowledge_ledger.seen(q) is True


@pytest.mark.asyncio
async def test_zero_web_knowledge_is_noop(tmp_path):
    agent = _agent(tmp_path)  # no web_knowledge seeded
    await agent._maybe_integrate_web_knowledge()
    assert agent._self_narrative.read_recent(5) == []
    assert agent._utility_backend.calls == []  # backend never invoked


# ── Group 3: autonomy + dedup behavior ───────────────────────────────────────
@pytest.mark.asyncio
async def test_empty_completion_writes_nothing(tmp_path):
    agent = _agent(tmp_path, reply="")  # Pico declines
    q = _seed_web_knowledge(agent._memory, query="empty case")
    await agent._maybe_integrate_web_knowledge()
    assert agent._self_narrative.read_recent(5) == []
    # row stays eligible next session — ledger NOT marked (autonomy preserved)
    assert agent._web_knowledge_ledger.seen(q) is False


@pytest.mark.asyncio
async def test_already_integrated_query_skipped(tmp_path):
    agent = _agent(tmp_path)
    q = _seed_web_knowledge(agent._memory, query="seen already")
    agent._web_knowledge_ledger.mark(q)  # pretend already folded in
    await agent._maybe_integrate_web_knowledge()
    assert agent._self_narrative.read_recent(5) == []  # not re-folded
    assert agent._utility_backend.calls == []


@pytest.mark.asyncio
async def test_backend_timeout_is_graceful(tmp_path):
    agent = _agent(tmp_path, delay=0.3, timeout=0.05)
    q = _seed_web_knowledge(agent._memory, query="slow case")
    await agent._maybe_integrate_web_knowledge()  # must not raise
    assert agent._self_narrative.read_recent(5) == []
    assert agent._web_knowledge_ledger.seen(q) is False


@pytest.mark.asyncio
async def test_backend_error_is_graceful(tmp_path):
    agent = _agent(tmp_path, raises=RuntimeError("backend boom"))
    q = _seed_web_knowledge(agent._memory, query="error case")
    await agent._maybe_integrate_web_knowledge()  # must not raise
    assert agent._self_narrative.read_recent(5) == []
    assert agent._web_knowledge_ledger.seen(q) is False


@pytest.mark.asyncio
async def test_no_conversation_turn_is_noop(tmp_path):
    agent = _agent(tmp_path)
    agent._turn_count = 0  # session with no conversation
    _seed_web_knowledge(agent._memory)
    await agent._maybe_integrate_web_knowledge()
    assert agent._self_narrative.read_recent(5) == []


# ── Group 4: locale parity + voice ───────────────────────────────────────────
def test_web_prompt_locale_is_permissive(monkeypatch):
    from familiar_agent._i18n import _t

    monkeypatch.setattr("familiar_agent._i18n._LANG", "en")
    en = _t("self_narrative_web_prompt", item="x")
    assert "I" in en and ("don't have to" in en or "leave it" in en)

    monkeypatch.setattr("familiar_agent._i18n._LANG", "ja")
    ja = _t("self_narrative_web_prompt", item="x")
    assert "私" in ja and ("無理に" in ja or "構わない" in ja)


def test_locale_keys_have_ja_en_parity():
    import json
    from pathlib import Path

    base = Path("src/familiar_agent/locales")
    en = json.loads((base / "en.json").read_text(encoding="utf-8"))
    ja = json.loads((base / "ja.json").read_text(encoding="utf-8"))
    for key in ("self_narrative_web_prompt", "self_narrative_web_share_line"):
        assert key in en and key in ja


@pytest.mark.asyncio
async def test_existing_self_narrative_entries_coexist(tmp_path):
    agent = _agent(tmp_path, reply="今日は鳥について学んだ。")
    # a pre-existing diary entry from another trigger
    agent._self_narrative.write("今日はカイニットと話した。", mood="happy", trigger="session_close")
    _seed_web_knowledge(agent._memory, query="bird facts")
    await agent._maybe_integrate_web_knowledge()
    entries = agent._self_narrative.read_recent(5)
    triggers = [e.trigger for e in entries]
    assert "session_close" in triggers and "web_knowledge" in triggers
