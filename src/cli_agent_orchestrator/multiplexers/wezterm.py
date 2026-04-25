"""WezTerm CLI-backed multiplexer implementation.

WezTerm's ``cli spawn`` command does not support environment injection flags.
The earlier CAO spike assumed ``--set-environment KEY=VALUE`` existed, but
that flag is ignored by ``wezterm cli spawn`` and silently drops
``CAO_TERMINAL_ID`` plus any provider-supplied launch env. Upstream confirmed
this is not in scope in wezterm/wezterm#6565, and there is no config-side Lua
hook for ``cli spawn`` that could repair it.

CAO therefore wraps the spawned argv and sets env vars inside that wrapper
before launching the real target:

- Unix uses ``env KEY=VALUE -- <argv...>``, which exec-replaces cleanly so the
  target remains pane pid 1.
- Windows uses ``powershell.exe -Command ...`` because Windows has no
  ``execve`` equivalent for a direct replace. That leaves PowerShell as the
  WezTerm child and the target as a grandchild, which is safe for CAO because
  this multiplexer does not depend on ``wezterm cli list`` or ``process_name``
  for status. If a future code path does inspect the foreground process name,
  WezTerm's Windows implementation walks descendants and reports the youngest
  attached process (``find_youngest()`` in ``mux/src/localpane.rs``), so the
  actual target should still win once started.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
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


def _default_runner(
    argv: Sequence[str], env: Optional[Mapping[str, str]] = None
) -> subprocess.CompletedProcess[str]:
    # WezTerm CLI emits UTF-8 (Rust). Without explicit encoding, Python's
    # subprocess reader thread uses the locale codepage (cp1252 on Windows),
    # which crashes with UnicodeDecodeError on any non-Latin-1 byte such as
    # box-drawing characters in pane snapshots. Force UTF-8 with replacement
    # so the multiplexer never dies on a stray byte.
    return subprocess.run(
        list(argv),
        env=env,
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
        errors="replace",
    )


def _default_shell() -> str:
    """Return the shell to spawn inside a wezterm pane when no LaunchSpec
    is given. On Windows we deliberately do *not* return ``COMSPEC``
    (= ``cmd.exe``); we prefer pwsh.exe (or Windows PowerShell) so the
    pane the user/agent sees is a modern UTF-8 shell. The PowerShell
    wrapper that injects ``CAO_TERMINAL_ID`` exists only because WezTerm's
    ``cli spawn --set-environment`` is broken upstream — it should not
    be exposing a cmd.exe child to the agent on top of that.
    """
    if sys.platform == "win32":
        return _resolve_powershell_bin()
    return os.environ.get("SHELL", "/bin/sh")


def _ps_single_quote(value: str) -> str:
    """Quote a string for a PowerShell single-quoted literal: ' -> ''."""
    return "'" + value.replace("'", "''") + "'"


def _resolve_powershell_bin() -> str:
    """Pick the PowerShell binary used to wrap WezTerm CLI spawns on Windows.

    Prefers PowerShell 7+ (``pwsh.exe``) over Windows PowerShell 5.1
    (``powershell.exe``). PS 7+ defaults to UTF-8, while PS 5.1 follows
    the locale codepage (cp1252 on Spanish Windows etc.) which mangles
    non-ASCII characters in env values, paste payloads, and provider
    output. Honors ``CAO_POWERSHELL_BIN`` for explicit override.
    """
    override = os.environ.get("CAO_POWERSHELL_BIN")
    if override:
        return override
    pwsh = shutil.which("pwsh") or shutil.which("pwsh.exe")
    if pwsh:
        return pwsh
    return "powershell.exe"


_GUI_BASENAME = re.compile(r"(?i)^wezterm-gui(\.exe)?$")


def _normalize_wezterm_bin(bin_path: str) -> str:
    """Rewrite a ``wezterm-gui[.exe]`` path to its CLI sibling ``wezterm[.exe]``.

    The WezTerm distribution ships two binaries side-by-side: ``wezterm.exe``
    (multiplexer with the ``cli`` subcommand) and ``wezterm-gui.exe`` (the GUI
    front-end which does *not* expose ``cli``). Users frequently set
    ``WEZTERM_EXECUTABLE`` to the GUI binary because the GUI is what they
    launch interactively, but ``wezterm cli spawn ...`` only works against
    ``wezterm.exe``. Rewriting here turns a confusing ``WinError 2`` /
    ``unrecognized subcommand 'cli'`` into a no-op at construction time.
    """
    p = Path(bin_path)
    new_name = _GUI_BASENAME.sub(r"wezterm\1", p.name)
    if new_name == p.name:
        return bin_path
    return str(p.with_name(new_name))


def _wrap_with_env(
    env_vars: Mapping[str, str], argv: Optional[Sequence[str]]
) -> list[str]:
    """Build a wezterm-cli-spawn target argv that injects ``env_vars`` and
    then either runs ``argv`` (when given) or stays as an interactive shell
    (when ``argv`` is None).

    The wrapper exists only because ``wezterm cli spawn --set-environment``
    is a no-op (upstream wezterm/wezterm#6565). Without this trampoline,
    ``CAO_TERMINAL_ID`` and any provider-supplied launch env would be
    silently dropped. We deliberately keep it to a *single* shell layer:

    * Windows + interactive: ``pwsh -NoExit -Command "<env-set>"`` — one
      pwsh process becomes the pane shell.
    * Windows + target argv: ``pwsh -Command "<env-set>; & '<exe>' <args>"``
      — one pwsh execs the target; pane closes when the target exits.
    * Unix: ``env K=V -- <argv-or-shell>`` — ``env`` exec-replaces, so the
      actual target/shell is pane PID 1.
    """
    if sys.platform == "win32":
        env_steps = [
            f"$env:{key}={_ps_single_quote(value)}" for key, value in env_vars.items()
        ]
        ps_bin = _resolve_powershell_bin()
        if not argv:
            # Interactive shell — same pwsh sets env and stays alive.
            env_command = "; ".join(env_steps) if env_steps else ""
            return [
                ps_bin,
                "-NoLogo",
                "-NoProfile",
                "-NoExit",
                "-Command",
                env_command,
            ]
        # Target argv — pwsh sets env then execs target; exits on target exit.
        exe = argv[0]
        args = list(argv[1:])
        ps_args = ",".join(_ps_single_quote(arg) for arg in args)
        command_parts = [f"{step};" for step in env_steps]
        command_parts.append(f"$args=@({ps_args}); " if args else "$args=@(); ")
        command_parts.append(f"& {_ps_single_quote(exe)} @args")
        return [
            ps_bin,
            "-NoLogo",
            "-NoProfile",
            "-Command",
            "".join(command_parts),
        ]

    target = list(argv) if argv else [_default_shell()]
    wrapped = ["env"]
    wrapped.extend(f"{key}={value}" for key, value in env_vars.items())
    wrapped.append("--")
    wrapped.extend(target)
    return wrapped


class WezTermMultiplexer(BaseMultiplexer):
    """WezTerm CLI-backed multiplexer.

    Session and window state is tracked in an in-memory registry keyed by
    session_name and window_name.  Cross-process visibility (sessions created
    by another process) is not supported — callers must use the same instance
    that created the session.  ``get_multiplexer()`` enforces this singleton.

    Supported send_special_key names (literal=False):
        Enter, Tab, Escape, Backspace, Up, Down, Left, Right
    Any other value raises KeyError — use literal=True for arbitrary VT bytes.

    Thread safety: all mutations and reads of ``_sessions`` and ``_pollers``
    are guarded by ``_lock``.  Subprocess calls (``_run``, ``_get_pane_text``,
    ``_spawn``) are intentionally performed *outside* the lock to avoid
    holding it across blocking I/O.
    """

    def __init__(
        self,
        runner: Optional[WezTermRunner] = None,
        wezterm_bin: Optional[str] = None,
        poll_interval: float = 0.5,
        clock_sleep: Optional[Callable[[float], None]] = None,
    ) -> None:
        self._run: WezTermRunner = runner or _default_runner
        resolved_bin = wezterm_bin or os.environ.get("WEZTERM_EXECUTABLE") or "wezterm"
        normalized_bin = _normalize_wezterm_bin(resolved_bin)
        if normalized_bin != resolved_bin:
            logger.warning(
                "WEZTERM_EXECUTABLE points to the GUI binary (%s); "
                "rewriting to its CLI sibling %s. wezterm-gui has no `cli` "
                "subcommand. Set WEZTERM_EXECUTABLE to the wezterm[.exe] "
                "path to silence this warning.",
                resolved_bin,
                normalized_bin,
            )
        self._bin: str = normalized_bin
        self._sessions: dict[str, dict[str, str]] = {}
        self._pollers: dict[tuple[str, str], _PollerState] = {}
        self._poll_interval = poll_interval
        self._clock_sleep = clock_sleep or time.sleep
        self._lock = threading.Lock()

    def _pane_id(self, session_name: str, window_name: str) -> str:
        with self._lock:
            session = self._sessions.get(session_name)
            if session is None or window_name not in session:
                raise KeyError(
                    f"WezTerm pane not found: session={session_name!r} window={window_name!r}"
                )
            return session[window_name]

    def _spawn(
        self,
        working_directory: str,
        terminal_id: str,
        launch_spec: Optional[LaunchSpec],
    ) -> str:
        env_vars: dict[str, str] = {"CAO_TERMINAL_ID": terminal_id}
        if launch_spec is not None and launch_spec.env:
            env_vars.update(launch_spec.env)

        target_argv: Optional[list[str]] = None
        if launch_spec is not None and launch_spec.argv:
            target_argv = list(launch_spec.argv)

        wrapped = _wrap_with_env(env_vars, target_argv)
        cmd: list[str] = [
            self._bin,
            "cli",
            "spawn",
            "--new-window",
            "--cwd",
            working_directory,
            "--",
            *wrapped,
        ]
        # Subprocess call outside lock — blocking I/O must not hold the lock.
        result = self._run(cmd, None)
        raw = result.stdout.strip()
        if not raw.isdigit():
            raise RuntimeError(
                f"WezTerm spawn returned no pane id; stdout={result.stdout!r}"
            )
        return raw

    def _create_pane(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        working_directory: Optional[str],
        launch_spec: Optional[LaunchSpec],
    ) -> str:
        """Shared body for create_session and create_window.

        Resolves cwd, spawns the pane (outside lock), registers the pane dict,
        and returns window_name.
        """
        cwd = self._resolve_and_validate_working_directory(working_directory)
        # Spawn outside lock — may block.
        pane_id = self._spawn(cwd, terminal_id, launch_spec)
        with self._lock:
            self._sessions.setdefault(session_name, {})[window_name] = pane_id
        return window_name

    def create_session(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        working_directory: Optional[str] = None,
        launch_spec: Optional[LaunchSpec] = None,
    ) -> str:
        """Create a detached CAO session/workspace and return the actual window name."""
        return self._create_pane(session_name, window_name, terminal_id, working_directory, launch_spec)

    def create_window(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        working_directory: Optional[str] = None,
        launch_spec: Optional[LaunchSpec] = None,
    ) -> str:
        """Create another CAO window/pane inside an existing session."""
        return self._create_pane(session_name, window_name, terminal_id, working_directory, launch_spec)

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
            return current[len(prev):]
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
        try:
            with open(file_path, "a", encoding="utf-8") as fh:
                while not stop_event.wait(self._poll_interval):
                    # Fetch pane text outside lock — subprocess call may block.
                    try:
                        snapshot = self._get_pane_text(pane_id)
                    except RuntimeError:
                        return
                    delta = self._diff_snapshot(prev, snapshot)
                    if delta:
                        fh.write(delta)
                        fh.flush()
                        prev = snapshot
        finally:
            # Self-clean on natural exit (pane disappeared or stop_event set).
            # A timed-out stop_pipe_pane leaves the registry entry in place to
            # block double-writers; this still runs once the zombie unblocks.
            with self._lock:
                self._pollers.pop(key, None)

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
            if key not in _VT_KEY_MAP:
                raise KeyError(
                    f"Unknown special key {key!r}; expected one of {sorted(_VT_KEY_MAP)}, "
                    f"or pass literal=True for raw VT sequences"
                )
            raw = _VT_KEY_MAP[key]
        self._run(
            [self._bin, "cli", "send-text", "--pane-id", pane_id, "--no-paste", "--", raw],
            None,
        )

    def get_history(
        self, session_name: str, window_name: str, tail_lines: Optional[int] = None
    ) -> str:
        """Return pane text via wezterm cli get-text (no --escapes).

        Plain mode is used because --escapes breaks Claude trust-prompt regex
        matching while plain output preserves all provider-relevant patterns.
        """
        pane_id = self._pane_id(session_name, window_name)
        text = self._get_pane_text(pane_id)
        if tail_lines is not None:
            lines = text.rstrip("\n").splitlines()
            text = "\n".join(lines[-tail_lines:])
        return text

    def list_sessions(self) -> list[dict[str, str]]:
        """List CAO-visible sessions from the in-memory registry."""
        with self._lock:
            return [
                {"id": name, "name": name, "status": "active"}
                for name in self._sessions
            ]

    def kill_session(self, session_name: str) -> bool:
        """Terminate a session and kill all owned panes."""
        with self._lock:
            session = self._sessions.get(session_name)
            if session is None:
                return False
            window_names = list(session)
        # Stop pollers outside lock (join may block).
        for window_name in window_names:
            try:
                self.stop_pipe_pane(session_name, window_name)
            except RuntimeError:
                pass
        # Kill panes and remove from registry under lock.
        with self._lock:
            session = self._sessions.pop(session_name, None)
        if session is not None:
            for pane_id in session.values():
                self._run(
                    [self._bin, "cli", "kill-pane", "--pane-id", pane_id],
                    None,
                )
        return True

    def kill_window(self, session_name: str, window_name: str) -> bool:
        """Terminate one CAO window/pane."""
        with self._lock:
            session = self._sessions.get(session_name)
            if session is None or window_name not in session:
                return False
        # Stop poller outside lock (join may block).
        try:
            self.stop_pipe_pane(session_name, window_name)
        except RuntimeError:
            pass
        with self._lock:
            session = self._sessions.get(session_name)
            if session is None:
                return False
            pane_id = session.pop(window_name, None)
        if pane_id is not None:
            self._run(
                [self._bin, "cli", "kill-pane", "--pane-id", pane_id],
                None,
            )
        return True

    def session_exists(self, session_name: str) -> bool:
        """Return True when the named session is in the registry."""
        with self._lock:
            return session_name in self._sessions

    def get_pane_working_directory(
        self, session_name: str, window_name: str
    ) -> Optional[str]:
        """Return the pane's working directory when the CLI exposes it.

        WezTerm CLI does not expose pane CWD reliably in early versions, so
        we return None until ``wezterm cli list --format json`` is validated.
        """
        return None

    def pipe_pane(self, session_name: str, window_name: str, file_path: str) -> None:
        pane_id = self._pane_id(session_name, window_name)
        key = (session_name, window_name)
        with self._lock:
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
            self._pollers[key] = _PollerState(thread=thread, stop_event=stop_event)
            thread.start()

    def stop_pipe_pane(self, session_name: str, window_name: str) -> None:
        key = (session_name, window_name)
        with self._lock:
            state = self._pollers.get(key)
        if state is None:
            raise RuntimeError(
                f"pipe_pane not running for {session_name}:{window_name}"
            )
        state.stop_event.set()
        # Join outside lock — blocking wait must not hold the lock.
        state.thread.join(timeout=2.0)
        if state.thread.is_alive():
            # Poller thread is stalled (zombie).  Leave the registry entry in
            # place so that a subsequent pipe_pane() call raises rather than
            # starting a second thread writing to the same file concurrently.
            # The zombie thread will self-clean via _poll_loop's finally block
            # once the pane disappears and _get_pane_text raises.
            logger.warning(
                "Timed out stopping WezTerm pipe poller for %s:%s — "
                "leaving zombie entry in registry to prevent double-write",
                session_name,
                window_name,
            )
            return
        # Thread exited cleanly; remove the registry entry.
        with self._lock:
            self._pollers.pop(key, None)
