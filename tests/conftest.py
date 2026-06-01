"""Global pytest configuration for familiar-ai + ピコ独自 helper。

外出期間 Day 2 追加進行 (タスク F): pico_agent.adapters 系テストで重複する
aiohttp mock helper を `make_aiohttp_session_mock` として共通化。既存テスト
ファイル (`test_adapter_*.py`) の `_make_mock_session` は **そのまま残置**
(既存動作維持優先)。新規テストや refactor 時に本 helper を使う想定。
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


# ObservationMemory prewarms heavy embedding threads on init. In the full test
# suite that causes many concurrent daemon loads, noisy progress bars, and
# unstable shutdown behavior. Disable it globally unless a test explicitly opts in.
os.environ.setdefault("FAMILIAR_EMBEDDING_PREWARM", "0")


@pytest.fixture(autouse=True)
def _isolate_emotion_state(tmp_path, monkeypatch):
    """Phase E: redirect boredom / last_interaction persistence to a per-test tmp
    dir so no test ever reads or writes the real ~/.familiar_ai state files.

    Tests that construct FamiliarApp (which builds Boredom()/LastInteraction()
    with default paths) or call interaction/idle methods would otherwise pollute
    the user's runtime state. Tests passing an explicit ``path=`` are unaffected.
    """
    try:
        monkeypatch.setattr(
            "familiar_agent.emotion.boredom._DEFAULT_PATH", tmp_path / "boredom.json"
        )
        monkeypatch.setattr(
            "familiar_agent.emotion.last_interaction._DEFAULT_PATH",
            tmp_path / "last_interaction.json",
        )
    except Exception:
        pass
    yield


# ── ピコ独自 helper: aiohttp mock 共通化 (外出 Day 2 タスク F 産物) ─────


def make_aiohttp_session_mock(
    *,
    status: int = 200,
    json_payload: dict[str, Any] | None = None,
    text_body: str = "",
    bytes_body: bytes | None = None,
    content_type: str | None = None,
    raise_on_request: Exception | None = None,
) -> MagicMock:
    """aiohttp.ClientSession の async-context-manager mock を作るヘルパー。

    Args:
        status: HTTP レスポンスステータス。
        json_payload: ``response.json()`` の戻り値 dict。
        text_body: ``response.text()`` / ``response.read()`` の戻り値。
        bytes_body: ``response.read()`` 専用。``text_body`` より優先。
        content_type: ``response.headers["Content-Type"]``。
            None なら json_payload があれば "application/json"、なければ "text/plain"。
        raise_on_request: 指定された場合、session.get/post/put 時に raise する例外。

    Returns:
        ``aiohttp.ClientSession`` 互換の MagicMock。

    使い方:
        ```python
        from tests.conftest import make_aiohttp_session_mock

        session = make_aiohttp_session_mock(
            status=200, json_payload={"text": "hello"}
        )
        with patch("pkg.module.aiohttp.ClientSession", return_value=session):
            result = await call_some_async_function()
        ```

    既存 `_make_mock_session` との違い:
        - 200 ライン以下のテストでは既存ヘルパーで十分
        - 新規テストや refactor 時はこちらを使うと、エラー系 (`raise_on_request`)
          や bytes_body の指定が dict 引数で 1 行
        - 共通化されているので fix 時 (e.g., aiohttp 3.x → 4.x) に 1 箇所修正で済む
    """
    if content_type is None:
        content_type = "application/json" if json_payload is not None else "text/plain"

    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.json = AsyncMock(return_value=json_payload or {})
    mock_resp.text = AsyncMock(return_value=text_body)
    mock_resp.read = AsyncMock(return_value=bytes_body if bytes_body is not None else text_body.encode())
    mock_resp.headers = {"Content-Type": content_type}
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()

    if raise_on_request is not None:
        def _raise_on_call(*args: Any, **kwargs: Any) -> None:
            raise raise_on_request

        mock_session.get = MagicMock(side_effect=_raise_on_call)
        mock_session.post = MagicMock(side_effect=_raise_on_call)
        mock_session.put = MagicMock(side_effect=_raise_on_call)
    else:
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.put = MagicMock(return_value=mock_resp)

    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


@pytest.fixture
def aiohttp_session_factory():
    """fixture 版: テスト関数内で `aiohttp_session_factory(status=200, ...)` で生成可能。

    使い方:
        ```python
        def test_something(aiohttp_session_factory):
            session = aiohttp_session_factory(status=200, json_payload={"ok": True})
            ...
        ```
    """
    return make_aiohttp_session_mock
