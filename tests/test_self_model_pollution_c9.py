"""Phase C-9 regression: self_model verbatim-echo pollution into first-turn prompt.

Investigation finding (Phase C-9): the utility backend (qwen2.5:1.5b) frequently
fails to abstract a first-person self-insight from `_SELF_MODEL_PROMPT` and instead
reflects the response text verbatim. That verbatim row is stored as kind='self_model'
and, on the next session's first turn, `_morning_reconstruction` injects it into the
system prompt under `[自分という存在 ...]`. Gemini Flash Lite then echoes it back,
producing the "same response repeated" bug.

These tests pin the *mechanism* (storage -> recall -> prompt block) using only the
real `ObservationMemory` API. They do NOT assert the qwen behavior itself (that is a
runtime/model property, reproduced separately via Ollama curl in the C-9 report).

When the fix lands (e.g. validating that a self_model insight is not a verbatim copy
of the response, or strengthening the prompt/model), the pollution test below should
be updated to assert the guard rejects the verbatim row.
"""

from __future__ import annotations

from unittest.mock import patch

from familiar_agent.tools.memory import ObservationMemory, _EmbeddingModel
from pico_agent.self_model_filter import is_valid_self_model_insight


def _make_mem(tmp_path, name: str) -> ObservationMemory:
    with (
        patch.object(_EmbeddingModel, "pre_warm"),
        patch.object(_EmbeddingModel, "encode_document", return_value=[[0.1, 0.2, 0.3]]),
        patch.object(_EmbeddingModel, "encode_query", return_value=[[0.1, 0.2, 0.3]]),
    ):
        return ObservationMemory(db_path=str(tmp_path / name))


def test_verbatim_response_self_model_reaches_first_turn_prompt_block(tmp_path) -> None:
    """A verbatim-echo self_model row appears, unfiltered, in the morning prompt block.

    This documents the C-9 pollution path: there is currently no guard between
    storing a self_model row and projecting it into the first-turn system prompt.
    """
    # This is a verbatim response echo (NOT a first-person insight) of the kind
    # observed in the real DB at 2026-05-25 12:56 / app.log:65-66.
    verbatim_echo = "画面に映っているのは、アニメの映像と、プログラムのコードが表示されている画面だよ。"

    mem = _make_mem(tmp_path, "c9_pollution.db")
    try:
        assert mem.save(verbatim_echo, kind="self_model", emotion="moved")

        # recall_self_model is what _morning_reconstruction calls on first turn.
        rows = mem.recall_self_model(n=5)
        assert rows, "self_model row should be recalled"

        # format_self_model_for_context builds the literal prompt block.
        block = mem.format_self_model_for_context(rows)
    finally:
        mem.close()

    # The verbatim response is presented to the LLM as the agent's *self-image*.
    # NOTE: this pins the *mechanism* — once a verbatim row is stored, it reaches
    # the first-turn prompt. The Phase C-10 fix prevents such a row from ever being
    # stored (asserted below), so the pollution path is cut off at the source.
    assert "[自分という存在" in block
    assert verbatim_echo in block, (
        "verbatim response echo leaks unfiltered into the first-turn self-image block; "
        "this is the C-9 pollution mechanism"
    )


def test_c10_guard_rejects_verbatim_echo() -> None:
    """Phase C-10 fix: the save-time filter rejects a verbatim-echo self_model.

    The C-9 pollution mechanism (stored row -> first-turn prompt -> Gemini echo)
    is cut off at the source: `_update_self_model` calls
    `is_valid_self_model_insight(insight, final_text)` and skips the save when it
    returns False. A verbatim echo of `final_text` must be rejected.
    """
    verbatim_echo = "画面に映っているのは、アニメの映像と、プログラムのコードが表示されている画面だよ。"
    # final_text is the same response the insight was (wrongly) reflected from.
    assert is_valid_self_model_insight(verbatim_echo, final_text=verbatim_echo) is False


def test_self_model_block_is_empty_when_no_rows(tmp_path) -> None:
    """Sanity: with no self_model rows, the block is empty (no spurious injection)."""
    mem = _make_mem(tmp_path, "c9_empty.db")
    try:
        block = mem.format_self_model_for_context(mem.recall_self_model(n=5))
    finally:
        mem.close()
    assert block == ""
