"""Textual TUI for familiar-ai."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from collections.abc import Callable
from typing import TYPE_CHECKING

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Input, RichLog, Static
from textual_autocomplete import AutoComplete, DropdownItem, TargetState

from ._i18n import _make_banner, _t
from ._ui_helpers import (
    ACTION_ICONS,
    DESIRE_COOLDOWN as _DESIRE_COOLDOWN,
    IDLE_CHECK_INTERVAL as _IDLE_CHECK_INTERVAL,
    desire_tick_prompt,
    format_action as _format_action,
    format_tool_result as _format_tool_result,
    should_fire_idle_desire,
)
from .realtime_stt_session import create_realtime_stt_controller, RealtimeSttController

# pico_v3 拡張 (Phase C-Ctrl+T 常時化): Tapo RTSP 音声トラックを Kotoba-Whisper で
# 常時購読する STT (Phase C-4 実装済 start_rtsp_subscription) を UI 層から配線する。
# realtime_stt_session と同型 (UI 責務の STT wiring)。一方向 import (familiar_agent→pico_agent)。
from pico_agent.adapters import stt_kotoba, tapo_event

if TYPE_CHECKING:
    from .agent import EmbodiedAgent
    from .desires import DesireSystem

logger = logging.getLogger(__name__)

IDLE_CHECK_INTERVAL = _IDLE_CHECK_INTERVAL
DESIRE_COOLDOWN = _DESIRE_COOLDOWN

_RICH_TAG_RE = re.compile(r"\[/?[^\[\]]*\]")

CSS = """
#log {
    height: 1fr;
    border: none;
    padding: 0 1;
    scrollbar-size: 1 1;
}

#stream {
    height: auto;
    min-height: 1;
    padding: 0 1;
    color: $text;
}

#stream.thinking {
    color: $text-muted;
}

#stream.recording {
    color: $error;
}

#input-bar {
    dock: bottom;
    height: 3;
    border-top: solid $primary-darken-2;
    padding: 0 1;
}
"""

_SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

# CC-style interrupt message constants
INTERRUPT_MSG = "[Request interrupted by user]"
INTERRUPT_TOOL_MSG = "[Request interrupted by user for tool use]"


def _format_elapsed(seconds: float) -> str:
    """Human-readable elapsed time: '45s' or '7m 43s'."""
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    m, s = divmod(total, 60)
    return f"{m}m {s:02d}s"


def _format_tokens(n: int) -> str:
    """Compact token count: '' for zero, '500' under 1k, '6.5k' above."""
    if n == 0:
        return ""
    if n < 1000:
        return str(n)
    return f"{n / 1000:.1f}k"


def _continuous_stt_enabled_default() -> bool:
    """常時 RTSP STT のデフォルト ON/OFF を環境変数 CONTINUOUS_STT から取得 (既定 ON)。

    ``CONTINUOUS_STT`` 未設定なら True (カイニット推奨「維持・デフォルト ON」)。
    ``false`` / ``0`` / ``no`` / ``off`` (大小無視) のときだけ False。
    """
    raw = os.environ.get("CONTINUOUS_STT", "").strip().lower()
    if raw in ("false", "0", "no", "off"):
        return False
    return True


def _tapo_events_enabled_default() -> bool:
    """Tapo ONVIF イベント購読の ON/OFF を ``TAPO_EVENT_MODE`` から導出 (既定 ON)。

    Phase X Stage C: ``disabled`` のときだけ False。``pullpoint``/``webhook`` (および
    未設定=既定 pullpoint) は True。実購読は tapo_event 側で mode を再判定するため、
    ここは「worker を起こすか」だけのゲート (no-op task も安全)。
    """
    mode = os.environ.get("TAPO_EVENT_MODE", "pullpoint").strip().lower() or "pullpoint"
    return mode != "disabled"


# Slash commands shown in the autocomplete dropdown
_SLASH_COMMANDS: list[tuple[str, str]] = [
    ("/btw", "💬  Quick one-shot question (no memory / tools)"),
    ("/transcribe", "🎙  Start / stop voice input (STT)"),
    ("/cost", "💰  Show token usage and cost for this session"),
    ("/clear", "🗑   Clear conversation history"),
    ("/quit", "✕   Quit"),
]


def _parse_btw(text: str) -> str:
    """Extract the question from '/btw <question>'."""
    after = text[len("/btw") :].strip()
    return after


async def handle_btw_command(question: str, backend) -> str:
    """Run a single lightweight LLM completion — no tools, no memory.

    Like CC's /btw: a quick side-question that doesn't pollute conversation history.
    """
    question = question.strip()
    if not question:
        return ""
    return await backend.complete(question, max_tokens=300)


def _format_cost(
    input_tokens: int,
    output_tokens: int,
    *,
    input_price_per_m: float = 3.0,
    output_price_per_m: float = 15.0,
) -> str:
    """Format token usage and estimated USD cost for display.

    Default prices match Sonnet 4.6 ($/1M tokens).
    """
    input_cost = input_tokens / 1_000_000 * input_price_per_m
    output_cost = output_tokens / 1_000_000 * output_price_per_m
    total = input_cost + output_cost
    return (
        f"📊 Tokens — in: {input_tokens:,}  out: {output_tokens:,}\n"
        f"💰 Cost   — ${total:.4f} (in ${input_cost:.4f} + out ${output_cost:.4f})"
    )


def _slash_candidates(state: TargetState) -> list[DropdownItem]:
    """Return matching commands only when input starts with '/'.

    Uses text up to cursor position so that the library's search_string and
    our own prefix-filter stay in sync.  Each item's .value is the bare
    command string so that selecting from the dropdown inserts only the
    command, not the description.
    """
    text = state.text[: state.cursor_position]
    if not text.startswith("/"):
        return []
    return [DropdownItem(main=cmd) for cmd, _desc in _SLASH_COMMANDS if cmd.startswith(text)]


class FamiliarApp(App):
    CSS = CSS
    BINDINGS = [
        Binding("ctrl+c", "quit", _t("quit_label"), show=True, priority=True),
        Binding("ctrl+l", "clear_history", _t("clear_label"), show=True),
        Binding("ctrl+t", "toggle_listen", "🎙 Voice", show=True),
        Binding("ctrl+r", "restart_realtime_stt", "↻ STT", show=True),
        Binding("escape", "cancel_turn", "🛑 Cancel", show=False),
        Binding("space", "start_ptt", "🎙 PTT", show=False),
    ]

    def __init__(self, agent: "EmbodiedAgent", desires: "DesireSystem") -> None:
        super().__init__()
        self.agent = agent
        self.desires = desires
        self._agent_name = agent.config.agent_name
        self._companion_name = agent.config.companion_name
        self._input_queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._last_interaction = time.time()
        self._agent_running = False
        self._current_text_buf = ""  # buffer for streaming text
        self._log_path = self._open_log_file()
        self._recording = False
        self._stop_recording: asyncio.Event = asyncio.Event()
        self._last_toggle_listen: float = 0.0  # debounce Ctrl+T key-repeat
        self._closing = False
        # ESC cancel support
        self._cancel_event: asyncio.Event = asyncio.Event()
        self._agent_task: asyncio.Task | None = None
        # Push-to-Talk state
        self._ptt_active: bool = False
        # Realtime STT (hands-free, always-on)
        self._realtime_stt: RealtimeSttController | None = create_realtime_stt_controller()
        # pico_v3 (Phase C-Ctrl+T 常時化): Tapo RTSP 常時 STT (Kotoba-Whisper) task。
        # CONTINUOUS_STT (既定 ON) が真なら on_mount で起動し、Ctrl+T で一時停止/再開する。
        self._continuous_stt_task: asyncio.Task | None = None
        self._continuous_stt_enabled: bool = _continuous_stt_enabled_default()
        # Phase X Stage C: Tapo ONVIF event 購読 (motion/person → desire boost)。
        self._tapo_event_task: asyncio.Task | None = None
        self._tapo_events_enabled: bool = _tapo_events_enabled_default()

    def _open_log_file(self) -> Path:
        log_dir = Path.home() / ".cache" / "familiar-ai"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "chat.log"
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"\n{'─' * 60}\n[{datetime.now():%Y-%m-%d %H:%M:%S}] セッション開始\n")
        return log_path

    def _append_log(self, line: str) -> None:
        plain = _RICH_TAG_RE.sub("", line)
        with self._log_path.open("a", encoding="utf-8") as f:
            f.write(plain + "\n")

    def compose(self) -> ComposeResult:
        yield RichLog(id="log", highlight=False, markup=True, wrap=True)
        yield Static("", id="stream")
        input_bar = Input(
            placeholder=_t("input_placeholder"),
            id="input-bar",
        )
        yield input_bar
        yield AutoComplete(input_bar, candidates=_slash_candidates)
        yield Footer()

    def on_mount(self) -> None:
        import signal as _signal

        # Re-enable ISIG so Ctrl+C generates SIGINT, independent of Textual's event loop.
        # Textual disables ISIG in raw mode, making Ctrl+C send 0x03 through Textual's key
        # dispatch. If the event loop or key dispatch is stuck, Ctrl+C is silently dropped.
        # Re-enabling ISIG ensures Ctrl+C → SIGINT → os._exit(0) unconditionally.
        try:
            import termios

            fd = sys.stdin.fileno()
            attrs = termios.tcgetattr(fd)
            attrs[3] |= termios.ISIG  # re-enable signal generation (VINTR/VQUIT/VSUSP)
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
        except Exception:
            pass

        try:
            _signal.signal(_signal.SIGINT, lambda *_: os._exit(0))
            _signal.signal(_signal.SIGQUIT, lambda *_: os._exit(0))  # Ctrl+\
            _signal.signal(_signal.SIGTSTP, _signal.SIG_IGN)  # Ctrl+Z (ignore suspend)
        except (OSError, ValueError, AttributeError):
            pass  # Not in main thread or signal not available on this OS (e.g. Windows)

        self.query_one("#input-bar", Input).focus()
        # Show startup banner
        log = self.query_one("#log", RichLog)
        for line in _make_banner(include_commands=False).splitlines():
            log.write(f"[bold]{line}[/bold]" if "familiar-ai" in line else f"[dim]{line}[/dim]")
        self._log_system(_t("startup", log_path=str(self._log_path)))
        self.set_interval(IDLE_CHECK_INTERVAL, self._desire_tick)
        self.run_worker(self._process_queue(), exclusive=False)
        # Start realtime STT if configured
        if self._realtime_stt:
            self.run_worker(self._start_realtime_stt(), exclusive=False)
        # pico_v3 (Phase C-Ctrl+T 常時化): Tapo RTSP 常時 STT (Kotoba-Whisper) を起動。
        # CONTINUOUS_STT 既定 ON。依存未満 (RTSP URL 無 / ffmpeg 無) でも no-op task が
        # 返るため安全 (stt_kotoba.start_rtsp_subscription の仕様)。
        if self._continuous_stt_enabled:
            self.run_worker(self._start_continuous_stt(), exclusive=False)
        # Phase X Stage C: Tapo event 購読 (既定 ON、mode=disabled で OFF)。no-op task
        # が返るため依存未満でも安全。
        if self._tapo_events_enabled:
            self.run_worker(self._start_tapo_events(), exclusive=False)
        # Show initializing status until embedding model is ready
        if not self.agent.is_embedding_ready:
            asyncio.create_task(self._embedding_ready_watcher())

    async def _embedding_ready_watcher(self) -> None:
        """Show initializing status in #stream until embedding model is ready."""
        if self.agent.is_embedding_ready:
            return
        start = time.time()
        with contextlib.suppress(Exception):
            stream: Static = self.query_one("#stream", Static)
        if "stream" not in locals():
            return
        while not self.agent.is_embedding_ready and not self._closing:
            elapsed = int(time.time() - start)
            with contextlib.suppress(Exception):
                stream.update(f"[dim]{_t('initializing')}... ({elapsed}s)[/dim]")
            await asyncio.sleep(0.5)
        if self._closing:
            return
        elapsed = int(time.time() - start)
        with contextlib.suppress(Exception):
            stream.update(f"[dim]{_t('initializing_done')} ({elapsed}s)[/dim]")
        await asyncio.sleep(2.0)
        with contextlib.suppress(Exception):
            stream.update("")

    # ── logging helpers ────────────────────────────────────────────

    def _write_log(self, text: str, style: str = "") -> None:
        log = self.query_one("#log", RichLog)
        if style:
            log.write(f"[{style}]{text}[/{style}]")
        else:
            log.write(text)
        self._append_log(text)

    def _log_system(self, text: str) -> None:
        self._write_log(f"[dim]{text}[/dim]")

    def _log_user(self, text: str) -> None:
        self._write_log(f"[bold cyan]{self._companion_name} ▶[/bold cyan] {text}")

    def _log_action(self, name: str, tool_input: dict) -> None:
        label = _format_action(name, tool_input)
        self._write_log(f"[dim]{label}[/dim]")

    # ── input handling ─────────────────────────────────────────────

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.clear()
        if not text:
            return

        if text == "/quit":
            self.exit()
            return
        if text == "/clear":
            self.agent.clear_history()
            self._log_system(_t("history_cleared"))
            return
        if text == "/transcribe":
            await self.action_toggle_listen()
            return
        if text == "/cost":
            in_tok = getattr(self.agent, "_session_input_tokens", 0)
            out_tok = getattr(self.agent, "_session_output_tokens", 0)
            self._log_system(_format_cost(in_tok, out_tok))
            return
        if text.startswith("/btw"):
            question = _parse_btw(text)
            if not question:
                self._log_system("Usage: /btw <question>")
                return
            answer = await handle_btw_command(question, self.agent.backend)
            name_tag = f"[bold magenta]{self._agent_name} ▶[/bold magenta]"
            self._write_log(f"{name_tag} {answer}")
            return

        self._log_user(text)
        self._last_interaction = time.time()
        await self._input_queue.put(text)

    # ── agent loop ─────────────────────────────────────────────────

    async def _process_queue(self) -> None:
        """Main loop: dequeue user messages and run agent."""
        while True:
            if self._agent_running:
                # Keep incoming input in queue so agent.run() can consume it via interrupt_queue.
                await asyncio.sleep(0.05)
                continue
            text = await self._input_queue.get()
            if text is None:
                break
            await self._run_agent(text)

    async def _spinner_loop(
        self,
        stream: Static,
        name_tag: str,
        stop: asyncio.Event,
        start_time: float,
        get_tokens: Callable[[], int] | None = None,
    ) -> None:
        """Animate the stream widget with a live status line.

        Shows:  ⠋ 23s · ↓ 6.5k
        The token count reflects _last_context_tokens, updated after each
        stream_turn() call in the agent loop.
        """
        stream.add_class("thinking")
        for i in range(10_000):
            if stop.is_set():
                break
            frame = _SPINNER_FRAMES[i % len(_SPINNER_FRAMES)]
            elapsed_str = _format_elapsed(time.time() - start_time)
            tokens = get_tokens() if get_tokens else 0
            tok_str = f" · ↓ {_format_tokens(tokens)}" if tokens else ""
            stream.update(f"{name_tag} {frame} [dim]{elapsed_str}{tok_str}[/dim]")
            await asyncio.sleep(0.08)
        stream.remove_class("thinking")

    async def _run_agent(self, user_input: str, inner_voice: str = "") -> None:
        self._agent_running = True
        self._cancel_event.clear()
        self._current_text_buf = ""
        start_time = time.time()

        log = self.query_one("#log", RichLog)
        stream = self.query_one("#stream", Static)
        text_buf: list[str] = []
        action_counts: dict[str, int] = {}
        say_fired = False

        name_tag = f"[bold magenta]{self._agent_name} ▶[/bold magenta]"

        # Spinner state — restarted after each tool call
        get_tokens = lambda: self.agent._last_context_tokens  # noqa: E731
        stop_spinner = asyncio.Event()
        spinner_task: asyncio.Task = asyncio.create_task(
            self._spinner_loop(stream, name_tag, stop_spinner, start_time, get_tokens)
        )

        def _stop_spinner() -> None:
            stop_spinner.set()

        def _restart_spinner() -> None:
            nonlocal spinner_task, stop_spinner
            stop_spinner.set()
            stop_spinner = asyncio.Event()
            spinner_task = asyncio.create_task(
                self._spinner_loop(stream, name_tag, stop_spinner, start_time, get_tokens)
            )

        def _flush_stream() -> None:
            """Commit streamed text to the log and clear the stream widget."""
            if text_buf:
                content = "".join(text_buf)
                log.write(f"{name_tag} {content}")
                self._append_log(f"{self._agent_name} ▶ {content}")
                text_buf.clear()
                stream.update("")

        def _log_turn_summary() -> None:
            elapsed = time.time() - start_time
            parts = [f"[dim]{elapsed:.1f}s[/dim]"]
            for tool_name, icon in ACTION_ICONS.items():
                count = action_counts.get(tool_name, 0)
                if count:
                    parts.append(f"[dim]{icon} ×{count}[/dim]")
            summary = "  [dim]──[/dim] " + "  ".join(parts) + "  [dim]" + "─" * 20 + "[/dim]"
            log.write(summary)
            self._append_log(f"── {elapsed:.1f}s ──")

        def on_action(name: str, tool_input: dict) -> None:
            nonlocal say_fired
            action_counts[name] = action_counts.get(name, 0) + 1
            _stop_spinner()
            if name == "say":
                # Discard pre-say text (usually redundant with spoken content),
                # then commit each say() directly to the log so multiple calls
                # all appear — not overwritten by the next one.
                say_fired = True
                text_buf.clear()
                stream.update("")
                raw = str(tool_input.get("text", ""))
                clean = re.sub(r"\[.*?\]", "", raw).strip()
                if clean:
                    log.write(f"[bold magenta]{self._agent_name} 🔊[/bold magenta] {clean}")
                    self._append_log(f"{self._agent_name} 🔊 {clean}")
            else:
                label = _format_action(name, tool_input)
                log.write(f"[dim]{label}[/dim]")
            # Restart spinner while waiting for the next LLM response
            _restart_spinner()

        def on_tool_result(name: str, tool_input: dict, result: str) -> None:
            formatted = _format_tool_result(name, tool_input, result)
            if formatted:
                _stop_spinner()
                log.write(f"[dim]{formatted}[/dim]")
                _restart_spinner()

        def on_text(chunk: str) -> None:
            if say_fired:
                # Discard post-say text: LLMs often re-emit say() content as plain
                # text after the tool call (with audio tags intact). Suppressing it
                # prevents the raw tagged text from appearing below the clean version.
                return
            _stop_spinner()
            text_buf.append(chunk)
            stream.update(f"{name_tag} {''.join(text_buf)}")

        async def _cancel_watcher() -> None:
            await self._cancel_event.wait()
            if self._agent_task and not self._agent_task.done():
                self._agent_task.cancel()

        watcher = asyncio.create_task(_cancel_watcher())
        try:
            self._agent_task = asyncio.create_task(
                self.agent.run(
                    user_input,
                    on_action=on_action,
                    on_text=on_text,
                    on_tool_result=on_tool_result,
                    desires=self.desires,
                    inner_voice=inner_voice,
                    interrupt_queue=self._input_queue,
                )
            )
            await self._agent_task
            _flush_stream()
            _log_turn_summary()
        except asyncio.CancelledError:
            _flush_stream()
            self._write_log(f"[dim]{INTERRUPT_TOOL_MSG}[/dim]")
        except Exception as e:
            self._write_log(f"[red]エラー: {e}[/red]")
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
            self._agent_task = None
            _stop_spinner()
            if not spinner_task.done():
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(spinner_task, timeout=0.2)
            if not spinner_task.done():
                spinner_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await spinner_task
            stream.update("")
            self._agent_running = False

    async def _desire_tick(self) -> None:
        """Check desires and fire autonomous actions when idle."""
        # Skip if auto_desire is disabled (default OFF)
        if not getattr(self.agent.config, "auto_desire", False):
            return
        now = time.time()
        if not should_fire_idle_desire(
            agent_running=self._agent_running,
            has_pending_input=not self._input_queue.empty(),
            last_interaction=self._last_interaction,
            now=now,
            cooldown=DESIRE_COOLDOWN,
        ):
            return

        tick = desire_tick_prompt(self.desires, [])
        if tick is None:
            return
        if not should_fire_idle_desire(
            agent_running=self._agent_running,
            has_pending_input=not self._input_queue.empty(),
            last_interaction=self._last_interaction,
            now=time.time(),
            cooldown=DESIRE_COOLDOWN,
        ):
            return

        desire_name, prompt, _pending = tick

        try:
            murmur = _t(f"desire_{desire_name}")
        except KeyError:
            murmur = _t("desire_default")
        self._log_system(murmur)

        self._last_interaction = time.time()  # reset cooldown
        await self._run_agent("", inner_voice=prompt)
        self.desires.satisfy(desire_name)
        self.desires.curiosity_target = None

    # ── Realtime STT (hands-free, always-on) ────────────────────

    async def _start_realtime_stt(self) -> None:
        """Initialize and run realtime STT in the background."""
        assert self._realtime_stt is not None
        try:
            loop = asyncio.get_event_loop()

            def _on_partial(text: str) -> None:
                try:
                    stream = self.query_one("#stream", Static)
                    stream.update(f"[dim]\U0001f3a4 {text}[/dim]")
                except Exception:
                    pass

            def _on_committed(text: str) -> None:
                try:
                    self._write_log(
                        f"[bold cyan]\U0001f3a4 {self._companion_name}[/bold cyan] {text}"
                    )
                    self._last_interaction = time.time()
                    stream = self.query_one("#stream", Static)
                    stream.update("")
                except Exception:
                    pass

            self._realtime_stt.on_partial = _on_partial
            self._realtime_stt.on_committed = _on_committed
            self._realtime_stt.on_restart = lambda reason: self._log_system(
                f"🎤 Realtime STT restarting ({reason})"
            )
            await self._realtime_stt.start(loop, self._input_queue)
            self._log_system("\U0001f3a4 Realtime STT ON (ElevenLabs)")
        except Exception as e:
            logger.warning("Realtime STT init failed: %s", e)
            self._log_system(f"\u26a0 Realtime STT init failed: {e}")
            self._realtime_stt = None

    async def _continuous_stt_on_speech(self, text: str) -> None:
        """常時 RTSP STT の 1 発話 committed コールバック (input queue へ投入)。

        realtime_stt の on_committed と同じ責務: ログ表示 + last_interaction 更新 +
        input_queue へ投入。実 I/O (RTSP/ffmpeg/whisper) は stt_kotoba 側で完結し、
        ここはテキスト 1 件を受け取るだけ (テストで直呼びして配線検証する)。
        """
        if not text or not text.strip():
            return
        try:
            self._write_log(
                f"[bold cyan]\U0001f3a4 {self._companion_name}[/bold cyan] {text}"
            )
        except Exception:
            pass
        self._last_interaction = time.time()
        await self._input_queue.put(text)

    async def _start_continuous_stt(self) -> None:
        """Tapo RTSP 常時 STT (Kotoba-Whisper) を起動し task を保持する。

        stt_kotoba.start_rtsp_subscription は依存未満なら no-op task を返すため、
        ここでは silent fail で握る (TUI を落とさない)。一時停止中 (Ctrl+T OFF) は
        起動しない。
        """
        if not self._continuous_stt_enabled:
            return
        if self._continuous_stt_task is not None and not self._continuous_stt_task.done():
            return  # 二重起動防止
        try:
            self._continuous_stt_task = await stt_kotoba.start_rtsp_subscription(
                on_speech=self._continuous_stt_on_speech
            )
            self._log_system("\U0001f3a4 Continuous STT ON (Tapo RTSP / Kotoba-Whisper)")
        except Exception as e:
            logger.warning("Continuous STT start failed: %s", e)
            self._log_system(f"⚠ Continuous STT start failed: {e}")
            self._continuous_stt_task = None

    async def _stop_continuous_stt(self) -> None:
        """常時 RTSP STT task を cancel して停止する (一時停止 / 終了時)。"""
        task = self._continuous_stt_task
        self._continuous_stt_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    async def _on_tapo_event(self, evt: "tapo_event.TapoEvent") -> None:
        """Tapo ONVIF イベント callback — desire を boost する (Phase X Stage C)。

        二層境界の familiar_agent 側。軽量方式: イベントは「何か動いた」の nudge で、
        ``look_around`` (person なら ``greet_companion`` も) を visual=True で boost する。
        次の idle desire tick で Stage B の視覚変化 prompt が立ち、ピコ自身が see/判断する。
        同期 see()/scene は呼ばない。無効 drive への boost は Stage A で no-op。
        """
        try:
            amount = self._tapo_boost_amount()
            if evt.event_type == "person":
                self.desires.boost("greet_companion", amount, visual=True)
            self.desires.boost("look_around", amount, visual=True)
            self._write_log(f"[dim]\U0001f441 {evt.event_type} detected (Tapo)[/dim]")
        except Exception as e:
            logger.warning("Tapo event handling failed: %s", e)

    @staticmethod
    def _tapo_boost_amount() -> float:
        raw = os.environ.get("TAPO_EVENT_BOOST_AMOUNT", "")
        try:
            return float(raw) if raw else 0.25
        except ValueError:
            return 0.25

    async def _start_tapo_events(self) -> None:
        """Tapo ONVIF event 購読を起動し task を保持する (依存未満は no-op task)。"""
        if not self._tapo_events_enabled:
            return
        if self._tapo_event_task is not None and not self._tapo_event_task.done():
            return  # 二重起動防止
        try:
            self._tapo_event_task = await tapo_event.start_event_subscription(
                on_event=self._on_tapo_event
            )
            self._log_system("\U0001f441 Tapo events ON (ONVIF motion/person)")
        except Exception as e:
            logger.warning("Tapo events start failed: %s", e)
            self._tapo_event_task = None

    async def _stop_tapo_events(self) -> None:
        """Tapo event 購読 task を cancel して停止する。"""
        task = self._tapo_event_task
        self._tapo_event_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    async def action_restart_realtime_stt(self) -> None:
        """Reconnect realtime STT after a loop or transient transport issue."""
        if not self._realtime_stt:
            self._log_system("Realtime STT not configured")
            return
        try:
            restarted = await self._realtime_stt.restart(reason="manual")
        except Exception as exc:
            logger.warning("Realtime STT restart failed: %s", exc)
            self._log_system(f"⚠ Realtime STT restart failed: {exc}")
            return
        if restarted:
            self._log_system("🎤 Realtime STT restarted")
        else:
            self._log_system("Realtime STT restart unavailable before startup")

    async def action_toggle_listen(self) -> None:
        """Ctrl+T — 常時 RTSP STT の一時停止 / 再開トグル (デフォルト ON)。

        pico_v3 (Phase C-Ctrl+T 常時化): 旧「一発録音トグル」を、常時購読の
        pause/resume に再定義する。STT は起動時から常時 ON のため、Ctrl+T は
        OFF→ON ではなく「今 ON なら止める / 今 OFF なら再開する」挙動になる。
        一発録音 (_do_record) は常時購読が ON の間は起動しない (二重起動排他)。
        """
        # Debounce: ignore key-repeat events within 0.5 s of the last toggle
        now = time.time()
        if now - self._last_toggle_listen < 0.5:
            return
        self._last_toggle_listen = now

        if self._continuous_stt_enabled:
            # 現在 ON → 一時停止
            self._continuous_stt_enabled = False
            await self._stop_continuous_stt()
            self._log_system("\U0001f507 Continuous STT paused (Ctrl+T to resume)")
        else:
            # 現在 OFF → 再開
            self._continuous_stt_enabled = True
            self._log_system("\U0001f3a4 Continuous STT resuming…")
            self.run_worker(self._start_continuous_stt(), exclusive=False)

    async def _do_record(self) -> None:
        """Worker: record until stop_event, transcribe, then submit as user input."""
        stream = self.query_one("#stream", Static)
        try:
            # Kick off recording as a background task so we can update the UI mid-way
            record_task = asyncio.create_task(
                self.agent.stt.record_and_transcribe(self._stop_recording)  # type: ignore[union-attr]
            )
            # Wait until the user presses Ctrl+M again (stop_event is set)
            await self._stop_recording.wait()
            # Recording has stopped — now transcribing
            stream.remove_class("recording")
            stream.update("🔄 Transcribing…")
            text = await record_task
            if text.strip():
                self._log_user(text)
                self._last_interaction = time.time()
                await self._input_queue.put(text)
        except Exception as e:
            self._log_system(f"STT error: {e}")
        finally:
            self._recording = False
            stream.remove_class("recording")
            stream.update("")

    def action_clear_history(self) -> None:
        self.agent.clear_history()
        self._log_system(_t("history_cleared"))

    def action_cancel_turn(self) -> None:
        """ESC — cancel the running agent turn."""
        if not self._agent_running:
            return
        self._cancel_event.set()
        # Directly cancel the task on every ESC press — the watcher only fires once,
        # so repeated ESC presses must hit the task directly.
        if self._agent_task and not self._agent_task.done():
            self._agent_task.cancel()
        self._log_system(f"[dim]{INTERRUPT_MSG}[/dim]")

    def action_start_ptt(self) -> None:
        """Space — start Push-to-Talk recording (if STT configured)."""
        if not self.agent.stt:
            return
        if self._recording or self._ptt_active:
            return
        self._ptt_active = True
        self._recording = True
        self._stop_recording.clear()
        stream = self.query_one("#stream", Static)
        stream.add_class("recording")
        stream.update("🎙 PTT… (release Space to send)")
        self.run_worker(self._do_record(), exclusive=False)

    def action_stop_ptt(self) -> None:
        """Space released — stop Push-to-Talk recording."""
        if not self._ptt_active:
            return
        self._ptt_active = False
        self._stop_recording.set()

    async def action_quit(self) -> None:
        self._closing = True
        try:
            self._input_queue.put_nowait(None)
            # Cancel the running agent task first (mirrors action_cancel_turn logic).
            # Without this, agent.run() keeps holding _db_lock while agent.close() waits.
            self._cancel_event.set()
            if self._agent_task and not self._agent_task.done():
                self._agent_task.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(self._agent_task), timeout=1.0)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    pass
            if self._realtime_stt:
                try:
                    await asyncio.wait_for(self._realtime_stt.stop(), timeout=2.0)
                except (asyncio.TimeoutError, Exception):
                    pass
            # pico_v3: 常時 RTSP STT task を停止 (cancel)。
            try:
                await asyncio.wait_for(self._stop_continuous_stt(), timeout=2.0)
            except (asyncio.TimeoutError, Exception):
                pass
            # pico_v3 Stage C: Tapo event task を停止 (cancel)。
            try:
                await asyncio.wait_for(self._stop_tapo_events(), timeout=2.0)
            except (asyncio.TimeoutError, Exception):
                pass
            try:
                await asyncio.wait_for(self.agent.close(), timeout=2.0)
            except (asyncio.TimeoutError, Exception):
                pass
            self.exit()
        except BaseException:
            pass
        finally:
            os._exit(0)
