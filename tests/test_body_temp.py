"""Phase E: body_temp read / categorize / interoception-contribution tests.

mutation 対応:
    - read_cpu_temp_celsius を 0/None 固定 → test_read_temp_from_file が fail
    - category 境界 (>=→>) → test_category_boundaries が fail
    - stress delta / label テーブル破壊 → test_stress_delta_and_label が fail
"""

from __future__ import annotations

import pytest

from familiar_agent.emotion.body_temp import (
    read_cpu_temp_celsius,
    temp_body_stress_delta,
    temp_category,
    temp_prompt_label,
)


def test_read_temp_from_file(tmp_path):
    p = tmp_path / "temp"
    p.write_text("55000")
    assert read_cpu_temp_celsius(path=str(p)) == 55.0


def test_read_missing_file_returns_none(tmp_path):
    assert read_cpu_temp_celsius(path=str(tmp_path / "nope")) is None


def test_body_temp_path_env_override(tmp_path, monkeypatch):
    p = tmp_path / "temp"
    p.write_text("48000")
    monkeypatch.setenv("BODY_TEMP_PATH", str(p))
    assert read_cpu_temp_celsius() == 48.0


@pytest.mark.parametrize(
    "c,expected",
    [
        (30.0, "cool"),
        (50.0, "normal"),
        (60.0, "warm"),
        (74.9, "warm"),
        (75.0, "hot"),
        (80.0, "hot"),
        (None, "unknown"),
    ],
)
def test_category_boundaries(c, expected):
    assert temp_category(c) == expected


def test_stress_delta_and_label():
    assert temp_body_stress_delta("hot") == 0.25
    assert temp_body_stress_delta("warm") == 0.1
    assert temp_body_stress_delta("normal") == 0.0
    assert temp_body_stress_delta("unknown") == 0.0
    assert temp_prompt_label("warm") == "feeling warm"
    assert temp_prompt_label("hot") == "overheating"
    assert temp_prompt_label("normal") == ""
    assert temp_prompt_label("unknown") == ""
