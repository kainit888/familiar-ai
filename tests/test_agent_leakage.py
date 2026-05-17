"""EmbodiedAgent.run() の漏出フィルタ統合の動作検証。

- 2 つの return パス (end_turn / max-iterations fallback) でフィルタが効くこと
- フィルタは「ユーザー宛返却値」だけでなく、post-response pipeline / TTS
  auto-say / mental_state_bus に渡される text にも適用されること
  (TTS adapter 経由で漏出が音声化されることを防ぐため)
- 漏出のない応答は変化しないこと
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from familiar_agent.backend import TurnResult

from tests.test_agent_react_loop import _HEAVY_PATCHES, _make_agent, _turn


def _patch_heavy_with(extra: dict | None = None):
    """test_agent_react_loop と同じ heavy-patch を適用するヘルパ。"""
    patches = dict(_HEAVY_PATCHES)
    if extra:
        patches.update(extra)
    return [patch(target, new) for target, new in patches.items()]


@pytest.mark.asyncio
async def test_run_strips_mental_state_leakage_from_return_value() -> None:
    """end_turn 経路: 漏出を含む応答からユーザー宛応答だけがフィルタされる。"""
    agent = _make_agent()
    leaked_text = (
        "[Mental state]\n"
        "- affect: calm\n"
        "- interoception: warm\n"
        "\n"
        "おはよう、いい朝だね"
    )
    agent.backend.stream_turn = AsyncMock(
        return_value=(_turn("end_turn", text=leaked_text), leaked_text)
    )

    ps = _patch_heavy_with()
    for p in ps:
        p.start()
    try:
        result = await agent.run("おはよう")
    finally:
        for p in ps:
            p.stop()

    # ユーザー宛応答は filter 後
    assert "[Mental state]" not in result
    assert "affect" not in result
    assert "interoception" not in result
    assert "おはよう、いい朝だね" in result


@pytest.mark.asyncio
async def test_run_keeps_clean_response_unchanged() -> None:
    """漏出を含まない自然な応答はそのまま返される (filter は無害)。"""
    agent = _make_agent()
    clean_text = "今日もよろしくね、嬉しい!"
    agent.backend.stream_turn = AsyncMock(
        return_value=(_turn("end_turn", text=clean_text), clean_text)
    )

    ps = _patch_heavy_with()
    for p in ps:
        p.start()
    try:
        result = await agent.run("おはよう")
    finally:
        for p in ps:
            p.stop()

    assert result == clean_text


@pytest.mark.asyncio
async def test_run_post_response_pipeline_receives_filtered_text() -> None:
    """漏出フィルタは post-response pipeline に渡される final_text にも適用される。

    検証方法: _run_post_response_pipeline AsyncMock の **呼び出し引数** の
    `final_text` が、フィルタ適用後の漏出を含まない text であることを確認する。
    実際の background task await は test 終了後に走るので、call_args の検査で
    十分。
    """
    agent = _make_agent()
    leaked_text = "[Mental state]\n- affect: calm\n- interoception: warm\n\nやあ"
    agent.backend.stream_turn = AsyncMock(
        return_value=(_turn("end_turn", text=leaked_text), leaked_text)
    )

    pipeline_mock = AsyncMock()
    extra = {
        "familiar_agent.agent.EmbodiedAgent._run_post_response_pipeline": pipeline_mock,
    }
    ps = _patch_heavy_with(extra)
    for p in ps:
        p.start()
    try:
        result = await agent.run("おはよう")
    finally:
        for p in ps:
            p.stop()

    # 返却値は filter 済み
    assert "[Mental state]" not in result
    assert "やあ" in result

    # pipeline も filter 後 text を受け取っているはず
    assert pipeline_mock.call_count >= 1
    kwargs = pipeline_mock.call_args.kwargs
    assert "[Mental state]" not in kwargs["final_text"]
    assert "affect" not in kwargs["final_text"]
    assert "interoception" not in kwargs["final_text"]
    assert "やあ" in kwargs["final_text"]


@pytest.mark.asyncio
async def test_run_mental_state_bus_append_called_with_clean_response() -> None:
    """`_mental_state_bus.append` は filter 後 text が空でない時のみ呼ばれる。

    実装上 `_mental_state_bus.append(mental_snapshot)` は別オブジェクト
    (MentalStateSnapshot) を渡しており filter とは独立だが、`filtered_text`
    が空でない時のみ append が呼ばれる経路をテストする。
    """
    agent = _make_agent()
    bus_mock = MagicMock()
    bus_mock.summarize_recent_for_prompt = MagicMock(return_value="")
    agent._mental_state_bus = bus_mock

    leaked_text = "[Mental state]\n- affect: warm\n\n本文"
    agent.backend.stream_turn = AsyncMock(
        return_value=(_turn("end_turn", text=leaked_text), leaked_text)
    )

    ps = _patch_heavy_with()
    for p in ps:
        p.start()
    try:
        result = await agent.run("やあ")
    finally:
        for p in ps:
            p.stop()

    # 返却値は filter 済み
    assert result == "本文"
    # mental_state_bus.append が呼ばれていること (filter 後 text が空でないため)
    assert bus_mock.append.called


@pytest.mark.asyncio
async def test_run_tts_auto_say_receives_filtered_text() -> None:
    """auto-say 有効時、TTS には filter 後 final_text が流れる (漏出の音声化防止)。"""
    agent = _make_agent(with_tts=True)
    agent.config.auto_say = True

    leaked_text = "[Mental state]\n- affect: bright\n- interoception: warm\n\nおはよう"
    agent.backend.stream_turn = AsyncMock(
        return_value=(_turn("end_turn", text=leaked_text), leaked_text)
    )

    ps = _patch_heavy_with()
    for p in ps:
        p.start()
    try:
        result = await agent.run("hi")
    finally:
        for p in ps:
            p.stop()

    # 返却値は filter 済み
    assert "[Mental state]" not in result
    assert "おはよう" in result

    # TTS は filter 後 final_text を受け取っている (漏出は音声化されない)
    assert agent._tts.call.await_count == 1
    call_args = agent._tts.call.await_args
    assert call_args.args[0] == "say"
    spoken_text = call_args.args[1]["text"]
    assert "[Mental state]" not in spoken_text
    assert "affect" not in spoken_text
    assert "interoception" not in spoken_text
    assert "おはよう" in spoken_text


@pytest.mark.asyncio
async def test_run_max_iterations_path_strips_leakage() -> None:
    """max-iter fallback 経路でも漏出フィルタが効く。"""
    agent = _make_agent()

    # tool_use を MAX_ITERATIONS 回繰り返して max-iter fallback まで到達させる
    from familiar_agent.agent import MAX_ITERATIONS
    from familiar_agent.backend import ToolCall

    tc = ToolCall(id="tc1", name="remember", input={"content": "x"})
    tool_turn = TurnResult(stop_reason="tool_use", text="", tool_calls=[tc])
    # MAX_ITERATIONS 回 tool_use を返す
    iter_results = [(tool_turn, None) for _ in range(MAX_ITERATIONS)]
    # その後 fallback の最終 stream_turn 呼び出しで漏出付き text を返す
    leaked_text = "[Mental state]\n- affect: tired\n\nもう限界かも"
    final_turn = TurnResult(stop_reason="end_turn", text=leaked_text)
    iter_results.append((final_turn, leaked_text))

    agent.backend.stream_turn = AsyncMock(side_effect=iter_results)

    ps = _patch_heavy_with()
    for p in ps:
        p.start()
    try:
        result = await agent.run("test")
    finally:
        for p in ps:
            p.stop()

    # max-iter fallback でも filter が効いている
    assert "[Mental state]" not in result
    assert "affect" not in result
    assert "もう限界かも" in result


@pytest.mark.asyncio
async def test_run_full_leakage_response_falls_back_to_raw() -> None:
    """応答が全部漏出だった場合、空文字を避けて raw final_text を返す (fallback)。"""
    agent = _make_agent()
    all_leaked = "[Mental state]\n- affect: calm\n- social: alone"
    agent.backend.stream_turn = AsyncMock(
        return_value=(_turn("end_turn", text=all_leaked), all_leaked)
    )

    ps = _patch_heavy_with()
    for p in ps:
        p.start()
    try:
        result = await agent.run("test")
    finally:
        for p in ps:
            p.stop()

    # filter 結果が空文字でも、raw text にフォールバック (空応答を避ける)
    # 実装: `strip_internal_state_leakage(final_text) or final_text`
    assert result == all_leaked
