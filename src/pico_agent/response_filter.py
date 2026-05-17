"""ピコ応答フィルタ — 内部メンタル状態スキャフォールディングの漏出を除去する。

ピコ (familiar-ai ベース) は SYSTEM_PROMPT 上で「(mental ...)」「ToM」「interoception」
等の S-expression / Markdown 風スキャフォールディングを参照するが、たまにこれを
ユーザー宛応答にそのまま echo back してしまうことがある。

このモジュールはエージェントの最終応答テキストに対し、漏出パターンを正規表現で
切り落とすポストフィルタを提供する。SYSTEM_PROMPT の `suppress-meta-reasoning`
constraint と二段構えで漏出を抑える設計。

Notes:
    - 自然な日本語の感情語彙 (例: 「嬉しい」「ちょっと疲れた」) は除去しない。
    - 漏出パターンは応答の冒頭ブロック・末尾ブロック・本文中の任意位置を対象。
    - パターン適用後に残った余分な空行・前後 whitespace は trim する。
    - 全て削除されて空文字になった場合は空文字を返す (呼び出し側で fallback)。
"""

from __future__ import annotations

import re
from typing import Final

from loguru import logger

# ── 漏出パターン定義 (Python 内定数) ──────────────────────────────────────────
#
# 行頭マッチで「これは漏出した内部スキャフォールディング行」と判定する。
# multi-line モードで使用する。

# Markdown スタイル ToM ヘッダ (tools/tom.py:131-156 由来)
_TOM_HEADER_PATTERNS: Final[tuple[str, ...]] = (
    r"^\s*#\s*ToM[:：]",
    r"^\s*##\s*エビデンス",
    r"^\s*##\s*推論",
    r"^\s*##\s*応答方針",
)

# Mental state bullet list (mental_state.py:262-274 由来)
_MENTAL_STATE_PATTERNS: Final[tuple[str, ...]] = (
    r"^\s*\[Mental\s*state\]",
    r"^\s*[-・]\s*interoception\s*[:：]",
    r"^\s*[-・]\s*affect\s*[:：]",
    r"^\s*[-・]\s*social\s*[:：]",
    r"^\s*[-・]\s*drives\s*[:：]",
    r"^\s*[-・]\s*working[-_]memory\s*[:：]",
    r"^\s*[-・]\s*continuity\s*[:：]",
)

# Numeric inner-state echo (例: "arousal: 0.85", "valence 0.2", "fatigue=0.4")
# 完全に行全体がこの形式の場合のみ削除する (自然文中の数値は残す)
_NUMERIC_STATE_PATTERNS: Final[tuple[str, ...]] = (
    r"^\s*[-・*]?\s*(arousal|valence|fatigue|social[_-]?pull|"
    r"sensor[_-]?confidence|unresolved[_-]?tension|focus[_-]?stability|"
    r"confidence)\s*[:：=]\s*[-+]?\d*\.?\d+\s*$",
)

# S-expression form IDs that should never appear in user-facing text
_SEXP_PATTERNS: Final[tuple[str, ...]] = (
    r"^\s*\(interoception\b.*$",
    r"^\s*\(body-state\b.*$",
    r"^\s*\(tension\b.*$",
    r"^\s*\(sensing\b.*$",
    r"^\s*\(mental\b.*$",
    r"^\s*:private\s+true\b.*$",
)

# ToM inference bullet with trailing confidence number
# 例: "- 不安 (0.8)" / "- frustrated (0.5)"
# これは漏出かつ自然文と紛らわしいので、行全体がこの形式の時のみ削除
_TOM_INFERENCE_PATTERNS: Final[tuple[str, ...]] = (
    r"^\s*[-・*]\s*[^()\n]+\s*\(\s*confidence\s*[:：=]?\s*[01]?\.\d+\s*\)\s*$",
    r"^\s*[-・*]\s*[^()\n]+\s*\(\s*[01]?\.\d+\s*\)\s*$",
)

_ALL_PATTERNS: Final[tuple[str, ...]] = (
    _TOM_HEADER_PATTERNS
    + _MENTAL_STATE_PATTERNS
    + _NUMERIC_STATE_PATTERNS
    + _SEXP_PATTERNS
    + _TOM_INFERENCE_PATTERNS
)

# Compiled once at import time
_COMPILED: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(pat, re.IGNORECASE | re.MULTILINE) for pat in _ALL_PATTERNS
)


def _line_is_leakage(line: str) -> bool:
    """1 行が漏出パターンに該当するかを判定する。

    Args:
        line: チェック対象の 1 行 (改行を含まない)。

    Returns:
        True の場合、その行は漏出として除去対象。
    """
    for pat in _COMPILED:
        if pat.match(line):
            return True
    return False


def strip_internal_state_leakage(text: str | None) -> str:
    """応答テキストから内部メンタル状態スキャフォールディング漏出を除去する。

    アルゴリズム:
        1. None または空文字は空文字を返す。
        2. テキストを行に分割する。
        3. 各行を漏出パターンに対してチェックし、該当行を捨てる。
        4. 連続する空行を 1 行にまとめる。
        5. 前後の whitespace を trim する。

    Args:
        text: フィルタ対象の応答テキスト。

    Returns:
        漏出行を除去した後のテキスト。全削除なら空文字。

    Examples:
        >>> strip_internal_state_leakage("[Mental state]\\n- affect: calm\\n\\nこんにちは")
        'こんにちは'
        >>> strip_internal_state_leakage("おはよう、嬉しい朝だね")
        'おはよう、嬉しい朝だね'
        >>> strip_internal_state_leakage(None)
        ''
    """
    if not text:
        return ""

    original = text
    lines = text.splitlines()
    kept: list[str] = []
    removed_count = 0

    for line in lines:
        if _line_is_leakage(line):
            removed_count += 1
            continue
        kept.append(line)

    # Collapse runs of consecutive empty lines into a single empty line
    collapsed: list[str] = []
    prev_blank = False
    for line in kept:
        is_blank = not line.strip()
        if is_blank and prev_blank:
            continue
        collapsed.append(line)
        prev_blank = is_blank

    result = "\n".join(collapsed).strip()

    if removed_count > 0:
        logger.debug(
            "response_filter: removed {} leakage line(s) (original_len={}, filtered_len={})",
            removed_count,
            len(original),
            len(result),
        )

    return result
