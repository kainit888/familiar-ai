"""ピコ応答フィルタ — 内部メンタル状態スキャフォールディングの漏出を除去する。

ピコ (familiar-ai ベース) は SYSTEM_PROMPT 上で「(mental ...)」「ToM」「interoception」
等の S-expression / Markdown 風スキャフォールディングを参照するが、たまにこれを
ユーザー宛応答にそのまま echo back してしまうことがある。

このモジュールはエージェントの最終応答テキストに対し、漏出パターンを正規表現で
切り落とすポストフィルタを提供する。SYSTEM_PROMPT の `suppress-meta-reasoning`
constraint と二段構えで漏出を抑える設計。

アルゴリズムの方針:
    保守的な「末尾ブロック限定」削除。本文中に偶発的にパターンマッチした 1 行が
    現れても削除しない (false positive 防止)。実際の漏出は応答の末尾に
    `:` 単独マーカ or 連続した構造化箇条書きとして現れることが観測されている
    (phase_c_handoff.md 1-4 参照)。

Notes:
    - 自然な日本語の感情語彙 (例: 「嬉しい」「ちょっと疲れた」) は除去しない。
    - 末尾の漏出ブロック (空行 + `:` マーカ or 2 行以上の連続漏出) のみ削除。
    - 本文中・前段の単発パターンマッチは false positive 防止のため残す。
    - 全て削除されて空文字になった場合は空文字を返す (呼び出し側で fallback)。
"""

from __future__ import annotations

import re
from typing import Final

from loguru import logger

# ── 漏出パターン定義 (Python 内定数) ──────────────────────────────────────────
#
# 行頭マッチで「これは漏出した内部スキャフォールディング行」と判定する。

# `:` / `：` 単独行マーカ (漏出ブロック開始の決定的指標)
_COLON_MARKER_PATTERNS: Final[tuple[str, ...]] = (
    r"^\s*[:：]\s*$",
)

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

# Numeric inner-state echo (emotion values + scalars)
# 完全に行全体がこの形式の場合のみ削除する (自然文中の数値は残す)
_NUMERIC_STATE_PATTERNS: Final[tuple[str, ...]] = (
    r"^\s*[-・*]?\s*("
    r"happy|sad|excited|curious|angry|fearful|tender|"
    r"valence|arousal|dominance|threat|frustration|loneliness|"
    r"attachment[_-]?pull|uncertainty|fatigue|social[_-]?pull|"
    r"sensor[_-]?confidence|unresolved[_-]?tension|focus[_-]?stability|"
    r"confidence"
    r")\s*[:：=]\s*[-+]?\d*\.?\d+\s*$",
)

# 英語ラベル箇条書き (例: "- companion: They treat me like a person.")
# 本文末尾の漏出パターンとして、英語の構造化ラベル+本文を検出
_LABEL_BULLET_PATTERNS: Final[tuple[str, ...]] = (
    r"^\s*[-・*]\s*(companion|self|user|agent)\s*[:：]\s+\S",
)

# 行動メモ (例: "- remember happy feeling.", "- continue exploring.")
_ACTION_MEMO_PATTERNS: Final[tuple[str, ...]] = (
    r"^\s*[-・*]\s*(remember|continue|consider|note|todo|plan)\s+\S",
)

# 英語一人称内部独白 (例: "- I feel happy.", "- I want to ...")
_ENG_FIRST_PERSON_PATTERNS: Final[tuple[str, ...]] = (
    r"^\s*[-・*]\s*I\s+(feel|want|need|will|am|was|have)\s+\S",
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
_TOM_INFERENCE_PATTERNS: Final[tuple[str, ...]] = (
    r"^\s*[-・*]\s*[^()\n]+\s*\(\s*confidence\s*[:：=]?\s*[01]?\.\d+\s*\)\s*$",
    r"^\s*[-・*]\s*[^()\n]+\s*\(\s*[01]?\.\d+\s*\)\s*$",
)

# 「漏出行」と判定する全パターン (コロンマーカ以外)
_LEAKAGE_PATTERNS: Final[tuple[str, ...]] = (
    _TOM_HEADER_PATTERNS
    + _MENTAL_STATE_PATTERNS
    + _NUMERIC_STATE_PATTERNS
    + _LABEL_BULLET_PATTERNS
    + _ACTION_MEMO_PATTERNS
    + _ENG_FIRST_PERSON_PATTERNS
    + _SEXP_PATTERNS
    + _TOM_INFERENCE_PATTERNS
)

# Compiled once at import time
_COMPILED_LEAKAGE: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(pat, re.IGNORECASE | re.MULTILINE) for pat in _LEAKAGE_PATTERNS
)
_COMPILED_COLON_MARKER: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(pat) for pat in _COLON_MARKER_PATTERNS
)


def _is_leakage_line(line: str) -> bool:
    """1 行が漏出パターンに該当するかを判定する。

    Args:
        line: チェック対象の 1 行 (改行を含まない)。

    Returns:
        True の場合、その行は漏出として除去候補。
    """
    for pat in _COMPILED_LEAKAGE:
        if pat.match(line):
            return True
    return False


def _is_colon_marker(line: str) -> bool:
    """1 行が `:` / `：` 単独マーカかを判定する (漏出ブロック開始の決定的指標)。"""
    for pat in _COMPILED_COLON_MARKER:
        if pat.match(line):
            return True
    return False


def strip_internal_state_leakage(text: str | None) -> str:
    """応答テキスト末尾の内部メンタル状態スキャフォールディング漏出を除去する。

    アルゴリズム (末尾ブロック限定の保守的削除):
        1. None または空文字は空文字を返す。
        2. テキストを行に分割し、末尾から走査する。
        3. 漏出パターンマッチ → 削除候補
           空行 → 削除候補 (ブロック内空行許容)
           `:` 単独マーカ → 削除確定マーカ、削除候補
        4. 通常の自然文に出会ったら走査終了。
        5. 削除確定条件:
           - `:` マーカが見つかった、または
           - 漏出行が 2 行以上
        6. どちらも満たさない場合は何も削除しない (false positive 防止)。
        7. 削除後、trailing whitespace を trim。
        8. 削除行があれば warning ログを出力 (false positive 検知用)。

    Args:
        text: フィルタ対象の応答テキスト。

    Returns:
        末尾漏出ブロックを除去した後のテキスト。全削除なら空文字。

    Examples:
        >>> strip_internal_state_leakage("おはよう\\n:\\n- affect: calm")
        'おはよう'
        >>> strip_internal_state_leakage("おはよう、嬉しい朝だね")
        'おはよう、嬉しい朝だね'
        >>> strip_internal_state_leakage(None)
        ''
        >>> # 単発の漏出マッチ (マーカなし) は保守的に残す
        >>> strip_internal_state_leakage("本文\\n- affect: calm")
        '本文\\n- affect: calm'
    """
    if not text:
        return ""

    lines = text.splitlines()
    n = len(lines)

    leakage_start = n  # 削除しない場合は n
    leakage_count = 0
    found_colon_marker = False

    for i in range(n - 1, -1, -1):
        line = lines[i]
        stripped = line.strip()

        # 空行: ブロック内空行として許容
        if not stripped:
            leakage_start = i
            continue

        # `:` 単独行マーカ (削除確定の決定的シグナル)
        if _is_colon_marker(line):
            leakage_start = i
            found_colon_marker = True
            continue

        # 漏出パターン
        if _is_leakage_line(line):
            leakage_start = i
            leakage_count += 1
            continue

        # 通常の自然文 → 漏出ブロック境界
        break

    # 削除確定条件: コロンマーカ または 漏出行 2 行以上
    if not (found_colon_marker or leakage_count >= 2):
        # false positive 回避: 何も削除しない
        return text.rstrip()

    kept = lines[:leakage_start]
    # trailing blank trim
    while kept and not kept[-1].strip():
        kept.pop()

    result = "\n".join(kept).rstrip()

    removed_count = n - leakage_start
    if removed_count > 0:
        logger.warning(
            "response_filter: stripped trailing leakage block "
            "(removed={}, leakage_lines={}, colon_marker={}, "
            "original_len={}, filtered_len={})",
            removed_count,
            leakage_count,
            found_colon_marker,
            len(text),
            len(result),
        )

    return result
