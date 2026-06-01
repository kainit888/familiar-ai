"""Phase D-1: Discord 自発 post tool テスト (全 mock; 実 Discord 接続なし)。

mutation 対応 (必須4):
    - Discord post を no-op (call が send しない) → test_discord_post_sends_text fail
    - graceful no-op を無効化 (channel/bot 欠落で raise) → test_discord_post_handles_no_token_gracefully
      / test_discord_post_handles_invalid_channel_id fail
    - 既存 on_message 経路を破壊 (bot.py) → test_existing_on_message_unchanged fail
    - autonomy 経路を bypass (空 message でも強制 send) → test_pico_can_skip_post fail
"""

from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from familiar_agent.tools import discord_post as dp
from familiar_agent.tools.discord_post import DiscordPostTool
from pico_agent.discord_bridge import PicoBot


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    for k in (
        "DISCORD_TOKEN",
        "DISCORD_BOT_TOKEN",
        "DISCORD_OWNER_ID",
        "DISCORD_GUILD_ID",
        "DISCORD_POST_CHANNEL_ID",
        "DISCORD_POST_COOLDOWN_S",
        "DISCORD_POST_CONNECT_TIMEOUT_S",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(dp, "_BOT_OVERRIDE", None)
    monkeypatch.setattr(dp, "_BOT_SINGLETON", None)
    yield


def _configure_env(monkeypatch, *, channel="333"):
    monkeypatch.setenv("DISCORD_TOKEN", "tok")
    monkeypatch.setenv("DISCORD_OWNER_ID", "111")
    monkeypatch.setenv("DISCORD_GUILD_ID", "222")
    if channel is not None:
        monkeypatch.setenv("DISCORD_POST_CHANNEL_ID", channel)
    monkeypatch.setattr("pico_agent.discord_bridge.is_discord_available", lambda: True)


def _fake_bot(*, send_result=True, raises=None):
    bot = MagicMock(name="PicoBot")
    bot.is_running = True
    if raises is not None:
        bot.send_message = AsyncMock(side_effect=raises)
    else:
        bot.send_message = AsyncMock(return_value=send_result)
    return bot


def _stub_agent():
    """Minimal EmbodiedAgent for _all_tool_defs gate testing (no __init__)."""
    from familiar_agent.agent import EmbodiedAgent

    a = EmbodiedAgent.__new__(EmbodiedAgent)
    a._camera = None
    a._mobility = None
    a._tts = None
    a._mcp = None
    empty = types.SimpleNamespace(get_tool_definitions=lambda: [])
    a._memory_tool = empty
    a._tom_tool = empty
    a._coding = empty
    a._web_search = None
    a._discord_post = DiscordPostTool()
    return a


# ── Group 1: post 本体 ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_discord_post_sends_text(monkeypatch):
    _configure_env(monkeypatch)
    bot = _fake_bot()
    monkeypatch.setattr(dp, "_BOT_OVERRIDE", bot)
    tool = DiscordPostTool()
    result, img = await tool.call("post_to_discord", {"message": "hi カイニット"})
    assert img is None
    assert result == "(posted)"
    bot.send_message.assert_awaited_once_with(333, "hi カイニット")


@pytest.mark.asyncio
async def test_discord_post_handles_no_token_gracefully(monkeypatch):
    # no env at all → not available; call no-ops without raising
    assert DiscordPostTool.available() is False
    tool = DiscordPostTool()
    result, img = await tool.call("post_to_discord", {"message": "hi"})
    assert result == "(discord posting unavailable)"
    assert img is None


@pytest.mark.asyncio
async def test_discord_post_handles_connection_failure(monkeypatch):
    _configure_env(monkeypatch)
    bot = _fake_bot(raises=RuntimeError("gateway down"))
    monkeypatch.setattr(dp, "_BOT_OVERRIDE", bot)
    tool = DiscordPostTool()
    result, _ = await tool.call("post_to_discord", {"message": "hi"})  # must not raise
    assert result == "(discord post failed)"


@pytest.mark.asyncio
async def test_discord_post_handles_invalid_channel_id(monkeypatch):
    _configure_env(monkeypatch, channel="not-an-int")
    bot = _fake_bot()
    monkeypatch.setattr(dp, "_BOT_OVERRIDE", bot)
    tool = DiscordPostTool()
    result, _ = await tool.call("post_to_discord", {"message": "hi"})
    assert result == "(discord posting unavailable)"
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_discord_post_returns_posted_on_success(monkeypatch):
    # PicoBot.send_message returns a bool (not a message id); success → "(posted)".
    _configure_env(monkeypatch)
    monkeypatch.setattr(dp, "_BOT_OVERRIDE", _fake_bot(send_result=True))
    result, _ = await DiscordPostTool().call("post_to_discord", {"message": "x"})
    assert result == "(posted)"


@pytest.mark.asyncio
async def test_discord_post_returns_failed_when_send_false(monkeypatch):
    _configure_env(monkeypatch)
    monkeypatch.setattr(dp, "_BOT_OVERRIDE", _fake_bot(send_result=False))
    result, _ = await DiscordPostTool().call("post_to_discord", {"message": "x"})
    assert result == "(discord post failed)"


# ── Group 2: 既存受動応答 (on_message) retention ──────────────────────────────
@pytest.mark.asyncio
async def test_existing_on_message_unchanged(monkeypatch):
    monkeypatch.setenv("DISCORD_OWNER_ID", "111")
    monkeypatch.setenv("DISCORD_GUILD_ID", "222")
    monkeypatch.setenv("DISCORD_TOKEN", "tok")
    seen = {}

    async def on_text(user_id, content):
        seen["args"] = (user_id, content)
        return "pico reply"

    bot = PicoBot(on_text_message=on_text)
    captured: dict = {}
    mock_client = MagicMock()
    mock_client.event = lambda f: captured.setdefault(f.__name__, f) or f
    bot._attach_event_handlers(mock_client)
    assert "on_message" in captured and "on_ready" in captured  # wiring intact

    msg = MagicMock()
    msg.author.id = 111
    msg.author.bot = False
    msg.guild.id = 222
    msg.channel.id = 333
    msg.content = "hello pico"
    msg.channel.send = AsyncMock()
    await captured["on_message"](msg)

    assert seen["args"] == ("111", "hello pico")  # passive path reached the handler
    msg.channel.send.assert_awaited_once_with("pico reply")


@pytest.mark.asyncio
async def test_existing_passive_response_still_works(monkeypatch):
    # a filtered (bot-authored) message yields no reply — boundary intact
    bot = PicoBot(on_text_message=AsyncMock(return_value="should not send"))
    bot.owner_id = 111
    bot.guild_id = 222
    captured: dict = {}
    mock_client = MagicMock()
    mock_client.event = lambda f: captured.setdefault(f.__name__, f) or f
    bot._attach_event_handlers(mock_client)

    msg = MagicMock()
    msg.author.id = 999  # not owner
    msg.author.bot = True  # bot-authored → filtered
    msg.guild.id = 222
    msg.channel.id = 333
    msg.content = "spam"
    msg.channel.send = AsyncMock()
    await captured["on_message"](msg)
    msg.channel.send.assert_not_awaited()


def test_new_post_module_does_not_patch_bot():
    # importing discord_post must not replace PicoBot's existing methods
    assert PicoBot.send_message.__qualname__.startswith("PicoBot.")
    assert hasattr(PicoBot, "start") and hasattr(PicoBot, "stop")


@pytest.mark.asyncio
async def test_discord_bridge_compatibility(monkeypatch):
    # the tool calls send_message(channel_id:int, content:str) — the real contract
    _configure_env(monkeypatch)
    bot = _fake_bot()
    monkeypatch.setattr(dp, "_BOT_OVERRIDE", bot)
    await DiscordPostTool().call("post_to_discord", {"message": "contract"})
    (cid, content), _kw = bot.send_message.call_args
    assert isinstance(cid, int) and isinstance(content, str)


# ── Group 3: ピコの judgment / 統合 / gate ────────────────────────────────────
def test_available_false_without_channel_id(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "tok")
    monkeypatch.setenv("DISCORD_OWNER_ID", "111")
    monkeypatch.setenv("DISCORD_GUILD_ID", "222")
    monkeypatch.setattr("pico_agent.discord_bridge.is_discord_available", lambda: True)
    assert DiscordPostTool.available() is False  # channel id missing


def test_available_false_when_discord_py_missing(monkeypatch):
    _configure_env(monkeypatch)
    monkeypatch.setattr("pico_agent.discord_bridge.is_discord_available", lambda: False)
    assert DiscordPostTool.available() is False


def test_available_true_when_fully_configured(monkeypatch):
    _configure_env(monkeypatch)
    assert DiscordPostTool.available() is True


def test_tool_not_registered_when_unavailable():
    a = _stub_agent()  # no env → unavailable
    names = [d["name"] for d in a._all_tool_defs]
    assert "post_to_discord" not in names


def test_tool_registered_when_available(monkeypatch):
    _configure_env(monkeypatch)
    a = _stub_agent()
    names = [d["name"] for d in a._all_tool_defs]
    assert "post_to_discord" in names  # offered during the heartbeat turn


@pytest.mark.asyncio
async def test_pico_can_choose_to_post(monkeypatch):
    _configure_env(monkeypatch)
    bot = _fake_bot()
    monkeypatch.setattr(dp, "_BOT_OVERRIDE", bot)
    result, _ = await DiscordPostTool().call("post_to_discord", {"message": "I learned X"})
    assert result == "(posted)"
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_pico_can_skip_post(monkeypatch):
    # autonomy: an empty message (Pico choosing not to compose) sends nothing
    _configure_env(monkeypatch)
    bot = _fake_bot()
    monkeypatch.setattr(dp, "_BOT_OVERRIDE", bot)
    result, _ = await DiscordPostTool().call("post_to_discord", {"message": "   "})
    assert result == "No message provided."
    bot.send_message.assert_not_awaited()


def test_post_content_format(monkeypatch):
    # tool description is plain-text guidance; schema requires a single message string
    _configure_env(monkeypatch)
    defs = DiscordPostTool().get_tool_definitions()
    assert defs[0]["name"] == "post_to_discord"
    assert defs[0]["input_schema"]["required"] == ["message"]


@pytest.mark.asyncio
async def test_cooldown_blocks_second_post(monkeypatch):
    _configure_env(monkeypatch)
    monkeypatch.setenv("DISCORD_POST_COOLDOWN_S", "600")
    bot = _fake_bot()
    monkeypatch.setattr(dp, "_BOT_OVERRIDE", bot)
    tool = DiscordPostTool()
    first, _ = await tool.call("post_to_discord", {"message": "first"})
    second, _ = await tool.call("post_to_discord", {"message": "second"})
    assert first == "(posted)"
    assert "Discord" in second or "投稿" in second  # cooldown notice (en/ja)
    bot.send_message.assert_awaited_once()  # only the first actually sent


@pytest.mark.asyncio
async def test_cooldown_only_updates_on_success(monkeypatch):
    # a failed send must NOT start the cooldown — the post stays eligible
    _configure_env(monkeypatch)
    monkeypatch.setenv("DISCORD_POST_COOLDOWN_S", "600")
    bot = _fake_bot(send_result=False)
    monkeypatch.setattr(dp, "_BOT_OVERRIDE", bot)
    tool = DiscordPostTool()
    await tool.call("post_to_discord", {"message": "first"})
    await tool.call("post_to_discord", {"message": "second"})
    assert bot.send_message.await_count == 2  # both attempted (no cooldown after fail)


@pytest.mark.asyncio
async def test_cooldown_elapsed_allows_post(monkeypatch):
    _configure_env(monkeypatch)
    monkeypatch.setenv("DISCORD_POST_COOLDOWN_S", "600")
    bot = _fake_bot()
    monkeypatch.setattr(dp, "_BOT_OVERRIDE", bot)
    tool = DiscordPostTool()
    await tool.call("post_to_discord", {"message": "first"})
    # simulate time passing beyond the cooldown window
    assert tool._last_post_monotonic is not None
    tool._last_post_monotonic -= 601.0
    second, _ = await tool.call("post_to_discord", {"message": "second"})
    assert second == "(posted)"
    assert bot.send_message.await_count == 2
