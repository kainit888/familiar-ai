"""pico_v3 応答フィルタの単体テスト。

`pico_agent.response_filter.strip_internal_state_leakage` が:
  - 漏出パターンの行を確実に除去する
  - 自然な日本語/感情語彙を残す
  - コーナーケース (空入力、改行のみ、絵文字、大文字小文字、全角コロン等) を
    安全に扱う
ことを検証する。
"""

from __future__ import annotations

import pytest

from pico_agent.response_filter import strip_internal_state_leakage


# ── 基本: None / 空文字 / whitespace のみ ─────────────────────────────────────


def test_none_input_returns_empty_string() -> None:
    """None を渡しても例外なく空文字を返す。"""
    assert strip_internal_state_leakage(None) == ""


def test_empty_string_returns_empty_string() -> None:
    """空文字はそのまま空文字。"""
    assert strip_internal_state_leakage("") == ""


def test_whitespace_only_returns_empty() -> None:
    """空白と改行のみは空文字に正規化される。"""
    assert strip_internal_state_leakage("   \n  \n\t") == ""


def test_newlines_only_returns_empty() -> None:
    """改行のみの入力も空文字。"""
    assert strip_internal_state_leakage("\n\n\n") == ""


# ── 通常応答: 自然文は変化しない ──────────────────────────────────────────────


def test_natural_japanese_passthrough() -> None:
    """自然な日本語応答は何も変えない。"""
    text = "おはよう、嬉しい朝だね"
    assert strip_internal_state_leakage(text) == text


def test_multiline_natural_response_passthrough() -> None:
    """複数行の自然な応答も保持される。"""
    text = "今日はちょっと疲れた感じ。\nでも頑張るよ。"
    assert strip_internal_state_leakage(text) == text


def test_natural_emotional_vocabulary_preserved() -> None:
    """「嬉しい」「不安」など自然な感情語彙はそのまま残す。"""
    text = "嬉しい気持ちと、ちょっと不安な気持ちが両方ある。"
    assert strip_internal_state_leakage(text) == text


def test_emoji_preserved() -> None:
    """Unicode/絵文字を含む応答も保持される。"""
    text = "やった〜！🎉 嬉しい！"
    assert strip_internal_state_leakage(text) == text


# ── Mental state bullet 漏出 (mental_state.py:262-274 由来) ─────────────────


def test_mental_state_header_removed() -> None:
    """[Mental state] ヘッダ行は除去される。"""
    text = "[Mental state]\nこんにちは"
    assert strip_internal_state_leakage(text) == "こんにちは"


def test_interoception_bullet_removed() -> None:
    """- interoception: ... 行は除去される。"""
    text = "- interoception: tired\n- affect: calm\nお疲れさま"
    result = strip_internal_state_leakage(text)
    assert "interoception" not in result
    assert "affect" not in result
    assert "お疲れさま" in result


def test_all_mental_bullets_removed() -> None:
    """interoception / affect / social / drives / working-memory / continuity を一括除去。"""
    text = (
        "[Mental state]\n"
        "- interoception: warm\n"
        "- affect: calm\n"
        "- social: alone\n"
        "- drives: rest\n"
        "- working-memory: nothing\n"
        "- continuity: same as before\n"
        "\n"
        "応答本文だよ"
    )
    result = strip_internal_state_leakage(text)
    assert result == "応答本文だよ"


def test_zenkaku_colon_in_mental_bullet_removed() -> None:
    """全角コロン (：) のメンタル bullet も削除される。"""
    text = "- affect：穏やか\nこんにちは"
    result = strip_internal_state_leakage(text)
    assert "affect" not in result
    assert "こんにちは" in result


# ── ToM 漏出 (tools/tom.py:131-156 由来) ─────────────────────────────────────


def test_tom_header_removed() -> None:
    """# ToM: ... ヘッダ行は除去される。"""
    text = "# ToM: カイニットの視点分析\nそれは大変だね"
    result = strip_internal_state_leakage(text)
    assert "ToM" not in result
    assert "それは大変だね" in result


def test_tom_zenkaku_colon_header_removed() -> None:
    """全角コロン版の ToM ヘッダも除去される。"""
    text = "# ToM：カイニットの視点分析\n了解"
    result = strip_internal_state_leakage(text)
    assert "ToM" not in result
    assert "了解" in result


def test_tom_section_headers_removed() -> None:
    """## エビデンス / ## 推論 / ## 応答方針 を除去。"""
    text = (
        "# ToM: 分析\n"
        "## エビデンス（観察されたシグナル）\n"
        "- 表情が固い\n"
        "## 推論（心的状態と確信度）\n"
        "- 不安 (0.8)\n"
        "## 応答方針\n"
        "受容を優先\n"
        "\n"
        "ねぇ、大丈夫?"
    )
    result = strip_internal_state_leakage(text)
    assert "エビデンス" not in result
    assert "推論" not in result
    assert "応答方針" not in result
    assert "(0.8)" not in result
    assert "ねぇ、大丈夫?" in result


def test_tom_inference_confidence_bullet_removed() -> None:
    """- <state> (0.8) 形式の推論行を除去する。"""
    text = "- 不安 (0.8)\n- 疲労 (0.5)\nそれは辛いね"
    result = strip_internal_state_leakage(text)
    assert "(0.8)" not in result
    assert "(0.5)" not in result
    assert "それは辛いね" in result


# ── 数値内部状態の漏出 ──────────────────────────────────────────────────────


def test_arousal_numeric_line_removed() -> None:
    """行全体が arousal: 0.85 形式なら除去。"""
    text = "arousal: 0.85\nおはよう"
    result = strip_internal_state_leakage(text)
    assert "arousal" not in result
    assert "おはよう" in result


def test_numeric_state_uppercase_removed() -> None:
    """大文字小文字バリエーション (AROUSAL=0.5 など) も除去される。"""
    text = "AROUSAL=0.5\nFATIGUE: 0.2\nやあ"
    result = strip_internal_state_leakage(text)
    assert "AROUSAL" not in result
    assert "FATIGUE" not in result
    assert "やあ" in result


def test_numeric_inside_natural_sentence_preserved() -> None:
    """自然文中の数値 (例: 「3 回目」) は除去対象外。"""
    text = "今日は 3 回目の挑戦だね、頑張ろう"
    assert strip_internal_state_leakage(text) == text


# ── S-expression 漏出 ───────────────────────────────────────────────────────


def test_sexp_interoception_form_removed() -> None:
    """(interoception ...) で始まる行は除去。"""
    text = "(interoception :private true (time-of-day morning))\nおはよう"
    result = strip_internal_state_leakage(text)
    assert "(interoception" not in result
    assert "おはよう" in result


def test_sexp_body_state_form_removed() -> None:
    """(body-state ...) 行を除去。"""
    text = "(body-state (arousal 0.5))\nそれだね"
    result = strip_internal_state_leakage(text)
    assert "body-state" not in result
    assert "それだね" in result


def test_private_true_marker_removed() -> None:
    """:private true マーカーが単独行で出てきたら除去。"""
    text = ":private true\n本文"
    result = strip_internal_state_leakage(text)
    assert ":private" not in result
    assert "本文" in result


# ── コーナーケース ──────────────────────────────────────────────────────────


def test_leakage_at_end_of_text_removed() -> None:
    """末尾の漏出ブロックも除去される。"""
    text = "おはよう、いい朝だね\n\n[Mental state]\n- affect: bright"
    result = strip_internal_state_leakage(text)
    assert result == "おはよう、いい朝だね"


def test_multiple_leakage_blocks_front_and_back() -> None:
    """前段と末尾の両方に漏出があっても両方とも除去する。"""
    text = (
        "[Mental state]\n"
        "- affect: calm\n"
        "\n"
        "本文だよ\n"
        "\n"
        "# ToM: 分析\n"
        "- 安心 (0.9)"
    )
    result = strip_internal_state_leakage(text)
    assert result == "本文だよ"


def test_trailing_whitespace_trimmed() -> None:
    """末尾の trailing whitespace は trim される。"""
    text = "こんにちは   \n\n  \n"
    result = strip_internal_state_leakage(text)
    assert result == "こんにちは"


def test_consecutive_blank_lines_collapsed() -> None:
    """漏出除去後の連続空行は 1 行にまとめる。"""
    text = "A\n\n\n\n\nB"
    result = strip_internal_state_leakage(text)
    # 空行が 1 つに圧縮される
    assert result.count("\n\n\n") == 0
    assert "A" in result and "B" in result


def test_pattern_with_leading_whitespace_removed() -> None:
    """行頭にインデントがあっても漏出パターンは検出される。"""
    text = "    - affect: calm\nお疲れ"
    result = strip_internal_state_leakage(text)
    assert "affect" not in result
    assert "お疲れ" in result


def test_combination_of_patterns_in_one_response() -> None:
    """複数のパターン種別が混在した応答も適切に整形。"""
    text = (
        "[Mental state]\n"
        "- affect: warm\n"
        "(interoception :private true)\n"
        "# ToM: 分析\n"
        "- 期待 (0.7)\n"
        "arousal: 0.4\n"
        "\n"
        "今日もよろしくね、嬉しいよ"
    )
    result = strip_internal_state_leakage(text)
    assert result == "今日もよろしくね、嬉しいよ"


def test_all_leakage_returns_empty() -> None:
    """応答が全部漏出だった場合、空文字を返す。"""
    text = "[Mental state]\n- affect: calm\n- social: alone"
    assert strip_internal_state_leakage(text) == ""


def test_word_boundary_no_false_positive() -> None:
    """自然文中の似た単語 (例: 「affection」) は誤検出しない。"""
    text = "I felt affection for the dog."
    # "- affect:" には完全一致しないので除去されない
    assert strip_internal_state_leakage(text) == text


def test_japanese_dash_bullet_handled() -> None:
    """・ (中黒) 始まりの漏出 bullet も削除。"""
    text = "・affect: 穏やか\n大丈夫?"
    result = strip_internal_state_leakage(text)
    assert "affect" not in result
    assert "大丈夫?" in result


# ── パフォーマンス/安全性 ──────────────────────────────────────────────────


def test_very_long_text_safe() -> None:
    """非常に長い入力でも例外を投げずに完了する。"""
    text = "あ" * 10000 + "\n[Mental state]\n本文"
    result = strip_internal_state_leakage(text)
    # [Mental state] は除去されている
    assert "[Mental state]" not in result
    assert "本文" in result


def test_idempotent() -> None:
    """フィルタは冪等 (二度かけても同じ結果)。"""
    text = "[Mental state]\n- affect: calm\n\nやあ"
    once = strip_internal_state_leakage(text)
    twice = strip_internal_state_leakage(once)
    assert once == twice


@pytest.mark.parametrize(
    "leak",
    [
        "[Mental state]",
        "- interoception: x",
        "- affect: y",
        "- social: z",
        "- drives: w",
        "- working-memory: q",
        "- continuity: r",
        "# ToM: analysis",
        "## エビデンス",
        "## 推論",
        "## 応答方針",
        "(interoception :private true)",
        "(body-state arousal)",
        "(tension high)",
        "(sensing low)",
        ":private true",
        "arousal: 0.5",
        "valence: -0.2",
        "fatigue: 0.9",
    ],
)
def test_each_pattern_individually_stripped(leak: str) -> None:
    """各パターンが単独で出てきても除去できる。"""
    text = f"{leak}\nuser-facing message"
    result = strip_internal_state_leakage(text)
    assert "user-facing message" in result
    # 漏出固有のキーワードが消えていることを確認
    # (キーワード単位で確認、行ごと消えるはず)
    assert leak not in result.splitlines()
