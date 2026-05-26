"""Vision 再質問プロンプト注入 (Phase C-10a)。

ピコ (familiar-ai ベース) は ``see`` ツールでカメラ画像を取得し、その
tool_result (画像) を会話履歴に残す。ユーザーが「もう一回見て」「今どう？」など
**新しい観察** を求めても、モデルは履歴に残った前回画像をそのまま再利用して
答えてしまうことがある (= カメラを今撮り直さない)。

このモジュールは、ユーザー入力が vision 関連で **かつ履歴に直近の see
tool_result がある** ときに、user メッセージへ「履歴の画像を再利用せず必ず
see() を今呼べ」という soft nudge (プロンプト注入) を付け足す判定とテキストを
提供する。

``response_filter.py`` / ``self_model_filter.py`` と同型の二層分離設計を厳守:
    - familiar_agent を一切 import しない (逆 import の片方向性を維持)
    - 依存は re / loguru のみ
    - 純ロジック (副作用なし、防御的)

Notes:
    - これは **soft nudge (プロンプト注入)** であり、「都度 see を強制」する
      hard 保証ではない (backend 非依存を優先)。実際に see を呼ぶかはモデル次第。
    - ``needs_force_see`` は保守的: vision キーワードを含み、かつ履歴に直近の
      see 痕跡があるときのみ True (= 初回観察や非 vision 入力では注入しない)。
"""

from __future__ import annotations

import re
from typing import Any, Final

from loguru import logger

# ── vision 関連キーワード (保守的) ──────────────────────────────────────────
#
# 「カメラで今の様子を見てほしい」系の語彙のみ。日常会話で誤爆しないよう、
# 観察・視覚に直結する表現に限定する。mutation 検知: この集合を空にすると
# vision テストが fail する。
_VISION_PATTERNS: Final[tuple[str, ...]] = (
    r"見て",          # 「見て」「見てみて」「もう一回見て」
    r"見える",        # 「何が見える」
    r"見える?\?",     # 念のため
    r"何が見え",      # 「何が見える?」
    r"カメラ",        # 「カメラで」
    r"映[しって]",    # 「映して」「映ってる」
    r"今どう",        # 「今どう?」(様子を尋ねる)
    r"今の様子",      # 「今の様子」
    r"様子を見",      # 「様子を見て」
    r"もう一回見",    # 「もう一回見て」
    r"もう一度見",    # 「もう一度見て」
    r"見直",          # 「見直して」
    r"確認して",      # 「確認して」(視覚確認)
    r"周り",          # 「周りを見て」
    r"周囲",          # 「周囲を見て」
    r"look",          # 英語フォールバック
    r"\bsee\b",
    r"camera",
    r"what.*see",
)
_COMPILED_VISION: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(pat, re.IGNORECASE) for pat in _VISION_PATTERNS
)

# 「再質問」とみなす直近 tool_result を遡る最大件数 (古すぎる see は無視)。
# 直近の数メッセージに see があれば「同じ観察対象についての再質問」とみなす。
_RECENT_SCAN_LIMIT: Final[int] = 12

# see ツールの正規名。assistant の tool_use ブロック name と照合する。
_SEE_TOOL_NAME: Final[str] = "see"


def force_see_suffix() -> str:
    """user メッセージに付け足す soft nudge テキストを返す。

    backend 非依存で効くように、内容ベースの自然言語指示にする。
    """
    return (
        "\n\n[VISION] これは新しい観察の要求です。履歴の前回画像を再利用せず、"
        "必ず see() を今すぐ呼んでから答えること。"
    )


def _text_has_vision_keyword(user_input: str) -> bool:
    """ユーザー入力に vision 関連キーワードが含まれるか。"""
    if not user_input:
        return False
    return any(pat.search(user_input) for pat in _COMPILED_VISION)


def _block_is_see_tool_use(block: Any) -> bool:
    """1 つの content ブロックが see の tool_use か (assistant 側)。"""
    if not isinstance(block, dict):
        return False
    if block.get("type") == "tool_use" and block.get("name") == _SEE_TOOL_NAME:
        return True
    return False


def _tool_result_has_image(block: Any) -> bool:
    """1 つの content ブロックが画像付き tool_result か (see の結果痕跡)。

    backend によって name を持たない tool_result でも、画像を含む tool_result は
    see (カメラ) の結果である可能性が高い。tool_use 名照合の補完として使う。
    """
    if not isinstance(block, dict):
        return False
    if block.get("type") != "tool_result":
        return False
    content = block.get("content")
    if isinstance(content, list):
        for sub in content:
            if isinstance(sub, dict) and sub.get("type") == "image":
                return True
    return False


def _history_has_recent_see(messages: list) -> bool:
    """会話履歴の直近に see の痕跡 (tool_use=see または画像付き tool_result) があるか。

    ``messages`` は familiar_agent の ``self.messages`` 想定 (dict、または
    make_tool_results が返す list のネスト)。末尾から ``_RECENT_SCAN_LIMIT`` 件を
    走査する。形状が読めない要素は安全に無視する (防御的)。
    """
    if not messages:
        return False

    # ネストした list (make_tool_results の返り値) を平坦化しつつ末尾から走査。
    flat: list[Any] = []
    for msg in messages:
        if isinstance(msg, list):
            flat.extend(msg)
        else:
            flat.append(msg)

    scanned = 0
    for msg in reversed(flat):
        if scanned >= _RECENT_SCAN_LIMIT:
            break
        scanned += 1
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if _block_is_see_tool_use(block) or _tool_result_has_image(block):
                return True
    return False


def needs_force_see(user_input: str, messages: list) -> bool:
    """vision 再質問なら True (= force_see_suffix を user メッセージに注入すべき)。

    判定 = vision 関連キーワードを含む AND 履歴の直近に see の痕跡がある。
    保守的な AND ゲートにより、初回観察 (履歴に see 痕跡なし) や非 vision 入力では
    注入しない (回帰防止)。

    Args:
        user_input: 今ターンのユーザー入力テキスト。
        messages: 会話履歴 (familiar_agent の ``self.messages`` 想定)。

    Returns:
        soft nudge を注入すべきなら True。
    """
    if not _text_has_vision_keyword(user_input):
        return False
    if not _history_has_recent_see(messages):
        return False
    logger.debug(
        "vision_reprompt: force_see nudge triggered (input={!r})",
        user_input[:40],
    )
    return True
