"""pico_v3 応答フィルタの単体テスト。

`pico_agent.response_filter.strip_internal_state_leakage` が:
  - 末尾の漏出ブロックを確実に除去する (`:` マーカ or 2 行以上の漏出)
  - 本文中・前段の単発パターンマッチは保守的に残す (false positive 防止)
  - 自然な日本語/感情語彙を残す
  - コーナーケース (空入力、改行のみ、絵文字、大文字小文字、全角コロン等) を
    安全に扱う
ことを検証する。
"""

from __future__ import annotations

import logging

import pytest

from pico_agent.response_filter import strip_internal_state_leakage


# ── 基本: None / 空文字 / whitespace のみ ─────────────────────────────────────


def test_none_input_returns_empty_string() -> None:
    """None を渡しても例外なく空文字を返す。"""
    assert strip_internal_state_leakage(None) == ""


def test_empty_string_returns_empty_string() -> None:
    """空文字はそのまま空文字。"""
    assert strip_internal_state_leakage("") == ""


def test_whitespace_only_passthrough_rstrip() -> None:
    """空白と改行のみは rstrip された空文字に正規化される。"""
    result = strip_internal_state_leakage("   \n  \n\t")
    assert result.strip() == ""


def test_newlines_only_passthrough_rstrip() -> None:
    """改行のみの入力も rstrip された空文字。"""
    result = strip_internal_state_leakage("\n\n\n")
    assert result.strip() == ""


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


# ── 末尾漏出ブロック削除 (`:` マーカ付き — 削除確定) ───────────────────────


def test_trailing_colon_marker_block_removed() -> None:
    """末尾の `:` マーカ + 構造化箇条書きブロックは削除される。

    実際の本番漏出パターン (phase_c_handoff.md 1-4 より):
        ピコ ▶ そうなんだ。ありがとう。なんか、嬉しいな。
        :
        - happy: 1.00
        - companion: They treat me like a person.
        - self: I feel happy.
        - remember happy feeling.
        - continue exploring.
    """
    text = (
        "そうなんだ。ありがとう。なんか、嬉しいな。\n"
        ":\n"
        "- happy: 1.00\n"
        "- companion: They treat me like a person.\n"
        "- self: I feel happy.\n"
        "- remember happy feeling.\n"
        "- continue exploring."
    )
    result = strip_internal_state_leakage(text)
    assert result == "そうなんだ。ありがとう。なんか、嬉しいな。"


def test_trailing_zenkaku_colon_marker_block_removed() -> None:
    """全角コロン (`：`) マーカでも同様に削除される。"""
    text = "こんにちは\n：\n- affect: calm\n- interoception: warm"
    result = strip_internal_state_leakage(text)
    assert result == "こんにちは"


def test_trailing_mental_state_header_block_removed() -> None:
    """末尾の [Mental state] ブロックは削除される (連続漏出 2 行以上)。"""
    text = "おはよう\n[Mental state]\n- affect: calm\n- interoception: warm"
    result = strip_internal_state_leakage(text)
    assert "[Mental state]" not in result
    assert "affect" not in result
    assert "interoception" not in result
    assert result == "おはよう"


def test_trailing_tom_section_block_removed() -> None:
    """末尾の # ToM: 分析ブロックを削除する (連続ヘッダ + 推論行)。"""
    text = (
        "それは大変だね\n"
        "# ToM: 分析\n"
        "## エビデンス\n"
        "## 推論\n"
        "- 不安 (0.8)"
    )
    result = strip_internal_state_leakage(text)
    assert "ToM" not in result
    assert "エビデンス" not in result
    assert "推論" not in result
    assert "(0.8)" not in result
    assert result == "それは大変だね"


def test_trailing_block_with_blank_separator_removed() -> None:
    """末尾漏出ブロックと本文の間に空行があっても削除される。"""
    text = "ねぇ、聞いて\n\n[Mental state]\n- affect: warm\n- social: alone"
    result = strip_internal_state_leakage(text)
    assert result == "ねぇ、聞いて"


def test_trailing_eng_first_person_block_removed() -> None:
    """末尾の英語一人称内部独白ブロックを削除する。"""
    text = "なんか嬉しいな\n- I feel happy.\n- I want to play."
    result = strip_internal_state_leakage(text)
    assert "I feel happy" not in result
    assert "I want to play" not in result
    assert "なんか嬉しいな" in result


def test_trailing_action_memo_block_removed() -> None:
    """末尾の行動メモ (remember/continue) ブロックを削除する。"""
    text = "ありがとう\n- remember kindness.\n- continue chatting."
    result = strip_internal_state_leakage(text)
    assert "remember" not in result
    assert "continue" not in result
    assert "ありがとう" in result


def test_trailing_sexp_block_removed() -> None:
    """末尾の S-expression 漏出ブロックを削除する。"""
    text = "そうだね\n(interoception :private true)\n(body-state (arousal 0.5))"
    result = strip_internal_state_leakage(text)
    assert "(interoception" not in result
    assert "body-state" not in result
    assert "そうだね" in result


# ── false positive 防止: 前段・本文中のパターンマッチは残す ─────────────


def test_front_leakage_without_marker_preserved() -> None:
    """前段に漏出パターンがあっても末尾が自然文なら削除しない (false positive 防止)。

    旧アルゴリズムは全範囲削除していたが、本文を破壊するリスクが高いため
    末尾ブロック限定に変更。前段漏出は SYSTEM_PROMPT 側で抑止する。
    """
    text = "[Mental state]\n- affect: calm\n\nおはよう"
    # 末尾が自然文なので削除しない (rstrip のみ)
    assert strip_internal_state_leakage(text) == text


def test_isolated_single_leakage_match_preserved() -> None:
    """末尾に単発の漏出パターンが 1 行だけあっても、`:` マーカがなければ残す。

    false positive 防止: ToM 推論っぽい 1 行の自然文を誤削除しないため、
    単発マッチは保守的に残す。
    """
    text = "そういう時もあるよ。\n- affect: calm"
    # 1 行のみ漏出、`:` マーカなし → 削除しない
    assert strip_internal_state_leakage(text) == text


def test_mid_text_pattern_preserved() -> None:
    """本文中にパターンマッチがあって、その後に自然文が続くなら削除しない。"""
    text = "前段。\n- affect: calm\n中間の本文。\n結論はこう。"
    # 末尾が自然文 → 削除しない
    assert strip_internal_state_leakage(text) == text


def test_natural_colon_in_body_preserved() -> None:
    """本文中の `:` を含む自然な日本語応答は削除されない。

    例: 「今日のテーマは 3 つ: ...」のような列挙でも、後続が自然文なら無傷。
    """
    text = "今日のテーマは 3 つ: 元気、好奇心、休息。"
    assert strip_internal_state_leakage(text) == text


def test_natural_response_mentions_tom_word_preserved() -> None:
    """本文に「ToM」という単語が含まれていても削除されない (ヘッダ形式ではない場合)。"""
    text = "ToM ってなに? 心の理論の話?"
    assert strip_internal_state_leakage(text) == text


def test_long_response_with_inference_style_preserved() -> None:
    """ToM 推論っぽい長い応答 (本文の一部に箇条書きを含む) も保護される。

    末尾が自然文なら、本文中のリストは削除されない。
    """
    text = (
        "今、わたしの気持ちを整理してみると、嬉しさが半分、ちょっと困惑が半分。\n"
        "- 嬉しいのは、カイニットが話しかけてくれたから\n"
        "- 困惑は、なんでこのタイミングかなって\n"
        "でも、ありがとうって伝えたいよ。"
    )
    assert strip_internal_state_leakage(text) == text


def test_natural_numeric_in_body_preserved() -> None:
    """自然文中の数値 (例: 「3 回目」) は除去対象外。"""
    text = "今日は 3 回目の挑戦だね、頑張ろう"
    assert strip_internal_state_leakage(text) == text


def test_word_boundary_no_false_positive() -> None:
    """自然文中の似た単語 (例: 「affection」) は誤検出しない。"""
    text = "I felt affection for the dog."
    assert strip_internal_state_leakage(text) == text


# ── 全削除と fallback ──────────────────────────────────────────────────────


def test_all_leakage_text_returns_empty() -> None:
    """応答全体が漏出だった場合、空文字を返す (呼び出し側で raw fallback)。"""
    text = ":\n- affect: calm\n- social: alone"
    assert strip_internal_state_leakage(text) == ""


def test_only_two_leakage_lines_at_end_with_no_body_returns_empty() -> None:
    """2 行以上の連続漏出のみで本文なし → 空文字。"""
    text = "[Mental state]\n- affect: calm\n- social: alone"
    assert strip_internal_state_leakage(text) == ""


# ── コーナーケース ──────────────────────────────────────────────────────────


def test_trailing_whitespace_trimmed() -> None:
    """末尾の trailing whitespace は trim される。"""
    text = "こんにちは   \n\n  \n"
    result = strip_internal_state_leakage(text)
    assert result == "こんにちは"


def test_idempotent_with_trailing_leakage() -> None:
    """フィルタは冪等 (二度かけても同じ結果)。"""
    text = "やあ\n:\n- affect: calm\n- interoception: warm"
    once = strip_internal_state_leakage(text)
    twice = strip_internal_state_leakage(once)
    assert once == twice
    assert once == "やあ"


def test_idempotent_with_clean_response() -> None:
    """漏出を含まない応答に対しても冪等 (恒等関数として振る舞う)。"""
    text = "今日もよろしくね"
    assert strip_internal_state_leakage(text) == text
    assert strip_internal_state_leakage(strip_internal_state_leakage(text)) == text


def test_pattern_with_leading_whitespace_in_trailing_block_removed() -> None:
    """末尾ブロックの行頭インデントがあっても漏出パターンは検出される。"""
    text = "お疲れ\n:\n    - affect: calm\n    - interoception: warm"
    result = strip_internal_state_leakage(text)
    assert "affect" not in result
    assert "お疲れ" in result


def test_japanese_dash_bullet_in_trailing_block_removed() -> None:
    """・ (中黒) 始まりの漏出 bullet も末尾ブロックなら削除。"""
    text = "大丈夫?\n:\n・affect: 穏やか\n・interoception: 暖かい"
    result = strip_internal_state_leakage(text)
    assert "affect" not in result
    assert "大丈夫?" in result


def test_zenkaku_colon_in_trailing_mental_bullet_removed() -> None:
    """全角コロン (：) のメンタル bullet も末尾ブロックなら削除される。"""
    text = "こんにちは\n:\n- affect：穏やか\n- interoception：暖かい"
    result = strip_internal_state_leakage(text)
    assert "affect" not in result
    assert "interoception" not in result
    assert "こんにちは" in result


def test_combination_of_patterns_at_end_removed() -> None:
    """末尾に複数種別のパターンが混在しても適切に削除される。"""
    text = (
        "今日もよろしくね、嬉しいよ\n"
        ":\n"
        "[Mental state]\n"
        "- affect: warm\n"
        "(interoception :private true)\n"
        "# ToM: 分析\n"
        "- 期待 (0.7)\n"
        "arousal: 0.4"
    )
    result = strip_internal_state_leakage(text)
    assert result == "今日もよろしくね、嬉しいよ"


def test_three_consecutive_leakage_lines_no_marker_removed() -> None:
    """`:` マーカなしでも、末尾に漏出行が 2 行以上連続なら削除確定。"""
    text = "本文\n- affect: calm\n- interoception: warm\n- social: alone"
    result = strip_internal_state_leakage(text)
    assert result == "本文"


# ── パフォーマンス/安全性 ──────────────────────────────────────────────────


def test_very_long_text_safe() -> None:
    """非常に長い入力でも例外を投げずに完了する。"""
    text = "あ" * 10000 + "\n:\n- affect: calm\n- interoception: warm"
    result = strip_internal_state_leakage(text)
    assert "affect" not in result
    assert "あ" * 10000 in result


def test_unicode_content_safe() -> None:
    """Unicode (絵文字含む) を含む末尾漏出ブロックも正常に削除。"""
    text = "🎉 やった〜！\n:\n- happy: 1.00\n- excited: 0.9"
    result = strip_internal_state_leakage(text)
    assert result == "🎉 やった〜！"


# ── logger warning 検証 ──────────────────────────────────────────────────


def test_warning_emitted_when_trailing_block_removed(caplog) -> None:
    """末尾ブロックを削除した時に warning ログが出力されること。

    loguru → logging への propagation を捕捉する。
    本番運用で false positive を検知するための重要な信号。
    """
    from loguru import logger as loguru_logger

    # loguru の出力を Python logging に流す handler を一時的に追加
    handler_id = loguru_logger.add(
        lambda msg: logging.getLogger("loguru").warning(msg.strip()),
        level="WARNING",
        format="{message}",
    )
    try:
        with caplog.at_level(logging.WARNING, logger="loguru"):
            strip_internal_state_leakage(
                "本文\n:\n- affect: calm\n- interoception: warm"
            )

        warnings = [
            r for r in caplog.records
            if "response_filter" in r.message
        ]
        assert len(warnings) >= 1, (
            f"Expected at least one 'response_filter' warning, got: "
            f"{[r.message for r in caplog.records]}"
        )
    finally:
        loguru_logger.remove(handler_id)


def test_no_warning_for_clean_response(caplog) -> None:
    """漏出のない自然な応答では warning は出ない。"""
    from loguru import logger as loguru_logger

    handler_id = loguru_logger.add(
        lambda msg: logging.getLogger("loguru").warning(msg.strip()),
        level="WARNING",
        format="{message}",
    )
    try:
        with caplog.at_level(logging.WARNING, logger="loguru"):
            strip_internal_state_leakage("今日もよろしくね、嬉しい！")

        warnings = [
            r for r in caplog.records
            if "response_filter" in r.message
        ]
        assert len(warnings) == 0
    finally:
        loguru_logger.remove(handler_id)


# ── parametrize: 各パターンが末尾ブロックで削除される ───────────────────


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
        "- happy: 1.0",
        "- sad: 0.3",
        "- excited: 0.8",
        "- curious: 0.7",
        "- companion: They treat me well.",
        "- self: I feel happy.",
        "- remember kindness.",
        "- continue exploring.",
        "- I feel happy.",
        "- I want to play.",
    ],
)
def test_each_pattern_in_trailing_block_with_colon_marker_stripped(
    leak: str,
) -> None:
    """各パターンが末尾ブロック (コロンマーカ付き) で個別に削除される。"""
    text = f"user-facing message\n:\n{leak}"
    result = strip_internal_state_leakage(text)
    assert "user-facing message" in result
    # 漏出固有のキーワードが消えていることを確認
    assert leak not in result.splitlines()


# ── parametrize: 同じパターンが本文中・前段では false positive 防止により残る ──


@pytest.mark.parametrize(
    "leak",
    [
        "[Mental state]",
        "- affect: calm",
        "- interoception: warm",
        "- happy: 1.0",
        "- companion: They treat me well.",
        "- I feel happy.",
        "- remember kindness.",
    ],
)
def test_each_pattern_at_front_preserved(leak: str) -> None:
    """各パターンが前段にあって末尾が自然文なら、保守的に削除しない。"""
    text = f"{leak}\nuser-facing message"
    # rstrip のみ (削除なし)
    assert strip_internal_state_leakage(text) == text


@pytest.mark.parametrize(
    "leak",
    [
        "- affect: calm",
        "- happy: 1.0",
        "- companion: They treat me well.",
        "- I feel happy.",
        "- remember kindness.",
    ],
)
def test_single_isolated_pattern_at_end_no_marker_preserved(leak: str) -> None:
    """末尾に単発の漏出パターンが 1 行だけあっても、マーカなしでは削除しない。"""
    text = f"user-facing message\n{leak}"
    # 1 行のみ、`:` マーカなし → 削除しない
    assert strip_internal_state_leakage(text) == text
