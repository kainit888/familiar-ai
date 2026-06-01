"""Tests for always-on RTSP STT wiring in FamiliarApp TUI (Phase C-Ctrl+T 常時化)。

Tapo RTSP 常時 STT (Kotoba-Whisper の stt_kotoba.start_rtsp_subscription) を UI 層
から配線したことを検証する。実 mic / RTSP / ffmpeg / ElevenLabs / whisper は一切
叩かない (start_rtsp_subscription を AsyncMock で差し替え)。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_app(env_continuous: str | None = None, monkeypatch=None):
    """heavy 依存を mock した FamiliarApp を作る。"""
    from familiar_agent.tui import FamiliarApp

    if monkeypatch is not None:
        if env_continuous is None:
            monkeypatch.delenv("CONTINUOUS_STT", raising=False)
        else:
            monkeypatch.setenv("CONTINUOUS_STT", env_continuous)

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


# ── __init__ 属性 / env デフォルト ─────────────────────────────────────────


def test_continuous_stt_attributes_exist(monkeypatch):
    """FamiliarApp に常時 STT 用の属性がある (task / enabled)。"""
    app = _make_app(monkeypatch=monkeypatch)
    assert hasattr(app, "_continuous_stt_task")
    assert app._continuous_stt_task is None
    assert hasattr(app, "_continuous_stt_enabled")


def test_continuous_stt_default_on(monkeypatch):
    """CONTINUOUS_STT 未設定なら常時 STT はデフォルト ON。"""
    app = _make_app(env_continuous=None, monkeypatch=monkeypatch)
    assert app._continuous_stt_enabled is True


@pytest.mark.parametrize("val", ["false", "0", "no", "off", "OFF", "False"])
def test_continuous_stt_env_off(monkeypatch, val):
    """CONTINUOUS_STT=false/0/no/off は OFF。"""
    app = _make_app(env_continuous=val, monkeypatch=monkeypatch)
    assert app._continuous_stt_enabled is False


@pytest.mark.parametrize("val", ["true", "1", "yes", "on"])
def test_continuous_stt_env_on(monkeypatch, val):
    """CONTINUOUS_STT=true/1/yes/on は ON。"""
    app = _make_app(env_continuous=val, monkeypatch=monkeypatch)
    assert app._continuous_stt_enabled is True


# ── on_mount → start_rtsp_subscription 配線 ────────────────────────────────


@pytest.mark.asyncio
async def test_start_continuous_stt_calls_subscription_with_callback(monkeypatch):
    """_start_continuous_stt が stt_kotoba.start_rtsp_subscription を on_speech 付きで呼ぶ。

    実 RTSP/ffmpeg/whisper は叩かない: start_rtsp_subscription を AsyncMock 化。
    """
    app = _make_app(monkeypatch=monkeypatch)

    fake_task = MagicMock()
    fake_task.done = MagicMock(return_value=False)
    captured: dict = {}

    async def fake_start(*, on_speech, on_audio_event=None):
        captured["on_speech"] = on_speech
        captured["on_audio_event"] = on_audio_event
        return fake_task

    monkeypatch.setattr(
        "familiar_agent.tui.stt_kotoba.start_rtsp_subscription",
        AsyncMock(side_effect=fake_start),
    )

    await app._start_continuous_stt()

    # task が保持され、on_speech callback が渡されている
    assert app._continuous_stt_task is fake_task
    assert callable(captured["on_speech"])
    # 渡された callback は app のメソッド (input_queue へ流す経路)
    assert captured["on_speech"] == app._continuous_stt_on_speech
    # B4: 環境音イベント callback も配線されている
    assert captured["on_audio_event"] == app._on_audio_event


@pytest.mark.asyncio
async def test_on_speech_callback_enqueues_text(monkeypatch):
    """on_speech("こんにちは") 直呼びで _input_queue にテキストが積まれる。"""
    app = _make_app(monkeypatch=monkeypatch)

    await app._continuous_stt_on_speech("こんにちは")

    assert app._input_queue.qsize() == 1
    assert app._input_queue.get_nowait() == "こんにちは"
    # last_interaction が更新される
    assert app._last_interaction > 0


@pytest.mark.asyncio
async def test_on_speech_empty_text_ignored(monkeypatch):
    """空 / 空白のみの発話は input_queue に積まれない。"""
    app = _make_app(monkeypatch=monkeypatch)
    await app._continuous_stt_on_speech("")
    await app._continuous_stt_on_speech("   ")
    assert app._input_queue.qsize() == 0


@pytest.mark.asyncio
async def test_start_continuous_stt_skipped_when_disabled(monkeypatch):
    """常時 STT 無効時は start_rtsp_subscription を呼ばない。"""
    app = _make_app(env_continuous="false", monkeypatch=monkeypatch)
    sub_mock = AsyncMock()
    monkeypatch.setattr(
        "familiar_agent.tui.stt_kotoba.start_rtsp_subscription", sub_mock
    )
    await app._start_continuous_stt()
    sub_mock.assert_not_called()
    assert app._continuous_stt_task is None


@pytest.mark.asyncio
async def test_start_continuous_stt_no_double_start(monkeypatch):
    """既に走っている task があれば二重起動しない。"""
    app = _make_app(monkeypatch=monkeypatch)
    existing = MagicMock()
    existing.done = MagicMock(return_value=False)
    app._continuous_stt_task = existing

    sub_mock = AsyncMock()
    monkeypatch.setattr(
        "familiar_agent.tui.stt_kotoba.start_rtsp_subscription", sub_mock
    )
    await app._start_continuous_stt()
    sub_mock.assert_not_called()
    assert app._continuous_stt_task is existing


# ── action_toggle_listen: 一時停止 ↔ 再開 ──────────────────────────────────


@pytest.mark.asyncio
async def test_toggle_listen_pauses_then_resumes(monkeypatch):
    """Ctrl+T: ON のとき止め (task cancel)、OFF のとき再開 (再起動)。"""
    app = _make_app(monkeypatch=monkeypatch)
    assert app._continuous_stt_enabled is True

    # 実 task を持たせる (cancel される側)
    async def _never():
        await asyncio.sleep(3600)

    running = asyncio.create_task(_never())
    app._continuous_stt_task = running

    # debounce を無効化
    app._last_toggle_listen = 0.0

    # 1 回目: ON → 一時停止 (enabled False, task cancel)
    await app.action_toggle_listen()
    assert app._continuous_stt_enabled is False
    assert running.cancelled() or running.done()
    assert app._continuous_stt_task is None

    # 2 回目: OFF → 再開 (run_worker で _start_continuous_stt 起動)
    app._last_toggle_listen = 0.0
    started: list = []

    def _consume_worker(coro, **_kwargs):
        started.append(coro)
        if asyncio.iscoroutine(coro):
            coro.close()

    app.run_worker = MagicMock(side_effect=_consume_worker)
    await app.action_toggle_listen()
    assert app._continuous_stt_enabled is True
    assert started, "expected _start_continuous_stt to be scheduled on resume"


@pytest.mark.asyncio
async def test_toggle_listen_debounced(monkeypatch):
    """Ctrl+T のキーリピートは 0.5s デバウンスされる (状態が反転しない)。"""
    import time as _time

    app = _make_app(monkeypatch=monkeypatch)
    app._continuous_stt_enabled = True
    app._continuous_stt_task = None
    app._last_toggle_listen = _time.time()  # 直前にトグル済 → 連打は無視

    before = app._continuous_stt_enabled
    await app.action_toggle_listen()
    assert app._continuous_stt_enabled == before  # 変化しない


# ── 停止 (cancel) ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_continuous_stt_cancels_task(monkeypatch):
    """_stop_continuous_stt は走っている task を cancel して None にする。"""
    app = _make_app(monkeypatch=monkeypatch)

    async def _never():
        await asyncio.sleep(3600)

    running = asyncio.create_task(_never())
    app._continuous_stt_task = running

    await app._stop_continuous_stt()
    assert app._continuous_stt_task is None
    assert running.cancelled() or running.done()


# ── binding 維持 ────────────────────────────────────────────────────────────


def test_ctrl_t_binding_label_preserved():
    """Ctrl+T binding (toggle_listen) のラベルは維持されている。"""
    from familiar_agent.tui import FamiliarApp

    ctrl_t = [b for b in FamiliarApp.BINDINGS if b.key == "ctrl+t"]
    assert ctrl_t, "ctrl+t binding must exist"
    assert ctrl_t[0].action == "toggle_listen"
    assert "Voice" in ctrl_t[0].description


def test_space_ptt_binding_untouched():
    """Space PTT binding は触らない (回帰防止)。"""
    from familiar_agent.tui import FamiliarApp

    space = [b for b in FamiliarApp.BINDINGS if b.key == "space"]
    assert space, "space binding must exist"
    assert space[0].action == "start_ptt"
