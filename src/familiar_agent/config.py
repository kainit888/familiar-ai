"""Configuration management."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from ._i18n import _t

load_dotenv()


def _default_companion_name() -> str:
    return _t("default_companion_name")


def _env_value(*names: str, default: str = "") -> str:
    """Return the first present env var, preserving explicit empty strings."""
    for name in names:
        value = os.environ.get(name)
        if value is not None:
            return value
    return default


def _optional_int_env(*names: str) -> int | None:
    value = _env_value(*names, default="")
    if not value:
        return None
    return int(value)


def _bool_env(*names: str, default: bool = False) -> bool:
    value = _env_value(*names, default="")
    if not value:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class CameraConfig:
    host: str = field(
        default_factory=lambda: _env_value("CAMERA_HOST", "TAPO_CAMERA_HOST", default="")
    )
    username: str = field(
        default_factory=lambda: _env_value("CAMERA_USERNAME", "TAPO_USERNAME", default="admin")
    )
    password: str = field(
        default_factory=lambda: _env_value("CAMERA_PASSWORD", "TAPO_PASSWORD", default="")
    )
    port: int = field(
        default_factory=lambda: int(
            _env_value("CAMERA_ONVIF_PORT", "TAPO_ONVIF_PORT", default="2020")
        )
    )
    preview: bool = field(
        default_factory=lambda: os.environ.get("CAMERA_PREVIEW", "false").lower() == "true"
    )
    ptz_host_override: str = field(
        default_factory=lambda: _env_value("CAMERA_PTZ_HOST", default="")
    )
    ptz_username_override: str = field(
        default_factory=lambda: _env_value("CAMERA_PTZ_USERNAME", default="")
    )
    ptz_password_override: str = field(
        default_factory=lambda: _env_value("CAMERA_PTZ_PASSWORD", default="")
    )
    ptz_port_override: int | None = field(
        default_factory=lambda: _optional_int_env("CAMERA_PTZ_PORT")
    )

    @property
    def ptz_host(self) -> str:
        return self.ptz_host_override or self.host

    @property
    def ptz_username(self) -> str:
        return self.ptz_username_override or self.username

    @property
    def ptz_password(self) -> str:
        return self.ptz_password_override or self.password

    @property
    def ptz_port(self) -> int:
        return self.ptz_port_override if self.ptz_port_override is not None else self.port


@dataclass
class MobilityConfig:
    api_region: str = field(default_factory=lambda: os.environ.get("TUYA_REGION", "us"))
    api_key: str = field(default_factory=lambda: os.environ.get("TUYA_API_KEY", ""))
    api_secret: str = field(default_factory=lambda: os.environ.get("TUYA_API_SECRET", ""))
    device_id: str = field(default_factory=lambda: os.environ.get("TUYA_DEVICE_ID", ""))


@dataclass
class TTSConfig:
    elevenlabs_api_key: str = field(
        default_factory=lambda: os.environ.get("ELEVENLABS_API_KEY", "")
    )
    voice_id: str = field(
        default_factory=lambda: os.environ.get("ELEVENLABS_VOICE_ID", "cgSgspJ2msm6clMCkdW9")
    )
    go2rtc_url: str = field(
        default_factory=lambda: os.environ.get("GO2RTC_URL", "http://localhost:1984")
    )
    go2rtc_stream: str = field(default_factory=lambda: os.environ.get("GO2RTC_STREAM", "tapo_cam"))
    # Audio output routing: "local" = PC speaker only, "remote" = camera speaker only,
    # "both" = camera speaker + PC speaker simultaneously.
    output: str = field(default_factory=lambda: os.environ.get("TTS_OUTPUT", "local"))

    def has_voice_output(self) -> bool:
        """True if a voice-output path is configured (gates `say` tool registration).

        pico_v3 fork: ``TTSTool.say()`` is hardwired to route through
        ``pico_agent.adapters.tts_sbv2`` → go2rtc HTTP API → Tapo C210, so a
        configured ``go2rtc_url`` is sufficient to give the agent a voice even
        with no ElevenLabs key. ``elevenlabs_api_key`` is kept as an alternate
        trigger so the backend-agnostic upstream path still registers ``say``.
        Note: ``go2rtc_url`` defaults to ``http://localhost:1984`` (non-empty),
        so in practice ``say`` registers unless ``GO2RTC_URL`` is set empty.
        """
        return bool(self.elevenlabs_api_key or self.go2rtc_url)


@dataclass
class MemoryConfig:
    db_path: str = field(
        default_factory=lambda: os.environ.get(
            "MEMORY_DB_PATH",
            str(Path.home() / ".claude" / "memories"),
        )
    )


@dataclass
class STTConfig:
    # Reuses ELEVENLABS_API_KEY — no separate key needed
    elevenlabs_api_key: str = field(
        default_factory=lambda: os.environ.get("ELEVENLABS_API_KEY", "")
    )
    language: str = field(default_factory=lambda: os.environ.get("STT_LANGUAGE", "ja"))


@dataclass
class CodingConfig:
    workdir: str = field(default_factory=lambda: os.environ.get("CODING_WORKDIR", ""))
    bash_enabled: bool = field(
        default_factory=lambda: os.environ.get("CODING_BASH", "false").lower() == "true"
    )


@dataclass
class AgentConfig:
    # Agent display name shown in TUI
    agent_name: str = field(default_factory=lambda: os.environ.get("AGENT_NAME", "AI"))

    # Name of the companion/user shown in TUI and ToM tool
    companion_name: str = field(
        default_factory=lambda: os.environ.get("COMPANION_NAME", _default_companion_name())
    )

    # ── Visual identity (Problem-1) ─────────────────────────────────────
    # Label used by the ToM tool when the person on camera is unidentified.
    # Decoupled from companion_name so Pico does NOT hard-label every face it
    # sees as the companion. Face recognition is a separate, out-of-scope task.
    tom_default_person: str = field(
        default_factory=lambda: os.environ.get("TOM_DEFAULT_PERSON_LABEL", "unknown_person")
    )
    # Reserved flag for future face recognition. No recognizer exists yet; when
    # set we warn and still use the label (never fall back to companion_name,
    # which would reintroduce the Problem-1 misidentification bug).
    face_recognition_enabled: bool = field(
        default_factory=lambda: _bool_env("FACE_RECOGNITION_ENABLED", default=False)
    )
    # Phase G: face recognition tuning. The pico_agent adapter reads these same
    # env vars at runtime; the fields exist for .env documentation + parity.
    # Lower tolerance = stricter match (fewer false positives, more misses).
    face_recognition_tolerance: float = field(
        default_factory=lambda: float(os.environ.get("FACE_RECOGNITION_TOLERANCE", "0.6") or "0.6")
    )
    face_encodings_dir: str = field(
        default_factory=lambda: os.path.expanduser(
            os.environ.get("FACE_ENCODINGS_DIR", "~/.familiar_ai/face_encodings")
        )
    )
    face_samples_dir: str = field(
        default_factory=lambda: os.path.expanduser(
            os.environ.get("FACE_SAMPLES_DIR", "~/.familiar_ai/face_samples")
        )
    )

    # Platform: "anthropic" | "gemini" | "openai" | "kimi" | "glm"
    platform: str = field(default_factory=lambda: os.environ.get("PLATFORM", "anthropic"))

    # Unified API key (used for whichever platform is selected).
    # Legacy ANTHROPIC_API_KEY is still accepted for backward compatibility.
    api_key: str = field(default_factory=lambda: _env_value("API_KEY", "ANTHROPIC_API_KEY"))

    # Model name — platform-specific defaults applied in create_backend().
    # Legacy ANTHROPIC_MODEL is still accepted for backward compatibility.
    model: str = field(default_factory=lambda: _env_value("MODEL", "ANTHROPIC_MODEL"))

    # OpenAI-compatible only: base URL and tool-calling mode
    # TOOLS_MODE: "native" = use function-calling API, "prompt" = inject into system prompt
    base_url: str = field(
        default_factory=lambda: os.environ.get("BASE_URL", "http://localhost:11434/v1")
    )
    tools_mode: str = field(default_factory=lambda: os.environ.get("TOOLS_MODE", "prompt"))

    # Thinking mode: "auto" | "adaptive" | "extended" | "disabled"
    # "auto" = adaptive for claude-sonnet-4/opus-4, disabled for others
    thinking_mode: str = field(default_factory=lambda: os.environ.get("THINKING_MODE", "auto"))

    # Budget tokens for "extended" thinking mode (ignored in "adaptive" / "disabled")
    thinking_budget: int = field(
        default_factory=lambda: int(os.environ.get("THINKING_BUDGET_TOKENS", "10000"))
    )

    # Effort level for adaptive thinking: "high" (default) | "medium" | "low" | "max"
    # "max" is Opus 4.6 only. Ignored unless THINKING_MODE=adaptive (or auto on supported models).
    thinking_effort: str = field(default_factory=lambda: os.environ.get("THINKING_EFFORT", "high"))

    realtime_stt: bool = field(default_factory=lambda: _bool_env("REALTIME_STT", default=False))

    # ── Utility backend (optional) ─────────────────────────────────────
    # Separate backend for non-conversation LLM calls (day summaries, emotion
    # inference, self-model updates, etc.).  Falls back to the main backend
    # when not configured.
    utility_platform: str = field(default_factory=lambda: os.environ.get("UTILITY_PLATFORM", ""))
    utility_api_key: str = field(default_factory=lambda: os.environ.get("UTILITY_API_KEY", ""))
    utility_model: str = field(default_factory=lambda: os.environ.get("UTILITY_MODEL", ""))
    # OpenAI-compatible エンドポイント上書き (Ollama 等のローカル backend 向け)。
    # 未指定時は公式 OpenAI エンドポイントにフォールバック。
    utility_base_url: str = field(default_factory=lambda: os.environ.get("UTILITY_BASE_URL", ""))
    # Timeout (seconds) for utility-backend calls such as day summaries and
    # today's self-narrative. Default 180s to accommodate slower local models.
    utility_timeout_s: float = field(
        default_factory=lambda: float(_env_value("UTILITY_TIMEOUT_S", default="180") or "180")
    )
    # Maximum seconds to wait for fire-and-forget background tasks (e.g. mid-session
    # self-narrative writes) during agent shutdown before cancelling them.
    self_narrative_shutdown_wait_s: float = field(
        default_factory=lambda: float(
            _env_value("SELF_NARRATIVE_SHUTDOWN_WAIT_S", default="30") or "30"
        )
    )

    # ── Scene backend (optional) ────────────────────────────────────────
    # Separate backend for scene entity extraction — cheaper/local model.
    # Falls back to utility backend (then main backend) when not configured.
    scene_platform: str = field(default_factory=lambda: os.environ.get("SCENE_PLATFORM", ""))
    scene_api_key: str = field(default_factory=lambda: os.environ.get("SCENE_API_KEY", ""))
    scene_model: str = field(default_factory=lambda: os.environ.get("SCENE_MODEL", ""))
    scene_base_url: str = field(default_factory=lambda: os.environ.get("SCENE_BASE_URL", ""))

    # ── Autonomous behavior ───────────────────────────────────────
    # Desire-driven idle turns are OFF by default.
    # Auto-say (speak text responses aloud) is ON by default.
    # Set FAMILIAR_AUTO=1 to enable all, or toggle individually:
    #   FAMILIAR_AUTO_DESIRE=1  — enable desire-driven idle turns
    #   FAMILIAR_AUTO_SAY=0     — disable auto-say
    auto_desire: bool = field(
        default_factory=lambda: (
            os.environ.get("FAMILIAR_AUTO_DESIRE", "").strip().lower() in ("1", "true", "yes")
            or os.environ.get("FAMILIAR_AUTO", "").strip().lower() in ("1", "true", "yes")
        )
    )
    auto_say: bool = field(
        default_factory=lambda: (
            os.environ.get("FAMILIAR_AUTO_SAY", "").strip().lower() in ("1", "true", "yes")
            or os.environ.get("FAMILIAR_AUTO", "").strip().lower() in ("1", "true", "yes")
        )
    )

    max_tokens: int = 4096
    camera: CameraConfig = field(default_factory=CameraConfig)
    mobility: MobilityConfig = field(default_factory=MobilityConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    stt: STTConfig = field(default_factory=STTConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    coding: CodingConfig = field(default_factory=CodingConfig)

    def resolve_tom_default_person(self) -> str:
        """Resolve the ToM default-person label (Problem-1 fix).

        The ToM default label stays ``unknown_person`` regardless: face
        recognition (Phase G) feeds identity to Pico as an additive hint on the
        see() result, never by overwriting this default — so we never fall back
        to companion_name (that is the misidentification bug being fixed).

        We only warn when the flag is on but the recognizer is genuinely
        unavailable (library or face encodings missing). When recognition is
        actually working, no warning is emitted.
        """
        if self.face_recognition_enabled and not self._face_recognition_available():
            logging.getLogger(__name__).warning(
                "FACE_RECOGNITION_ENABLED is set but no face recognizer is "
                "available (missing library or face encodings); using "
                "TOM_DEFAULT_PERSON_LABEL=%r and skipping identity hints.",
                self.tom_default_person,
            )
        return self.tom_default_person

    @staticmethod
    def _face_recognition_available() -> bool:
        """Whether the pico_agent face recognizer can actually run.

        Lazy, in-method import so config.py keeps no import-time dependency on
        pico_agent (config is imported everywhere). Any failure → unavailable.
        """
        try:
            from pico_agent.adapters import face_recognition

            return face_recognition.is_available()
        except Exception:
            return False
