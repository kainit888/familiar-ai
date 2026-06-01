"""Phase E: emotion subsystem — boredom, body temperature, last-interaction.

Self-contained latent-emotion modules with their own persistence, mirroring the
``self_state.py`` json pattern. The existing distributed homeostasis
(``self_state`` settle-toward-baseline, mood half-life) is intentionally left
untouched — Phase E adds a dedicated boredom signal only.
"""

from __future__ import annotations

from .body_temp import (
    read_cpu_temp_celsius,
    temp_body_stress_delta,
    temp_category,
    temp_prompt_label,
)
from .boredom import Boredom, boredom_heartbeat_should_fire, grown_value
from .last_interaction import LastInteraction

__all__ = [
    "Boredom",
    "boredom_heartbeat_should_fire",
    "grown_value",
    "read_cpu_temp_celsius",
    "temp_body_stress_delta",
    "temp_category",
    "temp_prompt_label",
    "LastInteraction",
]
