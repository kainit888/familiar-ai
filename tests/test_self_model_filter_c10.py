"""Phase C-10: self_model 保存前検証フィルタ + 既存行クリーンアップ判定のテスト。

`pico_agent.self_model_filter` の純ロジックを検証する。familiar_agent には
依存しない (二層分離の確認も兼ねる)。

mutation 対応 (各テストが捕捉する破壊):
    - verbatim ガード削除 → verbatim 部分包含 / Jaccard 近似コピーのテストが fail
    - ラベルチェック削除 → ラベルリークのテストが fail
    - 一人称要件削除 → 一人称欠落のテストが fail
    - (d) スクリプトチェック削除 → ハングル混入のテストが fail
    - 会話エコー検出削除 → 一人称含む会話エコーのテストが fail
    - 会話エコーの AND ゲートを緩める / genuine 内省を誤検出 →
      genuine 一人称内省保持のテストが fail
    - 中国語混入検出削除 → 中国語混入行のテストが fail
"""

from __future__ import annotations

from pico_agent.self_model_filter import (
    contamination_reason,
    is_contaminated_existing_row,
    is_valid_self_model_insight,
)

# ── is_valid_self_model_insight: 保存前フィルタ ──────────────────────────────


def test_verbatim_substring_echo_rejected() -> None:
    """応答を verbatim 反射した洞察は部分包含で reject される。"""
    final = "画面に映っているのは、アニメの映像と、プログラムのコードが表示されている画面だよ。"
    # insight は応答の部分文字列 (qwen の典型的な反射)。一人称も含むが (a) で弾く。
    insight = "私は、アニメの映像と、プログラムのコードが表示されている画面だよ"
    assert is_valid_self_model_insight(insight, final) is False


def test_full_verbatim_echo_rejected() -> None:
    """応答そのものを丸写しした洞察は reject される (Jaccard ほぼ 1.0)。"""
    final = "元気だよ！話しかけてくれて嬉しいな。最近は特に変わりなく、調子がすごくいい感じ。"
    insight = "私、" + final
    assert is_valid_self_model_insight(insight, final) is False


def test_near_copy_jaccard_rejected() -> None:
    """わずかに編集しただけの近似コピーは bigram Jaccard >= 0.8 で reject される。"""
    final = "私は時計より、空の移ろいで時間を感じている。"
    insight = "私は時計より、空の移ろいで時間を感じてるね。"  # 末尾を少し変えただけ
    assert is_valid_self_model_insight(insight, final) is False


def test_label_leak_rejected() -> None:
    """プロンプトのラベル (良い例:) が漏出した洞察は reject される。"""
    insight = "良い例:\n私は、ささやかなものに惹かれる。"
    assert is_valid_self_model_insight(insight, "全然違う応答テキスト") is False


def test_label_leak_nothing_rejected() -> None:
    """'nothing' を含むラベルリークも reject される。"""
    insight = "私には nothing meaningful が見出せません。"
    assert is_valid_self_model_insight(insight, "別の応答") is False


def test_hangul_mixed_rejected() -> None:
    """ハングル (異言語スクリプト) 混入は reject される (条件 d)。"""
    insight = "私は 의미가 ある存在だと気づいた。"
    assert is_valid_self_model_insight(insight, "全く別の応答内容") is False


def test_no_first_person_rejected() -> None:
    """一人称『私』を含まない洞察は reject される (条件 b)。"""
    insight = "ささやかなものに、何か秘密が隠れているような気がして惹かれる。"
    assert is_valid_self_model_insight(insight, "無関係な応答テキスト") is False


def test_none_empty_short_rejected() -> None:
    """None / 空 / 短すぎる入力は reject される。"""
    assert is_valid_self_model_insight(None, "応答") is False
    assert is_valid_self_model_insight("", "応答") is False
    assert is_valid_self_model_insight("私", "応答") is False  # _MIN_LEN 未満


def test_too_long_rejected() -> None:
    """過長 (_MAX_LEN 超) の洞察は reject される。"""
    insight = "私は" + "あ" * 250
    assert is_valid_self_model_insight(insight, "応答") is False


def test_valid_insight_accepted() -> None:
    """応答と非類似で『私』を含む正常な洞察は accept される。"""
    final = "うん、元気だよ！画面にはコードが映ってるよ。"
    insight = "私はアニメの映像とプログラムのコードが交差した表示画面に魅せられた。"
    assert is_valid_self_model_insight(insight, final) is True


def test_valid_insight_accepted_empty_final_text() -> None:
    """final_text 空なら verbatim 比較 (a) はスキップされ、(b)(c)(d) のみ適用。"""
    insight = "私は時計より、空の移ろいで時間を感じている。"
    assert is_valid_self_model_insight(insight, "") is True


def test_empty_final_text_skips_verbatim_but_applies_others() -> None:
    """final_text 空でも (b) 一人称欠落は弾かれる。"""
    insight = "ささやかなものに惹かれる。"  # 私 なし
    assert is_valid_self_model_insight(insight, "") is False


# ── is_contaminated_existing_row: DB クリーンアップ判定 ──────────────────────


def test_contaminated_label_leak_row() -> None:
    """ラベルリーク行は汚染と判定される。"""
    content = "良い例:\n私は、ピコさんの元気な情熱に気づく。"
    assert is_contaminated_existing_row(content) is True
    assert contamination_reason(content) == "label_leak"


def test_contaminated_hangul_row() -> None:
    """ハングル混入行は汚染と判定される。"""
    content = "何も 의미가 없는 답입니다."
    assert is_contaminated_existing_row(content) is True
    assert contamination_reason(content) == "unexpected_script"


def test_contaminated_english_row() -> None:
    """英語 verbatim (ASCII 過多) 行は異言語として汚染と判定される。"""
    content = "I get drawn to ordinary things that seem to hold a secret."
    assert is_contaminated_existing_row(content) is True
    assert contamination_reason(content) == "ascii_heavy_wrong_language"


def test_contaminated_response_tone_no_first_person_row() -> None:
    """一人称欠落 AND 応答調末尾の口語反射行は汚染と判定される。

    会話フィラーを含まないため `conversational_echo` ではなく一人称欠落 AND
    応答調の経路で捕捉される (両経路の役割分担を pin する)。
    """
    content = "今日はとてもいい感じだよ。"
    assert is_contaminated_existing_row(content) is True
    assert contamination_reason(content) == "no_first_person_response_tone"


def test_legit_japanese_insight_row_kept() -> None:
    """正常な日本語一人称洞察行は保持される (誤削除しない)。"""
    content = "私はアニメの映像とプログラムのコードが交差した表示画面に魅せられた。"
    assert is_contaminated_existing_row(content) is False
    assert contamination_reason(content) == ""


def test_first_person_alone_not_deleted() -> None:
    """一人称欠落『単独』では削除しない (応答調と AND を取る)。"""
    # 私 なし・応答調でもない自然な観察文 → 保持
    content = "空の色が少しずつ移ろっていく様子に気づく。"
    assert is_contaminated_existing_row(content) is False


def test_contaminated_first_person_conversational_echo() -> None:
    """一人称『私』を含む会話エコー行は汚染と判定される (C-9 再発の元凶)。

    口語末尾 (どう？) AND 会話フィラー (元気だよ / 話しかけてくれて / そっちは)
    の AND ゲートで捕捉する。一人称があるため `no_first_person_response_tone`
    では拾えなかった行 (mutation: 会話エコー検出を削除すると fail)。
    """
    content = "私、元気だよ！話しかけてくれて嬉しいな。そっちはどう？"
    assert is_contaminated_existing_row(content) is True
    assert contamination_reason(content) == "conversational_echo"


def test_contaminated_clip_echo_disclaimer_row() -> None:
    """LLM 定型 disclaimer (clip echo) 行も会話エコーとして汚染判定される。"""
    content = "そのような状況では私には、それが何を示しているかは分かりません。"
    assert is_contaminated_existing_row(content) is True
    assert contamination_reason(content) == "conversational_echo"


def test_genuine_first_person_insight_preserved() -> None:
    """genuine な一人称内省は会話エコー検出で誤削除されない (最優先要件)。

    会話フィラーを持たず、断定/内省末尾 (惹かれる。/魅せられた。) のため応答調
    末尾にも該当しない → AND ゲートで保持される (mutation: AND を OR に緩める /
    内省末尾を応答調に含めると fail)。
    """
    for content in (
        "私はささやかなものに惹かれる。",
        "私はアニメの映像とコードに魅せられた。",
    ):
        assert is_contaminated_existing_row(content) is False
        assert contamination_reason(content) == ""


def test_contaminated_chinese_mixin_row() -> None:
    """中国語混入行 (漢字のみ・仮名ゼロ) は異言語として汚染判定される (条件 d 補完)。"""
    content = (
        "我想尝试更多的地方，尤其是丰桥，还有可能亲自品尝当地的美食，"
        "以及学习新技能，如编程和电子工作。"
    )
    assert is_contaminated_existing_row(content) is True
    assert contamination_reason(content) == "chinese_mixin"


def test_save_time_conversational_echo_rejected_empty_final() -> None:
    """保存前: final_text 空でも会話エコーは弾く (a 補完で既存行判定と整合)。"""
    insight = "私、元気だよ！話しかけてくれて嬉しいな。そっちはどう？"
    assert is_valid_self_model_insight(insight, "") is False


def test_cleanup_predicate_idempotent() -> None:
    """同じ入力を 2 回判定しても結果は変わらない (冪等性の素地)。

    クリーンアップ後に残る行 (= 判定 False) は再実行でも False のまま →
    2 回目の削除候補は 0 件になる。
    """
    rows = [
        "I get drawn to ordinary things.",  # 英語 → del
        "良い例:\n私は…",  # ラベル → del
        "私はアニメの映像に魅せられた。",  # 正常 → keep
        "空の移ろいに時間を感じる私。",  # 正常 → keep
    ]
    first = [r for r in rows if is_contaminated_existing_row(r)]
    # クリーンアップ後に残るのは keep 行のみ
    survivors = [r for r in rows if not is_contaminated_existing_row(r)]
    # 残存行を再判定 → 削除候補 0 件 (冪等 no-op)
    second = [r for r in survivors if is_contaminated_existing_row(r)]
    assert len(first) == 2
    assert second == []
