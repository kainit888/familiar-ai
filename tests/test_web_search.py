"""Phase F: web search + curiosity + heartbeat-share tests (all mock; no real API).

mutation 対応 (必須4):
    - search_grounding を no-op → test_search_returns_summary が fail
    - web_knowledge 保存を skip → test_call_saves_and_returns_saved / 保存系が fail
    - curiosity prompt の locale 化を戻す → test_curiosity_drivespec_uses_locale が fail
    - heartbeat の web_knowledge 注入を消す → test_heartbeat_includes_recent_web_knowledge が fail
"""

from __future__ import annotations

import inspect
import time

import pytest

from familiar_agent.tools import web_search as ws
from familiar_agent.tools.web_search import Source, WebKnowledge, WebSearchTool, search_grounding


# ── Gemini grounding response fakes (verified structure) ────────────────────────
class _FakeWeb:
    def __init__(self, title, uri):
        self.title = title
        self.uri = uri


class _FakeChunk:
    def __init__(self, title, uri):
        self.web = _FakeWeb(title, uri)


class _FakeMeta:
    def __init__(self):
        self.web_search_queries = ["tokyo weather"]
        self.grounding_chunks = [
            _FakeChunk(
                "Weather JP",
                "https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc",
            )
        ]
        self.grounding_supports = []


class _FakeCand:
    def __init__(self):
        self.grounding_metadata = _FakeMeta()


class FakeGroundingResponse:
    def __init__(self, text="Tokyo is sunny today."):
        self.text = text
        self.candidates = [_FakeCand()]


class _FakeModels:
    def __init__(self, resp, recorder, raises):
        self._resp = resp
        self._recorder = recorder
        self._raises = raises

    def generate_content(self, *, model, contents, config=None):
        self._recorder["model"] = model
        self._recorder["contents"] = contents
        if self._raises is not None:
            raise self._raises
        return self._resp


class FakeClient:
    def __init__(self, resp=None, recorder=None, raises=None):
        self.models = _FakeModels(
            resp if resp is not None else FakeGroundingResponse(),
            recorder if recorder is not None else {},
            raises,
        )


def _mem(tmp_path):
    from familiar_agent.tools.memory import ObservationMemory

    return ObservationMemory(db_path=str(tmp_path / "obs.db"))


# ── Group 1: web_search tool & grounding ────────────────────────────────────────


def test_search_returns_summary():
    wk = search_grounding("tokyo weather", client=FakeClient())
    assert wk is not None
    assert wk.summary == "Tokyo is sunny today."


def test_search_returns_sources_title_uri():
    wk = search_grounding("tokyo weather", client=FakeClient())
    assert wk.sources[0].title == "Weather JP"
    assert wk.sources[0].uri.startswith("https://vertexaisearch.cloud.google.com")


def test_uses_grounding_model_name(monkeypatch):
    monkeypatch.delenv("GROUNDING_MODEL", raising=False)
    rec: dict = {}
    search_grounding("q", client=FakeClient(recorder=rec))
    assert rec["model"] == "gemini-3.1-flash-lite"


def test_api_error_silent_none():
    wk = search_grounding("q", client=FakeClient(raises=RuntimeError("boom")))
    assert wk is None  # no raise


def test_quota_error_graceful(caplog):
    wk = search_grounding("q", client=FakeClient(raises=Exception("RESOURCE_EXHAUSTED quota")))
    assert wk is None


@pytest.mark.asyncio
async def test_call_saves_and_returns_saved(tmp_path):
    mem = _mem(tmp_path)
    tool = WebSearchTool(mem, client=FakeClient())
    result, img = await tool.call("search_web", {"query": "tokyo weather"})
    assert img is None
    assert "(saved)" in result
    assert "Tokyo is sunny" in result
    assert mem.recall_web_knowledge(5)  # persisted


# ── Group 2: web_knowledge storage ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_saved_kind_web_knowledge(tmp_path):
    mem = _mem(tmp_path)
    await WebSearchTool(mem, client=FakeClient()).call("search_web", {"query": "x"})
    rows = mem.recall_web_knowledge(5)
    assert len(rows) == 1
    assert WebKnowledge.from_row(rows[0]["content"]) is not None


def test_content_json_roundtrip():
    wk = WebKnowledge("q", "summary text", [Source("T", "U")])
    back = WebKnowledge.from_row(wk.to_json())
    assert back.summary == "summary text"
    assert back.sources[0].title == "T" and back.sources[0].uri == "U"


@pytest.mark.asyncio
async def test_recall_by_kind(tmp_path):
    mem = _mem(tmp_path)
    mem.save(content="just an observation", kind="observation")
    await WebSearchTool(mem, client=FakeClient()).call("search_web", {"query": "x"})
    rows = mem.recall_web_knowledge(5)
    assert len(rows) == 1  # only the web_knowledge row, not the observation


@pytest.mark.asyncio
async def test_recent_web_knowledge_parsed(tmp_path):
    mem = _mem(tmp_path)
    await WebSearchTool(mem, client=FakeClient()).call("search_web", {"query": "x"})
    items = ws.recent_web_knowledge(mem, 3)
    assert items and items[0].sources[0].uri.startswith("https://vertexaisearch")


def test_persist_across_fresh_memory(tmp_path):
    from familiar_agent.tools.memory import ObservationMemory

    p = str(tmp_path / "obs.db")
    m1 = ObservationMemory(db_path=p)
    m1.save(content=WebKnowledge("q", "persisted", [Source("T", "U")]).to_json(), kind="web_knowledge")
    m2 = ObservationMemory(db_path=p)
    items = ws.recent_web_knowledge(m2, 3)
    assert items and items[0].summary == "persisted"


# ── Group 3: curiosity judgment ─────────────────────────────────────────────────


def test_curiosity_prompt_invites_optional_search(monkeypatch):
    monkeypatch.setattr("familiar_agent._i18n._LANG", "en")
    from familiar_agent._i18n import _t

    p = _t("desire_prompt_curiosity")
    assert "search_web" in p
    assert "don't have to" in p or "fine" in p  # permissive


def test_curiosity_prompt_three_sources(monkeypatch):
    monkeypatch.setattr("familiar_agent._i18n._LANG", "en")
    from familiar_agent._i18n import _t

    p = _t("desire_prompt_curiosity").lower()
    assert "saw" in p  # vision
    assert "recall" in p or "past interest" in p  # memory
    assert "wondering" in p or "your own" in p  # explicit/own


def test_curiosity_drivespec_uses_locale(tmp_path):
    from familiar_agent._i18n import _t
    from familiar_agent.desires import DesireSystem

    ds = DesireSystem(state_path=tmp_path / "d.json", disabled_drives=frozenset())
    spec = ds._drive_specs["curiosity"]
    assert spec.prompt_text == _t("desire_prompt_curiosity")
    assert "Internal impulse: investigate" not in spec.prompt_text  # old inline string gone


@pytest.mark.asyncio
async def test_curiosity_firing_does_not_force_search(tmp_path):
    from familiar_agent.desires import DesireSystem

    ds = DesireSystem(state_path=tmp_path / "d.json", disabled_drives=frozenset())
    ds._desires["curiosity"] = 1.0
    prompt = ds.dominant_as_prompt()  # just text; no search executed
    assert prompt is not None
    mem = _mem(tmp_path)
    assert mem.recall_web_knowledge(5) == []  # firing alone saved no web_knowledge


@pytest.mark.asyncio
async def test_pico_can_choose_search(tmp_path):
    # Pico's judgment to call the tool → search runs → web_knowledge saved
    mem = _mem(tmp_path)
    await WebSearchTool(mem, client=FakeClient()).call("search_web", {"query": "dog breeds"})
    assert ws.recent_web_knowledge(mem, 3)


# ── Group 4: heartbeat integration ──────────────────────────────────────────────


def _save_wk(mem, title="AKC Dog Breeds", uri="https://vertexaisearch.cloud.google.com/x"):
    mem.save(
        content=WebKnowledge("dogs", "Dogs have 400+ breeds.", [Source(title, uri)]).to_json(),
        kind="web_knowledge",
    )


def _hb(mem):
    from familiar_agent._ui_helpers import heartbeat_tick_prompt

    now = time.time()
    return heartbeat_tick_prompt(0.95, now - 3600, now, memory=mem)


def test_heartbeat_includes_recent_web_knowledge(tmp_path):
    mem = _mem(tmp_path)
    _save_wk(mem)
    out = _hb(mem)
    assert out is not None
    assert "最近調べたこと" in out or "Recently you looked up" in out
    assert "AKC Dog Breeds" in out


def test_heartbeat_share_block_permissive(tmp_path):
    mem = _mem(tmp_path)
    _save_wk(mem)
    out = _hb(mem)
    low = out.lower()
    assert "may share" in low or "気が向けば" in out or "黙っていても" in out
    assert "must share" not in low and "share now" not in low


def test_heartbeat_no_share_when_empty(tmp_path):
    from familiar_agent._i18n import _t

    mem = _mem(tmp_path)  # no web_knowledge
    out = _hb(mem)
    assert out == _t("heartbeat_prompt")  # base prompt only


def test_heartbeat_share_format_title_uri(tmp_path):
    mem = _mem(tmp_path)
    _save_wk(mem, title="MyTitle", uri="https://vertexaisearch.cloud.google.com/zzz")
    out = _hb(mem)
    assert "MyTitle" in out
    assert "https://vertexaisearch.cloud.google.com/zzz" in out


def test_heartbeat_no_discord_path():
    from familiar_agent import _ui_helpers

    src = inspect.getsource(_ui_helpers._web_share_block) + inspect.getsource(
        _ui_helpers.heartbeat_tick_prompt
    )
    assert "discord" not in src.lower()
    assert "say" not in src.lower()  # no immediate-speech / network call
