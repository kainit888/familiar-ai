"""Problem-1: 視覚 identity の誤認修正テスト。

ToM の default-person を companion_name から切り離し、未識別の人物を companion
本人と決めつけないようにする。顔認識は範囲外 (フラグのみ予約)。

mutation 対応:
    - tom_default_person 既定を unknown_person 以外にする → test_default_person_label_is_unknown_* が fail
    - resolve_tom_default_person が companion_name を返す (バグ復活) →
      test_companion_name_not_used_as_default_anymore が fail
    - FACE_RECOGNITION_ENABLED=true で companion_name へフォールバック →
      test_face_recognition_enabled_true_still_uses_label が fail
    - locale から識別不確実性の文言を消す → test_locale_prompt_includes_identity_uncertainty が fail
    - locale から文脈推論許可の文言を消す → test_unknown_person_can_be_resolved_via_context が fail
"""

from __future__ import annotations

import logging

from familiar_agent.config import AgentConfig


def _clear(monkeypatch):
    for k in ("TOM_DEFAULT_PERSON_LABEL", "FACE_RECOGNITION_ENABLED", "COMPANION_NAME"):
        monkeypatch.delenv(k, raising=False)


# ── default-person ラベル ──────────────────────────────────────────────────────


def test_default_person_label_is_unknown_when_not_set(monkeypatch):
    _clear(monkeypatch)
    assert AgentConfig().tom_default_person == "unknown_person"


def test_default_person_label_overridable_via_env(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("TOM_DEFAULT_PERSON_LABEL", "stranger")
    assert AgentConfig().tom_default_person == "stranger"


def test_companion_name_not_used_as_default_anymore(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("COMPANION_NAME", "カイニット")
    cfg = AgentConfig()
    resolved = cfg.resolve_tom_default_person()
    assert resolved == "unknown_person"
    assert resolved != cfg.companion_name  # ← もはや companion 名ではない


# ── 顔認識フラグ (予約) ────────────────────────────────────────────────────────


def test_face_recognition_flag_default_false(monkeypatch):
    _clear(monkeypatch)
    assert AgentConfig().face_recognition_enabled is False


def test_face_recognition_enabled_true_still_uses_label(monkeypatch, caplog):
    # フラグ true でも認識器は無いので companion_name へは戻さず、ラベルを使う + warning
    _clear(monkeypatch)
    monkeypatch.setenv("COMPANION_NAME", "カイニット")
    monkeypatch.setenv("FACE_RECOGNITION_ENABLED", "true")
    monkeypatch.setenv("TOM_DEFAULT_PERSON_LABEL", "guest")
    cfg = AgentConfig()
    with caplog.at_level(logging.WARNING):
        resolved = cfg.resolve_tom_default_person()
    assert resolved == "guest"
    assert resolved != cfg.companion_name
    assert "not implemented" in caplog.text


# ── locale ガイダンス ──────────────────────────────────────────────────────────


def test_locale_prompt_includes_identity_uncertainty(monkeypatch):
    monkeypatch.setattr("familiar_agent._i18n._LANG", "ja")
    from familiar_agent._i18n import _t

    text = _t("identity_uncertainty_guidance")
    assert "決めつけ" in text  # 「決めつけてはいけない」
    assert "断定" in text


def test_unknown_person_can_be_resolved_via_context(monkeypatch):
    monkeypatch.setattr("familiar_agent._i18n._LANG", "ja")
    from familiar_agent._i18n import _t

    text = _t("identity_uncertainty_guidance")
    # 文脈推論が許可されている (ピコが永久に不確実にならないための保証)
    assert "文脈" in text
    assert "推論してよい" in text


def test_locale_prompt_english_fallback(monkeypatch):
    monkeypatch.setattr("familiar_agent._i18n._LANG", "en")
    from familiar_agent._i18n import _t

    text = _t("identity_uncertainty_guidance")
    assert "do not assert who they are" in text
    assert "infer identity from context" in text
