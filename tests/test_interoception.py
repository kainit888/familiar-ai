from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from familiar_agent.interoception import MCPInteroceptionProvider, semantic_pressure


def test_mcp_interoception_provider_uses_latest_jsonl_payload(tmp_path: Path) -> None:
    path = tmp_path / "interoception.jsonl"
    old_payload = {
        "observed_at": (datetime.utcnow() - timedelta(seconds=5)).isoformat(),
        "energy": 0.2,
        "cognitive_load": 0.9,
    }
    new_payload = {
        "signal": {
            "observed_at": datetime.utcnow().isoformat(),
            "energy": 0.8,
            "cognitive_load": 0.1,
            "body_stress": 0.2,
            "social_openness": 0.7,
        }
    }
    path.write_text(
        "\n".join(json.dumps(item) for item in (old_payload, new_payload)),
        encoding="utf-8",
    )

    signal = MCPInteroceptionProvider(path).collect()
    pressure = semantic_pressure(signal)

    assert signal.provider == "mcp"
    assert signal.energy == 0.8
    assert pressure.need_rest < 0.4


def test_mcp_interoception_provider_ignores_stale_payload(tmp_path: Path) -> None:
    path = tmp_path / "interoception.json"
    payload = {
        "observed_at": (datetime.utcnow() - timedelta(minutes=5)).isoformat(),
        "energy": 0.1,
        "cognitive_load": 0.95,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    signal = MCPInteroceptionProvider(path, max_staleness_seconds=10).collect()

    assert signal.provider == "noop"


# ── Phase E: body_temp → interoception (body_stress + label, no raw °C) ─────────
#
# mutation 対応:
#   - collect() の temp delta を外す → test_runtime_body_stress_rises_when_hot が fail
#   - temp_label を prompt に出さない / 生 °C を漏らす → test_prompt_summary_label_not_celsius が fail


def _temp_signal(tmp_path: Path, monkeypatch, millideg: str):
    from familiar_agent.interoception import RuntimeInteroceptionProvider

    p = tmp_path / "temp"
    p.write_text(millideg)
    monkeypatch.setenv("BODY_TEMP_PATH", str(p))
    return RuntimeInteroceptionProvider().collect()


def test_runtime_body_stress_rises_when_hot(tmp_path: Path, monkeypatch) -> None:
    hot = _temp_signal(tmp_path, monkeypatch, "80000")  # 80°C → hot
    cool = _temp_signal(tmp_path, monkeypatch, "30000")  # 30°C → cool
    assert hot.body_stress > cool.body_stress


def test_prompt_summary_label_not_celsius(tmp_path: Path, monkeypatch) -> None:
    hot = _temp_signal(tmp_path, monkeypatch, "80000")
    summary = hot.prompt_summary()
    assert "overheating" in summary
    assert "80" not in summary  # raw °C must never leak (CLAUDE.md)
    assert "°" not in summary
