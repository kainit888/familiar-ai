"""Tests for pico_agent.adapters.vision_qwen3vl (Phase C-1)。

aiohttp HTTP 呼び出しを mock し、describe_scene / parse_scene の
silent fail + 戻り値仕様を検証する。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pico_agent.adapters import vision_qwen3vl


def _make_mock_session(status: int = 200, payload: dict | None = None, text_body: str = ""):
    """aiohttp.ClientSession の async context をスタブで組み立てる。"""
    mock_resp = MagicMock()
    mock_resp.status = status
    if payload is not None:
        mock_resp.json = AsyncMock(return_value=payload)
    mock_resp.text = AsyncMock(return_value=text_body)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


@pytest.mark.asyncio
async def test_describe_scene_returns_text_on_success():
    """qwen3-vl が text を返したらそのまま str で取り出す。"""
    payload = {
        "choices": [
            {"message": {"content": "白い犬がカーペットの上で寝ています。"}}
        ]
    }
    mock_session = _make_mock_session(status=200, payload=payload)

    with patch("pico_agent.adapters.vision_qwen3vl.aiohttp.ClientSession", return_value=mock_session):
        result = await vision_qwen3vl.describe_scene(b"\xff\xd8fake_jpeg", prompt="何が写ってる?")

    assert result == "白い犬がカーペットの上で寝ています。"
    mock_session.post.assert_called_once()


@pytest.mark.asyncio
async def test_describe_scene_empty_bytes_returns_empty_silently():
    """空 image_bytes でも raise せず空文字を返す。"""
    result = await vision_qwen3vl.describe_scene(b"")
    assert result == ""


@pytest.mark.asyncio
async def test_describe_scene_http_error_returns_empty():
    """500 等のサーバエラー時は silent fail で空文字。"""
    mock_session = _make_mock_session(status=500, text_body="internal error")

    with patch("pico_agent.adapters.vision_qwen3vl.aiohttp.ClientSession", return_value=mock_session):
        result = await vision_qwen3vl.describe_scene(b"\xff\xd8x")

    assert result == ""


@pytest.mark.asyncio
async def test_describe_scene_network_exception_returns_empty():
    """aiohttp 接続失敗時は silent fail で空文字。"""
    with patch(
        "pico_agent.adapters.vision_qwen3vl.aiohttp.ClientSession",
        side_effect=Exception("connection refused"),
    ):
        result = await vision_qwen3vl.describe_scene(b"\xff\xd8x")

    assert result == ""


@pytest.mark.asyncio
async def test_parse_scene_extracts_json_object():
    """parse_scene は JSON {description, entities} を辞書で返す。"""
    raw = '{"description": "猫がいる", "entities": ["猫", "椅子"]}'
    payload = {"choices": [{"message": {"content": raw}}]}
    mock_session = _make_mock_session(status=200, payload=payload)

    with patch("pico_agent.adapters.vision_qwen3vl.aiohttp.ClientSession", return_value=mock_session):
        result = await vision_qwen3vl.parse_scene(b"\xff\xd8x")

    assert result == {"description": "猫がいる", "entities": ["猫", "椅子"]}


@pytest.mark.asyncio
async def test_parse_scene_strips_markdown_codefence():
    """```json ... ``` でラップされたレスポンスも JSON 抽出できる。"""
    raw = "```json\n{\"description\": \"窓\", \"entities\": [\"窓\"]}\n```"
    payload = {"choices": [{"message": {"content": raw}}]}
    mock_session = _make_mock_session(status=200, payload=payload)

    with patch("pico_agent.adapters.vision_qwen3vl.aiohttp.ClientSession", return_value=mock_session):
        result = await vision_qwen3vl.parse_scene(b"\xff\xd8x")

    assert result["description"] == "窓"
    assert result["entities"] == ["窓"]


@pytest.mark.asyncio
async def test_parse_scene_returns_fallback_on_invalid_json():
    """JSON でない返答は {'description': '', 'entities': []} を返す。"""
    payload = {"choices": [{"message": {"content": "ただの自然文 (JSON ではない)"}}]}
    mock_session = _make_mock_session(status=200, payload=payload)

    with patch("pico_agent.adapters.vision_qwen3vl.aiohttp.ClientSession", return_value=mock_session):
        result = await vision_qwen3vl.parse_scene(b"\xff\xd8x")

    assert result == {"description": "", "entities": []}


@pytest.mark.asyncio
async def test_parse_scene_empty_bytes_returns_fallback():
    """空 image_bytes は raise せず空 dict を返す。"""
    result = await vision_qwen3vl.parse_scene(b"")
    assert result == {"description": "", "entities": []}


@pytest.mark.asyncio
async def test_post_chat_includes_keep_alive_in_payload(monkeypatch):
    """Ollama 拡張の keep_alive=-1 が payload に含まれる (常時メモリ ON)。"""
    captured: dict = {}

    class _CapturingSession:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def post(self, url, json=None):  # noqa: A002
            captured["url"] = url
            captured["json"] = json
            resp = MagicMock()
            resp.status = 200
            resp.json = AsyncMock(return_value={"choices": [{"message": {"content": "ok"}}]})
            resp.text = AsyncMock(return_value="")
            resp.__aenter__ = AsyncMock(return_value=resp)
            resp.__aexit__ = AsyncMock(return_value=False)
            return resp

    monkeypatch.setattr(vision_qwen3vl.aiohttp, "ClientSession", _CapturingSession)
    monkeypatch.setenv("VISION_BASE_URL", "http://test:11434/v1")
    monkeypatch.setenv("VISION_MODEL", "qwen3-vl:test")

    result = await vision_qwen3vl.describe_scene(b"\xff\xd8data", prompt="hi")

    assert result == "ok"
    assert captured["url"] == "http://test:11434/v1/chat/completions"
    assert captured["json"]["model"] == "qwen3-vl:test"
    assert captured["json"]["keep_alive"] == -1
