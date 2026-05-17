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
