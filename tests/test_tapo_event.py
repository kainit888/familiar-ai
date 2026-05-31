"""Phase X Stage C: Tapo ONVIF event receiver のテスト (全 mock、実カメラ不要)。

familiar_agent には依存しない (二層分離の確認も兼ねる)。

mutation 対応:
    - _classify person 優先削除 → test_classify_person_* が fail
    - 立下り (IsMotion=false) を None にしない → test_classify_motion_ended_ignored が fail
    - debounce 削除 → test_emit_debounces_within_window が fail
    - person_only filter 削除 → test_emit_person_only_drops_motion が fail
    - disabled/no-op ガード削除 → test_start_*_returns_noop_* が fail
    - PullPoint 配線崩し → test_pullpoint_session_emits_motion が fail
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from pico_agent.adapters import tapo_event
from pico_agent.adapters.tapo_event import TapoEvent, _classify, _Debouncer


# ── _classify ────────────────────────────────────────────────────────────────


def test_classify_motion_topic():
    assert _classify("tns1:RuleEngine/CellMotionDetector/Motion", {}) == "motion"


def test_classify_motion_via_simpleitem():
    assert _classify("", {"IsMotion": "true"}) == "motion"


def test_classify_person_topic():
    assert _classify("tns1:RuleEngine/PeopleDetector/People", {}) == "person"


def test_classify_person_via_simpleitem():
    assert _classify("tns1:VideoSource/MotionAlarm", {"IsPeople": "true"}) == "person"


def test_classify_motion_ended_ignored():
    # 立下り (motion ended) は採用しない
    assert _classify("tns1:RuleEngine/CellMotionDetector/Motion", {"IsMotion": "false"}) is None


def test_classify_unknown_returns_none():
    assert _classify("tns1:Device/Trigger/Relay", {"LogicalState": "true"}) is None


# ── SOAP / zeep extraction ─────────────────────────────────────────────────────

_SOAP = b"""<?xml version="1.0"?>
<Envelope xmlns="http://www.w3.org/2003/05/soap-envelope"
          xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2"
          xmlns:tt="http://www.onvif.org/ver10/schema">
 <Body>
  <wsnt:Notify>
   <wsnt:NotificationMessage>
    <wsnt:Topic>tns1:RuleEngine/CellMotionDetector/Motion</wsnt:Topic>
    <wsnt:Message>
     <tt:Message><tt:Data>
       <tt:SimpleItem Name="IsMotion" Value="true"/>
     </tt:Data></tt:Message>
    </wsnt:Message>
   </wsnt:NotificationMessage>
  </wsnt:Notify>
 </Body>
</Envelope>"""


def test_parse_soap_notifications():
    parsed = tapo_event._parse_soap_notifications(_SOAP)
    assert len(parsed) == 1
    topic, items = parsed[0]
    assert "CellMotionDetector/Motion" in topic
    assert items == {"IsMotion": "true"}
    assert _classify(topic, items) == "motion"


def test_parse_soap_malformed_returns_empty():
    assert tapo_event._parse_soap_notifications(b"not xml <<<") == []


def test_extract_topic_and_simpleitems_from_zeep_like():
    nm = SimpleNamespace(
        Topic=SimpleNamespace(_value_1="tns1:RuleEngine/CellMotionDetector/Motion"),
        Message=SimpleNamespace(
            Message=SimpleNamespace(
                Data=SimpleNamespace(SimpleItem=[SimpleNamespace(Name="IsMotion", Value="true")])
            )
        ),
    )
    assert "Motion" in tapo_event._extract_topic(nm)
    assert tapo_event._extract_simple_items(nm) == {"IsMotion": "true"}


def test_extract_defensive_on_malformed():
    bad = SimpleNamespace()  # no Topic / Message
    assert tapo_event._extract_topic(bad) == ""
    assert tapo_event._extract_simple_items(bad) == {}


# ── _Debouncer ─────────────────────────────────────────────────────────────────


def test_debouncer_window():
    d = _Debouncer(30.0)
    assert d.allow("motion", 100.0) is True
    assert d.allow("motion", 120.0) is False  # within 30s window
    assert d.allow("motion", 131.0) is True  # past window
    assert d.allow("person", 120.0) is True  # different type, independent


# ── _emit ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_emit_debounces_within_window():
    received: list[TapoEvent] = []

    async def on_event(evt):
        received.append(evt)

    deb = _Debouncer(30.0)
    await tapo_event._emit(on_event, "motion", "t", {}, debouncer=deb, person_only=False, now=100.0)
    await tapo_event._emit(on_event, "motion", "t", {}, debouncer=deb, person_only=False, now=110.0)
    await tapo_event._emit(on_event, "motion", "t", {}, debouncer=deb, person_only=False, now=140.0)
    assert len(received) == 2  # 100 emitted, 110 debounced, 140 emitted


@pytest.mark.asyncio
async def test_emit_person_only_drops_motion():
    received: list[TapoEvent] = []

    async def on_event(evt):
        received.append(evt)

    deb = _Debouncer(0.0)
    await tapo_event._emit(on_event, "motion", "t", {}, debouncer=deb, person_only=True, now=1.0)
    await tapo_event._emit(on_event, "person", "t", {}, debouncer=deb, person_only=True, now=2.0)
    assert [e.event_type for e in received] == ["person"]


@pytest.mark.asyncio
async def test_emit_callback_failure_does_not_raise():
    async def on_event(evt):
        raise RuntimeError("boom")

    deb = _Debouncer(0.0)
    # must not propagate
    await tapo_event._emit(on_event, "motion", "t", {}, debouncer=deb, person_only=False, now=1.0)


# ── start_event_subscription: no-op safety ─────────────────────────────────────


def _clear_env(monkeypatch):
    for k in (
        "TAPO_EVENT_MODE",
        "TAPO_EVENT_WEBHOOK_HOST",
        "CAMERA_HOST",
        "TAPO_CAMERA_HOST",
        "CAMERA_USERNAME",
        "TAPO_USERNAME",
        "CAMERA_PASSWORD",
        "TAPO_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)


@pytest.mark.asyncio
async def test_start_returns_noop_when_disabled(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("TAPO_EVENT_MODE", "disabled")
    called: list = []

    async def on_event(evt):
        called.append(evt)

    task = await tapo_event.start_event_subscription(on_event)
    await task  # no-op loop completes immediately
    assert called == []


@pytest.mark.asyncio
async def test_start_returns_noop_when_camera_unset(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("TAPO_EVENT_MODE", "pullpoint")
    monkeypatch.setattr(tapo_event, "_ONVIF_AVAILABLE", True)
    task = await tapo_event.start_event_subscription(lambda e: asyncio.sleep(0))
    await task
    assert task.done()


@pytest.mark.asyncio
async def test_start_returns_noop_when_onvif_unavailable(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("TAPO_EVENT_MODE", "pullpoint")
    monkeypatch.setenv("CAMERA_HOST", "192.168.10.110")
    monkeypatch.setenv("CAMERA_USERNAME", "u")
    monkeypatch.setenv("CAMERA_PASSWORD", "p")
    monkeypatch.setattr(tapo_event, "_ONVIF_AVAILABLE", False)
    task = await tapo_event.start_event_subscription(lambda e: asyncio.sleep(0))
    await task
    assert task.done()


@pytest.mark.asyncio
async def test_start_webhook_noop_without_host(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("TAPO_EVENT_MODE", "webhook")
    monkeypatch.setenv("CAMERA_HOST", "192.168.10.110")
    monkeypatch.setenv("CAMERA_USERNAME", "u")
    monkeypatch.setenv("CAMERA_PASSWORD", "p")
    monkeypatch.setattr(tapo_event, "_ONVIF_AVAILABLE", True)
    # TAPO_EVENT_WEBHOOK_HOST unset → no-op
    task = await tapo_event.start_event_subscription(lambda e: asyncio.sleep(0))
    await task
    assert task.done()


# ── env accessors ──────────────────────────────────────────────────────────────


def test_env_accessors_defaults(monkeypatch):
    for k in ("TAPO_EVENT_MODE", "TAPO_EVENT_DEBOUNCE_SEC", "TAPO_EVENT_PERSON_ONLY"):
        monkeypatch.delenv(k, raising=False)
    assert tapo_event._get_event_mode() == "pullpoint"
    assert tapo_event._get_debounce_sec() == 30.0
    assert tapo_event._get_person_only() is False


def test_env_accessors_override(monkeypatch):
    monkeypatch.setenv("TAPO_EVENT_DEBOUNCE_SEC", "12.5")
    monkeypatch.setenv("TAPO_EVENT_PERSON_ONLY", "true")
    assert tapo_event._get_debounce_sec() == 12.5
    assert tapo_event._get_person_only() is True
    monkeypatch.setenv("TAPO_EVENT_DEBOUNCE_SEC", "bad")
    assert tapo_event._get_debounce_sec() == 30.0  # fallback


# ── PullPoint session (mocked ONVIF) ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_pullpoint_session_emits_motion(monkeypatch):
    received: list[TapoEvent] = []

    async def on_event(evt):
        received.append(evt)

    nm = SimpleNamespace(
        Topic=SimpleNamespace(_value_1="tns1:RuleEngine/CellMotionDetector/Motion"),
        Message=SimpleNamespace(
            Message=SimpleNamespace(
                Data=SimpleNamespace(SimpleItem=[SimpleNamespace(Name="IsMotion", Value="true")])
            )
        ),
    )
    resp = SimpleNamespace(NotificationMessage=[nm])
    pulls = {"n": 0}

    async def pull(_args):
        pulls["n"] += 1
        if pulls["n"] == 1:
            return resp
        raise RuntimeError("stop loop")  # break after first batch

    service = SimpleNamespace(PullMessages=pull)

    class FakeManager:
        def get_service(self):
            return service

        async def shutdown(self):
            return None

    async def create_ppm(_interval, _cb):
        return FakeManager()

    fake_cam = SimpleNamespace(create_pullpoint_manager=create_ppm)

    async def fake_connect():
        return fake_cam

    monkeypatch.setattr(tapo_event, "_connect_onvif", fake_connect)
    await tapo_event._run_one_pullpoint_session(
        on_event, debounce_sec=0.0, person_only=False, renew_sec=60.0, pull_timeout_sec=1.0
    )
    assert len(received) == 1
    assert received[0].event_type == "motion"
    assert "Motion" in received[0].topic


@pytest.mark.asyncio
async def test_pullpoint_session_noop_when_connect_fails(monkeypatch):
    async def fake_connect():
        return None

    monkeypatch.setattr(tapo_event, "_connect_onvif", fake_connect)
    received: list = []
    await tapo_event._run_one_pullpoint_session(
        lambda e: received.append(e),
        debounce_sec=0.0,
        person_only=False,
        renew_sec=60.0,
        pull_timeout_sec=1.0,
    )
    assert received == []
