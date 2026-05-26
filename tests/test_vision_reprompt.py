"""Tests for pico_agent.vision_reprompt (Phase C-10a)。

vision 再質問 (vision キーワード AND 履歴に see 痕跡) のときだけ soft nudge を
注入すべき、という判定ロジックを検証する。実カメラ / backend は一切触らない
(純ロジックのみ)。
"""

from __future__ import annotations

from pico_agent import vision_reprompt


# ── ヘルパ: 履歴メッセージ組み立て ──────────────────────────────────────────


def _assistant_see_tool_use() -> dict:
    """assistant が see を tool_use した履歴メッセージ (Anthropic 形状)。"""
    return {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "見てみるね"},
            {"type": "tool_use", "id": "tu_1", "name": "see", "input": {}},
        ],
    }


def _tool_result_with_image() -> dict:
    """画像付き tool_result の履歴メッセージ (see の結果痕跡)。"""
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "tu_1",
                "content": [
                    {"type": "text", "text": "机の上にコップがあります"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": "FAKE",
                        },
                    },
                ],
            }
        ],
    }


def _plain_user(text: str) -> dict:
    return {"role": "user", "content": text}


def _assistant_say_tool_use() -> dict:
    """see 以外 (say) の tool_use 履歴 (see 痕跡ではない)。"""
    return {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": "tu_2", "name": "say", "input": {"text": "やあ"}},
        ],
    }


# ── force_see_suffix の内容 ────────────────────────────────────────────────


def test_force_see_suffix_content():
    """suffix は VISION マーカと see() 即時呼び出し指示を含む。"""
    suffix = vision_reprompt.force_see_suffix()
    assert "[VISION]" in suffix
    assert "see()" in suffix
    # 「再利用せず」「今すぐ」のニュアンスが含まれる (履歴画像の再利用抑制)
    assert "再利用" in suffix


# ── needs_force_see: vision キーワード × 履歴 see 有/無 ─────────────────────


def test_vision_keyword_with_see_history_triggers():
    """vision キーワード AND 履歴に see tool_use → True。"""
    messages = [
        _plain_user("ねえ、ちょっと見て"),
        _assistant_see_tool_use(),
        _tool_result_with_image(),
    ]
    assert vision_reprompt.needs_force_see("もう一回見て", messages) is True


def test_vision_keyword_with_image_tool_result_triggers():
    """vision キーワード AND 画像付き tool_result (name 無し backend) → True。"""
    # tool_use 名を持たず、画像付き tool_result のみで see 痕跡を検出する経路
    messages = [
        _plain_user("見て"),
        {"role": "assistant", "content": [{"type": "text", "text": "はい"}]},
        _tool_result_with_image(),
    ]
    assert vision_reprompt.needs_force_see("今どう？", messages) is True


def test_vision_keyword_without_see_history_does_not_trigger():
    """vision キーワードでも履歴に see 痕跡が無ければ False (初回観察)。"""
    messages = [
        _plain_user("こんにちは"),
        {"role": "assistant", "content": [{"type": "text", "text": "やあ"}]},
    ]
    assert vision_reprompt.needs_force_see("何が見える？", messages) is False
    # 空履歴も False
    assert vision_reprompt.needs_force_see("見て", []) is False


def test_non_vision_input_does_not_trigger_even_with_see_history():
    """非 vision 入力は履歴に see があっても False (回帰防止)。"""
    messages = [
        _assistant_see_tool_use(),
        _tool_result_with_image(),
    ]
    assert vision_reprompt.needs_force_see("今日は疲れたな", messages) is False
    assert vision_reprompt.needs_force_see("ありがとう", messages) is False


def test_say_tool_use_is_not_see_trace():
    """say の tool_use は see 痕跡とみなさない (vision キーワードでも False)。"""
    messages = [
        _plain_user("やあ"),
        _assistant_say_tool_use(),
    ]
    assert vision_reprompt.needs_force_see("見て", messages) is False


def test_nested_tool_result_list_is_flattened():
    """make_tool_results が返す list ネスト履歴でも see 痕跡を検出する。"""
    messages = [
        _plain_user("見て"),
        _assistant_see_tool_use(),
        # make_tool_results は [msg] の list を返し、agent はそれを append する
        [_tool_result_with_image()],
    ]
    assert vision_reprompt.needs_force_see("もう一度見て", messages) is True


def test_old_see_beyond_scan_limit_ignored():
    """古すぎる see (走査窓外) は痕跡とみなさない。"""
    messages = [_assistant_see_tool_use(), _tool_result_with_image()]
    # 走査窓 (_RECENT_SCAN_LIMIT) を超える数の無関係メッセージを後ろに積む
    for i in range(vision_reprompt._RECENT_SCAN_LIMIT + 2):
        messages.append(_plain_user(f"雑談 {i}"))
        messages.append({"role": "assistant", "content": [{"type": "text", "text": "ふむ"}]})
    assert vision_reprompt.needs_force_see("見て", messages) is False


def test_empty_keyword_set_would_not_trigger():
    """mutation 検知: vision キーワード集合が空なら何も検出しない。

    _VISION_PATTERNS を空に差し替えると vision 入力でも False になる
    (= キーワード集合がロジックの要であることを担保)。
    """
    messages = [_assistant_see_tool_use(), _tool_result_with_image()]
    # キーワードあり → 通常は True
    assert vision_reprompt.needs_force_see("見て", messages) is True
    # キーワード集合を空にすると検出されない
    saved = vision_reprompt._COMPILED_VISION
    try:
        vision_reprompt._COMPILED_VISION = ()
        assert vision_reprompt.needs_force_see("見て", messages) is False
    finally:
        vision_reprompt._COMPILED_VISION = saved
    # 復元確認
    assert vision_reprompt.needs_force_see("見て", messages) is True


def test_no_familiar_agent_import():
    """二層分離: vision_reprompt は familiar_agent を一切 import しない。"""
    import inspect

    src = inspect.getsource(vision_reprompt)
    # コメント / docstring の言及ではなく実際の import 文のみを検査する。
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            assert "familiar_agent" not in stripped, (
                f"vision_reprompt must not import familiar_agent: {stripped!r}"
            )
