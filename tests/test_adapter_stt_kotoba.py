"""Tests for pico_agent.adapters.stt_kotoba (Phase C-1)。

whisper_server.py の /transcribe エンドポイントを aiohttp で叩く想定。
JSON / text/plain 両レスポンスをサポートし、silent fail で空文字を返す。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pico_agent.adapters import stt_kotoba


def _make_mock_session(
    status: int = 200,
    json_payload: dict | None = None,
    text_body: str = "",
    content_type: str = "application/json",
):
    """aiohttp.ClientSession の async context をスタブで用意。"""
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.json = AsyncMock(return_value=json_payload or {})
    mock_resp.text = AsyncMock(return_value=text_body)
    mock_resp.headers = {"Content-Type": content_type}
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


@pytest.mark.asyncio
async def test_transcribe_returns_text_from_json_response():
    """正常系: {"text": "..."} を str で返す。"""
    mock_session = _make_mock_session(
        status=200,
        json_payload={"text": "おはよう、ピコ。"},
        content_type="application/json",
    )

    with patch("pico_agent.adapters.stt_kotoba.aiohttp.ClientSession", return_value=mock_session):
        result = await stt_kotoba.transcribe(b"WAVDATA")

    assert result == "おはよう、ピコ。"
    mock_session.post.assert_called_once()


@pytest.mark.asyncio
async def test_transcribe_supports_plain_text_response():
    """Content-Type: text/plain でも結果テキストを取り出せる。"""
    mock_session = _make_mock_session(
        status=200,
        text_body="hello world\n",
        content_type="text/plain",
    )

    with patch("pico_agent.adapters.stt_kotoba.aiohttp.ClientSession", return_value=mock_session):
        result = await stt_kotoba.transcribe(b"WAVDATA")

    assert result == "hello world"


@pytest.mark.asyncio
async def test_transcribe_empty_audio_returns_empty_silently():
    """空 audio_bytes は silent fail で空文字。"""
    result = await stt_kotoba.transcribe(b"")
    assert result == ""


@pytest.mark.asyncio
async def test_transcribe_http_error_returns_empty():
    """500 等は silent fail で空文字。"""
    mock_session = _make_mock_session(
        status=503,
        text_body="busy",
        content_type="text/plain",
    )

    with patch("pico_agent.adapters.stt_kotoba.aiohttp.ClientSession", return_value=mock_session):
        result = await stt_kotoba.transcribe(b"WAVDATA")

    assert result == ""


@pytest.mark.asyncio
async def test_transcribe_network_exception_returns_empty():
    """aiohttp 接続失敗時は silent fail で空文字 (raise しない)。"""
    with patch(
        "pico_agent.adapters.stt_kotoba.aiohttp.ClientSession",
        side_effect=Exception("connection refused"),
    ):
        result = await stt_kotoba.transcribe(b"WAVDATA")

    assert result == ""


@pytest.mark.asyncio
async def test_transcribe_uses_env_base_url(monkeypatch):
    """STT_BASE_URL が反映される。"""
    captured: dict = {}

    class _CapturingSession:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def post(self, url, data=None):
            captured["url"] = url
            captured["data"] = data
            resp = MagicMock()
            resp.status = 200
            resp.json = AsyncMock(return_value={"text": "ok"})
            resp.text = AsyncMock(return_value="")
            resp.headers = {"Content-Type": "application/json"}
            resp.__aenter__ = AsyncMock(return_value=resp)
            resp.__aexit__ = AsyncMock(return_value=False)
            return resp

    monkeypatch.setenv("STT_BASE_URL", "http://example.test:9000")
    monkeypatch.setattr(stt_kotoba.aiohttp, "ClientSession", _CapturingSession)

    result = await stt_kotoba.transcribe(b"AUDIO", sample_rate=16000)

    # /transcribe が末尾に自動補完される。
    assert captured["url"] == "http://example.test:9000/transcribe"
    assert result == "ok"


@pytest.mark.asyncio
async def test_transcribe_includes_sample_rate_field(monkeypatch):
    """sample_rate=22050 が FormData の add_field で送られる。"""
    captured_fields: list = []

    class _FakeForm:
        def __init__(self):
            self._fields = captured_fields

        def add_field(self, name, value, **kwargs):
            self._fields.append((name, value, kwargs))

    monkeypatch.setattr(stt_kotoba.aiohttp, "FormData", _FakeForm)

    mock_session = _make_mock_session(
        status=200,
        json_payload={"text": "ok"},
        content_type="application/json",
    )
    with patch("pico_agent.adapters.stt_kotoba.aiohttp.ClientSession", return_value=mock_session):
        await stt_kotoba.transcribe(b"AUDIO", sample_rate=22050)

    sr_fields = [f for f in captured_fields if f[0] == "sample_rate"]
    assert len(sr_fields) == 1
    assert sr_fields[0][1] == "22050"


# ── v4.2 14-4: start_rtsp_subscription() スケルトンのテスト ─────────────


@pytest.mark.asyncio
async def test_start_rtsp_subscription_no_url_returns_noop_task(monkeypatch):
    """STT_RTSP_URL 未設定・引数も None なら no-op タスクを返す。"""
    monkeypatch.delenv("STT_RTSP_URL", raising=False)

    async def _dummy_on_speech(text: str) -> None:
        pass

    task = await stt_kotoba.start_rtsp_subscription(_dummy_on_speech)
    # no-op タスクは即終了する
    await task
    assert task.done()


@pytest.mark.asyncio
async def test_start_rtsp_subscription_no_ffmpeg_returns_noop_task(monkeypatch):
    """ffmpeg が PATH になければ no-op タスクを返す。"""
    monkeypatch.setenv("STT_RTSP_URL", "rtsp://fake:fake@example/stream1")
    monkeypatch.setattr(
        "pico_agent.adapters.stt_kotoba.shutil.which",
        lambda name: None,
    )

    async def _dummy_on_speech(text: str) -> None:
        pass

    task = await stt_kotoba.start_rtsp_subscription(_dummy_on_speech)
    await task
    assert task.done()


@pytest.mark.asyncio
async def test_start_rtsp_subscription_no_vad_returns_noop_task(monkeypatch):
    """silero-vad / torch 未インストールなら no-op タスクを返す。"""
    monkeypatch.setenv("STT_RTSP_URL", "rtsp://fake:fake@example/stream1")
    # ffmpeg は存在することにする
    monkeypatch.setattr(
        "pico_agent.adapters.stt_kotoba.shutil.which",
        lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None,
    )
    monkeypatch.setattr(
        "pico_agent.adapters.stt_kotoba._silero_vad_available",
        lambda: False,
    )

    async def _dummy_on_speech(text: str) -> None:
        pass

    task = await stt_kotoba.start_rtsp_subscription(_dummy_on_speech)
    await task
    assert task.done()


@pytest.mark.asyncio
async def test_start_rtsp_subscription_all_deps_present_still_noop(monkeypatch):
    """全依存が揃っていても Phase C-1 段階では本実装が無いので no-op で抜ける。

    将来 (Phase D 以降) 本実装を入れたら、このテストは on_speech が呼ばれる
    か `transcribe()` が呼ばれるかを検証する形に書き換える。
    """
    monkeypatch.setenv("STT_RTSP_URL", "rtsp://fake:fake@example/stream1")
    monkeypatch.setattr(
        "pico_agent.adapters.stt_kotoba.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )
    monkeypatch.setattr(
        "pico_agent.adapters.stt_kotoba._silero_vad_available",
        lambda: True,
    )

    calls: list[str] = []

    async def _on_speech(text: str) -> None:
        calls.append(text)

    task = await stt_kotoba.start_rtsp_subscription(_on_speech)
    await task
    # Phase C-1 では on_speech は呼ばれない (本実装未着手)
    assert calls == []
    assert task.done()


@pytest.mark.asyncio
async def test_start_rtsp_subscription_returns_task_object(monkeypatch):
    """戻り値は asyncio.Task インスタンス (cancel 可能であること)。"""
    monkeypatch.delenv("STT_RTSP_URL", raising=False)

    async def _dummy_on_speech(text: str) -> None:
        pass

    task = await stt_kotoba.start_rtsp_subscription(_dummy_on_speech)
    import asyncio

    assert isinstance(task, asyncio.Task)
    # task.cancel() が呼べる (no-op 終了済みなら何もしない)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_start_rtsp_subscription_with_explicit_url(monkeypatch):
    """rtsp_url を明示指定したら環境変数より優先される (依存チェックは通る前提)。"""
    monkeypatch.delenv("STT_RTSP_URL", raising=False)
    monkeypatch.setattr(
        "pico_agent.adapters.stt_kotoba.shutil.which",
        lambda name: None,  # ffmpeg なし → no-op
    )

    async def _dummy_on_speech(text: str) -> None:
        pass

    # rtsp_url 指定で URL チェックは突破、ffmpeg チェックで no-op
    task = await stt_kotoba.start_rtsp_subscription(
        _dummy_on_speech,
        rtsp_url="rtsp://explicit:url@example/stream1",
    )
    await task
    assert task.done()
