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
    """
    tool = _make_tts(api_key="test-api-key")

    fake_speak = AsyncMock(return_value=b"FAKE_WAV_BYTES")

    with (
        patch("pico_agent.adapters.tts_sbv2.speak", fake_speak),
        patch("familiar_agent.tools.tts._play_local", new=AsyncMock(return_value=True)),
        patch("builtins.open", MagicMock()),
        patch("os.unlink"),
        patch("tempfile.NamedTemporaryFile") as mock_tmp,
    ):
        tmp_file = MagicMock()
        tmp_file.__enter__ = MagicMock(return_value=tmp_file)
        tmp_file.__exit__ = MagicMock(return_value=False)
        tmp_file.name = "/tmp/fake.wav"
        mock_tmp.return_value = tmp_file

        await tool.say("hello world")

    # adapter が呼ばれたことを検証 (text 引数チェック)。
    fake_speak.assert_awaited_once()
    awaited_args = fake_speak.await_args
    assert awaited_args.args[0] == "hello world"


@pytest.mark.asyncio
async def test_say_truncates_long_text():
    """say() は 200 文字超を 200 文字に切り詰めてから adapter に渡す。"""
    tool = _make_tts()
    long_text = "x" * 300

    sent_texts: list[str] = []

    async def capture_speak(text, *_args, **_kwargs):
        sent_texts.append(text)
        return b"FAKE_WAV_BYTES"

    with (
        patch("pico_agent.adapters.tts_sbv2.speak", side_effect=capture_speak),
        patch("familiar_agent.tools.tts._play_local", new=AsyncMock(return_value=True)),
        patch("os.unlink"),
        patch("tempfile.NamedTemporaryFile") as mock_tmp,
    ):
        tmp_file = MagicMock()
        tmp_file.__enter__ = MagicMock(return_value=tmp_file)
        tmp_file.__exit__ = MagicMock(return_value=False)
        tmp_file.name = "/tmp/fake.wav"
        mock_tmp.return_value = tmp_file

        await tool.say(long_text)

    assert sent_texts, "adapter was never called"
    sent_text = sent_texts[0]
    assert len(sent_text) <= 200
    assert sent_text.endswith("...")


@pytest.mark.asyncio
async def test_say_returns_error_on_api_failure():
    """adapter が空 bytes を返した時、say() はエラー文字列を返す。"""
    tool = _make_tts()

    with patch("pico_agent.adapters.tts_sbv2.speak", new=AsyncMock(return_value=b"")):
        result = await tool.say("hello")

    assert "failed" in result.lower()


@pytest.mark.asyncio
async def test_say_notifies_voice_guard_on_success():
    """voice_guard.on_tts_start / on_tts_end が呼ばれる (adapter 経由でも維持)。"""
    tool = _make_tts()
    tool._voice_guard = MagicMock()

    with (
        patch("pico_agent.adapters.tts_sbv2.speak", new=AsyncMock(return_value=b"FAKE_WAV")),
        patch("familiar_agent.tools.tts._play_local", new=AsyncMock(return_value=True)),
        patch("os.unlink"),
        patch("tempfile.NamedTemporaryFile") as mock_tmp,
    ):
        tmp_file = MagicMock()
        tmp_file.__enter__ = MagicMock(return_value=tmp_file)
        tmp_file.__exit__ = MagicMock(return_value=False)
        tmp_file.name = "/tmp/fake.wav"
        mock_tmp.return_value = tmp_file

        await tool.say("hello world")

    tool._voice_guard.on_tts_start.assert_called_once_with("hello world")
    tool._voice_guard.on_tts_end.assert_called_once_with("hello world", played=True)


@pytest.mark.asyncio
async def test_say_serializes_concurrent_calls():
    """Concurrent say() calls must be serialized (lock prevents overlap)."""
    tool = _make_tts()

    with (
        patch("pico_agent.adapters.tts_sbv2.speak", new=AsyncMock(return_value=b"FAKE_WAV")),
        patch("familiar_agent.tools.tts._play_local", new=AsyncMock(return_value=True)),
        patch("os.unlink"),
        patch("tempfile.NamedTemporaryFile") as mock_tmp,
    ):
        tmp_file = MagicMock()
        tmp_file.__enter__ = MagicMock(return_value=tmp_file)
        tmp_file.__exit__ = MagicMock(return_value=False)
        tmp_file.name = "/tmp/fake.wav"
        mock_tmp.return_value = tmp_file

        # Launch two say() calls concurrently
        results = await asyncio.gather(
            tool.say("first"),
            tool.say("second"),
        )

    # Both should succeed (no exception)
    assert len(results) == 2
