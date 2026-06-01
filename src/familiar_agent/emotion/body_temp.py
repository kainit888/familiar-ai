"""Phase E: body temperature — CPU temp as embodied sensation.

Reads the Linux thermal sysfs node and maps the °C to a coarse category that
feeds interoception (body_stress) and a prompt LABEL. The raw °C is NEVER shown
to Pico (CLAUDE.md: no raw interoception metrics in user-facing text) — only the
category word ("feeling warm" / "overheating"). Missing sensor → graceful None.
No self-speech trigger from temperature (avoids over-firing).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_TEMP_PATH = "/sys/class/thermal/thermal_zone0/temp"
WARM_C = float(os.environ.get("BODY_TEMP_WARM_C", "60"))
HOT_C = float(os.environ.get("BODY_TEMP_HOT_C", "75"))


def read_cpu_temp_celsius(path: str | None = None) -> float | None:
    """Read CPU temperature in °C from sysfs. None if unavailable (non-Linux, etc.)."""
    p = path or os.environ.get("BODY_TEMP_PATH") or _DEFAULT_TEMP_PATH
    try:
        raw = Path(p).read_text().strip()
        return int(raw) / 1000.0
    except Exception:
        return None


def temp_category(c: float | None, *, warm_c: float = WARM_C, hot_c: float = HOT_C) -> str:
    """Map °C to cool / normal / warm / hot / unknown."""
    if c is None:
        return "unknown"
    if c >= hot_c:
        return "hot"
    if c >= warm_c:
        return "warm"
    if c < 40.0:
        return "cool"
    return "normal"


def temp_body_stress_delta(category: str) -> float:
    """Extra body_stress contributed by an elevated temperature category."""
    return {"warm": 0.1, "hot": 0.25}.get(category, 0.0)


def temp_prompt_label(category: str) -> str:
    """Coarse prompt word for the temperature category (never the raw °C)."""
    return {"warm": "feeling warm", "hot": "overheating"}.get(category, "")
