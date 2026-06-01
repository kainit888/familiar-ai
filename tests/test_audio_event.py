"""B4: YAMNet environment-sound recognition tests (all mock-based, no real model).

mutation 対応 (必須4 + 他):
    - classify を no-op (None 固定) → test_inference_returns_top_label が fail
    - 案C 分岐 (空 transcribe→yamnet) を消す / always-speech → test_whisper_empty_routes_to_yamnet が fail
    - is_important_label の閾値チェックを skip → test_confidence_threshold_filters が fail
    - record_audio_event を no-op → test_audio_event_in_scene_events / _in_inner_voice_prompt が fail
"""

from __future__ import annotations

import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from pico_agent.adapters import audio_event, stt_kotoba

# テスト用クラス名 (実 521 の代用)。index 0=Speech ... を _CLASSES_OVERRIDE で注入。
_CLASSES = ("Speech", "Doorbell", "Bark", "Glass", "Silence")


class FakeInterpreter:
    """_run_interpreter が叩く最小 tflite interpreter mock。"""

    def __init__(self, scores: list[float]) -> None:
        self._scores = np.asarray([scores], dtype=np.float32)  # [1, N]
        self.invoke_count = 0

    def get_input_details(self):
        return [{"index": 0, "shape": [1]}]

    def get_output_details(self):
        return [{"index": 1}]

    def resize_tensor_input(self, index, shape):
        pass

    def allocate_tensors(self):
        pass

    def set_tensor(self, index, value):
        pass

    def invoke(self):
        self.invoke_count += 1

    def get_tensor(self, index):
        return self._scores


def _scores(idx: int, conf: float = 0.9) -> list[float]:
    s = [0.01] * len(_CLASSES)
    s[idx] = conf
    return s


def _wav(seconds: float = 1.0) -> bytes:
    pcm = b"\x00\x00" * int(stt_kotoba._PCM_SAMPLE_RATE * seconds)
    return stt_kotoba._wrap_pcm_to_wav(pcm, sample_rate=stt_kotoba._PCM_SAMPLE_RATE)


def _pcm_above_min(min_segment_sec: float = 0.3) -> bytearray:
    n = int(min_segment_sec * stt_kotoba._PCM_BYTES_PER_SEC) + 2000
    return bytearray(b"\x00" * n)


@pytest.fixture(autouse=True)
def _reset_audio_event(monkeypatch):
    """各テスト前にモジュール状態をリセット + YAMNET_* env をクリア + クラス名注入。"""
    for k in (
        "YAMNET_ENABLED",
        "YAMNET_MODEL_PATH",
        "YAMNET_CLASS_MAP_PATH",
        "YAMNET_CONFIDENCE_THRESHOLD",
        "YAMNET_IMPORTANT_LABELS",
        "YAMNET_TOP_K",
        "YAMNET_BOOST_AMOUNT",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(audio_event, "_INTERPRETER_OVERRIDE", None)
    monkeypatch.setattr(audio_event, "_INTERPRETER_SINGLETON", None)
    monkeypatch.setattr(audio_event, "_INTERPRETER_TRIED", False)
    monkeypatch.setattr(audio_event, "_CLASSES_OVERRIDE", _CLASSES)
    monkeypatch.setattr(audio_event, "_CLASSES_CACHE", None)
    monkeypatch.setattr(audio_event, "_WARNED_ONCE", False)
    yield


# ── Group 1: inference ──────────────────────────────────────────────────────────


def test_model_load_returns_none_when_tflite_missing(monkeypatch):
    # tflite_runtime is not installed here → _get_interpreter is None
    monkeypatch.setattr(audio_event, "_INTERPRETER_OVERRIDE", None)
    assert audio_event._get_interpreter() is None


def test_inference_returns_top_label():
    interp = FakeInterpreter(_scores(_CLASSES.index("Doorbell")))
    evt = audio_event.classify(_wav(), interpreter=interp)
    assert evt is not None
    assert evt.top_label == "Doorbell"


def test_inference_returns_confidence():
    interp = FakeInterpreter(_scores(_CLASSES.index("Doorbell"), conf=0.77))
    evt = audio_event.classify(_wav(), interpreter=interp)
    assert evt is not None
    assert evt.top_confidence == pytest.approx(0.77, abs=1e-4)


def test_missing_model_graceful_classify_none():
    # no interpreter override, no model → None, no exception
    assert audio_event.classify(_wav()) is None


def test_inference_under_1sec():
    interp = FakeInterpreter(_scores(0))
    t0 = time.time()
    evt = audio_event.classify(_wav(), interpreter=interp)
    assert (time.time() - t0) < 1.0
    assert evt is not None
    assert interp.invoke_count == 1  # single-pass, no per-frame python loop


# ── Group 2: routing (案C) ──────────────────────────────────────────────────────


def test_speech_vs_env_distinguished():
    speech = audio_event.classify(_wav(), interpreter=FakeInterpreter(_scores(0)))
    door = audio_event.classify(
        _wav(), interpreter=FakeInterpreter(_scores(_CLASSES.index("Doorbell")))
    )
    assert speech.top_label == "Speech"
    assert door.top_label == "Doorbell"
    assert speech.top_label != door.top_label


@pytest.mark.asyncio
async def test_whisper_empty_routes_to_yamnet(monkeypatch):
    async def _empty_transcribe(audio_bytes, sample_rate=16000):
        return ""

    fixed = audio_event.AudioEvent("Doorbell", 0.9, (("Doorbell", 0.9),), True, 1.0)
    monkeypatch.setattr(stt_kotoba, "transcribe", _empty_transcribe)
    monkeypatch.setattr(audio_event, "classify", lambda wav, *a, **k: fixed)

    received: list = []
    spoke: list = []

    async def on_audio(evt):
        received.append(evt)

    async def on_speech(t):
        spoke.append(t)

    await stt_kotoba._emit_segment(
        _pcm_above_min(), on_speech, min_segment_sec=0.3, on_audio_event=on_audio
    )
    assert received == [fixed]
    assert spoke == []  # not speech


@pytest.mark.asyncio
async def test_high_rms_low_speech_routes_to_yamnet(monkeypatch):
    # non-empty pcm but empty transcribe (sound without speech) → yamnet path
    async def _empty(audio_bytes, sample_rate=16000):
        return ""

    fixed = audio_event.AudioEvent("Glass", 0.8, (("Glass", 0.8),), True, 1.0)
    monkeypatch.setattr(stt_kotoba, "transcribe", _empty)
    monkeypatch.setattr(audio_event, "classify", lambda wav, *a, **k: fixed)
    got: list = []
    await stt_kotoba._emit_segment(
        _pcm_above_min(), AsyncMock(), min_segment_sec=0.3, on_audio_event=got.append
    )
    assert got == [fixed]


@pytest.mark.asyncio
async def test_whisper_text_skips_yamnet(monkeypatch):
    async def _text(audio_bytes, sample_rate=16000):
        return "ピコ、聞こえてる？"

    def _boom(*a, **k):
        raise AssertionError("YAMNet must not run when speech is present")

    monkeypatch.setattr(stt_kotoba, "transcribe", _text)
    monkeypatch.setattr(audio_event, "classify", _boom)
    spoke: list = []
    audio: list = []

    async def on_speech(t):
        spoke.append(t)

    await stt_kotoba._emit_segment(
        _pcm_above_min(), on_speech, min_segment_sec=0.3, on_audio_event=audio.append
    )
    assert spoke == ["ピコ、聞こえてる？"]
    assert audio == []


@pytest.mark.asyncio
async def test_maybe_emit_none_callback_noop(monkeypatch):
    monkeypatch.setattr(
        audio_event, "classify", lambda *a, **k: (_ for _ in ()).throw(AssertionError())
    )
    await audio_event.maybe_emit_audio_event(_wav(), None)  # must not call classify


# ── Group 3: importance ─────────────────────────────────────────────────────────


def test_important_label_above_threshold():
    assert audio_event.is_important_label("Doorbell", 0.9) is True


def test_unimportant_label_no_boost():
    assert audio_event.is_important_label("Speech", 0.9) is False
    evt = audio_event.classify(_wav(), interpreter=FakeInterpreter(_scores(0)))  # Speech
    assert evt.is_important is False


def test_confidence_threshold_filters():
    assert audio_event.is_important_label("Doorbell", 0.3) is False  # below 0.5
    assert audio_event.is_important_label("Doorbell", 0.9) is True


# ── TUI _on_audio_event stub (Groups 3-4 wiring) ────────────────────────────────


def _audio_stub(desires):
    from familiar_agent.tui import FamiliarApp

    stub = SimpleNamespace()
    stub.agent = MagicMock()
    stub.desires = desires
    stub._write_log = MagicMock()
    stub._audio_boost_amount = FamiliarApp._audio_boost_amount
    stub._on_audio_event = FamiliarApp._on_audio_event.__get__(stub)
    return stub


@pytest.mark.asyncio
async def test_doorbell_triggers_audio_concern(tmp_path):
    from familiar_agent.desires import DesireSystem

    desires = DesireSystem(state_path=tmp_path / "d.json", disabled_drives=frozenset())
    stub = _audio_stub(desires)
    evt = audio_event.AudioEvent("Doorbell", 0.9, (("Doorbell", 0.9),), True, 1.0)
    await stub._on_audio_event(evt)
    assert desires.level("audio_concern") > 0.0


@pytest.mark.asyncio
async def test_dog_bark_recorded_no_boost(tmp_path):
    from familiar_agent.desires import DesireSystem

    desires = DesireSystem(state_path=tmp_path / "d.json", disabled_drives=frozenset())
    stub = _audio_stub(desires)
    evt = audio_event.AudioEvent("Bark", 0.9, (("Bark", 0.9),), False, 1.0)  # not important
    await stub._on_audio_event(evt)
    assert desires.level("audio_concern") == 0.0  # no boost
    stub.agent._scene.record_audio_event.assert_called_once()  # but recorded


@pytest.mark.asyncio
async def test_audio_event_in_observations_db():
    stub = _audio_stub(MagicMock())
    evt = audio_event.AudioEvent("Doorbell", 0.9, (("Doorbell", 0.9),), True, 1.0)
    await stub._on_audio_event(evt)
    stub.agent._memory_tool.save.assert_called_once()
    assert stub.agent._memory_tool.save.call_args.kwargs.get("kind") == "audio_event"


@pytest.mark.asyncio
async def test_audio_event_does_not_trigger_say(tmp_path):
    from familiar_agent.desires import DesireSystem

    desires = DesireSystem(state_path=tmp_path / "d.json", disabled_drives=frozenset())
    stub = _audio_stub(desires)
    evt = audio_event.AudioEvent("Doorbell", 0.9, (("Doorbell", 0.9),), True, 1.0)
    await stub._on_audio_event(evt)
    # recorded + boosted, but NEVER a say/tts call
    assert stub.agent.say.call_count == 0
    assert stub.agent._tts.call.call_count == 0
    stub.agent._scene.record_audio_event.assert_called_once()
    assert desires.level("audio_concern") > 0.0


# ── Group 4: scene integration (real in-memory sqlite) ──────────────────────────


def _scene():
    from familiar_agent.scene import SceneTracker

    return SceneTracker(sqlite3.connect(":memory:"))


def test_audio_event_in_scene_events():
    scene = _scene()
    scene.record_audio_event("Doorbell", 0.9)
    events = scene.recent_events(5)
    assert any(e["event_type"] == "heard" and e["entity_label"] == "Doorbell" for e in events)


def test_audio_event_in_inner_voice_prompt():
    scene = _scene()
    scene.record_audio_event("Doorbell", 0.9)
    summary = scene.context_for_prompt()
    assert "Doorbell" in summary
    assert "Recent sounds" in summary


def test_audio_event_integrates_with_visual_events():
    scene = _scene()
    scene._persist_events(
        [{"event_type": "appeared", "entity_label": "person", "entity_id": None}]
    )
    scene.record_audio_event("Doorbell", 0.9)
    events = scene.recent_events(10)
    types = {(e["event_type"], e["entity_label"]) for e in events}
    assert ("appeared", "person") in types
    assert ("heard", "Doorbell") in types
