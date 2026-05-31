"""Tests for the EmbodiedAgent ReAct loop (run() method)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from familiar_agent.backend import ToolCall, TurnResult
from familiar_agent.desires import DesireSystem
from familiar_agent.exploration import ExplorationTracker


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _turn(stop: str, text: str = "", tool_calls: list | None = None) -> TurnResult:
    return TurnResult(
        stop_reason=stop,
        text=text,
        tool_calls=tool_calls or [],
        input_tokens=100,
        output_tokens=50,
    )


def _make_agent(*, with_tts: bool = False, with_camera: bool = False, with_mcp: bool = False):
    """Minimal EmbodiedAgent with all heavy dependencies mocked out."""
    from familiar_agent.agent import EmbodiedAgent

    agent = EmbodiedAgent.__new__(EmbodiedAgent)
    agent.config = MagicMock()
    agent.config.max_tokens = 1000
    agent.config.agent_name = "Kokone"
    agent.config.companion_name = "Kouta"

    agent._turn_count = 0
    agent._session_input_tokens = 0
    agent._session_output_tokens = 0
    agent._last_context_tokens = 0
    agent._post_compact = False
    agent._background_tasks = set()
    agent._cached_plan_ctx = ""
    agent._cached_workspace_ctx = ""
    agent._cached_temporal_ctx = None
    agent._cached_companion_mood = "engaged"
    agent._started_at = 0.0
    agent.messages = []
    agent._me_md = ""

    # Backend: make_tool_results must accept (tool_calls, results) and return a list
    backend = MagicMock()
    backend.complete = AsyncMock(return_value="")
    backend.make_user_message = lambda t: {"role": "user", "content": t}
    backend.make_assistant_message = lambda result, raw: {
        "role": "assistant",
        "content": result.text,
    }
    backend.make_tool_results = MagicMock(return_value=[{"role": "tool", "content": "ok"}])
    agent.backend = backend
    agent._utility_backend = backend

    # Memory
    mem = MagicMock()
    mem.is_embedding_ready = MagicMock(return_value=True)
    mem.recall_async = AsyncMock(return_value=[])
    mem.recent_feelings_async = AsyncMock(return_value=[])
    mem.recall_self_model_async = AsyncMock(return_value=[])
    mem.recall_curiosities_async = AsyncMock(return_value=[])
    mem.recall_day_summaries_async = AsyncMock(return_value=[])
    mem.recall_semantic_facts_async = AsyncMock(return_value=[])
    mem.recall_behavior_policies_async = AsyncMock(return_value=[])
    mem.format_for_context = MagicMock(return_value="")
    mem.format_feelings_for_context = MagicMock(return_value="")
    mem.format_day_summaries_for_context = MagicMock(return_value="")
    mem.format_semantic_facts_for_context = MagicMock(return_value="")
    mem.format_behavior_policies_for_context = MagicMock(return_value="")
    mem.format_self_model_for_context = MagicMock(return_value="")
    mem.format_curiosities_for_context = MagicMock(return_value="")
    mem.save_async = AsyncMock()
    mem.adjust_semantic_fact_confidence_async = AsyncMock(return_value=None)
    mem.adjust_behavior_policy_confidence_async = AsyncMock(return_value=None)
    mem.get_dates_with_observations = MagicMock(return_value=[])
    mem.get_dates_with_summaries = MagicMock(return_value=[])
    mem.as_coalition_async = AsyncMock(return_value=None)
    agent._memory = mem

    mem_tool = MagicMock()
    mem_tool.get_tool_definitions = MagicMock(return_value=[])
    mem_tool.call = AsyncMock(return_value=("remembered", None))
    agent._memory_tool = mem_tool

    tom = MagicMock()
    tom.get_tool_definitions = MagicMock(return_value=[])
    tom.call = AsyncMock(return_value=("tom result", None))
    agent._tom_tool = tom

    coding = MagicMock()
    coding.get_tool_definitions = MagicMock(return_value=[])
    coding.call = AsyncMock(return_value=("code result", None))
    agent._coding = coding

    agent._camera = None
    agent._mobility = None
    agent._mcp = None

    if with_tts:
        tts_tool = MagicMock()
        tts_tool.get_tool_definitions = MagicMock(return_value=[{"name": "say"}])
        tts_tool.call = AsyncMock(return_value=("spoken", None))
        agent._tts = tts_tool
    else:
        agent._tts = None

    if with_camera:
        cam = MagicMock()
        cam.get_tool_definitions = MagicMock(return_value=[{"name": "see"}, {"name": "look"}])
        cam.call = AsyncMock(return_value=("I see a room", "base64img"))
        agent._camera = cam

    if with_mcp:
        mcp_client = MagicMock()
        mcp_client.get_tool_definitions = MagicMock(return_value=[])
        mcp_client.call = AsyncMock(return_value=("mcp result", None))
        mcp_client.is_started = True
        agent._mcp = mcp_client

    agent._exploration = ExplorationTracker()
    agent._scene = None

    from familiar_agent.self_narrative import SelfNarrative
    from familiar_agent.relationship import RelationshipTracker
    from familiar_agent.workspace import GlobalWorkspace
    from familiar_agent.prediction import PredictionEngine
    from familiar_agent.attention_schema import AttentionSchema
    import time as _time

    agent._self_narrative = SelfNarrative()
    agent._relationship = RelationshipTracker()
    agent._self_state = MagicMock()
    agent._self_state.snapshot = MagicMock(return_value={"unresolved_tension": 0.2})
    agent._workspace = GlobalWorkspace()
    agent._prediction = PredictionEngine()
    agent._attention_schema = AttentionSchema()
    agent._dmn = MagicMock()
    agent._dmn.wander = AsyncMock(return_value=None)
    agent._meta_monitor = MagicMock()
    agent._meta_monitor.as_coalition = MagicMock(return_value=None)
    agent._meta_monitor.record_step = MagicMock()
    agent._memory_worker = MagicMock()
    agent._memory_worker.is_running = True
    agent._mood = "neutral"
    agent._mood_intensity = 0.0
    agent._mood_set_at = _time.time()

    return agent


# Patches that suppress heavy async sub-calls in run()
_HEAVY_PATCHES = {
    "familiar_agent.agent.EmbodiedAgent._morning_reconstruction": AsyncMock(return_value=""),
    "familiar_agent.agent.EmbodiedAgent._infer_companion_mood": AsyncMock(return_value="engaged"),
    "familiar_agent.agent.EmbodiedAgent._infer_emotion": AsyncMock(return_value="neutral"),
    "familiar_agent.agent.EmbodiedAgent._summarize_exchange": AsyncMock(return_value="summary"),
    "familiar_agent.agent.EmbodiedAgent._online_temporal_context": AsyncMock(return_value=None),
    "familiar_agent.agent.EmbodiedAgent._run_post_response_pipeline": AsyncMock(),
    "familiar_agent.agent.EmbodiedAgent._update_self_model": AsyncMock(),
    "familiar_agent.agent.EmbodiedAgent._maybe_update_self_narrative": AsyncMock(),
    "familiar_agent.agent.EmbodiedAgent._maybe_adapt_values": AsyncMock(),
    "familiar_agent.agent.EmbodiedAgent.extract_curiosity": AsyncMock(return_value=None),
    "familiar_agent.agent.generate_plan": AsyncMock(return_value=""),
    "familiar_agent.agent.check_plan_blocked": AsyncMock(return_value=False),
}


def _patch_heavy(extra: dict | None = None):
    """Apply all heavy patches; returns a list of patch objects (must be started/stopped by caller)."""
    patches = dict(_HEAVY_PATCHES)
    if extra:
        patches.update(extra)
    return [patch(target, new) for target, new in patches.items()]


# ---------------------------------------------------------------------------
# Tests: basic single-turn end_turn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_end_turn_returns_text():
    """run() with immediate end_turn returns the model's text."""
    agent = _make_agent()
    agent.backend.stream_turn = AsyncMock(return_value=(_turn("end_turn", text="Hello!"), "Hello!"))

    ps = _patch_heavy()
    for p in ps:
        p.start()
    try:
        result = await agent.run("こんにちは")
    finally:
        for p in ps:
            p.stop()

    assert result == "Hello!"


@pytest.mark.asyncio
async def test_brief_greeting_turn_uses_only_say_and_skips_heavy_prep():
    agent = _make_agent(with_tts=True, with_camera=True)
    agent.backend.stream_turn = AsyncMock(
        return_value=(_turn("end_turn", text="おはよう。"), "おはよう。")
    )

    morning_mock = AsyncMock(return_value="morning context")
    companion_mood_mock = AsyncMock(return_value="engaged")
    workspace_mock = AsyncMock(return_value="[workspace]")
    patches = dict(_HEAVY_PATCHES)
    patches["familiar_agent.agent.EmbodiedAgent._morning_reconstruction"] = morning_mock
    patches["familiar_agent.agent.EmbodiedAgent._infer_companion_mood"] = companion_mood_mock
    patches["familiar_agent.agent.EmbodiedAgent._gather_workspace_context"] = workspace_mock

    ps = [patch(t, n) for t, n in patches.items()]
    for p in ps:
        p.start()
    try:
        result = await agent.run("おはよう")
    finally:
        for p in ps:
            p.stop()

    assert result == "おはよう。"
    stream_kwargs = agent.backend.stream_turn.await_args.kwargs
    assert stream_kwargs["tools"] == [{"name": "say"}]
    assert stream_kwargs["max_tokens"] == 120
    morning_mock.assert_not_awaited()
    companion_mood_mock.assert_not_awaited()
    workspace_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_increments_turn_count():
    """run() increments _turn_count on each invocation."""
    agent = _make_agent()
    agent.backend.stream_turn = AsyncMock(return_value=(_turn("end_turn", text="Hi"), "Hi"))

    ps = _patch_heavy()
    for p in ps:
        p.start()
    try:
        assert agent._turn_count == 0
        await agent.run("test")
        assert agent._turn_count == 1
        await agent.run("test2")
        assert agent._turn_count == 2
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_run_appends_user_message_to_history():
    """run() appends the user message to agent.messages."""
    agent = _make_agent()
    agent.backend.stream_turn = AsyncMock(
        return_value=(_turn("end_turn", text="response"), "response")
    )

    ps = _patch_heavy()
    for p in ps:
        p.start()
    try:
        assert len(agent.messages) == 0
        await agent.run("hello from user")
        # At minimum, a user message was added
        assert any(m.get("role") == "user" for m in agent.messages)
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_repeated_tool_failure_raises_self_protect_without_irritable_tone(tmp_path):
    agent = _make_agent()
    agent.backend.stream_turn = AsyncMock(
        return_value=(_turn("end_turn", text="落ち着いて進めよう。"), "落ち着いて進めよう。")
    )
    agent._tool_failure_streak = 3
    desires = DesireSystem(state_path=tmp_path / "desires.json", companion_name="Kota")

    ps = _patch_heavy()
    for p in ps:
        p.start()
    try:
        result = await agent.run("助けて", desires=desires)
    finally:
        for p in ps:
            p.stop()

    assert desires.level("self_protect") > 0.0
    assert "ugh" not in result.lower()
    assert "annoy" not in result.lower()


@pytest.mark.asyncio
async def test_existing_no_hardware_mode_still_works_with_mental_pipeline():
    agent = _make_agent(with_tts=False, with_camera=False, with_mcp=False)
    agent.backend.stream_turn = AsyncMock(
        return_value=(_turn("end_turn", text="hardwareなしでも動く"), "hardwareなしでも動く")
    )

    ps = _patch_heavy()
    for p in ps:
        p.start()
    try:
        result = await agent.run("こんにちは")
    finally:
        for p in ps:
            p.stop()

    assert result == "hardwareなしでも動く"


@pytest.mark.asyncio
async def test_run_accumulates_tokens():
    """run() adds input/output tokens to session totals."""
    agent = _make_agent()
    agent.backend.stream_turn = AsyncMock(
        return_value=(
            _turn(
                "end_turn",
                text="ok",
            ),
            "ok",
        )
    )
    # Override to set token counts
    result_obj = TurnResult(stop_reason="end_turn", text="ok", input_tokens=200, output_tokens=80)
    agent.backend.stream_turn = AsyncMock(return_value=(result_obj, "ok"))

    ps = _patch_heavy()
    for p in ps:
        p.start()
    try:
        await agent.run("test")
    finally:
        for p in ps:
            p.stop()

    assert agent._session_input_tokens == 200
    assert agent._session_output_tokens == 80


# ---------------------------------------------------------------------------
# Tests: tool_use → end_turn sequence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_tool_use_then_end_turn():
    """run() executes a tool call then gets end_turn on the next iteration."""
    agent = _make_agent()

    tc = ToolCall(id="tc1", name="remember", input={"content": "test memory"})
    turn1 = TurnResult(stop_reason="tool_use", text="", tool_calls=[tc])
    turn2 = TurnResult(stop_reason="end_turn", text="Done!", tool_calls=[])

    agent.backend.stream_turn = AsyncMock(
        side_effect=[
            (turn1, None),
            (turn2, "Done!"),
        ]
    )

    ps = _patch_heavy()
    for p in ps:
        p.start()
    try:
        result = await agent.run("remember something")
    finally:
        for p in ps:
            p.stop()

    assert result == "Done!"
    assert agent._memory_tool.call.called


@pytest.mark.asyncio
async def test_run_tool_results_added_to_messages():
    """Tool results are added to message history after tool execution."""
    agent = _make_agent()

    tc = ToolCall(id="tc1", name="remember", input={"content": "hi"})
    turn1 = TurnResult(stop_reason="tool_use", text="", tool_calls=[tc])
    turn2 = TurnResult(stop_reason="end_turn", text="Saved.", tool_calls=[])

    agent.backend.stream_turn = AsyncMock(
        side_effect=[
            (turn1, None),
            (turn2, "Saved."),
        ]
    )

    ps = _patch_heavy()
    for p in ps:
        p.start()
    try:
        await agent.run("please remember")
    finally:
        for p in ps:
            p.stop()

    # make_tool_results was called with the tool call and its result
    assert agent.backend.make_tool_results.called


@pytest.mark.asyncio
async def test_run_passes_latest_pre_see_action_into_scene_update():
    """The last embodied action before see() conditions the scene update."""
    from familiar_agent.agent import EmbodiedAgent

    agent = _make_agent(with_camera=True)
    agent._scene = MagicMock()
    agent._scene.update = AsyncMock(return_value=[])
    agent._scene.context_for_prompt = MagicMock(return_value="")
    agent._scene_backend = MagicMock()
    agent._camera.call = AsyncMock(
        side_effect=[
            ("looked left", None),
            ("I see a room", "base64img"),
        ]
    )

    turn1 = TurnResult(
        stop_reason="tool_use",
        text="",
        tool_calls=[
            ToolCall(id="tc1", name="look", input={"direction": "left", "degrees": 45}),
            ToolCall(id="tc2", name="see", input={}),
        ],
    )
    turn2 = TurnResult(stop_reason="end_turn", text="There is a window.", tool_calls=[])
    agent.backend.stream_turn = AsyncMock(
        side_effect=[(turn1, None), (turn2, "There is a window.")]
    )

    patches = dict(_HEAVY_PATCHES)
    patches["familiar_agent.agent.EmbodiedAgent._run_post_response_pipeline"] = (
        EmbodiedAgent._run_post_response_pipeline
    )

    ps = [patch(t, n) for t, n in patches.items()]
    for p in ps:
        p.start()
    try:
        await agent.run("look and report")
        await agent._drain_background_tasks(timeout=0.5)
    finally:
        for p in ps:
            p.stop()

    agent._scene.update.assert_awaited_once()
    _, kwargs = agent._scene.update.call_args
    assert kwargs["action_name"] == "look"
    assert kwargs["action_input"] == {"direction": "left", "degrees": 45}


# ---------------------------------------------------------------------------
# Tests: auto-say
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_auto_say_fires_when_tts_available_and_no_say_call():
    """When TTS is present and model wrote text without calling say(), auto-say fires."""
    agent = _make_agent(with_tts=True)
    agent.backend.stream_turn = AsyncMock(
        return_value=(_turn("end_turn", text="Hello, I speak!"), "Hello, I speak!")
    )

    ps = _patch_heavy()
    for p in ps:
        p.start()
    try:
        await agent.run("speak to me")
    finally:
        for p in ps:
            p.stop()

    agent._tts.call.assert_awaited_once()
    call_args = agent._tts.call.call_args
    assert call_args[0][0] == "say"


@pytest.mark.asyncio
async def test_run_no_auto_say_when_tts_absent():
    """Without TTS, no auto-say even if model wrote text."""
    agent = _make_agent(with_tts=False)
    agent.backend.stream_turn = AsyncMock(
        return_value=(_turn("end_turn", text="Silent response"), "Silent response")
    )

    ps = _patch_heavy()
    for p in ps:
        p.start()
    try:
        await agent.run("respond")
    finally:
        for p in ps:
            p.stop()

    assert agent._tts is None


@pytest.mark.asyncio
async def test_run_no_auto_say_when_say_already_called():
    """If say() was called as a tool, auto-say should NOT fire again."""
    agent = _make_agent(with_tts=True)

    tc = ToolCall(id="tc1", name="say", input={"text": "I spoke"})
    turn1 = TurnResult(stop_reason="tool_use", text="", tool_calls=[tc])
    turn2 = TurnResult(stop_reason="end_turn", text="done", tool_calls=[])

    agent.backend.stream_turn = AsyncMock(
        side_effect=[
            (turn1, None),
            (turn2, "done"),
        ]
    )

    ps = _patch_heavy()
    for p in ps:
        p.start()
    try:
        await agent.run("speak via tool")
    finally:
        for p in ps:
            p.stop()

    # say() was called once via tool execution; auto-say must NOT add a second call
    assert agent._tts.call.call_count == 1


# ---------------------------------------------------------------------------
# Tests: morning reconstruction on first turn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_first_turn_calls_morning_reconstruction():
    """On the very first turn, _morning_reconstruction is invoked."""
    agent = _make_agent()
    agent.backend.stream_turn = AsyncMock(
        return_value=(_turn("end_turn", text="Good morning"), "Good morning")
    )

    morning_mock = AsyncMock(return_value="morning context")
    patches = dict(_HEAVY_PATCHES)
    patches["familiar_agent.agent.EmbodiedAgent._morning_reconstruction"] = morning_mock

    ps = [patch(t, n) for t, n in patches.items()]
    for p in ps:
        p.start()
    try:
        await agent.run("今日はどう？")
    finally:
        for p in ps:
            p.stop()

    morning_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_subsequent_turns_skip_morning_reconstruction():
    """_morning_reconstruction is NOT called on turns after the first."""
    agent = _make_agent()
    agent._turn_count = 5  # simulate subsequent turn
    agent.backend.stream_turn = AsyncMock(return_value=(_turn("end_turn", text="reply"), "reply"))

    morning_mock = AsyncMock(return_value="")
    patches = dict(_HEAVY_PATCHES)
    patches["familiar_agent.agent.EmbodiedAgent._morning_reconstruction"] = morning_mock

    ps = [patch(t, n) for t, n in patches.items()]
    for p in ps:
        p.start()
    try:
        await agent.run("follow up")
    finally:
        for p in ps:
            p.stop()

    morning_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# Tests: online temporal self + adaptive values
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_injects_online_temporal_context_into_user_message():
    agent = _make_agent()
    agent._turn_count = 1  # next run is a normal turn, not the first turn
    # Temporal context is now read from cache (populated by post-response pipeline)
    agent._cached_temporal_ctx = "[Temporal self]\n[Resurfaced memory]: 朝の空を探した"
    agent.backend.stream_turn = AsyncMock(return_value=(_turn("end_turn", text="reply"), "reply"))

    ps = [patch(t, n) for t, n in _HEAVY_PATCHES.items()]
    for p in ps:
        p.start()
    try:
        await agent.run("今日はどう？")
    finally:
        for p in ps:
            p.stop()

    user_messages = [m["content"] for m in agent.messages if m.get("role") == "user"]
    assert any("[Temporal self]" in msg for msg in user_messages)


@pytest.mark.asyncio
async def test_maybe_update_self_narrative_uses_agency_error_trigger():
    agent = _make_agent()
    agent._utility_backend.complete = AsyncMock(return_value="ウチは少し揺れながら確かめ直した。")
    agent._self_narrative.write = MagicMock()
    agent._prediction.last_signal = MagicMock(
        return_value=SimpleNamespace(action_name="look", agency_error=0.72)
    )

    await agent._maybe_update_self_narrative(
        user_input="何が見えた？",
        final_text="まだ少しずれてる気がする",
        emotion="neutral",
        is_desire_turn=False,
    )
    # Mid-session capture is fire-and-forget; drain the spawned task before asserting.
    await asyncio.gather(*agent._background_tasks, return_exceptions=True)

    agent._self_narrative.write.assert_called_once()
    assert agent._self_narrative.write.call_args.kwargs["trigger"] == "agency_error"


@pytest.mark.asyncio
async def test_maybe_adapt_values_updates_curiosity_and_support_policies():
    agent = _make_agent()
    agent._prediction.last_signal = MagicMock(
        return_value=SimpleNamespace(action_name="look", agency_error=0.68)
    )
    desires = MagicMock()
    desires.boost = MagicMock()

    await agent._maybe_adapt_values(
        user_input="大丈夫？",
        final_text="窓の向こうの空が気になったよ。",
        emotion="tender",
        camera_used=True,
        curiosity="窓の向こうの空",
        is_desire_turn=False,
        desires=desires,
    )

    calls = agent._memory.adjust_behavior_policy_confidence_async.await_args_list
    assert len(calls) >= 2
    assert any(call.args[:2] == ("curiosity:active", 0.08) for call in calls)
    assert any(call.args[:2] == ("curiosity:active", -0.05) for call in calls)
    assert any(call.args[:2] == ("conversation:supportive_style", 0.04) for call in calls)
    desires.boost.assert_called_once_with("share_memory", 0.08)


# ---------------------------------------------------------------------------
# Tests: empty / edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_empty_text_returns_no_response_placeholder():
    """When model returns no text, run() returns the placeholder string."""
    agent = _make_agent()
    agent.backend.stream_turn = AsyncMock(return_value=(_turn("end_turn", text=""), ""))

    ps = _patch_heavy()
    for p in ps:
        p.start()
    try:
        result = await agent.run("hi")
    finally:
        for p in ps:
            p.stop()

    assert result == "(no response)"


@pytest.mark.asyncio
async def test_run_schedules_post_response_pipeline_without_blocking_reply():
    """Post-response work should happen in the background after the reply is ready."""
    agent = _make_agent()
    agent.backend.stream_turn = AsyncMock(return_value=(_turn("end_turn", text="Hello!"), "Hello!"))

    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_pipeline(self, **kwargs):  # noqa: ARG001
        started.set()
        await release.wait()

    patches = dict(_HEAVY_PATCHES)
    patches["familiar_agent.agent.EmbodiedAgent._run_post_response_pipeline"] = _slow_pipeline

    ps = [patch(t, n) for t, n in patches.items()]
    for p in ps:
        p.start()
    try:
        result = await agent.run("こんにちは")
        assert result == "Hello!"
        await asyncio.wait_for(started.wait(), timeout=0.5)
        assert any(not task.done() for task in agent._background_tasks)
    finally:
        release.set()
        await agent._drain_background_tasks(timeout=0.5)
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_run_skips_tape_plan_when_no_separate_utility_backend():
    """No separate utility backend -> skip the extra TAPE planning round-trip."""
    agent = _make_agent()
    agent.backend.stream_turn = AsyncMock(return_value=(_turn("end_turn", text="Hello!"), "Hello!"))

    plan_mock = AsyncMock(return_value="1. say")
    patches = dict(_HEAVY_PATCHES)
    patches["familiar_agent.agent.generate_plan"] = plan_mock

    ps = [patch(t, n) for t, n in patches.items()]
    for p in ps:
        p.start()
    try:
        await agent.run("こんにちは")
    finally:
        for p in ps:
            p.stop()

    plan_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_response_pipeline_updates_self_continuity_state():
    from familiar_agent.agent import EmbodiedAgent

    agent = _make_agent()
    agent._concerns = MagicMock()
    agent._prediction.last_signal = MagicMock(
        return_value=SimpleNamespace(
            action_name="look",
            agency_error=0.62,
            external_surprise=0.18,
        )
    )
    agent._infer_emotion = AsyncMock(return_value="tender")
    agent._summarize_exchange = AsyncMock(return_value="summary")
    agent._update_self_model = AsyncMock()
    agent._maybe_update_self_narrative = AsyncMock()
    agent._maybe_adapt_values = AsyncMock()
    agent.extract_curiosity = AsyncMock(return_value="The window light still feels important.")

    desires = MagicMock()
    desires.boost = MagicMock()
    desires.curiosity_target = None

    await EmbodiedAgent._run_post_response_pipeline(
        agent,
        user_input="どう見えた？",
        final_text="窓の光が少し気になってる。",
        camera_used=True,
        observation_action_name="look",
        observation_action_input={"direction": "left", "degrees": 30},
        companion_mood="frustrated",
        is_desire_turn=False,
        desires=desires,
    )

    agent._concerns.update_from_turn.assert_called_once()
    agent._self_state.apply_turn_context.assert_called_once()


# ---------------------------------------------------------------------------
# B-4: Empty-text LLM response observability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_logs_warning_when_llm_returns_empty_text(caplog):
    """LLM が text="" + end_turn を返したとき "LLM returned empty text" warning が出る。

    B-4 デバッグ計装: result.text or "(no response)" でセンチネル化される直前に
    根本観察ログを残し、どの backend / どの状況で空応答が来たかを追える。
    """
    import logging

    agent = _make_agent()
    # text="" で end_turn (= LLM が何も生成しなかったケース)
    agent.backend.stream_turn = AsyncMock(
        return_value=(_turn("end_turn", text=""), "")
    )

    ps = _patch_heavy()
    for p in ps:
        p.start()
    try:
        with caplog.at_level(logging.WARNING, logger="familiar_agent.agent"):
            result = await agent.run("hello")
    finally:
        for p in ps:
            p.stop()

    # センチネル化された応答 (= 既存挙動の維持) と warning ログ両方を確認
    assert result == "(no response)"
    matching = [
        rec for rec in caplog.records if "LLM returned empty text" in rec.getMessage()
    ]
    assert matching, f"expected empty-text warning, got: {[r.getMessage() for r in caplog.records]}"
    assert matching[0].levelno == logging.WARNING


@pytest.mark.asyncio
async def test_agent_does_not_warn_when_llm_returns_text(caplog):
    """正常応答 (text 非空) のとき "LLM returned empty text" warning は出ない (regression guard)。"""
    import logging

    agent = _make_agent()
    agent.backend.stream_turn = AsyncMock(
        return_value=(_turn("end_turn", text="ok"), "ok")
    )

    ps = _patch_heavy()
    for p in ps:
        p.start()
    try:
        with caplog.at_level(logging.WARNING, logger="familiar_agent.agent"):
            await agent.run("hello")
    finally:
        for p in ps:
            p.stop()

    matching = [
        rec for rec in caplog.records if "LLM returned empty text" in rec.getMessage()
    ]
    assert not matching, "warning must not fire when LLM returns non-empty text"


# ---------------------------------------------------------------------------
# Phase C-10a: vision 再質問時の see 強制 soft nudge
# ---------------------------------------------------------------------------


def _make_vision_agent():
    """see を tool_use する backend を持つ camera 付き agent。

    make_assistant_message / make_tool_results を Anthropic 互換の content
    ブロック形状にして、vision_reprompt が see 痕跡を検出できるようにする。
    """
    agent = _make_agent(with_camera=True)

    # see の tool_use ブロックを含む assistant content / 画像付き tool_result を
    # 履歴に残す形状に差し替える (vision_reprompt の検出経路を実体化)。
    def _assistant_msg(result, raw):
        content = []
        if result.text:
            content.append({"type": "text", "text": result.text})
        for tc in result.tool_calls:
            content.append(
                {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input}
            )
        return {"role": "assistant", "content": content}

    def _tool_results(tool_calls, results):
        blocks = []
        for tc, (text, image) in zip(tool_calls, results):
            sub = [{"type": "text", "text": text}]
            if image:
                sub.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": image,
                        },
                    }
                )
            blocks.append(
                {"type": "tool_result", "tool_use_id": tc.id, "content": sub}
            )
        return [{"role": "user", "content": blocks}]

    agent.backend.make_assistant_message = _assistant_msg
    agent.backend.make_tool_results = _tool_results
    return agent


@pytest.mark.asyncio
async def test_vision_requestion_triggers_second_see_capture():
    """vision 再質問で see が 2 回撮影される (1 回目は初回観察、2 回目は再質問)。

    soft nudge が user メッセージに注入され、モデルが see を呼べば camera.call が
    2 回呼ばれる。実カメラには一切触れない (camera.call は AsyncMock)。
    """
    agent = _make_vision_agent()
    # see → end_turn を 2 ターン分 (初回観察 + 再質問)
    see_tc1 = ToolCall(id="see1", name="see", input={})
    see_tc2 = ToolCall(id="see2", name="see", input={})
    agent.backend.stream_turn = AsyncMock(
        side_effect=[
            (TurnResult(stop_reason="tool_use", text="", tool_calls=[see_tc1]), None),
            (TurnResult(stop_reason="end_turn", text="机が見える", tool_calls=[]), "机が見える"),
            (TurnResult(stop_reason="tool_use", text="", tool_calls=[see_tc2]), None),
            (TurnResult(stop_reason="end_turn", text="まだ机がある", tool_calls=[]), "まだ机がある"),
        ]
    )

    ps = _patch_heavy()
    for p in ps:
        p.start()
    try:
        await agent.run("何が見える？")        # 初回観察 (see #1)
        await agent.run("もう一回見て")          # vision 再質問 (see #2)
    finally:
        for p in ps:
            p.stop()

    # camera.call は see を 2 回実行 (再質問でも撮り直す)
    see_calls = [c for c in agent._camera.call.call_args_list if c.args and c.args[0] == "see"]
    assert len(see_calls) == 2

    # 2 回目の user メッセージに soft nudge が注入されている
    user_texts = [
        m["content"]
        for m in agent.messages
        if isinstance(m, dict) and m.get("role") == "user" and isinstance(m.get("content"), str)
    ]
    assert any("[VISION]" in t for t in user_texts), (
        f"expected force_see nudge in re-question user message, got: {user_texts}"
    )


@pytest.mark.asyncio
async def test_non_vision_input_does_not_inject_nudge():
    """非 vision 入力では履歴に see があっても nudge を注入しない (回帰)。"""
    agent = _make_vision_agent()
    see_tc = ToolCall(id="see1", name="see", input={})
    agent.backend.stream_turn = AsyncMock(
        side_effect=[
            (TurnResult(stop_reason="tool_use", text="", tool_calls=[see_tc]), None),
            (TurnResult(stop_reason="end_turn", text="机が見える", tool_calls=[]), "机が見える"),
            (TurnResult(stop_reason="end_turn", text="そうだね", tool_calls=[]), "そうだね"),
        ]
    )

    ps = _patch_heavy()
    for p in ps:
        p.start()
    try:
        await agent.run("何が見える？")   # see #1
        await agent.run("ありがとう")      # 非 vision 入力 (nudge 注入しない)
    finally:
        for p in ps:
            p.stop()

    user_texts = [
        m["content"]
        for m in agent.messages
        if isinstance(m, dict) and m.get("role") == "user" and isinstance(m.get("content"), str)
    ]
    # 「ありがとう」ターンの user メッセージに VISION nudge は無い
    assert not any("[VISION]" in t for t in user_texts), (
        f"nudge must not be injected for non-vision input, got: {user_texts}"
    )
    # see は 1 回だけ (非 vision 入力は撮り直さない)
    see_calls = [c for c in agent._camera.call.call_args_list if c.args and c.args[0] == "see"]
    assert len(see_calls) == 1


# ── Phase X Problem-2 B: say blind-retry suppression after timeout ─────────────
#
# mutation 対応:
#   - timed_out_tools の skip チェック削除 → test_say_timeout_then_same_say_not_re_executed が fail
#   - 全 tool を抑止 (tool 名 key でない) → test_say_timeout_does_not_block_different_tool が fail
#   - timed_out_tools を instance 属性化 (turn 跨ぎ漏れ) → test_say_works_normally_in_next_turn が fail
#   - timeout gate 無しで同名抑止 → test_two_different_says_both_run_when_no_timeout が fail


@pytest.mark.asyncio
async def test_say_timeout_then_same_say_not_re_executed():
    """say がタイムアウトしたら、同一ターン内で再 say しても実行されない。"""
    agent = _make_agent(with_tts=True)
    agent._tts.call = AsyncMock(side_effect=asyncio.TimeoutError())

    tc1 = ToolCall(id="t1", name="say", input={"text": "X"})
    tc2 = ToolCall(id="t2", name="say", input={"text": "X"})  # blind retry
    agent.backend.stream_turn = AsyncMock(
        side_effect=[
            (TurnResult(stop_reason="tool_use", text="", tool_calls=[tc1]), None),
            (TurnResult(stop_reason="tool_use", text="", tool_calls=[tc2]), None),
            (TurnResult(stop_reason="end_turn", text="ok", tool_calls=[]), "ok"),
        ]
    )

    ps = _patch_heavy()
    for p in ps:
        p.start()
    try:
        await agent.run("test")
    finally:
        for p in ps:
            p.stop()

    # first say timed out (call_count 1); second say skipped (not re-executed)
    assert agent._tts.call.call_count == 1


@pytest.mark.asyncio
async def test_say_timeout_does_not_block_different_tool():
    """say のタイムアウトは別 tool (remember) の実行を妨げない。"""
    agent = _make_agent(with_tts=True)
    agent._tts.call = AsyncMock(side_effect=asyncio.TimeoutError())

    tc_say = ToolCall(id="t1", name="say", input={"text": "X"})
    tc_rem = ToolCall(id="t2", name="remember", input={"content": "note"})
    agent.backend.stream_turn = AsyncMock(
        side_effect=[
            (TurnResult(stop_reason="tool_use", text="", tool_calls=[tc_say]), None),
            (TurnResult(stop_reason="tool_use", text="", tool_calls=[tc_rem]), None),
            (TurnResult(stop_reason="end_turn", text="done", tool_calls=[]), "done"),
        ]
    )

    ps = _patch_heavy()
    for p in ps:
        p.start()
    try:
        await agent.run("test")
    finally:
        for p in ps:
            p.stop()

    assert agent._memory_tool.call.called  # remember not suppressed


@pytest.mark.asyncio
async def test_say_works_normally_in_next_turn():
    """timed_out_tools は run() ごとにリセット — 次ターンの say は通常実行される。"""
    agent = _make_agent(with_tts=True)

    # run 1: say times out
    agent._tts.call = AsyncMock(side_effect=asyncio.TimeoutError())
    tc1 = ToolCall(id="t1", name="say", input={"text": "X"})
    agent.backend.stream_turn = AsyncMock(
        side_effect=[
            (TurnResult(stop_reason="tool_use", text="", tool_calls=[tc1]), None),
            (TurnResult(stop_reason="end_turn", text="a", tool_calls=[]), "a"),
        ]
    )
    ps = _patch_heavy()
    for p in ps:
        p.start()
    try:
        await agent.run("turn1")
    finally:
        for p in ps:
            p.stop()

    # run 2: say succeeds (fresh turn → timed_out_tools reset)
    agent._tts.call = AsyncMock(return_value=("spoken", None))
    tc2 = ToolCall(id="t2", name="say", input={"text": "Y"})
    agent.backend.stream_turn = AsyncMock(
        side_effect=[
            (TurnResult(stop_reason="tool_use", text="", tool_calls=[tc2]), None),
            (TurnResult(stop_reason="end_turn", text="b", tool_calls=[]), "b"),
        ]
    )
    ps = _patch_heavy()
    for p in ps:
        p.start()
    try:
        await agent.run("turn2")
    finally:
        for p in ps:
            p.stop()

    assert agent._tts.call.call_count == 1  # the new mock ran once → say executed


@pytest.mark.asyncio
async def test_two_different_says_both_run_when_no_timeout():
    """タイムアウトしなければ同一ターンの複数 say は両方実行される (過抑止しない)。"""
    agent = _make_agent(with_tts=True)
    agent._tts.call = AsyncMock(return_value=("spoken", None))

    tc1 = ToolCall(id="t1", name="say", input={"text": "A"})
    tc2 = ToolCall(id="t2", name="say", input={"text": "B"})
    agent.backend.stream_turn = AsyncMock(
        side_effect=[
            (TurnResult(stop_reason="tool_use", text="", tool_calls=[tc1]), None),
            (TurnResult(stop_reason="tool_use", text="", tool_calls=[tc2]), None),
            (TurnResult(stop_reason="end_turn", text="ok", tool_calls=[]), "ok"),
        ]
    )

    ps = _patch_heavy()
    for p in ps:
        p.start()
    try:
        await agent.run("test")
    finally:
        for p in ps:
            p.stop()

    assert agent._tts.call.call_count == 2  # both says executed (no timeout)
