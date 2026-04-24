"""WezTerm CLI-backed multiplexer implementation."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from cli_agent_orchestrator.multiplexers.base import BaseMultiplexer, LaunchSpec

WezTermRunner = Callable[
    [Sequence[str], Optional[Mapping[str, str]]],
    "subprocess.CompletedProcess[str]",
]

logger = logging.getLogger(__name__)

_VT_KEY_MAP: dict[str, str] = {
    "Enter": "\r",
    "Tab": "\t",
    "Escape": "\x1b",
    "Backspace": "\x7f",
    "Up": "\x1b[A",
    "Down": "\x1b[B",
    "Left": "\x1b[D",
    "Right": "\x1b[C",
}


@dataclass
class _PollerState:
    thread: threading.Thread
    stop_event: threading.Event
    snapshot: str
    file_path: str


def _default_runner(
    argv: Sequence[str], env: Optional[Mapping[str, str]] = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv), env=env, capture_output=True, text=True, check=False
    )


class WezTermMultiplexer(BaseMultiplexer):
    """WezTerm CLI-backed multiplexer.

    Session and window state is tracked in an in-memory registry keyed by
    session_name and window_name.  Cross-process visibility (sessions created
    by another process) is not supported in this MVP — callers must use the
    same instance that created the session.  This limitation is intentional
    for Phase 2; Task 4's get_multiplexer() accessor enforces singleton use.

    Supported send_special_key names (literal=False):
        Enter, Tab, Escape, Backspace, Up, Down, Left, Right
    Any other value raises KeyError — use literal=True for arbitrary VT bytes.
    """

    def __init__(
        self,
        runner: Optional[WezTermRunner] = None,
        wezterm_bin: Optional[str] = None,
        poll_interval: float = 0.5,
        clock_sleep: Optional[Callable[[float], None]] = None,
    ) -> None:
        self._run: WezTermRunner = runner or _default_runner
        self._bin: str = wezterm_bin or os.environ.get("WEZTERM_EXECUTABLE") or "wezterm"
        self._sessions: dict[str, dict[str, dict[str, Optional[str]]]] = {}
        self._pollers: dict[tuple[str, str], _PollerState] = {}
        self._poll_interval = poll_interval
        self._clock_sleep = clock_sleep or time.sleep

    def _pane_id(self, session_name: str, window_name: str) -> str:
        session = self._sessions.get(session_name)
        if session is None or window_name not in session:
            raise RuntimeError(
                f"WezTerm pane not found: session={session_name!r} window={window_name!r}"
            )
        pane_id = session[window_name].get("pane_id")
        if pane_id is None:
            raise RuntimeError(
                f"WezTerm pane not found: session={session_name!r} window={window_name!r}"
            )
        return pane_id

    def _spawn(
        self,
        working_directory: str,
        terminal_id: str,
        launch_spec: Optional[LaunchSpec],
    ) -> str:
        cmd: list[str] = [self._bin, "cli", "spawn", "--new-window", "--cwd", working_directory]
        cmd += ["--set-environment", f"CAO_TERMINAL_ID={terminal_id}"]
        if launch_spec is not None and launch_spec.env:
            for key, value in launch_spec.env.items():
                cmd += ["--set-environment", f"{key}={value}"]
        if launch_spec is not None and launch_spec.argv:
            cmd.append("--")
            cmd.extend(launch_spec.argv)
        result = self._run(cmd, None)
        raw = result.stdout.strip()
        if not raw.isdigit():
            raise RuntimeError(
                f"WezTerm spawn returned no pane id; stdout={result.stdout!r}"
            )
        return raw

    def create_session(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        working_directory: Optional[str] = None,
        launch_spec: Optional[LaunchSpec] = None,
    ) -> str:
        """Create a detached CAO session/workspace and return the actual window name."""
        cwd = self._resolve_and_validate_working_directory(working_directory)
        pane_id = self._spawn(cwd, terminal_id, launch_spec)
        self._sessions.setdefault(session_name, {})
        self._sessions[session_name][window_name] = {
            "pane_id": pane_id,
            "tab_id": None,
            "window_id": None,
        }
        return window_name

    def create_window(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        working_directory: Optional[str] = None,
        launch_spec: Optional[LaunchSpec] = None,
    ) -> str:
        """Create another CAO window/pane inside an existing session."""
        cwd = self._resolve_and_validate_working_directory(working_directory)
        pane_id = self._spawn(cwd, terminal_id, launch_spec)
        self._sessions.setdefault(session_name, {})
        self._sessions[session_name][window_name] = {
            "pane_id": pane_id,
            "tab_id": None,
            "window_id": None,
        }
        return window_name

    def _paste_text(self, session_name: str, window_name: str, text: str) -> None:
        """Inject literal text using bracketed paste (default send-text mode)."""
        pane_id = self._pane_id(session_name, window_name)
        self._run(
            [self._bin, "cli", "send-text", "--pane-id", pane_id, "--", text],
            None,
        )

    def _submit_input(
        self, session_name: str, window_name: str, enter_count: int = 1
    ) -> None:
        """Submit already-pasted input with one or more Enter presses."""
        pane_id = self._pane_id(session_name, window_name)
        for i in range(enter_count):
            if i == 0:
                self._clock_sleep(0.3)
            else:
                self._clock_sleep(0.5)
            self._run(
                [self._bin, "cli", "send-text", "--pane-id", pane_id, "--no-paste", "--", "\r"],
                None,
            )

    def _get_pane_text(self, pane_id: str) -> str:
        result = self._run(
            [self._bin, "cli", "get-text", "--pane-id", pane_id],
            None,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"WezTerm get-text failed for pane {pane_id}; returncode={result.returncode}"
            )
        return result.stdout

    def _diff_snapshot(self, prev: str, current: str) -> str:
        if not prev:
            return current
        if current == prev:
            return ""
        if current.startswith(prev):
            return current[len(prev) :]
        prev_lines = prev.splitlines(keepends=True)
        cur_lines = current.splitlines(keepends=True)
        for k in range(min(len(prev_lines), len(cur_lines)), 0, -1):
            if prev_lines[-k:] == cur_lines[:k]:
                return "".join(cur_lines[k:])
        return current

    def _poll_loop(
        self,
        session_name: str,
        window_name: str,
        pane_id: str,
        stop_event: threading.Event,
        file_path: str,
    ) -> None:
        key = (session_name, window_name)
        prev = ""
        while not stop_event.wait(self._poll_interval):
            try:
                snapshot = self._get_pane_text(pane_id)
            except RuntimeError:
                return
            delta = self._diff_snapshot(prev, snapshot)
            if delta:
                with open(file_path, "a", encoding="utf-8") as fh:
                    fh.write(delta)
                prev = snapshot
                poller = self._pollers.get(key)
                if poller is not None:
                    poller.snapshot = snapshot
            elif current_state := self._pollers.get(key):
                current_state.snapshot = snapshot
                prev = snapshot

    def send_special_key(
        self,
        session_name: str,
        window_name: str,
        key: str,
        *,
        literal: bool = False,
    ) -> None:
        """Send a control key or literal VT sequence without paste semantics.

        When literal=False, key must be one of the named keys in _VT_KEY_MAP:
            Enter, Tab, Escape, Backspace, Up, Down, Left, Right
        When literal=True, the key string is sent as-is (raw VT bytes).
        """
        pane_id = self._pane_id(session_name, window_name)
        if literal:
            raw = key
        else:
            raw = _VT_KEY_MAP[key]
        self._run(
            [self._bin, "cli", "send-text", "--pane-id", pane_id, "--no-paste", "--", raw],
            None,
        )

    def get_history(
        self, session_name: str, window_name: str, tail_lines: Optional[int] = None
    ) -> str:
        """Return pane text via wezterm cli get-text (no --escapes).

        Plain mode is used because spike 4 showed that --escapes breaks
        Claude trust-prompt regex matching while plain output preserves all
        provider-relevant patterns.
        """
        pane_id = self._pane_id(session_name, window_name)
        text = self._get_pane_text(pane_id)
        if tail_lines is not None:
            lines = text.rstrip("\n").splitlines()
            text = "\n".join(lines[-tail_lines:])
        return text

    def list_sessions(self) -> list[dict[str, str]]:
        """List CAO-visible sessions from the in-memory registry."""
        return [
            {"id": name, "name": name, "status": "active"}
            for name in self._sessions
        ]

    def kill_session(self, session_name: str) -> bool:
        """Terminate a session and kill all owned panes."""
        session = self._sessions.get(session_name)
        if session is None:
            return False
        for window_name in list(session):
            try:
                self.stop_pipe_pane(session_name, window_name)
            except RuntimeError:
                pass
        for window_info in session.values():
            pane_id = window_info.get("pane_id")
            if pane_id is not None:
                self._run(
                    [self._bin, "cli", "kill-pane", "--pane-id", pane_id],
                    None,
                )
        self._sessions.pop(session_name, None)
        return True

    def kill_window(self, session_name: str, window_name: str) -> bool:
        """Terminate one CAO window/pane."""
        session = self._sessions.get(session_name)
        if session is None or window_name not in session:
            return False
        try:
            self.stop_pipe_pane(session_name, window_name)
        except RuntimeError:
            pass
        window_info = session.pop(window_name)
        pane_id = window_info.get("pane_id")
        if pane_id is not None:
            self._run(
                [self._bin, "cli", "kill-pane", "--pane-id", pane_id],
                None,
            )
        return True

    def session_exists(self, session_name: str) -> bool:
        """Return True when the named session is in the registry."""
        return session_name in self._sessions

    def get_pane_working_directory(
        self, session_name: str, window_name: str
    ) -> Optional[str]:
        """Return the pane's working directory when the CLI exposes it.

        WezTerm CLI does not expose pane CWD reliably in early versions.
        Returns None for MVP; Task 9 can add wezterm cli list --format json
        parsing once the JSON API is validated against the target release.
        """
        return None

    def pipe_pane(self, session_name: str, window_name: str, file_path: str) -> None:
        pane_id = self._pane_id(session_name, window_name)
        key = (session_name, window_name)
        if key in self._pollers:
            raise RuntimeError(
                f"pipe_pane already running for {session_name}:{window_name}"
            )
        Path(file_path).touch(exist_ok=True)
        stop_event = threading.Event()
        thread = threading.Thread(
            target=self._poll_loop,
            args=(session_name, window_name, pane_id, stop_event, file_path),
            daemon=True,
            name=f"wezterm-pipe-{session_name}-{window_name}",
        )
        self._pollers[key] = _PollerState(
            thread=thread,
            stop_event=stop_event,
            snapshot="",
            file_path=file_path,
        )
        thread.start()

    def stop_pipe_pane(self, session_name: str, window_name: str) -> None:
        key = (session_name, window_name)
        state = self._pollers.get(key)
        if state is None:
            raise RuntimeError(
                f"pipe_pane not running for {session_name}:{window_name}"
            )
        state.stop_event.set()
        state.thread.join(timeout=2.0)
        if state.thread.is_alive():
            logger.warning(
                "Timed out stopping WezTerm pipe poller for %s:%s",
                session_name,
                window_name,
            )
        self._pollers.pop(key, None)
