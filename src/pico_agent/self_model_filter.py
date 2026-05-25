"""self_model 自己洞察の保存前検証フィルタ (Phase C-10)。

familiar-ai の `_update_self_model` (agent.py) は utility backend
(qwen2.5:1.5b) に「この発言を書いた存在について明らかになること」を一文で
書かせ、kind='self_model' として保存する。しかし 1.5b は抽象化に失敗し、
応答テキストをほぼ verbatim で反射する / プロンプトのラベル (例:
`良い例:`) を漏出する / 韓国語・中国語など想定外スクリプトを混入させる、
という汚染を起こす。汚染行は次セッション 1 ターン目の morning
reconstruction で system prompt に注入され、Gemini がそれをエコーする。

このモジュールは **保存前** の検証フィルタと、**既存 DB 行** の汚染判定を
提供する。`response_filter.py` と同型の二層分離設計を厳守する:

    - familiar_agent を一切 import しない (逆 import の片方向性を維持)
    - 依存は re / unicodedata / loguru のみ
    - 防御的 (None / 空 / 短すぎ / 過長 は弾く)

Notes:
    - `is_valid_self_model_insight(insight, final_text)` … 保存可なら True。
      応答との verbatim/高類似、一人称欠落、ラベルリーク、想定外スクリプト
      混入を弾く。`final_text=""` を渡すと verbatim 比較 (a) はスキップされ、
      DB クリーンアップ用途で (b)(c)(d) のみ適用できる。
    - `is_contaminated_existing_row(content)` … 既存行の汚染判定。final_text
      が手元に無い前提なので、ラベル/異言語/過長/(一人称欠落 AND 応答調) で
      判定する。正常な self_model を誤削除しないよう一人称欠落単独では消さない。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

from loguru import logger

# ── 閾値定数 (根拠付き) ──────────────────────────────────────────────────────

# 最小・最大長 (正規化前の生テキスト長で判定)。短すぎる断片や、一文を超えた
# 長文 (= 応答 verbatim の可能性が高い) を弾く。
_MIN_LEN: Final[int] = 4
_MAX_LEN: Final[int] = 200

# 部分包含判定の最小正規化長。短い文字列は偶発的に包含関係が成立しやすいため、
# norm_i が 12 文字以上のときだけ「norm_i in norm_f / norm_f in norm_i」を
# verbatim とみなす。
_CONTAIN_MIN_LEN: Final[int] = 12

# 文字 bigram Jaccard 類似度の verbatim 閾値。0.8 以上で「ほぼ同一」とみなす。
_VERBATIM_RATIO: Final[float] = 0.8

# 一人称マーカ (日本語の自己洞察に必須)
_FIRST_PERSON_MARKER: Final[str] = "私"

# ASCII 比率の閾値 (既存行クリーンアップ専用)。`_has_unexpected_language` (ja)
# の 0.5 と同根拠: 自然な日本語は ASCII 15-25% 程度なので 0.5 で十分なマージン。
# 英語 verbatim 反射 (qwen の多言語フォールバック) を異言語として弾く。
_ASCII_RATIO_THRESHOLD: Final[float] = 0.5

# プロンプトのラベル / メタ指示が漏出したことを示すパターン (re.IGNORECASE)。
# `_SELF_MODEL_PROMPT` の「良い例 / 悪い例 / 条件: / 一文だけ / nothing」等。
_LABEL_LEAK_PATTERNS: Final[tuple[str, ...]] = (
    r"良い例",
    r"悪い例",
    r"例[:：]",
    r"条件[:：]",
    r"発言[:：]",
    r"一文だけ",
    r"一文で",
    r"nothing",
    r"the response reveals",
    r"this (response|message) (reveals|shows)",
)
_COMPILED_LABEL_LEAK: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(pat, re.IGNORECASE) for pat in _LABEL_LEAK_PATTERNS
)

# 想定外スクリプト (Unicode 名のプレフィックス)。日本語 self_model に
# 混入してはならない言語。`_has_unexpected_language` は ASCII 比率しか見ない
# ため、ハングル / 漢字以外の表音文字混入を取りこぼす。それを補完する。
_UNEXPECTED_SCRIPT_PREFIXES: Final[tuple[str, ...]] = (
    "HANGUL",  # 韓国語
    "ARABIC",
    "HEBREW",
    "THAI",
    "DEVANAGARI",
    "CYRILLIC",
)

# 中国語混入の検出閾値。中国語は CJK 漢字のみで構成され、仮名 (HIRAGANA /
# KATAKANA) を一切含まない。一方、自然な日本語の self_model は必ず仮名を含む
# (助詞・送り仮名・活用語尾)。よって「漢字が `_CHINESE_HAN_MIN` 文字以上 AND
# 仮名がゼロ」を中国語混入とみなす。`_has_unexpected_language` (ASCII 比率) も
# `_UNEXPECTED_SCRIPT_PREFIXES` (表音文字) も漢字だけの中国語を取りこぼすため、
# これを (d) の補完として追加する。閾値 4 は短い日本語洞察 (例「私はコードに
# 魅せられた。」漢字 4・仮名 14) でも仮名が必ず付くため誤検出しない。
_CHINESE_HAN_MIN: Final[int] = 4

# 「これは応答 (会話) であって自己洞察ではない」ことを示す口語末尾パターン。
# DB クリーンアップ用 (final_text が無い既存行) の補助判定にのみ使う。
_RESPONSE_TONE_PATTERNS: Final[tuple[str, ...]] = (
    r"だよ[。！!?？]?$",
    r"だね[。！!?？]?$",
    r"んだ[。！!?？]?$",
    r"んだよ[。！!?？]?$",
    r"だな[。！!?？]?$",
    r"かな[。！!?？]?$",
    r"です[。！!?？]?$",
    r"ます[。！!?？]?$",
    r"ません[。！!?？]?$",
    r"ですよ[。！!?？]?$",
    r"くれる[?？]?$",
    r"嬉しいな[。！!?？]?$",
    r"どう[?？]$",  # 対外問いかけ「〜はどう？」(genuine 内省は疑問で終わらない)
)
_COMPILED_RESPONSE_TONE: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(pat) for pat in _RESPONSE_TONE_PATTERNS
)

# 会話フィラー / 対外発話マーカー。「これは内省ではなく、ユーザに向けた会話
# (応答 verbatim 反射) だ」と示す語句。一人称「私」を含む口語反射 (C-9 で
# morning エコーを再発させた 4 行) は一人称欠落判定では捕捉できないため、
# **一人称の有無に依らず** このマーカーを検出する。あいさつ・問いかけ・相手
# 言及・LLM 定型 disclaimer (clip echo) を含む。
#
# 重要 (誤削除防止): genuine な一人称内省 (例「私はささやかなものに惹かれる。」
# 「私はアニメの映像とコードに魅せられた。」) はこれらマーカーを一切含まない。
# また会話エコー判定は **このマーカー AND 応答調末尾 (`_RESPONSE_TONE_PATTERNS`)**
# の AND ゲートにする。内省文は断定/内省末尾 (〜られた。/〜ている。) であり
# 応答調末尾を持たないため、二重に保護される。
_CONVERSATIONAL_ECHO_MARKERS: Final[tuple[str, ...]] = (
    # あいさつ・相づち
    "うん、",
    "そうだね",
    "こんにちは",
    "こんばんは",
    "おはよう",
    "ありがとう",
    # 体調・近況の対外報告
    "元気だよ",
    "元気です",
    "調子",
    # 相手への言及・問いかけ
    "話しかけてくれて",
    "そっちは",
    "どう？",
    "どうかな",
    "何か面白いこと",
    "あったかな",
    "だから、こうして",
    # LLM 定型 disclaimer (clip echo)
    "分かりません",
    "わかりません",
    "それが何を示しているか",
    "そのような状況では",
)

# 正規化時に除去する空白・句読点・記号 (verbatim 比較用)
_STRIP_FOR_NORM: Final[re.Pattern[str]] = re.compile(
    r"[\s、。，．,.!！?？…「」『』\"'（）()\[\]【】・〜~ー\-—:：;；]+"
)


def _normalize(text: str) -> str:
    """NFKC 正規化 + 空白・句読点除去 + casefold で比較用文字列を作る。"""
    norm = unicodedata.normalize("NFKC", text)
    norm = _STRIP_FOR_NORM.sub("", norm)
    return norm.casefold()


def _char_bigrams(s: str) -> set[str]:
    """文字 bigram 集合 (長さ 1 はそれ自身を 1 要素として返す)。"""
    if len(s) < 2:
        return {s} if s else set()
    return {s[i : i + 2] for i in range(len(s) - 1)}


def _bigram_jaccard(a: str, b: str) -> float:
    """2 文字列の文字 bigram Jaccard 類似度 (0.0–1.0)。"""
    ba, bb = _char_bigrams(a), _char_bigrams(b)
    if not ba and not bb:
        return 1.0
    if not ba or not bb:
        return 0.0
    inter = len(ba & bb)
    union = len(ba | bb)
    return inter / union if union else 0.0


def _bigram_overlap(a: str, b: str) -> float:
    """文字 bigram の overlap 係数 (intersection / min(|A|,|B|))。

    Jaccard と違い、片方の bigram がほぼ他方に内包される (= 短い側が長い側の
    verbatim 反射) ケースを捕捉する。冒頭に『私は、』等の差異があり厳密な部分
    包含が崩れても高値になる。
    """
    ba, bb = _char_bigrams(a), _char_bigrams(b)
    if not ba or not bb:
        return 0.0
    inter = len(ba & bb)
    denom = min(len(ba), len(bb))
    return inter / denom if denom else 0.0


def _has_label_leak(text: str) -> bool:
    """プロンプトのラベル / メタ指示の漏出を検出する (条件 c)。"""
    return any(pat.search(text) for pat in _COMPILED_LABEL_LEAK)


def _has_unexpected_script(text: str) -> bool:
    """想定外スクリプト (ハングル等) の混入を検出する (条件 d)。

    `_has_unexpected_language` の ASCII 比率判定では拾えない表音文字を補完する。
    """
    for ch in text:
        if ch.isspace() or ch.isascii():
            continue
        try:
            name = unicodedata.name(ch)
        except ValueError:
            continue
        if name.startswith(_UNEXPECTED_SCRIPT_PREFIXES):
            return True
    return False


def _has_response_tone(text: str) -> bool:
    """口語の応答調末尾を検出する (DB クリーンアップ補助)。"""
    stripped = text.strip()
    return any(pat.search(stripped) for pat in _COMPILED_RESPONSE_TONE)


def _has_conversational_echo(text: str) -> bool:
    """会話エコー (応答 verbatim 反射) を検出する。一人称の有無に依らない。

    判定 = 応答調末尾 (`_RESPONSE_TONE_PATTERNS`) AND 会話フィラー / 対外発話
    マーカー (`_CONVERSATIONAL_ECHO_MARKERS`) を含む。

    C-9 で morning エコーを再発させた口語反射行 (「私、元気だよ！…そっちは
    どう？」等) は一人称『私』を含むため `no_first_person AND response_tone`
    では捕捉できなかった。本判定はその穴を塞ぐ。AND ゲートにより、フィラーを
    持たない genuine な一人称内省 (断定/内省末尾) は保持される。
    """
    stripped = text.strip()
    if not any(pat.search(stripped) for pat in _COMPILED_RESPONSE_TONE):
        return False
    return any(marker in stripped for marker in _CONVERSATIONAL_ECHO_MARKERS)


def _has_chinese_mixin(text: str) -> bool:
    """中国語混入を検出する (条件 d の補完)。

    中国語は CJK 漢字のみで構成され仮名を含まない。自然な日本語 self_model は
    必ず仮名 (助詞・送り仮名・活用) を含むため、「漢字が `_CHINESE_HAN_MIN`
    文字以上 AND 仮名ゼロ」を中国語混入とみなす。`_has_unexpected_language`
    (ASCII 比率) も `_UNEXPECTED_SCRIPT_PREFIXES` (表音文字) も漢字のみの中国語を
    取りこぼすため、これを補完する。
    """
    han = 0
    kana = 0
    for ch in text:
        if ch.isspace() or ch.isascii():
            continue
        try:
            name = unicodedata.name(ch)
        except ValueError:
            continue
        if name.startswith("CJK UNIFIED"):
            han += 1
        elif name.startswith(("HIRAGANA", "KATAKANA")):
            kana += 1
    return han >= _CHINESE_HAN_MIN and kana == 0


def _is_ascii_heavy(text: str) -> bool:
    """ASCII 比率が閾値超か (既存行クリーンアップ専用)。

    `_has_unexpected_language(text, "ja")` と同じ判定。日本語 self_model に
    英語 verbatim 反射が混入した既存行を異言語汚染として検出する。
    """
    if not text:
        return False
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    return (ascii_chars / len(text)) > _ASCII_RATIO_THRESHOLD


def _is_verbatim_or_near(insight: str, final_text: str) -> bool:
    """応答との verbatim / 高類似を検出する (条件 a)。

    final_text が空なら呼び出し側でスキップされる前提だが、防御的に False を返す。
    """
    norm_i = _normalize(insight)
    norm_f = _normalize(final_text)
    if not norm_f or not norm_i:
        return False
    # 部分包含 (どちらかが他方を包含)。短い文字列の偶発一致を避けるため
    # norm_i が一定長以上のときだけ適用。
    if len(norm_i) >= _CONTAIN_MIN_LEN and (norm_i in norm_f or norm_f in norm_i):
        return True
    # 文字 bigram Jaccard 類似度 (対称的な近似コピー)
    if _bigram_jaccard(norm_i, norm_f) >= _VERBATIM_RATIO:
        return True
    # bigram overlap 係数 (冒頭差異で厳密包含が崩れた verbatim 反射)。
    # 短文の偶発一致を避けるため norm_i が一定長以上のときだけ適用。
    if (
        len(norm_i) >= _CONTAIN_MIN_LEN
        and _bigram_overlap(norm_i, norm_f) >= _VERBATIM_RATIO
    ):
        return True
    return False


def is_valid_self_model_insight(insight: str | None, final_text: str) -> bool:
    """self_model 洞察が保存可能か検証する。True=保存可 / False=破棄。

    判定順序 (どれか 1 つでも該当すれば破棄):
        - 長さ: None / 空 / `_MIN_LEN` 未満 / `_MAX_LEN` 超過
        - (a) verbatim / 高類似: `final_text` が非空のときのみ評価。NFKC 正規化
          後の部分包含 (norm_i >= 12) または文字 bigram Jaccard >= 0.8。
        - (b) 一人称「私」を含まない
        - (c) ラベルリーク (良い例 / 条件: / nothing 等)
        - (d) 想定外スクリプト混入 (ハングル等)

    Args:
        insight: 検証対象の洞察 (utility backend の出力)。
        final_text: その洞察を抽出した元の応答テキスト。クリーンアップ用途では
            空文字を渡すと verbatim 比較 (a) をスキップできる。

    Returns:
        保存して良ければ True、破棄すべきなら False。
    """
    if insight is None:
        return False
    text = insight.strip()
    if len(text) < _MIN_LEN:
        logger.warning("self_model_filter: reject too_short '{}'", text[:40])
        return False
    if len(text) > _MAX_LEN:
        logger.warning("self_model_filter: reject too_long '{}'", text[:40])
        return False

    # (a) verbatim / 高類似 (final_text 非空のときのみ)
    if final_text and _is_verbatim_or_near(text, final_text):
        logger.warning("self_model_filter: reject verbatim_echo '{}'", text[:40])
        return False

    # (a 補完) 会話エコー: final_text が無くても、応答調末尾 AND 会話フィラーの
    # 反射文を弾く。保存前は (a) verbatim で大半捕捉できるが、final_text と
    # 既存行クリーンアップの判定を整合させ、verbatim 閾値をすり抜けた口語反射も
    # 確実に弾く。AND ゲートのため genuine 一人称内省は保持される。
    if _has_conversational_echo(text):
        logger.warning("self_model_filter: reject conversational_echo '{}'", text[:40])
        return False

    # (d 補完) 中国語混入 (漢字のみ・仮名ゼロ)
    if _has_chinese_mixin(text):
        logger.warning("self_model_filter: reject chinese_mixin '{}'", text[:40])
        return False

    # (c) ラベルリーク
    if _has_label_leak(text):
        logger.warning("self_model_filter: reject label_leak '{}'", text[:40])
        return False

    # (d) 想定外スクリプト
    if _has_unexpected_script(text):
        logger.warning("self_model_filter: reject unexpected_script '{}'", text[:40])
        return False

    # (b) 一人称要件
    if _FIRST_PERSON_MARKER not in text:
        logger.warning("self_model_filter: reject no_first_person '{}'", text[:40])
        return False

    return True


def is_contaminated_existing_row(content: str) -> bool:
    """既存 DB 行 (kind='self_model') が汚染されているか判定する。True=削除候補。

    既存行は抽出元の `final_text` が手元に無いため、verbatim 比較 (a) は使えない。
    代わりに次のいずれかを汚染とみなす:
        - (c) ラベルリーク
        - (d) 想定外スクリプト混入 (ハングル等) / 中国語混入 (漢字のみ・仮名ゼロ)
          / ASCII 過多 (英語 verbatim 反射 = ja ロケールでは異言語。save 時
          `_has_unexpected_language` 相当)
        - 長さ過長 (`_MAX_LEN` 超 = 応答 verbatim の可能性大)
        - 会話エコー: 応答調末尾 AND 会話フィラー (一人称の有無に依らない反射)
        - (b) 一人称欠落 AND 応答調末尾 (口語の会話文 = 反射)

    一人称欠落「単独」では削除しない (正常な self_model の誤削除を防ぐため、
    必ず応答調末尾と AND を取る)。会話エコー判定も応答調 AND フィラーの AND
    ゲートで、フィラーを持たない genuine 一人称内省を保持する。純ロジックのみ。

    Args:
        content: 対象行の content。

    Returns:
        汚染 (削除候補) なら True。
    """
    return bool(contamination_reason(content))


def contamination_reason(content: str) -> str:
    """`is_contaminated_existing_row` が True を返す理由ラベルを返す (監査用)。

    複数該当時は最初に当たった理由を返す。汚染でなければ空文字。
    """
    if content is None:
        return ""
    text = content.strip()
    if not text:
        return "empty"
    if len(text) > _MAX_LEN:
        return "too_long"
    if _has_label_leak(text):
        return "label_leak"
    if _has_unexpected_script(text):
        return "unexpected_script"
    if _has_chinese_mixin(text):
        return "chinese_mixin"
    if _is_ascii_heavy(text):
        return "ascii_heavy_wrong_language"
    if _has_conversational_echo(text):
        return "conversational_echo"
    if _FIRST_PERSON_MARKER not in text and _has_response_tone(text):
        return "no_first_person_response_tone"
    return ""
