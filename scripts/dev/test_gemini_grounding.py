#!/usr/bin/env python3
"""Phase F 事前調査: Gemini Search Grounding 動作確認 (実 API を叩く)。

カイニットの GOOGLE/Gemini billing 状態が不明なため、実際に呼んで baseline +
Google Search Grounding が使えるかを確認する。ランタイム無変更の dev スクリプト。

使い方:
    uv run python scripts/dev/test_gemini_grounding.py

API key は既存運用と同じ ``.env`` の ``API_KEY`` (PLATFORM=gemini 用)。
``GOOGLE_API_KEY`` / ``GEMINI_API_KEY`` もフォールバックで見る。
モデルは ``.env`` の ``MODEL`` (既定 gemini-3.1-flash-lite)。
"""

from __future__ import annotations

import os
import sys
import traceback

# .env をロード (config import の副作用 load_dotenv)。
try:
    from familiar_agent import config as _cfg  # noqa: F401  (load_dotenv 副作用)
except Exception:
    from dotenv import load_dotenv

    load_dotenv()


def _mask(k: str) -> str:
    return f"{k[:6]}…{k[-4:]} (len {len(k)})" if k and len(k) > 10 else "(empty/short)"


def main() -> int:
    print("=" * 72)
    print("Gemini Search Grounding 動作確認")
    print("=" * 72)

    # ── (a) SDK インストール状況 ──────────────────────────────────────────
    print("\n[a] SDK 確認")
    try:
        import google.genai as _g

        print(f"  google-genai (new): {getattr(_g, '__version__', 'installed')}")
    except Exception as e:
        print(f"  google-genai NOT importable: {e}")
        return 1
    try:
        import google.generativeai as _go

        print(f"  google-generativeai (old): {getattr(_go, '__version__', 'installed')}")
    except Exception:
        print("  google-generativeai (old): not installed (OK, using new SDK)")

    api_key = (
        os.environ.get("API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or os.environ.get("GEMINI_API_KEY")
        or ""
    )
    model = os.environ.get("MODEL", "gemini-3.1-flash-lite")
    print(f"  API_KEY: {_mask(api_key)}")
    print(f"  MODEL:   {model}")
    print(f"  PLATFORM: {os.environ.get('PLATFORM', '(unset)')}")
    if not api_key:
        print("  [error] no API key in env (API_KEY / GOOGLE_API_KEY / GEMINI_API_KEY)")
        return 1

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)

    # ── (b) baseline (grounding なし) ─────────────────────────────────────
    print("\n[b] Baseline (grounding なし)")
    try:
        base = client.models.generate_content(model=model, contents="こんにちは")
        text = (base.text or "")[:80]
        print(f"  BASELINE OK: {text!r}")
    except Exception as e:
        print(f"  BASELINE FAILED: {type(e).__name__}: {e}")
        traceback.print_exc()
        print("\n=> ケース C: baseline 失敗 (API key / model 名 / SDK の基本問題)")
        return 1

    # ── (c) Google Search Grounding ──────────────────────────────────────
    print("\n[c] Google Search Grounding")
    grounded = None
    try:
        grounded = client.models.generate_content(
            model=model,
            contents="今日の東京の天気は?",
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
            ),
        )
        print("  GROUNDING OK")
        print(f"  response.text: {(grounded.text or '')[:200]!r}")
    except Exception as e:
        print(f"  GROUNDING FAILED: {type(e).__name__}: {e}")
        traceback.print_exc()
        msg = str(e).lower()
        if "billing" in msg or "permission" in msg or "quota" in msg:
            print("\n=> ケース B: baseline OK / grounding 不可 (billing/quota/permission)")
        else:
            print("\n=> grounding 失敗 (上記エラー全文を claude.ai へ)")
        return 2

    # ── (d) レスポンス構造解析 ────────────────────────────────────────────
    print("\n[d] Grounding metadata 構造")
    try:
        cand = grounded.candidates[0] if grounded.candidates else None
        meta = getattr(cand, "grounding_metadata", None) if cand else None
        if meta is None:
            print("  grounding_metadata: なし (grounding は呼べたが metadata 未付与)")
        else:
            chunks = getattr(meta, "grounding_chunks", None) or []
            queries = getattr(meta, "web_search_queries", None) or []
            print(f"  web_search_queries: {list(queries)}")
            print(f"  grounding_chunks: {len(chunks)} 件")
            for i, ch in enumerate(chunks[:8]):
                web = getattr(ch, "web", None)
                uri = getattr(web, "uri", None) if web else None
                title = getattr(web, "title", None) if web else None
                print(f"    [{i}] {title} — {uri}")
            supports = getattr(meta, "grounding_supports", None) or []
            print(f"  grounding_supports: {len(supports)} 件 (citation spans)")
    except Exception as e:
        print(f"  metadata 解析失敗: {type(e).__name__}: {e}")
        traceback.print_exc()

    print("\n=> ケース A: baseline + grounding 両方成功。Phase F 着手可。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
