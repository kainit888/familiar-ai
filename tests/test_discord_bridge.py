"""Tests for pico_agent.discord_bridge (Phase D スケルトン)。

Phase C-1 完了時点では discord.py 未インストール想定で:
  - availability check が False を返す
  - PicoBot.start() が DiscordDisabledError を投げる
  - TextChannelHandler.should_handle のフィルタロジックが正しい
  - VoiceChannelListener の状態遷移と STT/TTS パイプライン
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pico_agent.discord_bridge import (
    DiscordDisabledError,
    PicoBot,
    TextChannelHandler,
    VoiceChannelListener,
    is_discord_available,
)
from pico_agent.discord_bridge.availability import last_import_error
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


def test_discord_disabled_error_accepts_custom_message():
    """例外コンストラクタに任意メッセージを渡せる (Phase D で接続失敗詳細を載せる前提)。"""
    e = DiscordDisabledError("custom: voice client missing")
    assert "custom: voice client missing" in str(e)


def test_last_import_error_consistent_with_availability():
    """last_import_error() は is_discord_available() の直近結果と整合する。

    - 利用可能なら None、利用不可なら非空 str (どちらも本実装で許容)。
    - 公開診断 API として例外を投げず一貫した型を返すことを保証。
    """
    available = is_discord_available()
    err = last_import_error()
    if available:
        assert err is None
    else:
        assert isinstance(err, str) and len(err) > 0


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


# ── .env から読めることの smoke テスト (値そのものは expose しない) ──────


def test_env_file_contains_discord_credentials_smoke():
    """プロジェクトの .env に DISCORD_TOKEN/OWNER_ID/GUILD_ID が存在すること。

    値そのものはテスト出力に出さない (機密)。キーの存在と非空であることだけ確認。
    .env が無い CI 等の環境では skip。
    """
    from pathlib import Path

    from dotenv import dotenv_values

    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        pytest.skip(f".env not found at {env_path} (CI / fresh checkout)")

    values = dotenv_values(env_path)
    for key in ("DISCORD_TOKEN", "DISCORD_GUILD_ID", "DISCORD_OWNER_ID"):
        assert key in values, f"{key} missing from .env"
        v = values[key]
        assert v is not None and v.strip() != "", f"{key} present but empty in .env"


def test_picobot_reads_token_when_env_loaded_from_dotenv(monkeypatch, tmp_path):
    """.env の内容を os.environ に load_dotenv した時、PicoBot がそれを拾うこと。

    実 .env は触らず、fake .env を作って load_dotenv → PicoBot 初期化の流れを mock。
    """
    from dotenv import load_dotenv

    fake_env = tmp_path / ".env"
    fake_env.write_text(
        "DISCORD_TOKEN=mock-discord-token-for-test\n"
        "DISCORD_OWNER_ID=111\n"
        "DISCORD_GUILD_ID=222\n",
        encoding="utf-8",
    )

    # 既存値を消して、fake .env からのみ読む
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_OWNER_ID", raising=False)
    monkeypatch.delenv("DISCORD_GUILD_ID", raising=False)

    load_dotenv(fake_env, override=True)
    try:
        bot = PicoBot()
        assert bot.token == "mock-discord-token-for-test"
        assert bot.owner_id == 111
        assert bot.guild_id == 222
        assert bot.is_configured is True
    finally:
        # tear-down: 他のテストに影響を残さない
        monkeypatch.delenv("DISCORD_TOKEN", raising=False)
        monkeypatch.delenv("DISCORD_OWNER_ID", raising=False)
        monkeypatch.delenv("DISCORD_GUILD_ID", raising=False)


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
async def test_voice_listener_speak_returns_false_without_voice_client():
    """state=LISTENING かつ tts 成功でも、voice_client が無ければ False。"""
    tts = AsyncMock(return_value=b"WAV_DATA")
    listener = VoiceChannelListener(tts_callable=tts)
    listener._state = VoiceChannelState.LISTENING
    # voice_client は None のまま
    result = await listener.speak("こんにちは")
    assert result is False
    tts.assert_awaited_once_with("こんにちは")
    # speak 後に LISTENING に戻る (finally)
    assert listener.state == VoiceChannelState.LISTENING


@pytest.mark.asyncio
async def test_voice_listener_speak_returns_true_with_mock_voice_client(monkeypatch):
    """voice_client があれば speak() は True を返す (mock 再生)。"""
    tts = AsyncMock(return_value=b"WAV_DATA")
    listener = VoiceChannelListener(tts_callable=tts)
    listener._state = VoiceChannelState.LISTENING

    # voice_client を MagicMock で差し込み
    mock_vc = MagicMock()
    mock_vc.play = MagicMock()
    listener._voice_client = mock_vc

    # _play_audio_via_voice_client を直接モック (discord.py 非依存)
    async def fake_play(wav_bytes):
        return True

    monkeypatch.setattr(listener, "_play_audio_via_voice_client", fake_play)

    result = await listener.speak("こんにちは")
    assert result is True
    tts.assert_awaited_once_with("こんにちは")
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
async def test_voice_listener_audio_chunk_pipes_stt_to_on_speech(monkeypatch):
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

    # voice_client を mock し _play_audio_via_voice_client を no-op に置換
    listener._voice_client = MagicMock()

    async def fake_play(wav_bytes):
        return True

    monkeypatch.setattr(listener, "_play_audio_via_voice_client", fake_play)

    await listener._on_audio_chunk("user1", b"audio_bytes")
    stt.assert_awaited_once_with(b"audio_bytes")
    on_speech.assert_awaited_once_with("user1", "こんにちは")
    # 応答 "やあ" → speak() → tts も呼ばれる
    tts.assert_awaited_once_with("やあ")


# ── 外出期間タスク C: Phase D 本実装 mock テスト ───────────────────────


# incoming_message_from_discord (duck typing) ─────────────────────


def test_incoming_message_from_discord_with_full_attrs():
    """duck-typed discord.Message 相当オブジェクトから IncomingMessage に変換。"""
    from pico_agent.discord_bridge.text_channel import incoming_message_from_discord

    fake_msg = MagicMock()
    fake_msg.author.id = 42
    fake_msg.author.bot = False
    fake_msg.guild.id = 100
    fake_msg.channel.id = 5
    fake_msg.content = "やあ"

    result = incoming_message_from_discord(fake_msg)
    assert result.author_id == 42
    assert result.author_is_bot is False
    assert result.guild_id == 100
    assert result.channel_id == 5
    assert result.content == "やあ"


def test_incoming_message_from_discord_dm_has_no_guild():
    """DM の場合 (msg.guild=None) は guild_id=None になる。"""
    from pico_agent.discord_bridge.text_channel import incoming_message_from_discord

    fake_msg = MagicMock()
    fake_msg.author.id = 42
    fake_msg.author.bot = False
    fake_msg.guild = None  # DM
    fake_msg.channel.id = 5
    fake_msg.content = "DM だよ"

    result = incoming_message_from_discord(fake_msg)
    assert result.guild_id is None
    assert result.content == "DM だよ"


def test_incoming_message_from_discord_bot_author_detected():
    """bot からのメッセージは author_is_bot=True。"""
    from pico_agent.discord_bridge.text_channel import incoming_message_from_discord

    fake_msg = MagicMock()
    fake_msg.author.id = 99
    fake_msg.author.bot = True
    fake_msg.guild.id = 1
    fake_msg.channel.id = 1
    fake_msg.content = "Bot ack"

    result = incoming_message_from_discord(fake_msg)
    assert result.author_is_bot is True


def test_incoming_message_from_discord_content_none_treated_as_empty():
    """content が None でも空文字として扱う。"""
    from pico_agent.discord_bridge.text_channel import incoming_message_from_discord

    fake_msg = MagicMock()
    fake_msg.author.id = 1
    fake_msg.author.bot = False
    fake_msg.guild.id = 1
    fake_msg.channel.id = 1
    fake_msg.content = None

    result = incoming_message_from_discord(fake_msg)
    assert result.content == ""


def test_incoming_message_from_discord_missing_required_raises():
    """author.id がなければ AttributeError。"""
    from pico_agent.discord_bridge.text_channel import incoming_message_from_discord

    fake_msg = MagicMock()
    fake_msg.author = MagicMock(spec=[])  # id 属性なし
    fake_msg.channel.id = 1
    fake_msg.content = ""

    with pytest.raises(AttributeError):
        incoming_message_from_discord(fake_msg)


# PicoBot.start() / stop() / send_message() の mock client テスト ────


@pytest.mark.asyncio
async def test_picobot_start_initializes_client_when_configured(monkeypatch):
    """設定揃 + discord.py mock で start() が discord.Client.start() を呼ぶ。"""
    monkeypatch.setenv("DISCORD_TOKEN", "fake-token")
    monkeypatch.setenv("DISCORD_OWNER_ID", "42")
    monkeypatch.setenv("DISCORD_GUILD_ID", "100")

    # is_discord_available を True に
    monkeypatch.setattr(
        "pico_agent.discord_bridge.bot.is_discord_available",
        lambda: True,
    )

    # _build_intents で discord.Intents を mock
    mock_intents = MagicMock(name="intents")
    monkeypatch.setattr(
        "pico_agent.discord_bridge.bot._build_intents",
        lambda: mock_intents,
    )

    # discord.Client クラスを mock
    mock_client_instance = MagicMock(name="discord_client")
    mock_client_instance.start = AsyncMock()
    mock_client_instance.event = lambda func: func  # decorator passthrough

    mock_discord_module = MagicMock()
    mock_discord_module.Client = MagicMock(return_value=mock_client_instance)

    monkeypatch.setitem(__import__("sys").modules, "discord", mock_discord_module)

    bot = PicoBot()
    await bot.start()

    # discord.Client が intents 引数で生成された
    mock_discord_module.Client.assert_called_once_with(intents=mock_intents)
    # Client.start(token) が呼ばれた
    mock_client_instance.start.assert_awaited_once_with("fake-token")


@pytest.mark.asyncio
async def test_picobot_send_message_uses_get_channel_first(monkeypatch):
    """send_message: キャッシュにあるチャンネルを get_channel で取得。"""
    bot = PicoBot()
    mock_client = MagicMock()
    mock_channel = MagicMock()
    mock_channel.send = AsyncMock()
    mock_client.get_channel = MagicMock(return_value=mock_channel)
    bot._client = mock_client

    result = await bot.send_message(channel_id=999, content="hello")

    assert result is True
    mock_client.get_channel.assert_called_once_with(999)
    mock_channel.send.assert_awaited_once_with("hello")


@pytest.mark.asyncio
async def test_picobot_send_message_falls_back_to_fetch_channel(monkeypatch):
    """get_channel が None を返したら fetch_channel に fallback。"""
    bot = PicoBot()
    mock_client = MagicMock()
    mock_channel = MagicMock()
    mock_channel.send = AsyncMock()
    mock_client.get_channel = MagicMock(return_value=None)
    mock_client.fetch_channel = AsyncMock(return_value=mock_channel)
    bot._client = mock_client

    result = await bot.send_message(channel_id=999, content="hello")

    assert result is True
    mock_client.fetch_channel.assert_awaited_once_with(999)
    mock_channel.send.assert_awaited_once_with("hello")


@pytest.mark.asyncio
async def test_picobot_send_message_empty_content_returns_false():
    """空文字 / 空白のみは送信しない (False)。"""
    bot = PicoBot()
    bot._client = MagicMock()
    result = await bot.send_message(channel_id=1, content="")
    assert result is False
    result2 = await bot.send_message(channel_id=1, content="   \n  ")
    assert result2 is False


@pytest.mark.asyncio
async def test_picobot_send_message_no_client_raises():
    """client が None なら DiscordDisabledError。"""
    bot = PicoBot()
    bot._client = None
    with pytest.raises(DiscordDisabledError):
        await bot.send_message(channel_id=1, content="hi")


@pytest.mark.asyncio
async def test_picobot_send_message_silent_fail_on_channel_send_error():
    """channel.send() が例外を投げても raise せず False を返す。"""
    bot = PicoBot()
    mock_client = MagicMock()
    mock_channel = MagicMock()
    mock_channel.send = AsyncMock(side_effect=RuntimeError("Discord API error"))
    mock_client.get_channel = MagicMock(return_value=mock_channel)
    bot._client = mock_client

    result = await bot.send_message(channel_id=1, content="hi")
    assert result is False


@pytest.mark.asyncio
async def test_picobot_stop_closes_client(monkeypatch):
    """stop() で client.close() が awaited される。"""
    bot = PicoBot()
    mock_client = MagicMock()
    mock_client.close = AsyncMock()
    bot._client = mock_client
    bot._is_running = True

    await bot.stop()
    mock_client.close.assert_awaited_once()
    assert bot.is_running is False
    assert bot._client is None


@pytest.mark.asyncio
async def test_picobot_stop_safe_when_client_close_raises():
    """client.close() が例外でも stop() は raise しない。"""
    bot = PicoBot()
    mock_client = MagicMock()
    mock_client.close = AsyncMock(side_effect=ConnectionError("network"))
    bot._client = mock_client

    await bot.stop()
    assert bot._client is None


@pytest.mark.asyncio
async def test_picobot_start_in_background_returns_task(monkeypatch):
    """start_in_background() は asyncio.Task を返す。"""
    import asyncio as _asyncio

    monkeypatch.setenv("DISCORD_TOKEN", "fake-token")
    monkeypatch.setenv("DISCORD_OWNER_ID", "42")
    monkeypatch.setenv("DISCORD_GUILD_ID", "100")
    # is_discord_available を True に + start を即終了 mock
    monkeypatch.setattr(
        "pico_agent.discord_bridge.bot.is_discord_available", lambda: True
    )

    bot = PicoBot()
    bot.start = AsyncMock()  # start() を即終了 mock に

    task = await bot.start_in_background()
    assert isinstance(task, _asyncio.Task)
    await task  # 即終了


@pytest.mark.asyncio
async def test_picobot_start_in_background_returns_existing_if_already_running(monkeypatch):
    """既に起動中なら同じ task を返す (二重起動防止)。"""
    import asyncio as _asyncio

    monkeypatch.setattr(
        "pico_agent.discord_bridge.bot.is_discord_available", lambda: True
    )

    bot = PicoBot()

    started = False

    async def long_running():
        nonlocal started
        started = True
        await _asyncio.sleep(10)  # long task

    bot.start = long_running

    task1 = await bot.start_in_background()
    task2 = await bot.start_in_background()
    assert task1 is task2  # 同じ task

    task1.cancel()
    try:
        await task1
    except _asyncio.CancelledError:
        pass


# VoiceChannelListener.join() with mock client ───────────────────


@pytest.mark.asyncio
async def test_voice_listener_join_raises_when_no_client(monkeypatch):
    """client=None で join() を呼ぶと DiscordDisabledError。"""
    monkeypatch.setattr(
        "pico_agent.discord_bridge.voice_channel.is_discord_available",
        lambda: True,
    )
    listener = VoiceChannelListener()
    with pytest.raises(DiscordDisabledError):
        await listener.join(channel_id=1, client=None)


@pytest.mark.asyncio
async def test_voice_listener_join_returns_true_on_success(monkeypatch):
    """mock client + mock channel.connect() で join() が True。"""
    monkeypatch.setattr(
        "pico_agent.discord_bridge.voice_channel.is_discord_available",
        lambda: True,
    )
    listener = VoiceChannelListener()

    mock_channel = MagicMock()
    mock_voice_client = MagicMock()
    mock_channel.connect = AsyncMock(return_value=mock_voice_client)

    mock_client = MagicMock()
    mock_client.get_channel = MagicMock(return_value=mock_channel)

    result = await listener.join(channel_id=555, client=mock_client)
    assert result is True
    assert listener.state == VoiceChannelState.LISTENING
    assert listener.current_channel_id == 555
    assert listener._voice_client is mock_voice_client


@pytest.mark.asyncio
async def test_voice_listener_join_falls_back_to_fetch_channel(monkeypatch):
    """get_channel が None なら fetch_channel に fallback。"""
    monkeypatch.setattr(
        "pico_agent.discord_bridge.voice_channel.is_discord_available",
        lambda: True,
    )
    listener = VoiceChannelListener()

    mock_channel = MagicMock()
    mock_voice_client = MagicMock()
    mock_channel.connect = AsyncMock(return_value=mock_voice_client)

    mock_client = MagicMock()
    mock_client.get_channel = MagicMock(return_value=None)
    mock_client.fetch_channel = AsyncMock(return_value=mock_channel)

    result = await listener.join(channel_id=555, client=mock_client)
    assert result is True
    mock_client.fetch_channel.assert_awaited_once_with(555)


@pytest.mark.asyncio
async def test_voice_listener_join_returns_false_when_channel_not_found(monkeypatch):
    """get_channel / fetch_channel が両方 None なら False を返し DISCONNECTED に戻る。"""
    monkeypatch.setattr(
        "pico_agent.discord_bridge.voice_channel.is_discord_available",
        lambda: True,
    )
    listener = VoiceChannelListener()

    mock_client = MagicMock()
    mock_client.get_channel = MagicMock(return_value=None)
    mock_client.fetch_channel = AsyncMock(return_value=None)

    result = await listener.join(channel_id=999, client=mock_client)
    assert result is False
    assert listener.state == VoiceChannelState.DISCONNECTED
    assert listener._voice_client is None


@pytest.mark.asyncio
async def test_voice_listener_join_silent_fail_on_connect_exception(monkeypatch):
    """channel.connect() が例外を投げたら silent fail で False。"""
    monkeypatch.setattr(
        "pico_agent.discord_bridge.voice_channel.is_discord_available",
        lambda: True,
    )
    listener = VoiceChannelListener()

    mock_channel = MagicMock()
    mock_channel.connect = AsyncMock(side_effect=ConnectionError("VC unreachable"))

    mock_client = MagicMock()
    mock_client.get_channel = MagicMock(return_value=mock_channel)

    result = await listener.join(channel_id=1, client=mock_client)
    assert result is False
    assert listener.state == VoiceChannelState.DISCONNECTED


@pytest.mark.asyncio
async def test_voice_listener_join_when_already_listening_leaves_first(monkeypatch):
    """既に LISTENING 状態で join() を呼ぶと、先に leave() してから再接続を試みる。"""
    monkeypatch.setattr(
        "pico_agent.discord_bridge.voice_channel.is_discord_available",
        lambda: True,
    )
    listener = VoiceChannelListener()
    listener._state = VoiceChannelState.LISTENING

    old_vc = MagicMock()
    old_vc.disconnect = AsyncMock()
    listener._voice_client = old_vc
    listener._current_channel_id = 100

    new_channel = MagicMock()
    new_voice_client = MagicMock()
    new_channel.connect = AsyncMock(return_value=new_voice_client)

    mock_client = MagicMock()
    mock_client.get_channel = MagicMock(return_value=new_channel)

    result = await listener.join(channel_id=200, client=mock_client)
    assert result is True
    old_vc.disconnect.assert_awaited_once()
    assert listener.current_channel_id == 200
    assert listener._voice_client is new_voice_client


@pytest.mark.asyncio
async def test_voice_listener_leave_calls_voice_client_disconnect():
    """leave() で voice_client.disconnect() が awaited される。"""
    listener = VoiceChannelListener()
    mock_vc = MagicMock()
    mock_vc.disconnect = AsyncMock()
    listener._voice_client = mock_vc
    listener._state = VoiceChannelState.LISTENING
    listener._current_channel_id = 50

    await listener.leave()
    mock_vc.disconnect.assert_awaited_once()
    assert listener.state == VoiceChannelState.DISCONNECTED
    assert listener._voice_client is None
    assert listener._current_channel_id is None


@pytest.mark.asyncio
async def test_voice_listener_leave_silent_fail_on_disconnect_exception():
    """voice_client.disconnect() で例外が出ても leave() は raise しない。"""
    listener = VoiceChannelListener()
    mock_vc = MagicMock()
    mock_vc.disconnect = AsyncMock(side_effect=ConnectionError("network"))
    listener._voice_client = mock_vc
    listener._state = VoiceChannelState.LISTENING

    await listener.leave()
    assert listener.state == VoiceChannelState.DISCONNECTED
