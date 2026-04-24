"""WezTerm CLI-backed multiplexer implementation."""

from __future__ import annotations

import os
import subprocess
import time
from typing import Callable, Mapping, Optional, Sequence

from cli_agent_orchestrator.multiplexers.base import BaseMultiplexer, LaunchSpec

WezTermRunner = Callable[
    [Sequence[str], Optional[Mapping[str, str]]],
    "subprocess.CompletedProcess[str]",
]

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
    ) -> None:
        self._run: WezTermRunner = runner or _default_runner
        self._bin: str = wezterm_bin or os.environ.get("WEZTERM_EXECUTABLE") or "wezterm"
        self._sessions: dict[str, dict[str, dict[str, Optional[str]]]] = {}

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
                time.sleep(0.3)
            else:
                time.sleep(0.5)
            self._run(
                [self._bin, "cli", "send-text", "--pane-id", pane_id, "--no-paste", "--", "\r"],
                None,
            )

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
        result = self._run(
            [self._bin, "cli", "get-text", "--pane-id", pane_id],
            None,
        )
        text = result.stdout
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
        session = self._sessions.pop(session_name, None)
        if session is None:
            return False
        for window_info in session.values():
            pane_id = window_info.get("pane_id")
            if pane_id is not None:
                self._run(
                    [self._bin, "cli", "kill-pane", "--pane-id", pane_id],
                    None,
                )
        return True

    def kill_window(self, session_name: str, window_name: str) -> bool:
        """Terminate one CAO window/pane."""
        session = self._sessions.get(session_name)
        if session is None or window_name not in session:
            return False
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
        raise NotImplementedError(
            "Task 7 (poller-backed pipe_pane) not yet implemented"
        )

    def stop_pipe_pane(self, session_name: str, window_name: str) -> None:
        raise NotImplementedError(
            "Task 7 (poller-backed pipe_pane) not yet implemented"
        )
