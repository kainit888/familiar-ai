"""Tests for pico_agent.discord_bridge (Phase D スケルトン)。

Phase C-1 完了時点では discord.py 未インストール想定で:
  - availability check が False を返す
  - PicoBot.start() が DiscordDisabledError を投げる
  - TextChannelHandler.should_handle のフィルタロジックが正しい
  - VoiceChannelListener の状態遷移と STT/TTS パイプライン
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from pico_agent.discord_bridge import (
    DiscordDisabledError,
    PicoBot,
    TextChannelHandler,
    VoiceChannelListener,
    is_discord_available,
)
from pico_agent.discord_bridge.text_channel import IncomingMessage
from pico_agent.discord_bridge.voice_channel import VoiceChannelState


# ── availability ────────────────────────────────────────────────────────


def test_is_discord_available_returns_bool():
    """is_discord_available() は bool を返す (discord.py 有無に関わらず例外なし)。"""
    result = is_discord_available()
    assert isinstance(result, bool)


def test_discord_disabled_error_is_runtime_error():
    """DiscordDisabledError は RuntimeError のサブクラス。"""
    assert issubclass(DiscordDisabledError, RuntimeError)


def test_discord_disabled_error_has_default_message():
    """例外を引数なしで上げた時にデフォルトメッセージが入る。"""
    e = DiscordDisabledError()
    assert "Phase D" in str(e) or "discord.py" in str(e)


# ── PicoBot ─────────────────────────────────────────────────────────────


def test_picobot_is_configured_false_without_env(monkeypatch):
    """token/owner_id/guild_id 未設定なら is_configured=False。"""
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_OWNER_ID", raising=False)
    monkeypatch.delenv("DISCORD_GUILD_ID", raising=False)
    bot = PicoBot()
    assert bot.is_configured is False


def test_picobot_is_configured_true_when_all_env_set(monkeypatch):
    """DISCORD_TOKEN + owner_id + guild_id が揃っていれば is_configured=True。"""
    monkeypatch.setenv("DISCORD_TOKEN", "fake-token")
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.setenv("DISCORD_OWNER_ID", "123456789012345678")
    monkeypatch.setenv("DISCORD_GUILD_ID", "987654321098765432")
    bot = PicoBot()
    assert bot.is_configured is True
    assert bot.token == "fake-token"
    assert bot.owner_id == 123456789012345678
    assert bot.guild_id == 987654321098765432


def test_picobot_legacy_discord_bot_token_fallback(monkeypatch):
    """DISCORD_BOT_TOKEN だけでも (後方互換) token は読める。"""
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "legacy-token")
    monkeypatch.setenv("DISCORD_OWNER_ID", "1")
    monkeypatch.setenv("DISCORD_GUILD_ID", "2")
    bot = PicoBot()
    assert bot.token == "legacy-token"
    assert bot.is_configured is True


def test_picobot_discord_token_takes_precedence_over_legacy(monkeypatch):
    """DISCORD_TOKEN が DISCORD_BOT_TOKEN より優先される。"""
    monkeypatch.setenv("DISCORD_TOKEN", "primary-token")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "legacy-token")
    bot = PicoBot()
    assert bot.token == "primary-token"


def test_picobot_invalid_owner_id_falls_back_to_none(monkeypatch):
    """DISCORD_OWNER_ID が int に変換できない場合は None で扱う (silent fail)。"""
    monkeypatch.setenv("DISCORD_OWNER_ID", "not-a-number")
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_GUILD_ID", raising=False)
    bot = PicoBot()
    assert bot.owner_id is None
    assert bot.is_configured is False


def test_picobot_is_running_starts_false():
    """初期化直後は is_running=False。"""
    bot = PicoBot()
    assert bot.is_running is False


@pytest.mark.asyncio
async def test_picobot_start_raises_when_discord_unavailable(monkeypatch):
    """discord.py が import できない時、start() は DiscordDisabledError を投げる。"""
    monkeypatch.setattr(
        "pico_agent.discord_bridge.bot.is_discord_available",
        lambda: False,
    )
    bot = PicoBot()
    with pytest.raises(DiscordDisabledError):
        await bot.start()


@pytest.mark.asyncio
async def test_picobot_start_raises_when_not_configured(monkeypatch):
    """discord.py が存在しても token/owner_id/guild_id 未設定なら例外。"""
    monkeypatch.setattr(
        "pico_agent.discord_bridge.bot.is_discord_available",
        lambda: True,
    )
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_OWNER_ID", raising=False)
    monkeypatch.delenv("DISCORD_GUILD_ID", raising=False)
    bot = PicoBot()
    with pytest.raises(DiscordDisabledError):
        await bot.start()


@pytest.mark.asyncio
async def test_picobot_send_message_always_raises_in_phase_c1():
    """Phase C-1 段階では send_message は常に DiscordDisabledError。"""
    bot = PicoBot()
    with pytest.raises(DiscordDisabledError):
        await bot.send_message(channel_id=12345, content="hi")


@pytest.mark.asyncio
async def test_picobot_stop_is_safe_noop():
    """stop() は例外を投げず副作用なく終了する (Phase D pending)。"""
    bot = PicoBot()
    await bot.stop()
    assert bot.is_running is False


# ── TextChannelHandler.should_handle フィルタ ─────────────────────────


def _make_msg(
    author_id: int,
    *,
    is_bot: bool = False,
    guild_id: int | None = None,
    channel_id: int = 1,
    content: str = "hi",
) -> IncomingMessage:
    return IncomingMessage(
        author_id=author_id,
        author_is_bot=is_bot,
        guild_id=guild_id,
        channel_id=channel_id,
        content=content,
    )


def test_text_handler_ignores_bot_messages():
    """bot からのメッセージは常に無視。"""
    h = TextChannelHandler(owner_id=42, guild_id=100)
    msg = _make_msg(author_id=42, is_bot=True, guild_id=100)
    assert h.should_handle(msg) is False


def test_text_handler_accepts_owner_anywhere():
    """OWNER からのメッセージは Guild 外でも応答する (DM も OK)。"""
    h = TextChannelHandler(owner_id=42, guild_id=100)
    msg = _make_msg(author_id=42, guild_id=None)  # DM 想定
    assert h.should_handle(msg) is True


def test_text_handler_accepts_non_owner_in_correct_guild():
    """OWNER 以外でも GUILD_ID 内チャンネルなら応答する。"""
    h = TextChannelHandler(owner_id=42, guild_id=100)
    msg = _make_msg(author_id=999, guild_id=100)
    assert h.should_handle(msg) is True


def test_text_handler_rejects_non_owner_outside_guild():
    """OWNER 以外で GUILD_ID 外 (DM / 別 Guild) なら無視。"""
    h = TextChannelHandler(owner_id=42, guild_id=100)
    msg = _make_msg(author_id=999, guild_id=200)  # 別 Guild
    assert h.should_handle(msg) is False


def test_text_handler_rejects_dm_from_non_owner():
    """OWNER 以外からの DM (guild_id=None) は無視。"""
    h = TextChannelHandler(owner_id=42, guild_id=100)
    msg = _make_msg(author_id=999, guild_id=None)
    assert h.should_handle(msg) is False


def test_text_handler_safe_default_when_unconfigured():
    """owner_id/guild_id が両方 None なら安全側で全無視。"""
    h = TextChannelHandler(owner_id=None, guild_id=None)
    msg = _make_msg(author_id=42, guild_id=100)
    assert h.should_handle(msg) is False


@pytest.mark.asyncio
async def test_text_handler_handle_invokes_callback():
    """should_handle が True かつ on_input がセットされていれば callback が呼ばれる。"""
    on_input = AsyncMock(return_value="ピコだよ")
    h = TextChannelHandler(owner_id=42, guild_id=100, on_input=on_input)
    msg = _make_msg(author_id=42, guild_id=100, content="やあ")
    result = await h.handle(msg)
    assert result == "ピコだよ"
    on_input.assert_awaited_once_with("42", "やあ")


@pytest.mark.asyncio
async def test_text_handler_handle_returns_none_for_filtered_message():
    """フィルタアウトされたメッセージは callback を呼ばず None を返す。"""
    on_input = AsyncMock()
    h = TextChannelHandler(owner_id=42, guild_id=100, on_input=on_input)
    msg = _make_msg(author_id=999, guild_id=200)  # フィルタアウト
    result = await h.handle(msg)
    assert result is None
    on_input.assert_not_awaited()


@pytest.mark.asyncio
async def test_text_handler_handle_warns_when_no_callback():
    """on_input が未設定だと None を返して warning ログ。"""
    h = TextChannelHandler(owner_id=42, guild_id=100, on_input=None)
    msg = _make_msg(author_id=42, guild_id=100)
    result = await h.handle(msg)
    assert result is None


# ── VoiceChannelListener ─────────────────────────────────────────────


def test_voice_listener_initial_state_disconnected():
    """初期状態は DISCONNECTED。"""
    listener = VoiceChannelListener()
    assert listener.state == VoiceChannelState.DISCONNECTED
    assert listener.current_channel_id is None


@pytest.mark.asyncio
async def test_voice_listener_join_raises_when_discord_unavailable(monkeypatch):
    """discord.py 未インストール時、join() は DiscordDisabledError。"""
    monkeypatch.setattr(
        "pico_agent.discord_bridge.voice_channel.is_discord_available",
        lambda: False,
    )
    listener = VoiceChannelListener()
    with pytest.raises(DiscordDisabledError):
        await listener.join(channel_id=12345)


@pytest.mark.asyncio
async def test_voice_listener_leave_is_safe_noop():
    """leave() は例外を投げない (初期状態でも、再呼び出しでも)。"""
    listener = VoiceChannelListener()
    await listener.leave()
    assert listener.state == VoiceChannelState.DISCONNECTED
    await listener.leave()  # 二回目も安全
    assert listener.state == VoiceChannelState.DISCONNECTED


@pytest.mark.asyncio
async def test_voice_listener_speak_returns_false_when_not_listening():
    """state=DISCONNECTED で speak() を呼ぶと False を返す (副作用なし)。"""
    listener = VoiceChannelListener(tts_callable=AsyncMock(return_value=b"WAV"))
    result = await listener.speak("hi")
    assert result is False


@pytest.mark.asyncio
async def test_voice_listener_speak_returns_false_when_tts_not_configured():
    """tts_callable 未設定なら speak() は False を返す。"""
    listener = VoiceChannelListener()
    listener._state = VoiceChannelState.LISTENING  # 状態を強制設定
    result = await listener.speak("hi")
    assert result is False


@pytest.mark.asyncio
async def test_voice_listener_speak_calls_tts_and_returns_true_when_listening():
    """state=LISTENING かつ tts_callable があれば speak() は True を返す。"""
    tts = AsyncMock(return_value=b"WAV_DATA")
    listener = VoiceChannelListener(tts_callable=tts)
    listener._state = VoiceChannelState.LISTENING
    result = await listener.speak("こんにちは")
    assert result is True
    tts.assert_awaited_once_with("こんにちは")
    # speak 後に LISTENING に戻る
    assert listener.state == VoiceChannelState.LISTENING


@pytest.mark.asyncio
async def test_voice_listener_audio_chunk_skips_when_stt_not_configured():
    """stt_callable 未設定だと _on_audio_chunk は何もせず終了。"""
    on_speech = AsyncMock()
    listener = VoiceChannelListener(on_speech_detected=on_speech)
    await listener._on_audio_chunk("user1", b"audio")
    on_speech.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_listener_audio_chunk_skips_empty_transcript():
    """STT が空テキストを返したら on_speech_detected は呼ばれない。"""
    stt = AsyncMock(return_value="")
    on_speech = AsyncMock()
    listener = VoiceChannelListener(
        stt_callable=stt, on_speech_detected=on_speech
    )
    await listener._on_audio_chunk("user1", b"audio")
    stt.assert_awaited_once()
    on_speech.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_listener_audio_chunk_pipes_stt_to_on_speech():
    """STT が text を返したら on_speech_detected が呼ばれる。"""
    stt = AsyncMock(return_value="こんにちは")
    on_speech = AsyncMock(return_value="やあ")
    tts = AsyncMock(return_value=b"WAV")
    listener = VoiceChannelListener(
        stt_callable=stt,
        on_speech_detected=on_speech,
        tts_callable=tts,
    )
    listener._state = VoiceChannelState.LISTENING
    await listener._on_audio_chunk("user1", b"audio_bytes")
    stt.assert_awaited_once_with(b"audio_bytes")
    on_speech.assert_awaited_once_with("user1", "こんにちは")
    # 応答 "やあ" → speak() → tts も呼ばれる
    tts.assert_awaited_once_with("やあ")
