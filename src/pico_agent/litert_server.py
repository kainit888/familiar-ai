"""LiteRT-LM + Gemma 4 E2B を OpenAI 互換 FastAPI で公開する utility サーバ (Phase C-11)。

familiar-ai の utility backend (backend.py の OpenAI 互換経路) は
`complete(prompt, max_tokens)` だけを使う。これは単一 user メッセージ
`[{"role":"user","content":prompt}]` を `POST /v1/chat/completions` に送り、
`resp.choices[0].message.content` 1 フィールドだけを読む経路である。
このサーバはその経路を満たす最小の OpenAI 互換サーフェスを LiteRT の
Gemma 4 E2B に対して提供し、`.env` の `UTILITY_BASE_URL` 切替のみで
familiar_agent を 1 行も変えずに qwen2.5:1.5b (Ollama) から差し替えられる。

二層分離設計 (response_filter.py / self_model_filter.py と同型):
    - familiar_agent を一切 import しない (逆 import の片方向性を維持)。
    - pico_agent 自己完結。依存は fastapi / uvicorn / litert_lm のみ。

LiteRT Engine / Conversation は並行駆動安全でないため、推論は
`asyncio.Lock` で直列化し、Engine は起動時に 1 回だけ warm する
(`lifespan` で `app.state.engine` に保持する singleton)。

DI 差し込み点:
    `_engine_factory` / `_build_sampler` は module レベル変数であり、
    テストは `litert_lm` の実 import を回避するためここを monkeypatch する。
    本番では `_real_engine_factory` / `_real_build_sampler` が
    `litert_lm` を関数内で遅延 import して使う。
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from loguru import logger
from pydantic import BaseModel

# max_num_tokens は 2048 以上が必須。小さいと長いプロンプトで
# DYNAMIC_UPDATE_SLICE エラーでクラッシュする (前段 probe で確定)。
_MAX_NUM_TOKENS = 2048


class _Msg(BaseModel):
    role: str
    content: Any  # str、または OpenAI content-parts list


class _ChatRequest(BaseModel):
    model: str | None = None
    messages: list[_Msg]
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None


# ---------------------------------------------------------------------------
# テキスト整形ヘルパー (PoC scripts/dev/litert_utility_dev.py から流用)
# ---------------------------------------------------------------------------
def _flatten_content(content: Any) -> str:
    """plain string か OpenAI content-parts list を受け、text のみ返す。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text", "")))
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return str(content)


def _messages_to_prompt(messages: list[_Msg]) -> str:
    """chat メッセージ列を単一プロンプト文字列に畳む。

    utility 経路は常に単一 user メッセージしか送らないため、その場合は
    passthrough。system/assistant ターンは完全性のため prefix を付ける。
    """
    if len(messages) == 1 and messages[0].role == "user":
        return _flatten_content(messages[0].content)
    lines: list[str] = []
    for m in messages:
        text = _flatten_content(m.content)
        if m.role == "system":
            lines.append(f"[System]\n{text}")
        elif m.role == "assistant":
            lines.append(f"[Assistant]\n{text}")
        else:
            lines.append(text)
    return "\n\n".join(lines)


def _extract_text(resp: Any) -> str:
    """resp shape: {'content': [{'type':'text','text': <body>}]}。"""
    try:
        return resp["content"][0]["text"]
    except Exception:
        return str(resp)


# ---------------------------------------------------------------------------
# DI 差し込み点: 本番実装 (litert_lm を遅延 import)
# ---------------------------------------------------------------------------
def _real_engine_factory(model_path: str) -> Any:
    """LiteRT Engine を構築する本番 factory。

    `litert_lm` を関数内で遅延 import する (テストが module を import せず
    `_engine_factory` を monkeypatch できるようにするため)。
    """
    import litert_lm
    from litert_lm import interfaces

    litert_lm.set_min_log_severity(litert_lm.LogSeverity.ERROR)
    logger.info("[litert] loading engine: {}", model_path)
    t0 = time.time()
    engine = litert_lm.Engine(
        model_path=model_path,
        backend=interfaces.CPU(),
        max_num_tokens=_MAX_NUM_TOKENS,
    )
    logger.info("[litert] engine ready in {:.1f}s", time.time() - t0)
    return engine


def _real_build_sampler(req: _ChatRequest) -> Any:
    """SamplerConfig を構築する本番 helper (litert_lm を遅延 import)。"""
    import litert_lm

    return litert_lm.SamplerConfig(
        top_k=req.top_k if req.top_k is not None else 64,
        top_p=req.top_p if req.top_p is not None else 0.95,
        temperature=req.temperature if req.temperature is not None else 1.0,
    )


# module レベル変数 = テストの差し込み点。
_engine_factory: Callable[[str], Any] = _real_engine_factory
_build_sampler: Callable[[_ChatRequest], Any] = _real_build_sampler


# ---------------------------------------------------------------------------
# FastAPI アプリ
# ---------------------------------------------------------------------------
@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """起動時に Engine を 1 回だけ warm する (singleton)。"""
    model_path = os.environ.get("LITERT_MODEL_PATH", "")
    app.state.engine = None
    app.state.lock = asyncio.Lock()
    app.state.load_error = None
    app.state.model_path = model_path
    try:
        # Engine 構築はブロッキング → to_thread でイベントループを塞がない。
        app.state.engine = await asyncio.to_thread(_engine_factory, model_path)
    except Exception as e:  # noqa: BLE001 — 起動失敗は health 503 で表面化させる
        app.state.load_error = str(e)
        logger.error("[litert] engine load failed: {}", e)
    yield
    # shutdown: Engine は明示 close 不要 (プロセス終了で解放)。


def create_app() -> FastAPI:
    """FastAPI アプリを生成する (テストはこれを TestClient で呼ぶ)。"""
    app = FastAPI(title="pico-agent litert utility server (Phase C-11)", lifespan=_lifespan)

    @app.get("/health")
    async def _health() -> dict[str, Any]:
        engine = getattr(app.state, "engine", None)
        if engine is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "status": "loading_or_failed",
                    "load_error": getattr(app.state, "load_error", None),
                },
            )
        return {
            "status": "ok",
            "model": getattr(app.state, "model_path", None),
            "loaded": True,
        }

    @app.post("/v1/chat/completions")
    async def _chat(req: _ChatRequest) -> dict[str, Any]:
        if not req.messages:
            raise HTTPException(status_code=400, detail="messages must not be empty")

        engine = getattr(app.state, "engine", None)
        if engine is None:
            raise HTTPException(status_code=503, detail="engine not loaded")

        prompt = _messages_to_prompt(req.messages)

        def _run() -> str:
            sampler = _build_sampler(req)
            conv = engine.create_conversation(sampler_config=sampler)
            try:
                resp = conv.send_message(prompt)
                return _extract_text(resp)
            finally:
                try:
                    conv.close()
                except Exception:  # noqa: BLE001 — close 失敗は致命でない
                    pass

        lock: asyncio.Lock = app.state.lock
        try:
            async with lock:
                text = await asyncio.to_thread(_run)
        except Exception as e:  # noqa: BLE001 — 推論失敗は 500 で返す
            logger.error("[litert] inference failed: {}", e)
            raise HTTPException(status_code=500, detail=f"inference failed: {e}") from e

        model_name = req.model or os.path.basename(getattr(app.state, "model_path", "") or "")
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model_name,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    return app


app = create_app()


def main() -> None:
    import uvicorn

    port = int(os.environ.get("LITERT_LISTEN_PORT") or 11435)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
