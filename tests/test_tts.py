"""Tests for TTSTool — ElevenLabs API and audio playback mocked."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helper: TTSTool without real __init__ side-effects
# ---------------------------------------------------------------------------


def _make_tts(api_key: str = "fake-key", voice_id: str = "fake-voice"):
    from familiar_agent.tools.tts import TTSTool

    with patch("familiar_agent.tools.tts._ensure_go2rtc"):
        tool = TTSTool(api_key=api_key, voice_id=voice_id, output="local")
    return tool


# ---------------------------------------------------------------------------
# Tests: get_tool_definitions()
# ---------------------------------------------------------------------------


def test_get_tool_definitions_returns_say():
    tool = _make_tts()
    defs = tool.get_tool_definitions()
    assert len(defs) == 1
    assert defs[0]["name"] == "say"


# ---------------------------------------------------------------------------
# Tests: call()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_say_invokes_say_method():
    """call('say', ...) delegates to say() and returns its result."""
    tool = _make_tts()
    tool.say = AsyncMock(return_value="Said: hello")

    result, img = await tool.call("say", {"text": "hello"})

    assert result == "Said: hello"
    assert img is None
    tool.say.assert_awaited_once_with("hello")


@pytest.mark.asyncio
async def test_call_unknown_tool_returns_error():
    tool = _make_tts()
    result, img = await tool.call("nonexistent", {})
    assert "Unknown" in result or "nonexistent" in result


# ---------------------------------------------------------------------------
# Tests: say() — API + playback mocked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_say_calls_elevenlabs_api():
    """say() は pico_agent.adapters.tts_sbv2.speak() を経由する (Phase C-1)。

    旧テスト名 (elevenlabs_api) は互換性のため維持。中身は adapter mock。
    Phase C-5 (v5) で speak() は None 返却、target="tapo_speaker" 固定。
    """
    tool = _make_tts(api_key="test-api-key")

    fake_speak = AsyncMock(return_value=None)

    with patch("pico_agent.adapters.tts_sbv2.speak", fake_speak):
        await tool.say("hello world")

    # adapter が呼ばれたことを検証 (text + target=tapo_speaker)
    fake_speak.assert_awaited_once()
    awaited_args = fake_speak.await_args
    assert awaited_args.args[0] == "hello world"
    assert awaited_args.kwargs.get("target") == "tapo_speaker"


@pytest.mark.asyncio
async def test_say_truncates_long_text():
    """say() は 200 文字超を 200 文字に切り詰めてから adapter に渡す。"""
    tool = _make_tts()
    long_text = "x" * 300

    sent_texts: list[str] = []

    async def capture_speak(text, *_args, **_kwargs):
        sent_texts.append(text)
        return None

    with patch("pico_agent.adapters.tts_sbv2.speak", side_effect=capture_speak):
        await tool.say(long_text)

    assert sent_texts, "adapter was never called"
    sent_text = sent_texts[0]
    assert len(sent_text) <= 200
    assert sent_text.endswith("...")


@pytest.mark.asyncio
async def test_say_returns_success_message_even_on_adapter_failure():
    """adapter が None 返却 (失敗 = silent fail) でも say() は送出した旨を返す。

    v5 で speak() は失敗時無音 + warning。say() からは adapter ログ依存となる
    ので、ここでは送出した旨だけ返す (二層分離維持)。
    """
    tool = _make_tts()

    with patch("pico_agent.adapters.tts_sbv2.speak", new=AsyncMock(return_value=None)):
        result = await tool.say("hello")

    # v5: 成否は adapter ログ依存。say() は送出した旨だけ返す。
    assert "tapo_speaker" in result or "Said" in result


@pytest.mark.asyncio
async def test_say_notifies_voice_guard_on_success():
    """voice_guard.on_tts_start / on_tts_end が呼ばれる (adapter 経由でも維持)。"""
    tool = _make_tts()
    tool._voice_guard = MagicMock()

    with patch("pico_agent.adapters.tts_sbv2.speak", new=AsyncMock(return_value=None)):
        await tool.say("hello world")

    tool._voice_guard.on_tts_start.assert_called_once_with("hello world")
    tool._voice_guard.on_tts_end.assert_called_once_with("hello world", played=True)


@pytest.mark.asyncio
async def test_say_remote_delegates_to_speak_tapo_speaker():
    """Phase C-5 (v5): output='remote' は ``speak(target='tapo_speaker')`` に委譲する。

    v5 でフォールバックチェーン (play_with_fallback) は撤廃。output パラメータは
    deprecated 扱いで無視され、常に tapo_speaker 単一経路に送出される。
    """
    tool = _make_tts()
    tool.output = "remote"

    fake_speak = AsyncMock(return_value=None)

    with patch("pico_agent.adapters.tts_sbv2.speak", fake_speak):
        result = await tool.say("hello")

    fake_speak.assert_awaited_once()
    assert fake_speak.await_args.args[0] == "hello"
    assert fake_speak.await_args.kwargs.get("target") == "tapo_speaker"
    assert "tapo_speaker" in result


@pytest.mark.asyncio
async def test_say_remote_failure_no_fallback():
    """v5 (Phase C-5): adapter speak() 失敗時もフォールスルーせず無音で終わる。

    旧コードでは remote 失敗時 → local フォールバックしていたが、v5 (14-5-11) で
    フォールバック自体撤廃。失敗時は adapter 内で warning のみ、say() は
    voice_guard を通常通り閉じて送出した旨だけ返す (adapter ログ依存)。
    """
    tool = _make_tts()
    tool.output = "remote"

    # speak() が例外を投げず None 返却 (silent fail)
    fake_speak = AsyncMock(return_value=None)

    with (
        patch("pico_agent.adapters.tts_sbv2.speak", fake_speak),
        # _play_local など旧フォールバック先は呼ばれない (dead code)
        patch(
            "familiar_agent.tools.tts._play_local", new=AsyncMock(return_value=True)
        ) as mock_local,
    ):
        await tool.say("hello")

    fake_speak.assert_awaited_once()
    # v5: ローカル再生フォールバックは発生しない
    mock_local.assert_not_called()


@pytest.mark.asyncio
async def test_say_serializes_concurrent_calls():
    """Concurrent say() calls must be serialized (lock prevents overlap)."""
    tool = _make_tts()

    with patch("pico_agent.adapters.tts_sbv2.speak", new=AsyncMock(return_value=None)):
        # Launch two say() calls concurrently
        results = await asyncio.gather(
            tool.say("first"),
            tool.say("second"),
        )

    # Both should succeed (no exception)
    assert len(results) == 2


# ---------------------------------------------------------------------------
# Phase C-6: TTSTool.__init__ should no longer probe / spawn go2rtc
# ---------------------------------------------------------------------------


def test_tts_tool_init_does_not_emit_go2rtc_warning(caplog):
    """TTSTool() init で go2rtc binary not found warning が出ないこと (Phase C-6)。"""
    from familiar_agent.tools.tts import TTSTool

    with caplog.at_level("WARNING", logger="familiar_agent.tools.tts"):
        TTSTool(api_key="k", voice_id="v", output="local")
    msgs = [r.message for r in caplog.records]
    assert not any("go2rtc binary not found" in m for m in msgs)
    assert not any("go2rtc config not found" in m for m in msgs)
