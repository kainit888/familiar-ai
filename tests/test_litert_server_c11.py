"""Phase C-11: LiteRT OpenAI 互換 FastAPI ラッパーのテスト。

`pico_agent.litert_server` を検証する。実 Gemma / litert_lm は一切ロードせず、
autouse fixture が `_engine_factory` と `_build_sampler` を monkeypatch して
FakeEngine を注入する (これにより `litert_lm` の import も発生しない)。
二層分離の確認も兼ね、familiar_agent には依存しない。

mutation 対応 (各テストが捕捉する破壊):
    - Lock 削除 → (b) test_lock_serializes_concurrent_requests が fail
      (並行 send_message が直列化されず max_concurrent>1 になる)
    - singleton 削除 (リクエスト毎に再 init) → (c) test_engine_loaded_once_singleton が fail
    - 推論例外の捕捉削除 (500 に変換しない) → (f) test_engine_exception_500 が fail
    - health の 503 ガード削除 → (d) test_health_503_then_200 が fail
    - max_num_tokens を 2048 未満に緩める → (g) が fail
"""

from __future__ import annotations

import threading
import time
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import pico_agent.litert_server as srv


# ---------------------------------------------------------------------------
# Fake LiteRT objects (実 litert_lm を import しない)
# ---------------------------------------------------------------------------
class FakeConversation:
    def __init__(self, engine: "FakeEngine") -> None:
        self._engine = engine
        self.closed = False

    def send_message(self, prompt: str) -> dict[str, Any]:
        eng = self._engine
        # (b) 入域時に並行カウンタを上げ、Lock が効いていれば常に 1 のはず。
        with eng._counter_lock:
            eng.active += 1
            eng.max_concurrent = max(eng.max_concurrent, eng.active)
            assert eng.active == 1, f"concurrent send_message detected: active={eng.active}"
        try:
            time.sleep(0.05)
        finally:
            with eng._counter_lock:
                eng.active -= 1
        if eng.raise_on_send is not None:
            raise eng.raise_on_send
        return {"content": [{"type": "text", "text": f"echo:{prompt}"}]}

    def close(self) -> None:
        self.closed = True


class FakeEngine:
    def __init__(self, raise_on_send: Exception | None = None) -> None:
        self.active = 0
        self.max_concurrent = 0
        self._counter_lock = threading.Lock()
        self.raise_on_send = raise_on_send

    def create_conversation(self, sampler_config: Any = None) -> FakeConversation:
        return FakeConversation(self)


# ---------------------------------------------------------------------------
# autouse fixture: factory / sampler を差し替え、実 Gemma ロードを防ぐ
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _patch_litert(monkeypatch: pytest.MonkeyPatch) -> Any:
    """既定では成功する FakeEngine factory を注入。

    個別テストは戻り値の "control" 経由で挙動 (失敗 factory / 例外 send) を
    上書きできる。`_build_sampler` も差し替えて litert_lm import を抑止する。
    """
    control: dict[str, Any] = {
        "calls": 0,
        "fail": False,
        "raise_on_send": None,
        "captured_engines": [],
    }

    def fake_factory(model_path: str) -> Any:
        control["calls"] += 1
        if control["fail"]:
            raise RuntimeError("simulated load failure")
        eng = FakeEngine(raise_on_send=control["raise_on_send"])
        control["captured_engines"].append(eng)
        return eng

    monkeypatch.setattr(srv, "_engine_factory", fake_factory)
    monkeypatch.setattr(srv, "_build_sampler", lambda req: object())
    return control


# ---------------------------------------------------------------------------
# (a) OpenAI shape — backend が読む形を保証
# ---------------------------------------------------------------------------
def test_chat_completions_openai_shape(_patch_litert: dict[str, Any]) -> None:
    """200 / choices[0].message.role=='assistant' / content が str。"""
    with TestClient(srv.create_app()) as client:
        r = client.post(
            "/v1/chat/completions",
            json={
                "model": "gemma-4-e2b",
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 50,
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        choice = body["choices"][0]
        assert choice["index"] == 0
        assert choice["finish_reason"] == "stop"
        assert choice["message"]["role"] == "assistant"
        assert isinstance(choice["message"]["content"], str)
        assert choice["message"]["content"] == "echo:hello"


# ---------------------------------------------------------------------------
# (b) Lock が並行リクエストを直列化する
# ---------------------------------------------------------------------------
def test_lock_serializes_concurrent_requests(_patch_litert: dict[str, Any]) -> None:
    """同時 3 リクエストでも engine.max_concurrent == 1 (Lock 効果)。"""
    import concurrent.futures as cf

    with TestClient(srv.create_app()) as client:

        def _hit() -> int:
            r = client.post(
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "x"}]},
            )
            return r.status_code

        with cf.ThreadPoolExecutor(max_workers=3) as ex:
            results = list(ex.map(lambda _: _hit(), range(3)))

        assert all(s == 200 for s in results), results
        # singleton engine は 1 つだけ生成されているはず。
        engines = _patch_litert["captured_engines"]
        assert len(engines) == 1
        assert engines[0].max_concurrent == 1, (
            f"Lock failed to serialize: max_concurrent={engines[0].max_concurrent}"
        )


# ---------------------------------------------------------------------------
# (c) Engine は 1 回だけロードされる (singleton)
# ---------------------------------------------------------------------------
def test_engine_loaded_once_singleton(_patch_litert: dict[str, Any]) -> None:
    """複数リクエスト後も factory 呼び出し回数 == 1。"""
    with TestClient(srv.create_app()) as client:
        for _ in range(3):
            r = client.post(
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "y"}]},
            )
            assert r.status_code == 200, r.text
    assert _patch_litert["calls"] == 1, f"engine re-initialised: calls={_patch_litert['calls']}"


# ---------------------------------------------------------------------------
# (d) health: ロード失敗 → 503 / 成功 → 200
# ---------------------------------------------------------------------------
def test_health_503_then_200(_patch_litert: dict[str, Any]) -> None:
    """失敗 factory で /health 503、成功 factory で 200。"""
    # 失敗ケース
    _patch_litert["fail"] = True
    with TestClient(srv.create_app()) as client:
        r = client.get("/health")
        assert r.status_code == 503, r.text

    # 成功ケース
    _patch_litert["fail"] = False
    with TestClient(srv.create_app()) as client:
        r = client.get("/health")
        assert r.status_code == 200, r.text
        assert r.json()["loaded"] is True


# ---------------------------------------------------------------------------
# (e) messages 欠落 / 空
# ---------------------------------------------------------------------------
def test_missing_messages_400_or_422(_patch_litert: dict[str, Any]) -> None:
    """空 messages → 400、messages キー欠落 → 422 (pydantic validation)。"""
    with TestClient(srv.create_app()) as client:
        r_empty = client.post("/v1/chat/completions", json={"messages": []})
        assert r_empty.status_code == 400, r_empty.text

        r_missing = client.post("/v1/chat/completions", json={"model": "x"})
        assert r_missing.status_code == 422, r_missing.text


# ---------------------------------------------------------------------------
# (f) send_message が raise → 500
# ---------------------------------------------------------------------------
def test_engine_exception_500(_patch_litert: dict[str, Any]) -> None:
    """推論例外は 500 に変換され detail に 'inference failed' を含む。"""
    _patch_litert["raise_on_send"] = ValueError("boom")
    with TestClient(srv.create_app()) as client:
        r = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "z"}]},
        )
        assert r.status_code == 500, r.text
        assert "inference failed" in r.json()["detail"]


# ---------------------------------------------------------------------------
# (g, mutation 用) _real_engine_factory が max_num_tokens>=2048 で Engine を作る
# ---------------------------------------------------------------------------
def test_engine_factory_uses_max_num_tokens_ge_2048(monkeypatch: pytest.MonkeyPatch) -> None:
    """litert_lm.Engine を MagicMock 化し、kwargs の max_num_tokens>=2048 を assert。

    実モデルはロードしない。本番 _real_engine_factory のみを直接呼ぶ。
    """
    fake_litert = MagicMock()
    fake_litert.LogSeverity.ERROR = 4
    fake_interfaces = MagicMock()

    import sys

    monkeypatch.setitem(sys.modules, "litert_lm", fake_litert)
    monkeypatch.setitem(sys.modules, "litert_lm.interfaces", fake_interfaces)
    fake_litert.interfaces = fake_interfaces

    srv._real_engine_factory("x")

    assert fake_litert.Engine.called, "Engine was not constructed"
    _, kwargs = fake_litert.Engine.call_args
    assert "max_num_tokens" in kwargs, f"max_num_tokens missing: {kwargs}"
    assert kwargs["max_num_tokens"] >= 2048, f"max_num_tokens too small: {kwargs['max_num_tokens']}"
