"""Vision adapter (qwen3-vl on Ollama) - 設計書 v4.0 第 7-4 章。

メイン PC (192.168.10.104:11434) で稼働する Ollama の qwen3-vl:4b に
画像を投げて「シーン記述」「構造化エンティティ抽出」を返す。

I/F (設計書 7-4 章):
    async def describe_scene(image_bytes: bytes, prompt: str = "") -> str
    async def parse_scene(image_bytes: bytes) -> dict  # joint_attention 用

エラーハンドリング方針 (planner 確認済み):
    - 例外を raise せず silent fail + logger.warning
    - describe_scene 失敗時は空文字
    - parse_scene 失敗時は空 dict {"description": "", "entities": []}

OLLAMA_KEEP_ALIVE: 常時 ON 推奨 (payload に keep_alive=-1 を含める)。

Phase G で familiar-ai agent.run() の image_b64 経路への完全統合を予定
(Phase C-1 ではモジュール単体実装のみ)。
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

import aiohttp
from loguru import logger

# ── 設定 ─────────────────────────────────────────────────────────────────
# .env 上書き可能なエンドポイント / モデル名。デフォルトはメイン PC を想定。
_DEFAULT_BASE_URL = "http://192.168.10.104:11434/v1"
_DEFAULT_MODEL = "qwen3-vl:4b"
_DEFAULT_TIMEOUT_SEC = 60.0
_PARSE_SCENE_PROMPT = (
    "Look at this image and respond with ONLY a JSON object of the form "
    '{"description": "<one short sentence in Japanese>", '
    '"entities": ["<noun1>", "<noun2>", ...]}. '
    "No extra commentary, no markdown, no code fence. "
    "List up to 8 salient entities."
)


def _get_base_url() -> str:
    """環境変数 VISION_BASE_URL 経由で OpenAI 互換エンドポイントを取得。"""
    return os.environ.get("VISION_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")


def _get_model() -> str:
    """環境変数 VISION_MODEL 経由で qwen3-vl モデル名を取得。"""
    return os.environ.get("VISION_MODEL", _DEFAULT_MODEL)


def _get_timeout() -> float:
    """環境変数 VISION_TIMEOUT_SEC で HTTP timeout 秒数を取得。"""
    raw = os.environ.get("VISION_TIMEOUT_SEC", "")
    try:
        return float(raw) if raw else _DEFAULT_TIMEOUT_SEC
    except ValueError:
        return _DEFAULT_TIMEOUT_SEC


def _build_messages(image_bytes: bytes, prompt: str) -> list[dict[str, Any]]:
    """Ollama OpenAI 互換 chat/completions の image_url payload を組み立てる。"""
    b64 = base64.b64encode(image_bytes).decode("ascii")
    image_url = f"data:image/jpeg;base64,{b64}"
    user_text = prompt.strip() if prompt and prompt.strip() else "What is in this image?"
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": image_url}},
            ],
        }
    ]


async def _post_chat(messages: list[dict[str, Any]]) -> str:
    """qwen3-vl に chat/completions を投げて返答テキストを取り出す (silent fail)。"""
    if not messages:
        return ""

    base_url = _get_base_url()
    model = _get_model()
    timeout = aiohttp.ClientTimeout(total=_get_timeout())
    url = f"{base_url}/chat/completions"
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 512,
        # Ollama 拡張: モデルをメモリに常駐させてレイテンシを抑える。
        "keep_alive": -1,
    }

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(
                        "vision_qwen3vl: HTTP {} from {} body={!r}",
                        resp.status,
                        url,
                        body[:200],
                    )
                    return ""
                data = await resp.json()
    except Exception as e:
        logger.warning("vision_qwen3vl: request failed: {}", e)
        return ""

    try:
        choices = data.get("choices", [])
        if not choices:
            return ""
        message = choices[0].get("message", {})
        content = message.get("content", "")
        if isinstance(content, list):
            # OpenAI multimodal response: pick first text fragment.
            text_parts: list[str] = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text_parts.append(str(part.get("text", "")))
            return "".join(text_parts).strip()
        return str(content).strip()
    except Exception as e:
        logger.warning("vision_qwen3vl: response parse failed: {}", e)
        return ""


async def describe_scene(image_bytes: bytes, prompt: str = "") -> str:
    """画像を qwen3-vl に投げて自然言語のシーン記述を返す (設計書 7-4 章)。

    Args:
        image_bytes: JPEG/PNG エンコード済みの画像 bytes。
        prompt: 任意の追加プロンプト (空のときは「何が写っているか」を尋ねる)。

    Returns:
        モデルの返答テキスト。失敗時は空文字 (例外は投げない)。
    """
    if not image_bytes:
        logger.warning("vision_qwen3vl.describe_scene: empty image_bytes")
        return ""
    messages = _build_messages(image_bytes, prompt)
    return await _post_chat(messages)


async def parse_scene(image_bytes: bytes) -> dict[str, Any]:
    """画像から構造化シーン情報 (description + entities) を抽出する。

    Args:
        image_bytes: JPEG/PNG エンコード済みの画像 bytes。

    Returns:
        ``{"description": str, "entities": list[str]}`` 形式の dict。
        失敗時は ``{"description": "", "entities": []}`` を返す
        (Phase G で joint_attention.ingest_scene_parse 側で確定スキーマ予定)。
    """
    fallback: dict[str, Any] = {"description": "", "entities": []}
    if not image_bytes:
        logger.warning("vision_qwen3vl.parse_scene: empty image_bytes")
        return fallback

    messages = _build_messages(image_bytes, _PARSE_SCENE_PROMPT)
    raw = await _post_chat(messages)
    if not raw:
        return fallback

    # JSON 抽出 (markdown コードフェンス耐性のために { } ペアで切り出す)。
    text = raw.strip()
    if text.startswith("```"):
        # ```json ... ``` 形式を緩く剥がす。
        lines = [ln for ln in text.splitlines() if not ln.startswith("```")]
        text = "\n".join(lines).strip()

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        logger.warning("vision_qwen3vl.parse_scene: no JSON object found in: {!r}", text[:200])
        return fallback

    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        logger.warning("vision_qwen3vl.parse_scene: JSON decode failed: {}", e)
        return fallback

    description = str(parsed.get("description", "")).strip()
    entities_raw = parsed.get("entities", [])
    if isinstance(entities_raw, list):
        entities = [str(item).strip() for item in entities_raw if str(item).strip()]
    else:
        entities = []
    return {"description": description, "entities": entities}
