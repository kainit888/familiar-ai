"""Phase E: boredom — grows slowly over real time, decays on interaction.

Lazy time-based model (restart-safe): the stored ``(value, updated_at)`` pair is
persisted; the live value is recomputed from elapsed wall-clock on access, so a
process restart does not lose accumulated boredom and no background asyncio task
is needed (the existing idle tick calls ``tick()``).

heartbeat: when boredom exceeds a threshold AND the companion has been idle long
enough, ``boredom_heartbeat_should_fire`` returns True and the idle tick fires a
self-speech turn (see _ui_helpers.heartbeat_tick_prompt). This is unrelated to
the pre-existing ``heartbeat.py`` (continuation-control runtime).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# 0 → 0.8 in ~48h (4.6e-6 * 172800 ≈ 0.795). Lower = slower (3.1e-6 ≈ 72h).
GROWTH_PER_SEC = float(os.environ.get("BOREDOM_GROWTH_PER_SEC", "4.6e-6"))
DECAY_ON_INTERACTION = float(os.environ.get("BOREDOM_DECAY_ON_INTERACTION", "0.3"))
HEARTBEAT_THRESHOLD = float(os.environ.get("BOREDOM_HEARTBEAT_THRESHOLD", "0.8"))
HEARTBEAT_IDLE_SECONDS = float(os.environ.get("HEARTBEAT_IDLE_SECONDS", "1800"))

_DEFAULT_PATH = Path.home() / ".familiar_ai" / "boredom.json"


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def grown_value(
    stored: float, last_update_ts: float, now: float, *, growth_per_sec: float = GROWTH_PER_SEC
) -> float:
    """Boredom after `now - last_update_ts` seconds of growth (pure, clamped)."""
    elapsed = max(0.0, now - last_update_ts)
    return _clamp(stored + growth_per_sec * elapsed)


def boredom_heartbeat_should_fire(
    boredom: float,
    last_interaction: float,
    now: float,
    *,
    threshold: float = HEARTBEAT_THRESHOLD,
    idle_seconds: float = HEARTBEAT_IDLE_SECONDS,
) -> bool:
    """True iff boredom is high enough AND the companion has been idle long enough."""
    return boredom > threshold and (now - last_interaction) > idle_seconds


class Boredom:
    """Persistent, lazily-grown boredom level in [0, 1]."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _DEFAULT_PATH
        self._value: float = 0.0
        self._updated_at: float = time.time()
        self._load()

    def _load(self) -> None:
        try:
            if not self._path.exists():
                return
            raw = json.loads(self._path.read_text())
            self._value = _clamp(float(raw.get("value", 0.0)))
            updated = raw.get("updated_at")
            self._updated_at = float(updated) if updated is not None else time.time()
        except Exception as exc:
            logger.warning("Could not load boredom state: %s", exc)

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps({"value": self._value, "updated_at": self._updated_at}, indent=2)
            )
        except Exception as exc:
            logger.warning("Could not save boredom state: %s", exc)

    def value(self, now: float | None = None) -> float:
        """Current boredom (lazy growth applied, NOT persisted)."""
        now = now if now is not None else time.time()
        return grown_value(self._value, self._updated_at, now)

    def tick(self, now: float | None = None) -> float:
        """Apply lazy growth, persist, return the new value (called from idle tick)."""
        now = now if now is not None else time.time()
        self._value = grown_value(self._value, self._updated_at, now)
        self._updated_at = now
        self._save()
        return self._value

    def decay(self, amount: float = DECAY_ON_INTERACTION, now: float | None = None) -> None:
        """Drop boredom by `amount` (after applying growth up to `now`), persist."""
        now = now if now is not None else time.time()
        self._value = _clamp(grown_value(self._value, self._updated_at, now) - amount)
        self._updated_at = now
        self._save()

    def reset(self, now: float | None = None) -> None:
        """Zero boredom (used after a heartbeat fires so it does not re-fire)."""
        self._value = 0.0
        self._updated_at = now if now is not None else time.time()
        self._save()
