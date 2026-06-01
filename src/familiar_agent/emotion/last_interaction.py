"""Phase E: last-interaction persistence.

Persists the timestamp + kind of the most recent companion interaction so the
heartbeat's "idle for >30min" check survives restarts (previously this was an
in-memory float reset to now() on every launch).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_PATH = Path.home() / ".familiar_ai" / "last_interaction.json"
_VALID_KINDS = ("stt", "chat", "greeting")


class LastInteraction:
    """Persisted {timestamp (ISO8601), kind} of the last interaction."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _DEFAULT_PATH
        self._timestamp: float | None = None
        self._kind: str | None = None
        self._load()

    def _load(self) -> None:
        try:
            if not self._path.exists():
                return
            raw = json.loads(self._path.read_text())
            ts = raw.get("timestamp")
            if ts is not None:
                self._timestamp = datetime.fromisoformat(str(ts)).timestamp()
            kind = raw.get("kind")
            self._kind = str(kind) if kind is not None else None
        except Exception as exc:
            logger.warning("Could not load last_interaction: %s", exc)
            self._timestamp = None
            self._kind = None

    def update(self, kind: str, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        self._timestamp = now
        self._kind = kind if kind in _VALID_KINDS else "chat"
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(
                    {
                        "timestamp": datetime.fromtimestamp(now).isoformat(),
                        "kind": self._kind,
                    },
                    indent=2,
                )
            )
        except Exception as exc:
            logger.warning("Could not save last_interaction: %s", exc)

    def load(self) -> float | None:
        """Return the persisted last-interaction epoch seconds, or None."""
        return self._timestamp

    def kind(self) -> str | None:
        return self._kind
