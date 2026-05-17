"""Tests for pico_agent.adapters.tts_sbv2 (Phase C-1)。

Style-BERT-VITS2 公式 API は GET /voice?text=... を返す WAV bytes を
返す前提 (planner 確定値) で、aiohttp HTTP を mock 検証する。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pico_agent.adapters import tts_sbv2


def _make_mock_session(status: int = 200, audio: bytes = b"FAKE_WAV_BYTES", text_body: str = ""):
    """aiohttp.ClientSession の async context を組み立てて返す。"""
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.read = AsyncMock(return_value=audio)
    mock_resp.text = AsyncMock(return_value=text_body)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


@pytest.mark.asyncio
async def test_speak_returns_wav_bytes_on_success():
    """正常系: WAV bytes が返る。"""
    mock_session = _make_mock_session(status=200, audio=b"RIFF\x24\x00\x00\x00WAVE")

    with patch("pico_agent.adapters.tts_sbv2.aiohttp.ClientSession", return_value=mock_session):
        audio = await tts_sbv2.speak("こんにちは")

    assert audio == b"RIFF\x24\x00\x00\x00WAVE"
    mock_session.get.assert_called_once()


@pytest.mark.asyncio
async def test_speak_empty_text_returns_empty_bytes():
    """空テキストは silent fail で空 bytes (raise しない)。"""
    audio = await tts_sbv2.speak("")
    assert audio == b""

    audio = await tts_sbv2.speak("   ")
    assert audio == b""


@pytest.mark.asyncio
async def test_speak_http_error_returns_empty_bytes():
    """500 等は silent fail で空 bytes。"""
    mock_session = _make_mock_session(status=500, text_body="server error")

    with patch("pico_agent.adapters.tts_sbv2.aiohttp.ClientSession", return_value=mock_session):
        audio = await tts_sbv2.speak("hello")

    assert audio == b""


@pytest.mark.asyncio
async def test_speak_network_exception_returns_empty_bytes():
    """接続失敗も silent fail。"""
    with patch(
        "pico_agent.adapters.tts_sbv2.aiohttp.ClientSession",
        side_effect=Exception("connection refused"),
    ):
        audio = await tts_sbv2.speak("hi")

    assert audio == b""


@pytest.mark.asyncio
async def test_speak_splits_long_text_into_multiple_requests():
    """100 文字超は句読点で分割 → 複数 GET になる。"""
    # 句読点ありで合計 250 文字程度。
    long_text = ("こんにちは。今日は良い天気ですね。" + "ピコは元気です、ええ、本当に元気です。") * 5
    mock_session = _make_mock_session(status=200, audio=b"WAV")

    with patch("pico_agent.adapters.tts_sbv2.aiohttp.ClientSession", return_value=mock_session):
        audio = await tts_sbv2.speak(long_text)

    # 複数チャンクに分かれ、それぞれ b"WAV" が連結される。
    call_count = mock_session.get.call_count
    assert call_count >= 2, f"expected >=2 requests for long text, got {call_count}"
    assert audio == b"WAV" * call_count


@pytest.mark.asyncio
async def test_speak_passes_speaker_id_to_query():
    """speaker_id=3 が URL クエリに含まれる。"""
    captured: dict = {}

    class _CapturingSession:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def get(self, url):
            captured["url"] = url
            resp = MagicMock()
            resp.status = 200
            resp.read = AsyncMock(return_value=b"WAV")
            resp.text = AsyncMock(return_value="")
            resp.__aenter__ = AsyncMock(return_value=resp)
            resp.__aexit__ = AsyncMock(return_value=False)
            return resp

    with patch("pico_agent.adapters.tts_sbv2.aiohttp.ClientSession", _CapturingSession):
        audio = await tts_sbv2.speak("hi", speaker_id=3)

    assert audio == b"WAV"
    assert "speaker_id=3" in captured["url"]
    assert "text=hi" in captured["url"]


@pytest.mark.asyncio
async def test_speak_emotion_affects_style_weight():
    """emotion={'valence': 1.0} → style_weight=1.0 (高揚) がクエリに乗る。"""
    captured: dict = {}

    class _CapturingSession:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def get(self, url):
            captured["url"] = url
            resp = MagicMock()
            resp.status = 200
            resp.read = AsyncMock(return_value=b"WAV")
            resp.text = AsyncMock(return_value="")
            resp.__aenter__ = AsyncMock(return_value=resp)
            resp.__aexit__ = AsyncMock(return_value=False)
            return resp

    with patch("pico_agent.adapters.tts_sbv2.aiohttp.ClientSession", _CapturingSession):
        await tts_sbv2.speak("happy!", emotion={"valence": 1.0, "arousal": 0.5})

    assert "style_weight=1.00" in captured["url"]


@pytest.mark.asyncio
async def test_speak_invalid_target_falls_back_silently():
    """不正な target は warning だけ出して discord_vc にフォールバック。"""
    mock_session = _make_mock_session(status=200, audio=b"WAV")

    with patch("pico_agent.adapters.tts_sbv2.aiohttp.ClientSession", return_value=mock_session):
        audio = await tts_sbv2.speak("hi", target="bogus")

    # raise せず WAV を返す。
    assert audio == b"WAV"


# ── v4.2 14-5: play_with_fallback() のテスト ──────────────────────────


@pytest.mark.asyncio
async def test_play_with_fallback_empty_text():
    """空テキストは即 False を返す。"""
    ok, via = await tts_sbv2.play_with_fallback("")
    assert ok is False
    assert via == "empty_text"


@pytest.mark.asyncio
async def test_play_with_fallback_no_audio_from_sbv2():
    """SBV2 が空 bytes を返したら no_audio を返す。"""
    with patch(
        "pico_agent.adapters.tts_sbv2.speak",
        new=AsyncMock(return_value=b""),
    ):
        ok, via = await tts_sbv2.play_with_fallback("hi")
    assert ok is False
    assert via == "no_audio"


@pytest.mark.asyncio
async def test_play_with_fallback_auto_tries_tapo_first():
    """target=auto では tapo_speaker → main_pc → rpi5 の順で試行する。"""
    call_order: list[str] = []

    async def fake_tapo(audio):
        call_order.append("tapo")
        return False  # tapo は失敗

    async def fake_main_pc(audio):
        call_order.append("main_pc")
        return True  # main_pc で成功

    async def fake_rpi5(audio):
        call_order.append("rpi5")
        return True

    with patch(
        "pico_agent.adapters.tts_sbv2.speak",
        new=AsyncMock(return_value=b"WAV"),
    ), patch(
        "pico_agent.adapters.tts_sbv2._BACKENDS",
        {"tapo_speaker": fake_tapo, "main_pc": fake_main_pc, "rpi5": fake_rpi5},
    ):
        ok, via = await tts_sbv2.play_with_fallback("hi", target="auto")

    assert ok is True
    assert via == "main_pc"
    # 確認: rpi5 までは到達しない (main_pc で成功したので)
    assert call_order == ["tapo", "main_pc"]


@pytest.mark.asyncio
async def test_play_with_fallback_auto_all_failed():
    """全 backend が失敗したら all_failed を返す。"""

    async def always_fail(audio):
        return False

    with patch(
        "pico_agent.adapters.tts_sbv2.speak",
        new=AsyncMock(return_value=b"WAV"),
    ), patch(
        "pico_agent.adapters.tts_sbv2._BACKENDS",
        {
            "tapo_speaker": always_fail,
            "main_pc": always_fail,
            "rpi5": always_fail,
        },
    ):
        ok, via = await tts_sbv2.play_with_fallback("hi", target="auto")

    assert ok is False
    assert via == "all_failed"


@pytest.mark.asyncio
async def test_play_with_fallback_explicit_target_no_chain():
    """target を明示指定したらフォールバックなし (その backend のみ試行)。"""
    call_order: list[str] = []

    async def fake_main_pc(audio):
        call_order.append("main_pc")
        return False

    async def fake_rpi5(audio):
        call_order.append("rpi5")
        return True

    with patch(
        "pico_agent.adapters.tts_sbv2.speak",
        new=AsyncMock(return_value=b"WAV"),
    ), patch(
        "pico_agent.adapters.tts_sbv2._BACKENDS",
        {"main_pc": fake_main_pc, "rpi5": fake_rpi5},
    ):
        ok, via = await tts_sbv2.play_with_fallback("hi", target="main_pc")

    # main_pc 単独試行 → 失敗で all_failed
    assert ok is False
    assert via == "all_failed"
    assert call_order == ["main_pc"]
    # rpi5 にはフォールバックしない


@pytest.mark.asyncio
async def test_play_with_fallback_unknown_target_falls_back_to_auto():
    """未知の target は warning 後 auto チェーンにフォールバック。"""
    call_order: list[str] = []

    async def fake_tapo(audio):
        call_order.append("tapo")
        return True

    async def fake_main_pc(audio):
        call_order.append("main_pc")
        return True

    async def fake_rpi5(audio):
        call_order.append("rpi5")
        return True

    with patch(
        "pico_agent.adapters.tts_sbv2.speak",
        new=AsyncMock(return_value=b"WAV"),
    ), patch(
        "pico_agent.adapters.tts_sbv2._BACKENDS",
        {"tapo_speaker": fake_tapo, "main_pc": fake_main_pc, "rpi5": fake_rpi5},
    ):
        ok, via = await tts_sbv2.play_with_fallback("hi", target="bogus_target")

    assert ok is True
    assert via == "tapo_speaker"  # auto チェーンで tapo 最初
    assert call_order == ["tapo"]


@pytest.mark.asyncio
async def test_play_with_fallback_backend_raising_exception_skipped():
    """backend が例外を投げても次の backend に移る (silent fail)。"""

    async def fake_tapo(audio):
        raise RuntimeError("simulated backend crash")

    async def fake_main_pc(audio):
        return True

    async def fake_rpi5(audio):
        return False

    with patch(
        "pico_agent.adapters.tts_sbv2.speak",
        new=AsyncMock(return_value=b"WAV"),
    ), patch(
        "pico_agent.adapters.tts_sbv2._BACKENDS",
        {"tapo_speaker": fake_tapo, "main_pc": fake_main_pc, "rpi5": fake_rpi5},
    ):
        ok, via = await tts_sbv2.play_with_fallback("hi", target="auto")

    assert ok is True
    assert via == "main_pc"


@pytest.mark.asyncio
async def test_play_via_go2rtc_skipped_when_disabled(monkeypatch):
    """GO2RTC_ENABLED が設定されていない時、_play_via_go2rtc は何もせず False。"""
    monkeypatch.delenv("GO2RTC_ENABLED", raising=False)
    result = await tts_sbv2._play_via_go2rtc(b"WAV")
    assert result is False


@pytest.mark.asyncio
async def test_play_via_go2rtc_empty_bytes_returns_false():
    """空 bytes は GO2RTC_ENABLED に関わらず False を返す。"""
    result = await tts_sbv2._play_via_go2rtc(b"")
    assert result is False


@pytest.mark.asyncio
async def test_play_via_main_pc_no_player_returns_false(monkeypatch):
    """mpv/ffplay のどちらも PATH になければ False。"""
    monkeypatch.setattr("pico_agent.adapters.tts_sbv2._which", lambda name: None)
    result = await tts_sbv2._play_via_main_pc(b"WAV")
    assert result is False


@pytest.mark.asyncio
async def test_play_via_rpi5_no_player_returns_false(monkeypatch):
    """aplay/paplay のどちらも PATH になければ False。"""
    monkeypatch.setattr("pico_agent.adapters.tts_sbv2._which", lambda name: None)
    monkeypatch.setattr("pico_agent.adapters.tts_sbv2.sys.platform", "linux")
    result = await tts_sbv2._play_via_rpi5(b"WAV")
    assert result is False


@pytest.mark.asyncio
async def test_play_via_rpi5_non_linux_skipped(monkeypatch):
    """非 Linux プラットフォームでは _play_via_rpi5 は即 False。"""
    monkeypatch.setattr("pico_agent.adapters.tts_sbv2.sys.platform", "darwin")
    result = await tts_sbv2._play_via_rpi5(b"WAV")
    assert result is False


@pytest.mark.asyncio
async def test_play_via_main_pc_empty_bytes_returns_false():
    """空 bytes はバックエンド呼び出し前に False を返す。"""
    result = await tts_sbv2._play_via_main_pc(b"")
    assert result is False


@pytest.mark.asyncio
async def test_play_via_rpi5_empty_bytes_returns_false():
    """空 bytes はバックエンド呼び出し前に False を返す。"""
    result = await tts_sbv2._play_via_rpi5(b"")
    assert result is False
