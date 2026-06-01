"""Phase E: heartbeat predicate + selection + locale tests (deterministic).

mutation 対応:
    - boredom_heartbeat_should_fire を always-False → test_predicate_true_* が fail
    - 閾値チェック削除 → test_predicate_false_low_boredom が fail
    - 無音チェック削除 → test_predicate_false_short_idle が fail
    - heartbeat_tick_prompt の enable gate / 選択を破壊 → test_heartbeat_tick_prompt_* が fail
    - locale key 欠落 → test_heartbeat_prompt_locale_* が fail
"""

from __future__ import annotations

from familiar_agent._ui_helpers import heartbeat_tick_prompt
from familiar_agent.emotion.boredom import boredom_heartbeat_should_fire

_NOW = 2_000_000.0


def test_predicate_true_when_bored_and_idle():
    assert boredom_heartbeat_should_fire(0.85, _NOW - 1801, _NOW) is True


def test_predicate_false_low_boredom():
    assert boredom_heartbeat_should_fire(0.5, _NOW - 2000, _NOW) is False


def test_predicate_false_short_idle():
    assert boredom_heartbeat_should_fire(0.9, _NOW - 100, _NOW) is False


def test_heartbeat_tick_prompt_returns_prompt():
    p = heartbeat_tick_prompt(0.9, _NOW - 2000, _NOW, enabled=True)
    assert p is not None and len(p) > 0


def test_heartbeat_tick_prompt_disabled_returns_none():
    assert heartbeat_tick_prompt(0.9, _NOW - 2000, _NOW, enabled=False) is None


def test_heartbeat_tick_prompt_none_when_not_due():
    assert heartbeat_tick_prompt(0.5, _NOW - 100, _NOW, enabled=True) is None


def test_heartbeat_prompt_locale_ja_en(monkeypatch):
    from familiar_agent._i18n import _t

    monkeypatch.setattr("familiar_agent._i18n._LANG", "ja")
    assert "退屈" in _t("heartbeat_prompt")
    monkeypatch.setattr("familiar_agent._i18n._LANG", "en")
    assert "bored" in _t("heartbeat_prompt")
