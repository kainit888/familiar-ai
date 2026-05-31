"""Phase X Stage C: TUI ↔ tapo_event 配線テスト。

Tapo ONVIF event 購読 (tapo_event.start_event_subscription) を UI 層から配線し、
イベント callback が desires.boost(visual=True) を呼ぶことを検証する。実 ONVIF は
叩かない (start_event_subscription を AsyncMock 化)。

mutation 対応:
    - _on_tapo_event の look_around boost 削除 → test_on_tapo_event_motion_* が fail
    - person で greet_companion を boost しない → test_on_tapo_event_person_* が fail
    - visual=True を渡さない → 両 test が fail
    - 配線 (on_event 引数) 崩し → test_start_tapo_calls_subscription_* が fail
    - disabled ゲート崩し → test_tapo_disabled_off / test_start_skipped_when_disabled が fail
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pico_agent.adapters.tapo_event import TapoEvent


def _make_app(tapo_mode: str | None = None, monkeypatch=None):
    from familiar_agent.tui import FamiliarApp

    if monkeypatch is not None:
        if tapo_mode is None:
            monkeypatch.delenv("TAPO_EVENT_MODE", raising=False)
        else:
            monkeypatch.setenv("TAPO_EVENT_MODE", tapo_mode)
        # STT は本テストの関心外、env を固定しておく
        monkeypatch.delenv("TAPO_EVENT_BOOST_AMOUNT", raising=False)

    agent = MagicMock()
    agent.config.agent_name = "A"
    agent.config.companion_name = "U"
    agent.stt = MagicMock()
    desires = MagicMock()

    with patch("familiar_agent.tui._make_banner", return_value=""), patch(
        "familiar_agent.tui.create_realtime_stt_controller", return_value=None
    ):
        app = FamiliarApp(agent, desires)
    app._log_system = MagicMock()
    app._write_log = MagicMock()
    return app


# ── 属性 / env デフォルト ──────────────────────────────────────────────────────


def test_tapo_attributes_exist(monkeypatch):
    app = _make_app(monkeypatch=monkeypatch)
    assert hasattr(app, "_tapo_event_task")
    assert app._tapo_event_task is None
    assert hasattr(app, "_tapo_events_enabled")


def test_tapo_default_on(monkeypatch):
    # TAPO_EVENT_MODE 未設定 → 既定 pullpoint → enabled
    app = _make_app(tapo_mode=None, monkeypatch=monkeypatch)
    assert app._tapo_events_enabled is True


def test_tapo_pullpoint_on(monkeypatch):
    app = _make_app(tapo_mode="pullpoint", monkeypatch=monkeypatch)
    assert app._tapo_events_enabled is True


def test_tapo_disabled_off(monkeypatch):
    app = _make_app(tapo_mode="disabled", monkeypatch=monkeypatch)
    assert app._tapo_events_enabled is False


# ── on_mount → start_event_subscription 配線 ───────────────────────────────────


@pytest.mark.asyncio
async def test_start_tapo_calls_subscription_with_callback(monkeypatch):
    app = _make_app(monkeypatch=monkeypatch)

    fake_task = MagicMock()
    fake_task.done = MagicMock(return_value=False)
    captured: dict = {}

    async def fake_start(*, on_event):
        captured["on_event"] = on_event
        return fake_task

    monkeypatch.setattr(
        "familiar_agent.tui.tapo_event.start_event_subscription",
        AsyncMock(side_effect=fake_start),
    )

    await app._start_tapo_events()

    assert app._tapo_event_task is fake_task
    assert captured["on_event"] == app._on_tapo_event


@pytest.mark.asyncio
async def test_start_skipped_when_disabled(monkeypatch):
    app = _make_app(tapo_mode="disabled", monkeypatch=monkeypatch)
    sub = AsyncMock()
    monkeypatch.setattr("familiar_agent.tui.tapo_event.start_event_subscription", sub)
    await app._start_tapo_events()
    sub.assert_not_called()
    assert app._tapo_event_task is None


# ── _on_tapo_event → desires.boost(visual=True) ────────────────────────────────


@pytest.mark.asyncio
async def test_on_tapo_event_motion_boosts_look_around(monkeypatch):
    app = _make_app(monkeypatch=monkeypatch)
    await app._on_tapo_event(TapoEvent(event_type="motion", timestamp=1.0))
    app.desires.boost.assert_called_once_with("look_around", 0.25, visual=True)


@pytest.mark.asyncio
async def test_on_tapo_event_person_boosts_both(monkeypatch):
    app = _make_app(monkeypatch=monkeypatch)
    await app._on_tapo_event(TapoEvent(event_type="person", timestamp=1.0))
    app.desires.boost.assert_any_call("greet_companion", 0.25, visual=True)
    app.desires.boost.assert_any_call("look_around", 0.25, visual=True)


@pytest.mark.asyncio
async def test_on_tapo_event_boost_amount_env(monkeypatch):
    monkeypatch.setenv("TAPO_EVENT_BOOST_AMOUNT", "0.4")
    app = _make_app(monkeypatch=monkeypatch)
    monkeypatch.setenv("TAPO_EVENT_BOOST_AMOUNT", "0.4")
    await app._on_tapo_event(TapoEvent(event_type="motion", timestamp=1.0))
    app.desires.boost.assert_called_once_with("look_around", 0.4, visual=True)
