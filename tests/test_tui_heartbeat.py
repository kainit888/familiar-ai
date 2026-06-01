"""Phase E: TUI heartbeat / boredom / last_interaction wiring tests.

Hermetic: the emotion default paths are redirected to tmp so the real
~/.familiar_ai files are never touched.

mutation 対応:
    - _desire_tick の heartbeat 分岐削除 → test_heartbeat_fires_over_desire が fail
    - __init__ の last_interaction 復元削除 → test_init_restores_last_interaction が fail
    - _mark_interaction の decay/persist 削除 → test_mark_interaction_decays_boredom が fail
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _redirect_emotion_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "familiar_agent.emotion.boredom._DEFAULT_PATH", tmp_path / "boredom.json"
    )
    monkeypatch.setattr(
        "familiar_agent.emotion.last_interaction._DEFAULT_PATH",
        tmp_path / "last_interaction.json",
    )


def _make_app(monkeypatch, tmp_path):
    from familiar_agent.tui import FamiliarApp

    _redirect_emotion_paths(monkeypatch, tmp_path)
    monkeypatch.delenv("FAMILIAR_AUTO_DESIRE", raising=False)
    agent = MagicMock()
    agent.config.agent_name = "A"
    agent.config.companion_name = "U"
    agent.config.auto_desire = True
    desires = MagicMock()
    with patch("familiar_agent.tui._make_banner", return_value=""), patch(
        "familiar_agent.tui.create_realtime_stt_controller", return_value=None
    ):
        app = FamiliarApp(agent, desires)
    app._log_system = MagicMock()
    app._write_log = MagicMock()
    return app


def test_init_restores_last_interaction(monkeypatch, tmp_path):
    _redirect_emotion_paths(monkeypatch, tmp_path)
    t_old = time.time() - 5000
    (tmp_path / "last_interaction.json").write_text(
        json.dumps({"timestamp": datetime.fromtimestamp(t_old).isoformat(), "kind": "chat"})
    )
    app = _make_app(monkeypatch, tmp_path)
    assert app._last_interaction == pytest.approx(t_old, abs=1.0)


def test_mark_interaction_decays_boredom(monkeypatch, tmp_path):
    app = _make_app(monkeypatch, tmp_path)
    app._boredom = MagicMock()
    app._last_interaction_store = MagicMock()
    before = app._last_interaction
    app._mark_interaction("chat")
    assert app._last_interaction >= before
    app._boredom.decay.assert_called_once()
    app._last_interaction_store.update.assert_called_once()
    assert app._last_interaction_store.update.call_args.args[0] == "chat"


@pytest.mark.asyncio
async def test_heartbeat_fires_over_desire(monkeypatch, tmp_path):
    app = _make_app(monkeypatch, tmp_path)
    # boredom high, idle long → heartbeat must fire (and NOT the normal desire path)
    now = time.time()
    app._boredom._value = 0.95
    app._boredom._updated_at = now
    app._last_interaction = now - 3600  # 1h idle
    app._agent_running = False
    app._run_agent = AsyncMock()
    with patch("familiar_agent.tui.desire_tick_prompt") as desire_tick:
        await app._desire_tick()
    app._run_agent.assert_awaited_once()
    # fired with the heartbeat prompt (inner_voice kwarg), not a desire prompt
    inner = app._run_agent.await_args.kwargs.get("inner_voice", "")
    assert "退屈" in inner or "bored" in inner
    desire_tick.assert_not_called()  # heartbeat took precedence


@pytest.mark.asyncio
async def test_no_heartbeat_when_recent_interaction(monkeypatch, tmp_path):
    app = _make_app(monkeypatch, tmp_path)
    now = time.time()
    app._boredom._value = 0.95
    app._boredom._updated_at = now
    app._last_interaction = now - 10  # just interacted → no heartbeat
    app._agent_running = False
    app._run_agent = AsyncMock()
    with patch("familiar_agent.tui.desire_tick_prompt", return_value=None):
        await app._desire_tick()
    # heartbeat must NOT have fired (idle too short)
    if app._run_agent.await_count:
        inner = app._run_agent.await_args.kwargs.get("inner_voice", "")
        assert "退屈" not in inner and "bored" not in inner
