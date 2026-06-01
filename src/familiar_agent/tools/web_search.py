"""Phase F: web search tool — Gemini 3.1 Flash-Lite + Google Search Grounding.

Pico calls ``search_web`` by its own judgment (on a curiosity turn) when it
genuinely wants to know something current/factual. The result (summary +
grounding sources) is saved to observations.db as kind="web_knowledge"; the
heartbeat later offers recent entries as share candidates. Failures are silent
(WARNING log only, Pico stays quiet) — there is no fallback search engine.

SDK: google-genai (new). Mirrors backend.py's call shape but is a STANDALONE
module — the dialogue Gemini path (backend.py) is untouched. The yamnet-style
mock seam (``_CLIENT_OVERRIDE`` / ``client=``) lets tests inject a fake client
so the suite never hits the real API / consumes quota.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from .memory import ObservationMemory

_GROUNDING_MODEL_DEFAULT = "gemini-3.1-flash-lite"
_TRUTHY = ("1", "true", "yes", "on")
_FALSY = ("0", "false", "no", "off")

# Mock seam (tests set this to a FakeClient) + lazy real-client cache.
_CLIENT_OVERRIDE: Any | None = None
_CACHED_CLIENT: Any | None = None
_CLIENT_TRIED = False


@dataclass(frozen=True)
class Source:
    title: str
    uri: str


@dataclass
class WebKnowledge:
    query: str
    summary: str
    sources: list[Source]

    def to_json(self) -> str:
        return json.dumps(
            {
                "query": self.query,
                "summary": self.summary,
                "sources": [{"title": s.title, "uri": s.uri} for s in self.sources],
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_row(cls, content: str) -> "WebKnowledge | None":
        try:
            d = json.loads(content)
            sources = [
                Source(str(s.get("title", "")), str(s.get("uri", "")))
                for s in (d.get("sources") or [])
            ]
            return cls(str(d.get("query", "")), str(d.get("summary", "")), sources)
        except Exception:
            return None


# ── config (env-direct, like the emotion modules; config.py stays lean) ───────
def _grounding_model() -> str:
    return os.environ.get("GROUNDING_MODEL", _GROUNDING_MODEL_DEFAULT)


def _web_search_enabled() -> bool:
    raw = os.environ.get("WEB_SEARCH_ENABLED", "").strip().lower()
    if raw in _FALSY:
        return False
    return True


def _resolve_grounding_key() -> str | None:
    """Resolve a Gemini API key independent of the main PLATFORM.

    Order: GOOGLE_API_KEY → GEMINI_API_KEY → API_KEY (only when PLATFORM=gemini,
    so an anthropic key is never handed to genai).
    """
    for name in ("GOOGLE_API_KEY", "GEMINI_API_KEY"):
        v = os.environ.get(name, "").strip()
        if v:
            return v
    if os.environ.get("PLATFORM", "").strip().lower() == "gemini":
        v = os.environ.get("API_KEY", "").strip()
        if v:
            return v
    return None


def _get_client() -> Any | None:
    global _CACHED_CLIENT, _CLIENT_TRIED
    if _CLIENT_OVERRIDE is not None:
        return _CLIENT_OVERRIDE
    if not _web_search_enabled():
        return None
    if _CLIENT_TRIED:
        return _CACHED_CLIENT
    _CLIENT_TRIED = True
    key = _resolve_grounding_key()
    if not key:
        return None
    try:
        from google import genai

        _CACHED_CLIENT = genai.Client(api_key=key)
    except Exception as e:
        logger.warning("web_search: could not build Gemini client: {}", e)
        _CACHED_CLIENT = None
    return _CACHED_CLIENT


def _parse_response(query: str, resp: Any) -> WebKnowledge | None:
    summary = (getattr(resp, "text", None) or "").strip()
    sources: list[Source] = []
    try:
        cands = getattr(resp, "candidates", None) or []
        meta = getattr(cands[0], "grounding_metadata", None) if cands else None
        for ch in getattr(meta, "grounding_chunks", None) or []:
            web = getattr(ch, "web", None)
            uri = getattr(web, "uri", "") if web else ""
            title = getattr(web, "title", "") if web else ""
            if uri:
                sources.append(Source(str(title), str(uri)))
    except Exception:
        pass
    if not summary and not sources:
        return None
    return WebKnowledge(query=query, summary=summary, sources=sources)


def search_grounding(query: str, *, client: Any | None = None) -> WebKnowledge | None:
    """Run a grounded Gemini search. None on any failure (silent — Pico stays quiet)."""
    c = client or _get_client()
    if c is None:
        return None
    try:
        from google.genai import types

        resp = c.models.generate_content(
            model=_grounding_model(),
            contents=query,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())]
            ),
        )
    except Exception as exc:
        logger.warning("web_search: grounding failed: {}", exc)
        return None
    return _parse_response(query, resp)


def recent_web_knowledge(memory: "ObservationMemory", n: int = 3) -> list[WebKnowledge]:
    """Most-recent web_knowledge entries, parsed (empty list on none/error)."""
    try:
        rows = memory.recall_web_knowledge(n=n)
    except Exception:
        return []
    out: list[WebKnowledge] = []
    for r in rows:
        wk = WebKnowledge.from_row(r.get("content", ""))
        if wk is not None:
            out.append(wk)
    return out


class WebSearchTool:
    """search_web tool — grounded web lookup, results saved to memory."""

    def __init__(self, memory: "ObservationMemory", *, client: Any | None = None) -> None:
        self._memory = memory
        self._client = client  # test override; None → module _get_client()

    @staticmethod
    def available() -> bool:
        """True if a Gemini key is resolvable and the feature is enabled."""
        return _web_search_enabled() and _resolve_grounding_key() is not None

    def get_tool_definitions(self) -> list[dict]:
        return [
            {
                "name": "search_web",
                "description": (
                    "Look something up on the web for current or factual information "
                    "(news, weather, facts you're unsure of). Use only when you "
                    "genuinely want to know — reflecting from memory is also fine. "
                    "Results are saved to your memory automatically."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "What to look up, as a natural-language query.",
                        }
                    },
                    "required": ["query"],
                },
            }
        ]

    async def call(self, tool_name: str, tool_input: dict) -> tuple[str, None]:
        if tool_name != "search_web":
            return f"Unknown tool: {tool_name}", None
        query = (tool_input.get("query") or "").strip()
        if not query:
            return "No query provided.", None
        knowledge = await asyncio.to_thread(search_grounding, query, client=self._client)
        if knowledge is None:
            return "(search unavailable right now)", None  # silent — Pico stays quiet
        try:
            await self._memory.save_async(
                content=knowledge.to_json(),
                kind="web_knowledge",
                emotion="curious",
                direction="web",
            )
        except Exception as e:
            logger.warning("web_search: failed to save web_knowledge: {}", e)
        src_line = ""
        if knowledge.sources:
            s = knowledge.sources[0]
            src_line = f"\nsource: {s.title} — {s.uri}"
        return f"{knowledge.summary[:600]}{src_line}\n(saved)", None
