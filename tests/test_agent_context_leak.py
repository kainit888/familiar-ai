"""Tests for Gemini-Flash-Lite context-leak countermeasures.

Covers two complementary defences:

* (1A) ``SYSTEM_PROMPT`` enumerates every bracketed internal context header
  inside the ``suppress-meta-reasoning`` constraint, so the model is told to
  read them silently rather than echo them.
* (1C) ``_compact_memory_context`` strips bracket headers from the memory
  context that is appended to the user message, so even if the model does
  echo, there is no scaffolding structure left to leak.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from familiar_agent.agent import (
    SYSTEM_PROMPT,
    _COMPACT_MEMORY_CONTEXT_MAX_CHARS,
    _compact_memory_context,
)
from familiar_agent.backend import TurnResult


# ---------------------------------------------------------------------------
# 3-A. SYSTEM_PROMPT enumerates every internal context header
# ---------------------------------------------------------------------------


def test_system_prompt_lists_all_internal_context_headers() -> None:
    """suppress-meta-reasoning constraint enumerates every injected bracket header."""
    expected_headers = [
        "[Mental state]",
        "[Interaction policy]",
        "[Continuation]",
        "[Routine notes]",
        "[Open unfinished business]",
        "[Recent mental continuity]",
        "[昨日からの私",
        "[Me from yesterday",
        "[安定した事実",
        "[行動方針",
        "[最近の気持ち・出来事]",
        "[自分という存在",
        "[過去の記憶",
        "[Action plan",
    ]
    for header in expected_headers:
        assert header in SYSTEM_PROMPT, (
            f"missing header in suppress-meta-reasoning constraint: {header}"
        )


def test_system_prompt_warns_against_echoing_context_blocks() -> None:
    """The constraint must tell the model not to echo bracketed context blocks."""
    # We don't pin exact wording, but the constraint should clearly forbid
    # echoing the bracketed scaffolding.
    assert "NOT messages from the user" in SYSTEM_PROMPT
    assert "internal context blocks" in SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# 3-B. _compact_memory_context unit tests
# ---------------------------------------------------------------------------


def test_compact_memory_context_empty_returns_empty() -> None:
    assert _compact_memory_context("", "", "", "") == ""
    assert _compact_memory_context("", "", "", "", temporal_ctx="") == ""


def test_compact_memory_context_strips_bracket_headers() -> None:
    """Bracket-prefixed header lines are removed from each block."""
    memories = "[過去の記憶（証拠つき）: conf<0.55 は不確か。断定しないこと]:\n- foo bar"
    out = _compact_memory_context(memories, "", "", "")
    # Bracket header is gone.
    assert "[" not in out
    assert "過去の記憶" not in out
    # Content survives.
    assert "foo bar" in out
    # Natural-language label is present.
    assert out.startswith("記憶:")


def test_compact_memory_context_all_blocks_have_no_brackets() -> None:
    """Across every block type, no `[…]` header survives in the output."""
    memories = "[過去の記憶 ...]:\n- yesterday felt warm"
    feelings = "[最近の気持ち・出来事]:\n- 嬉しかった"
    semantic = "[安定した事実（semantic memory）]:\n- conf:0.82 picoの好物はりんご"
    policies = "[行動方針（policy memory）]:\n- conf:0.7 trigger:greeting action:wave: 笑顔で挨拶"
    out = _compact_memory_context(memories, feelings, semantic, policies)
    forbidden = [
        "[Mental state]",
        "[安定した事実",
        "[行動方針",
        "[過去の記憶",
        "[最近の気持ち・出来事]",
    ]
    for bracket in forbidden:
        assert bracket not in out, f"leaked bracket header: {bracket}"
    # Each label appears exactly once at the start of its line.
    for label in ("記憶:", "最近:", "事実:", "方針:"):
        assert label in out


def test_compact_memory_context_respects_max_chars() -> None:
    """Even with huge inputs the output stays within the configured cap."""
    huge = "[過去の記憶 ...]:\n" + "\n".join(f"- bullet {i} " + "あ" * 80 for i in range(40))
    out = _compact_memory_context(huge, huge, huge, huge)
    assert len(out) <= _COMPACT_MEMORY_CONTEXT_MAX_CHARS
    # Brackets must still be gone even after truncation.
    assert "[" not in out


def test_compact_memory_context_temporal_passthrough() -> None:
    """Temporal context appends to the output even when blocks are empty."""
    out = _compact_memory_context("", "", "", "", temporal_ctx="今は午後3時ごろ")
    assert "今は午後3時" in out
    assert "[" not in out


# ---------------------------------------------------------------------------
# 3-C. agent.run integration: user message must not contain bracket headers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_run_user_message_has_no_bracket_headers() -> None:
    """When agent.run wires memory context into the user message, the
    bracket scaffolding ([安定した事実...], [行動方針...], …) must not appear
    in the message handed to the backend.
    """
    from tests.test_agent_react_loop import _make_agent, _patch_heavy, _turn

    agent = _make_agent()
    # Memory returns non-empty rows so the memory context branch runs.
    agent._memory.recall_async = AsyncMock(
        return_value=[{"summary": "warm yesterday", "date": "2026-05-22", "time": "12:00"}]
    )
    agent._memory.recent_feelings_async = AsyncMock(
        return_value=[{"summary": "嬉しかった", "date": "2026-05-22", "time": "12:00"}]
    )
    agent._memory.recall_semantic_facts_async = AsyncMock(
        return_value=[{"key": "fav", "summary": "picoはりんごが好き", "confidence": 0.82}]
    )
    agent._memory.recall_behavior_policies_async = AsyncMock(
        return_value=[
            {
                "key": "polite",
                "summary": "丁寧語を使う",
                "trigger_context": "greeting",
                "action_hint": "wave",
                "confidence": 0.7,
            }
        ]
    )
    # format_*_for_context return strings with bracket headers (mirrors prod).
    agent._memory.format_for_context.return_value = "[過去の記憶 ...]:\n- 昨晩空を眺めた"
    agent._memory.format_feelings_for_context.return_value = (
        "[最近の気持ち・出来事]:\n- 嬉しかった"
    )
    agent._memory.format_semantic_facts_for_context.return_value = (
        "[安定した事実（semantic memory）]:\n- conf:0.82 picoはりんごが好き"
    )
    agent._memory.format_behavior_policies_for_context.return_value = (
        "[行動方針（policy memory）]:\n- 丁寧語を使う"
    )

    agent.backend.stream_turn = AsyncMock(
        return_value=(_turn("end_turn", text="うん"), "うん")
    )

    ps = _patch_heavy()
    for p in ps:
        p.start()
    try:
        await agent.run("元気？")
    finally:
        for p in ps:
            p.stop()

    # Inspect the user message that was sent to the backend. The agent appends
    # the user message to agent.messages just before invoking stream_turn.
    user_messages = [m for m in agent.messages if m.get("role") == "user"]
    assert user_messages, "agent.run did not append a user message"
    last_user = user_messages[-1]
    content = last_user.get("content", "")
    if isinstance(content, list):
        # Anthropic-style content blocks: concatenate text segments.
        content = "\n".join(
            blk.get("text", "") if isinstance(blk, dict) else str(blk) for blk in content
        )
    leaked_headers = [
        "[安定した事実",
        "[行動方針",
        "[最近の気持ち・出来事]",
        "[過去の記憶",
    ]
    for header in leaked_headers:
        assert header not in content, f"bracket header leaked into user msg: {header}"
    # The natural-language labels should be present instead.
    assert any(label in content for label in ("記憶:", "最近:", "事実:", "方針:"))


@pytest.mark.asyncio
async def test_agent_run_user_message_unchanged_when_no_memories() -> None:
    """If every memory recall returns nothing, the user message is unmodified."""
    from tests.test_agent_react_loop import _make_agent, _patch_heavy, _turn

    agent = _make_agent()
    # Defaults already return [] / "" for every memory async, so this exercises
    # the empty-context branch end-to-end.
    agent.backend.stream_turn = AsyncMock(
        return_value=(_turn("end_turn", text="ok"), "ok")
    )

    ps = _patch_heavy()
    for p in ps:
        p.start()
    try:
        await agent.run("plain hello")
    finally:
        for p in ps:
            p.stop()

    user_messages = [m for m in agent.messages if m.get("role") == "user"]
    assert user_messages
    content = user_messages[-1].get("content", "")
    if isinstance(content, list):
        content = "\n".join(
            blk.get("text", "") if isinstance(blk, dict) else str(blk) for blk in content
        )
    assert content == "plain hello"
    # And of course no bracket headers slipped in via some other path.
    assert "[" not in content


# Silence unused-import warnings for the helper symbols imported above.
_ = TurnResult
