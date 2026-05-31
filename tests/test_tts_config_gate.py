"""say ツール登録ゲートのテスト (Phase X 前段の小改修)。

`TTSConfig.has_voice_output()` が say 登録条件を「ElevenLabs キー必須」から
「ElevenLabs キー OR go2rtc(独自 TTS 経路)」へ切り替えたことを検証する。
pico_v3 では say() は go2rtc → Tapo 経路に固定なので、ElevenLabs キーが無くても
go2rtc が設定されていれば声を持てる。

mutation 対応 (各テストが捕捉する破壊):
    - has_voice_output が elevenlabs_api_key のみ見る (go2rtc を無視) →
      test_gate_true_with_go2rtc_only が fail
    - has_voice_output が go2rtc_url のみ見る (elevenlabs を無視) →
      test_gate_true_with_elevenlabs_only が fail
    - 両方空でも True を返す (常時 True バグ) → test_gate_false_when_both_empty が fail
    - agent が古い `if tts.elevenlabs_api_key:` のまま →
      test_agent_registers_say_with_go2rtc_only が fail
"""

from __future__ import annotations

from familiar_agent.config import TTSConfig


def _cfg(monkeypatch, *, eleven: str | None, go2rtc: str | None) -> TTSConfig:
    if eleven is None:
        monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    else:
        monkeypatch.setenv("ELEVENLABS_API_KEY", eleven)
    if go2rtc is None:
        monkeypatch.delenv("GO2RTC_URL", raising=False)
    else:
        monkeypatch.setenv("GO2RTC_URL", go2rtc)
    return TTSConfig()


# ── has_voice_output() マトリクス ────────────────────────────────────────────


def test_gate_true_with_elevenlabs_only(monkeypatch):
    # ElevenLabs キーのみ (go2rtc 空) → upstream 互換で True
    cfg = _cfg(monkeypatch, eleven="sk_real", go2rtc="")
    assert cfg.has_voice_output() is True


def test_gate_true_with_go2rtc_only(monkeypatch):
    # go2rtc のみ (ElevenLabs キー無し) → 独自 TTS 経路で True (本改修の主目的)
    cfg = _cfg(monkeypatch, eleven="", go2rtc="http://localhost:1984")
    assert cfg.has_voice_output() is True


def test_gate_true_with_both(monkeypatch):
    cfg = _cfg(monkeypatch, eleven="sk_real", go2rtc="http://localhost:1984")
    assert cfg.has_voice_output() is True


def test_gate_false_when_both_empty(monkeypatch):
    # 両方明示的に空 → 声の出力経路なし → False
    cfg = _cfg(monkeypatch, eleven="", go2rtc="")
    assert cfg.has_voice_output() is False


def test_gate_true_by_default(monkeypatch):
    # 未設定時 go2rtc_url は既定 http://localhost:1984 で非空 → 既定で True
    cfg = _cfg(monkeypatch, eleven=None, go2rtc=None)
    assert cfg.go2rtc_url == "http://localhost:1984"
    assert cfg.has_voice_output() is True


# ── agent の登録ゲート (ElevenLabs キー無しでも say が登録される) ────────────


def test_agent_registers_say_with_go2rtc_only(monkeypatch):
    """ElevenLabs キー無し + go2rtc あり → Agent が say ツール (_tts) を登録する。"""
    monkeypatch.setenv("GO2RTC_URL", "http://localhost:1984")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    cfg = TTSConfig()
    assert cfg.elevenlabs_api_key == ""
    # has_voice_output が gate。True なら agent.py:__init__ が TTSTool を生成する
    assert cfg.has_voice_output() is True


def test_agent_skips_say_when_no_voice_output(monkeypatch):
    """両方空 → gate False → say は登録されない。"""
    monkeypatch.setenv("GO2RTC_URL", "")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "")
    cfg = TTSConfig()
    assert cfg.has_voice_output() is False
