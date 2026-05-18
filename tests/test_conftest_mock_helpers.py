"""conftest.py の共通 mock helper の動作確認 (外出期間 Day 2 タスク F)。

`make_aiohttp_session_mock` / `aiohttp_session_factory` が想定通りに
動作することを確認するためのメタテスト。

これらの helper は将来テストの重複削減のために用意。本ファイル自体は
helper の動作を検証するテストのみ。
"""

from __future__ import annotations

from unittest.mock import patch

import aiohttp
import pytest

from tests.conftest import make_aiohttp_session_mock


# ── 基本動作 ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mock_session_returns_default_200():
    """デフォルト引数で生成すると status=200 のレスポンスを返す。"""
    session = make_aiohttp_session_mock()
    async with session as s:
        async with s.get("http://example.com") as resp:
            assert resp.status == 200


@pytest.mark.asyncio
async def test_mock_session_returns_json_payload():
    """json_payload を指定すると response.json() で取得できる。"""
    session = make_aiohttp_session_mock(
        json_payload={"text": "ピコだよ", "count": 3}
    )
    async with session as s:
        async with s.get("http://example.com") as resp:
            data = await resp.json()
    assert data == {"text": "ピコだよ", "count": 3}


@pytest.mark.asyncio
async def test_mock_session_returns_text_body():
    """text_body を指定すると response.text() で取得できる。"""
    session = make_aiohttp_session_mock(text_body="OK", content_type="text/plain")
    async with session as s:
        async with s.post("http://example.com") as resp:
            body = await resp.text()
    assert body == "OK"


@pytest.mark.asyncio
async def test_mock_session_returns_bytes_body_via_read():
    """bytes_body を指定すると response.read() で取得できる。"""
    session = make_aiohttp_session_mock(bytes_body=b"WAV_DATA_BYTES")
    async with session as s:
        async with s.get("http://example.com") as resp:
            data = await resp.read()
    assert data == b"WAV_DATA_BYTES"


@pytest.mark.asyncio
async def test_mock_session_returns_text_as_bytes_when_no_bytes_body():
    """bytes_body 未指定なら text_body を encode した bytes を read() が返す。"""
    session = make_aiohttp_session_mock(text_body="hello")
    async with session as s:
        async with s.get("http://example.com") as resp:
            data = await resp.read()
    assert data == b"hello"


@pytest.mark.asyncio
async def test_mock_session_supports_status_4xx():
    """status=4xx も指定可能。"""
    session = make_aiohttp_session_mock(status=404, text_body="Not Found")
    async with session as s:
        async with s.get("http://example.com") as resp:
            assert resp.status == 404
            assert await resp.text() == "Not Found"


@pytest.mark.asyncio
async def test_mock_session_supports_status_5xx():
    """status=5xx も指定可能。"""
    session = make_aiohttp_session_mock(status=503, text_body="Service Unavailable")
    async with session as s:
        async with s.put("http://example.com") as resp:
            assert resp.status == 503


# ── content_type 自動推定 ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_mock_session_content_type_defaults_json_when_json_payload():
    """json_payload があると Content-Type は application/json になる。"""
    session = make_aiohttp_session_mock(json_payload={"a": 1})
    async with session as s:
        async with s.get("http://example.com") as resp:
            assert resp.headers["Content-Type"] == "application/json"


@pytest.mark.asyncio
async def test_mock_session_content_type_defaults_text_when_no_json():
    """json_payload なしで text_body のみだと Content-Type は text/plain。"""
    session = make_aiohttp_session_mock(text_body="hello")
    async with session as s:
        async with s.get("http://example.com") as resp:
            assert resp.headers["Content-Type"] == "text/plain"


@pytest.mark.asyncio
async def test_mock_session_explicit_content_type_override():
    """content_type を明示すれば json_payload があっても使われる。"""
    session = make_aiohttp_session_mock(
        json_payload={"a": 1},
        content_type="application/x-custom",
    )
    async with session as s:
        async with s.get("http://example.com") as resp:
            assert resp.headers["Content-Type"] == "application/x-custom"


# ── 例外発生のシミュレーション ────────────────────────────────────


@pytest.mark.asyncio
async def test_mock_session_raise_on_request_get():
    """raise_on_request を指定すると get() で例外が raise される。"""
    session = make_aiohttp_session_mock(
        raise_on_request=ConnectionError("network down")
    )
    with pytest.raises(ConnectionError, match="network down"):
        async with session as s:
            s.get("http://example.com")  # この呼び出しで raise


@pytest.mark.asyncio
async def test_mock_session_raise_on_request_post():
    """raise_on_request は post() でも raise される。"""
    session = make_aiohttp_session_mock(raise_on_request=TimeoutError("slow"))
    with pytest.raises(TimeoutError, match="slow"):
        async with session as s:
            s.post("http://example.com", data=b"x")


@pytest.mark.asyncio
async def test_mock_session_raise_on_request_put():
    """raise_on_request は put() でも raise される (go2rtc 用)。"""
    session = make_aiohttp_session_mock(
        raise_on_request=ConnectionRefusedError("go2rtc not running")
    )
    with pytest.raises(ConnectionRefusedError):
        async with session as s:
            s.put("http://example.com", data=b"WAV")


# ── fixture 版 ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fixture_version_works(aiohttp_session_factory):
    """fixture 版 aiohttp_session_factory が helper として動く。"""
    session = aiohttp_session_factory(
        status=200, json_payload={"from_fixture": True}
    )
    async with session as s:
        async with s.get("http://example.com") as resp:
            assert (await resp.json()) == {"from_fixture": True}


# ── patching での使用例 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_mock_session_works_with_aiohttp_patch():
    """実モジュールが aiohttp.ClientSession を呼ぶ部分に patch できる。"""
    session = make_aiohttp_session_mock(
        status=200, json_payload={"text": "patched"}
    )

    async def call_with_aiohttp():
        async with aiohttp.ClientSession() as s:
            async with s.get("http://example.com") as resp:
                return await resp.json()

    with patch("aiohttp.ClientSession", return_value=session):
        result = await call_with_aiohttp()

    assert result == {"text": "patched"}