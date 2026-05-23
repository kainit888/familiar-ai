"""Tests for EmbodiedAgent._morning_reconstruction()."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from familiar_agent.exploration import ExplorationTracker


# ---------------------------------------------------------------------------
# Shared helper — minimal agent without __init__
# ---------------------------------------------------------------------------


def _make_agent():
    from familiar_agent.agent import EmbodiedAgent

    agent = EmbodiedAgent.__new__(EmbodiedAgent)
    agent.config = MagicMock()
    agent.config.max_tokens = 1000
    agent.config.agent_name = "Kokone"
    agent.config.companion_name = "Kouta"
    agent.config.utility_timeout_s = 180.0

    agent._turn_count = 0
    agent._session_input_tokens = 0
    agent._session_output_tokens = 0
    agent._last_context_tokens = 0
    agent._post_compact = False
    agent._started_at = 0.0
    agent.messages = []
    agent._me_md = ""
    agent._exploration = ExplorationTracker()

    backend = MagicMock()
    backend.complete = AsyncMock(return_value="")
    agent.backend = backend
    agent._utility_backend = backend

    mem = MagicMock()
    mem.recall_self_model_async = AsyncMock(return_value=[])
    mem.recall_curiosities_async = AsyncMock(return_value=[])
    mem.recent_feelings_async = AsyncMock(return_value=[])
    mem.recall_day_summaries_async = AsyncMock(return_value=[])
    mem.recall_semantic_facts_async = AsyncMock(return_value=[])
    mem.recall_behavior_policies_async = AsyncMock(return_value=[])
    mem.format_for_context = MagicMock(return_value="")
    mem.format_feelings_for_context = MagicMock(return_value="[feelings]")
    mem.format_day_summaries_for_context = MagicMock(return_value="[day_summaries]")
    mem.format_semantic_facts_for_context = MagicMock(return_value="[semantic_facts]")
    mem.format_behavior_policies_for_context = MagicMock(return_value="[behavior_policies]")
    mem.format_self_model_for_context = MagicMock(return_value="[self_model]")
    mem.format_curiosities_for_context = MagicMock(return_value="[curiosities]")
    mem.save_async = AsyncMock()
    mem.get_dates_with_observations = MagicMock(return_value=[])
    mem.get_dates_with_summaries = MagicMock(return_value=[])
    agent._memory = mem

    from familiar_agent.self_narrative import SelfNarrative

    agent._self_narrative = SelfNarrative()

    return agent


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_morning_calls_all_six_memory_methods():
    """_morning_reconstruction() must call all 6 memory async methods."""
    agent = _make_agent()

    with (
        patch("familiar_agent.agent.EmbodiedAgent._backfill_day_summaries", new=AsyncMock()),
        patch("asyncio.ensure_future"),
    ):
        await agent._morning_reconstruction()

    agent._memory.recall_self_model_async.assert_awaited_once()
    agent._memory.recall_curiosities_async.assert_awaited_once()
    agent._memory.recent_feelings_async.assert_awaited_once()
    agent._memory.recall_day_summaries_async.assert_awaited()
    agent._memory.recall_semantic_facts_async.assert_awaited_once()
    agent._memory.recall_behavior_policies_async.assert_awaited_once()


@pytest.mark.asyncio
async def test_morning_returns_no_history_when_all_empty():
    """With no memories at all, the function returns the no-history placeholder string."""
    agent = _make_agent()

    with (
        patch("familiar_agent.agent.EmbodiedAgent._backfill_day_summaries", new=AsyncMock()),
        patch("asyncio.ensure_future"),
    ):
        result = await agent._morning_reconstruction()

    # Result should be non-empty (no-history placeholder)
    assert result
    assert isinstance(result, str)


@pytest.mark.asyncio
async def test_morning_includes_memory_content_in_output():
    """When memories exist, the formatted content appears in the result."""
    agent = _make_agent()
    agent._memory.recall_day_summaries_async = AsyncMock(
        return_value=[{"summary": "yesterday was great"}]
    )

    with (
        patch("familiar_agent.agent.EmbodiedAgent._backfill_day_summaries", new=AsyncMock()),
        patch("asyncio.ensure_future"),
    ):
        result = await agent._morning_reconstruction()

    # format_day_summaries_for_context was called and its output is in the result
    agent._memory.format_day_summaries_for_context.assert_called_once()
    assert "[day_summaries]" in result


@pytest.mark.asyncio
async def test_morning_sets_curiosity_target_on_desires():
    """Surface first curiosity into desires.curiosity_target when it's None."""
    agent = _make_agent()
    agent._memory.recall_curiosities_async = AsyncMock(
        return_value=[{"summary": "black holes"}, {"summary": "language models"}]
    )

    desires = MagicMock()
    desires.curiosity_target = None

    with (
        patch("familiar_agent.agent.EmbodiedAgent._backfill_day_summaries", new=AsyncMock()),
        patch("asyncio.ensure_future"),
    ):
        await agent._morning_reconstruction(desires=desires)

    assert desires.curiosity_target == "black holes"


@pytest.mark.asyncio
async def test_morning_does_not_overwrite_existing_curiosity_target():
    """If desires.curiosity_target is already set, it must NOT be overwritten."""
    agent = _make_agent()
    agent._memory.recall_curiosities_async = AsyncMock(return_value=[{"summary": "new topic"}])

    desires = MagicMock()
    desires.curiosity_target = "existing topic"

    with (
        patch("familiar_agent.agent.EmbodiedAgent._backfill_day_summaries", new=AsyncMock()),
        patch("asyncio.ensure_future"),
    ):
        await agent._morning_reconstruction(desires=desires)

    assert desires.curiosity_target == "existing topic"


@pytest.mark.asyncio
async def test_morning_schedules_backfill_via_ensure_future():
    """_morning_reconstruction() must schedule _backfill_day_summaries via asyncio.ensure_future."""
    agent = _make_agent()

    with (
        patch("familiar_agent.agent.EmbodiedAgent._backfill_day_summaries", new=AsyncMock()),
        patch("familiar_agent.agent.asyncio.ensure_future") as mock_ensure,
    ):
        await agent._morning_reconstruction()

    mock_ensure.assert_called_once()


@pytest.mark.asyncio
async def test_morning_no_desires_arg_is_safe():
    """Passing no desires argument (None) must not raise."""
    agent = _make_agent()

    with (
        patch("familiar_agent.agent.EmbodiedAgent._backfill_day_summaries", new=AsyncMock()),
        patch("asyncio.ensure_future"),
    ):
        result = await agent._morning_reconstruction(desires=None)

    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Cycle 5: backfill skip env, utility_timeout_s, self-model lang, narrative log
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_skipped_when_env_default_on(monkeypatch, caplog):
    """Default ON: env unset => backfill returns early, observations not queried."""
    monkeypatch.delenv("FAMILIAR_SKIP_BACKFILL_ON_STARTUP", raising=False)
    agent = _make_agent()
    # Ensure utility backend != main backend so the earlier guard doesn't fire.
    agent._utility_backend = MagicMock()
    agent._utility_backend.complete = AsyncMock(return_value="")

    with caplog.at_level("INFO", logger="familiar_agent.agent"):
        await agent._backfill_day_summaries()

    agent._memory.get_dates_with_observations.assert_not_called()
    assert any("skipped" in rec.message.lower() for rec in caplog.records)


@pytest.mark.asyncio
async def test_backfill_runs_when_env_zero(monkeypatch):
    """env=0 disables the skip => backfill proceeds to query observations."""
    monkeypatch.setenv("FAMILIAR_SKIP_BACKFILL_ON_STARTUP", "0")
    agent = _make_agent()
    agent._utility_backend = MagicMock()
    agent._utility_backend.complete = AsyncMock(return_value="")

    await agent._backfill_day_summaries()

    agent._memory.get_dates_with_observations.assert_called_once()


@pytest.mark.asyncio
async def test_backfill_runs_when_env_false(monkeypatch):
    """env=false disables the skip => backfill proceeds."""
    monkeypatch.setenv("FAMILIAR_SKIP_BACKFILL_ON_STARTUP", "false")
    agent = _make_agent()
    agent._utility_backend = MagicMock()
    agent._utility_backend.complete = AsyncMock(return_value="")

    await agent._backfill_day_summaries()

    agent._memory.get_dates_with_observations.assert_called_once()


@pytest.mark.asyncio
async def test_generate_day_summary_uses_config_timeout():
    """_generate_day_summary must use AgentConfig.utility_timeout_s for asyncio.wait_for."""
    agent = _make_agent()
    agent.config.utility_timeout_s = 5.0
    agent._memory.get_observations_for_date = MagicMock(
        return_value=[{"time": "10:00", "kind": "user", "emotion": "neutral", "content": "hi"}]
    )
    agent._memory.decay_importance_async = AsyncMock()
    agent._memory_dedupe_key = MagicMock(return_value="key")
    agent._utility_backend = MagicMock()
    agent._utility_backend.complete = AsyncMock(return_value="summary text")

    captured: dict[str, float] = {}

    async def fake_wait_for(coro, timeout):
        captured["timeout"] = timeout
        return await coro

    with patch("familiar_agent.agent.asyncio.wait_for", side_effect=fake_wait_for):
        await agent._generate_day_summary("2026-05-23")

    assert captured["timeout"] == 5.0


@pytest.mark.asyncio
async def test_write_today_narrative_uses_config_timeout():
    """_write_today_narrative must use AgentConfig.utility_timeout_s for asyncio.wait_for."""
    agent = _make_agent()
    agent.config.utility_timeout_s = 7.0
    agent._turn_count = 3
    agent._decayed_mood = MagicMock(return_value=("neutral", 0.0))
    agent._memory.recall_day_summaries_async = AsyncMock(
        return_value=[{"content": "today was fine"}]
    )
    agent._memory.recall_async = AsyncMock(return_value=[])
    agent._utility_backend = MagicMock()
    agent._utility_backend.complete = AsyncMock(return_value="narrative line")

    captured: dict[str, float] = {}

    async def fake_wait_for(coro, timeout):
        captured["timeout"] = timeout
        return await coro

    with patch("familiar_agent.agent.asyncio.wait_for", side_effect=fake_wait_for):
        await agent._write_today_narrative()

    assert captured["timeout"] == 7.0


def test_self_model_prompt_includes_lang():
    """_SELF_MODEL_PROMPT must have a {lang} placeholder that interpolates."""
    from familiar_agent.agent import _SELF_MODEL_PROMPT

    rendered = _SELF_MODEL_PROMPT.format(text="x", lang="日本語")
    assert "日本語" in rendered


@pytest.mark.asyncio
async def test_update_self_model_passes_lang():
    """_update_self_model must include the localized summary_lang in its prompt."""
    from familiar_agent.agent import _t

    agent = _make_agent()
    agent._memory_dedupe_key = MagicMock(return_value="key")
    agent._utility_backend = MagicMock()
    agent._utility_backend.complete = AsyncMock(return_value="insight sentence")

    await agent._update_self_model("today felt strange", emotion="moved")

    agent._utility_backend.complete.assert_awaited_once()
    sent_prompt = agent._utility_backend.complete.await_args.args[0]
    assert _t("summary_lang") in sent_prompt


@pytest.mark.asyncio
async def test_self_narrative_timeout_logs_explicitly(caplog):
    """asyncio.TimeoutError in _maybe_update_self_narrative must log the explicit timeout message."""
    import asyncio as _asyncio

    agent = _make_agent()
    agent._SALIENT_NARRATIVE_EMOTIONS = {"moved"}
    agent._decayed_mood = MagicMock(return_value=("neutral", 0.0))
    agent._prediction = MagicMock()
    agent._prediction.last_signal = MagicMock(return_value=None)

    async def raise_timeout(*_args, **_kwargs):
        raise _asyncio.TimeoutError()

    with (
        patch("familiar_agent.agent.asyncio.wait_for", side_effect=raise_timeout),
        caplog.at_level("WARNING", logger="familiar_agent.agent"),
    ):
        await agent._maybe_update_self_narrative(
            user_input="hi",
            final_text="response",
            emotion="moved",
            is_desire_turn=False,
        )

    assert any("timeout after 12.0s" in rec.message for rec in caplog.records)
