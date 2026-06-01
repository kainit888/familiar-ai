"""Phase E: boredom growth / decay / persistence tests (deterministic — now injected).

mutation 対応:
    - grown_value の成長を no-op (growth_per_sec→0 / +→無視) → test_growth_over_elapsed が fail
    - decay の amount を無視 → test_decay_on_interaction が fail
    - _save() を skip → test_persistence_round_trip が fail
"""

from __future__ import annotations

import pytest

from familiar_agent.emotion.boredom import Boredom, grown_value


_T0 = 1_000_000.0


def test_initial_value_zero(tmp_path):
    b = Boredom(path=tmp_path / "boredom.json")
    assert b.value(now=_T0) == 0.0


def test_growth_over_elapsed():
    # 4.6e-6 * 172800s (48h) ≈ 0.795
    v = grown_value(0.0, _T0, _T0 + 172800)
    assert v == pytest.approx(0.795, abs=0.02)


def test_growth_clamped_to_one():
    assert grown_value(0.9, _T0, _T0 + 1e9) == 1.0


def test_growth_per_sec_override():
    # explicit rate so 0→1 in 100s
    assert grown_value(0.0, _T0, _T0 + 50, growth_per_sec=0.01) == pytest.approx(0.5)


def test_decay_on_interaction(tmp_path):
    b = Boredom(path=tmp_path / "boredom.json")
    # grow to ~0.5 via a fast-rate tick (inject value directly)
    b._value = 0.5
    b._updated_at = _T0
    b.decay(amount=0.3, now=_T0)
    assert b.value(now=_T0) == pytest.approx(0.2, abs=1e-9)


def test_persistence_round_trip(tmp_path):
    p = tmp_path / "boredom.json"
    b = Boredom(path=p)
    b._value = 0.4
    b._updated_at = _T0
    b.tick(now=_T0)  # persists value at _T0 (0 elapsed → stays 0.4)
    b2 = Boredom(path=p)
    assert b2.value(now=_T0) == pytest.approx(0.4, abs=1e-9)


def test_reset_zeroes(tmp_path):
    b = Boredom(path=tmp_path / "boredom.json")
    b._value = 0.9
    b.reset(now=_T0)
    assert b.value(now=_T0) == 0.0
