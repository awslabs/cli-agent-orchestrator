"""Simplified tmux client as module singleton."""

import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional

import libtmux

from cli_agent_orchestrator.constants import TMUX_HISTORY_LINES
from cli_agent_orchestrator.services.control_input_contract import (
    contains_bracketed_paste_sentinel,
    is_valid_pane_id,
)
from cli_agent_orchestrator.utils.path_validation import (
    BLOCKED_SYSTEM_DIRECTORIES,
    resolve_and_validate_path,
)
from cli_agent_orchestrator.utils.terminal import validate_tmux_name

logger = logging.getLogger(__name__)

_TMUX_BINARY: Optional[str] = None

# Immutable pane facts, tab-separated.  The two variable-content fields
# (session and window name) come last so a tab inside a foreign window's
# name can shift only itself, never the identity fields ahead of it.
_PANE_CONTROL_FORMAT = "\t".join(
    (
        "#{pane_id}",
        "#{window_id}",
        "#{pane_pid}",
        "#{bracket_paste_flag}",
        "#{pane_dead}",
        "#{session_name}",
        "#{window_name}",
    )
)
_PANE_CONTROL_FIELDS = 7

# Literal control text is written in bounded chunks so one oversized
# control cannot produce a single unbounded argv.
_LITERAL_CHUNK_CHARS = 1024

# Bytes that must never appear in literal control text: ESC and its
# single-byte C1 CSI equivalent U+009B would both let a payload
# synthesise its own escape sequences (including the very paste sentinels
# this path exists to eliminate), and CR/LF would submit at a point the
# caller did not choose.  The control contract is one line plus one
# explicit Enter.  Screening ESC alone would leave the 8-bit spelling as
# a working way to write the identical bytes.
_ILLEGAL_LITERAL_CHARS = ("\x1b", "\x9b", "\r", "\n")


@dataclass(frozen=True)
class PaneControlIdentity:
    """Live tmux facts about one pane, as observed at a single instant.

    ``pane_id`` and ``window_id`` are immutable for the resource's life;
    ``pane_pid`` is immutable for one incarnation of the pane's root
    process.  Together they are the only tmux facts a control call may
    bind to — a window *name* is a label that can be reused by a later,
    unrelated window.
    """

    pane_id: str
    window_id: str
    pane_pid: int
    session_name: str
    window_name: str
    bracketed_paste_proven: bool
    dead: bool


class TmuxLiteralSendError(RuntimeError):
    """A literal control write failed part-way through.

    ``chunks_sent`` and ``enter_attempted`` bound what may already have
    reached the pane, so the caller can distinguish "provably nothing
    was written" from "the outcome is unknowable" instead of assuming
    the write can simply be repeated.
    """

    def __init__(self, message: str, *, chunks_sent: int, enter_attempted: bool) -> None:
        super().__init__(message)
        # Writes tmux reported as successful before this failure.  The
        # failing write itself may still have landed in part.
        self.chunks_sent = chunks_sent
        self.enter_attempted = enter_attempted


def _parse_pane_control_record(line: str) -> Optional[PaneControlIdentity]:
    """Parse one ``list-panes -F`` line, or None if it is not usable.

    A line that cannot be parsed is dropped rather than repaired: partial
    identity is worse than absent identity, because the caller would bind
    a control to facts that were never observed.
    """
    fields = line.split("\t", _PANE_CONTROL_FIELDS - 1)
    if len(fields) != _PANE_CONTROL_FIELDS:
        return None
    pane_id, window_id, pane_pid, bracket_flag, dead_flag, session_name, window_name = fields
    if not is_valid_pane_id(pane_id) or not window_id.startswith("@"):
        return None
    try:
        pid = int(pane_pid)
    except ValueError:
        return None
    if pid <= 0:
        return None
    return PaneControlIdentity(
        pane_id=pane_id,
        window_id=window_id,
        pane_pid=pid,
        session_name=session_name,
        window_name=window_name,
        # Only an explicit '1' proves the pane's application advertised
        # ?2004h.  An older tmux that does not know this format expands
        # it to nothing, which stays unproven rather than becoming
        # support the pane never claimed.
        bracketed_paste_proven=bracket_flag == "1",
        dead=dead_flag == "1",
    )


def tmux_binary() -> str:
    """The absolute canonical tmux executable, resolved once and reused.

    P1-9 (final conformance §20.2f): the managed campaign path's tmux
    invocation is wholly absolute — a per-call PATH lookup (or a mid-run PATH
    change) can never redirect managed window creation to a different binary.
    Fails closed when tmux is not resolvable at all.
    """
    global _TMUX_BINARY
    if _TMUX_BINARY is None:
        resolved = shutil.which("tmux")
        if not resolved:
            raise RuntimeError("tmux executable is not resolvable")
        _TMUX_BINARY = os.path.realpath(resolved)
    return _TMUX_BINARY


class TmuxClient:
    """Simplified tmux client for basic operations."""

    def __init__(self) -> None:
        self.server = libtmux.Server()

    # Kept as an alias so existing callers/tests referencing the class
    # attribute keep working; the canonical set lives in
    # utils/path_validation.py (shared with archive export/import, D5).
    _BLOCKED_DIRECTORIES = BLOCKED_SYSTEM_DIRECTORIES

    def _resolve_and_validate_working_directory(self, working_directory: Optional[str]) -> str:
        """Resolve and validate working directory.

        Delegates to the shared validator
        (``utils.path_validation.resolve_and_validate_path``) with its
        strictest settings: the directory must already exist and file
        targets are rejected — byte-identical to the pre-extraction
        behavior.

        **Allowed directories:**

        - Any real directory that is not a blocked system path
        - Paths outside ``~/`` are permitted (e.g., ``/Volumes/workplace``,
          ``/opt/projects``, NFS mounts)

        **Blocked (unsafe) directories:**

        - System directories: ``/``, ``/bin``, ``/sbin``, ``/usr/bin``,
          ``/usr/sbin``, ``/etc``, ``/var``, ``/tmp``, ``/dev``, ``/proc``,
          ``/sys``, ``/root``, ``/boot``, ``/lib``, ``/lib64``

        Args:
            working_directory: Optional directory path, defaults to current directory

        Returns:
            Canonicalized absolute path

        Raises:
            ValueError: If directory does not exist or is a blocked system path
        """
        if working_directory is None:
            working_directory = os.getcwd()

        return resolve_and_validate_path(
            working_directory,
            allow_create=False,
            allow_file=False,
            description="Working directory",
        )

    # Provider env vars that would cause "nested session" errors when CAO
    # itself runs inside a provider (e.g. Claude Code), unless explicitly
    # allow-listed for provider authentication (Bedrock, Vertex AI, Foundry).
    # Applied to BOTH inherited env and operator-supplied --env vars so a
    # forwarded ``CLAUDE_CODE_*`` cannot reintroduce nesting.
    _BLOCKED_ENV_PREFIXES = ("CLAUDE", "CODEX_", "__MISE_")
    _BLOCKED_PREFIX_ALLOWLIST = frozenset(
        {
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX",
            "CLAUDE_CODE_USE_FOUNDRY",
            "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
            "CLAUDE_CODE_SKIP_VERTEX_AUTH",
            "CLAUDE_CODE_SKIP_FOUNDRY_AUTH",
        }
    )
    # Per-var value cap (PR #246) — keeps the full tmux ``new-session -e`` /
    # ``new-window -e`` argv under the kernel argv limit on busy hosts.
    _MAX_ENV_VALUE_BYTES = 2048

    @classmethod
    def _is_blocked_env_key(cls, key: str) -> bool:
        """Return True if ``key`` matches a blocked prefix and isn't allowlisted."""
        if key in cls._BLOCKED_PREFIX_ALLOWLIST:
            return False
        return any(key.startswith(p) for p in cls._BLOCKED_ENV_PREFIXES)

    @classmethod
    def _merge_extra_env(
        cls, environment: Dict[str, str], extra_env: Optional[Dict[str, str]]
    ) -> None:
        """Merge operator-supplied env vars into ``environment`` in place.

        Mirrors the safety constraints applied to inherited env (blocked
        prefixes, 2048-byte value cap) so a malformed --env entry cannot
        slip past the validation that runs at the CLI boundary.
        """
        if not extra_env:
            return
        for key, value in extra_env.items():
            if cls._is_blocked_env_key(key):
                logger.warning("Dropping forwarded env var with blocked prefix: %s", key)
                continue
            if len(value.encode("utf-8")) >= cls._MAX_ENV_VALUE_BYTES:
                logger.warning(
                    "Dropping forwarded env var %s — value exceeds %d bytes",
                    key,
                    cls._MAX_ENV_VALUE_BYTES,
                )
                continue
            environment[key] = value

    def create_session(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        working_directory: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> str:
        """Create detached tmux session with initial window and return window name."""
        try:
            working_directory = self._resolve_and_validate_working_directory(working_directory)

            # Only pass essential env vars to avoid tmux "command too long"
            essential_keys = {
                "HOME",
                "PATH",
                "SHELL",
                "USER",
                "LANG",
                "LC_ALL",
                "LC_CTYPE",
                "TERM",
                "SSH_AUTH_SOCK",
                "DISPLAY",
                "XDG_RUNTIME_DIR",
                "DO_NOT_TRACK",
            }
            environment = {
                k: v
                for k, v in os.environ.items()
                if (
                    k in essential_keys
                    or k in self._BLOCKED_PREFIX_ALLOWLIST
                    or (
                        not self._is_blocked_env_key(k)
                        and k.startswith(("CAO_", "KIRO_", "MISE_", "AWS_"))
                        and len(v.encode("utf-8")) < self._MAX_ENV_VALUE_BYTES
                    )
                )
            }
            # Operator-forwarded vars (from ``cao launch --env``) merge AFTER
            # the inherited slice and override on key collision, so an
            # explicit ``--env AWS_REGION=us-west-2`` wins over the inherited
            # value. See issue #248.
            self._merge_extra_env(environment, extra_env)
            environment["CAO_TERMINAL_ID"] = terminal_id

            # Explicit 220x50 pane size avoids the default 80x24 that tmux
            # assigns to detached sessions. kiro-cli 2.1.x's TUI v2 fails to
            # repaint after a SIGWINCH from the attach-time resize (80x24 →
            # user's real terminal): the screen goes blank and input is
            # silently dropped. Starting at a larger size makes the attach
            # resize a no-op/shrink, which kiro handles correctly. All other
            # providers tolerate wider panes. See issue #216.
            session = self.server.new_session(
                session_name=session_name,
                window_name=window_name,
                start_directory=working_directory,
                detach=True,
                environment=environment,
                x=220,
                y=50,
            )
            logger.info(
                f"Created tmux session: {session_name} with window: {window_name} in directory: {working_directory}"
            )
            window_name_result = session.windows[0].name
            if window_name_result is None:
                raise ValueError(f"Window name is None for session {session_name}")
            return window_name_result
        except Exception as e:
            logger.error(f"Failed to create session {session_name}: {e}")
            raise

    def create_window(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        working_directory: Optional[str] = None,
        window_shell: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> str:
        """Create window in session and return window name.

        ``extra_env`` carries operator-forwarded vars from
        ``cao launch --env`` so workers spawned via ``assign`` / ``handoff`` /
        the web UI inherit the same context as the supervisor. See issue #248.
        """
        try:
            working_directory = self._resolve_and_validate_working_directory(working_directory)

            session = self.server.sessions.get(session_name=session_name)
            if not session:
                raise ValueError(f"Session '{session_name}' not found")

            window_env: dict[str, str] = {}
            self._merge_extra_env(window_env, extra_env)
            window_env["CAO_TERMINAL_ID"] = terminal_id

            kwargs: dict = {
                "window_name": window_name,
                "start_directory": working_directory,
                "environment": window_env,
            }
            if window_shell:
                kwargs["window_shell"] = window_shell

            window = session.new_window(**kwargs)

            logger.info(
                f"Created window '{window.name}' in session '{session_name}' in directory: {working_directory}"
            )
            window_name_result = window.name
            if window_name_result is None:
                raise ValueError(f"Window name is None for session {session_name}")
            return window_name_result
        except Exception as e:
            logger.error(f"Failed to create window in session {session_name}: {e}")
            raise

    def create_window_with_argv(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        argv: List[str],
        working_directory: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> str:
        """Create a window running ``argv`` as the pane's OWN process.

        tmux >= 3.2 executes a multi-argument command directly — no shell is
        ever started and nothing is typed into one (the zero-keystroke managed
        bridge contract). Older tmux rejects the extra arguments, which fails
        closed here. Raises on any failure: the managed caller never degrades
        to typing a command into a shell."""
        if not argv or not all(isinstance(item, str) and "\x00" not in item for item in argv):
            raise ValueError("argv must be a non-empty list of NUL-free strings")
        if not os.path.isabs(argv[0]):
            raise ValueError("argv executable must be an absolute path")
        working_directory = self._resolve_and_validate_working_directory(working_directory)
        window_env: dict[str, str] = {}
        self._merge_extra_env(window_env, extra_env)
        window_env["CAO_TERMINAL_ID"] = terminal_id
        cmd = [tmux_binary(), "new-window", "-d", "-n", window_name, "-c", working_directory]
        for key, value in window_env.items():
            cmd += ["-e", f"{key}={value}"]
        cmd += ["-t", session_name, "--", *argv]
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"tmux could not create the managed window process atomically: "
                f"{proc.stderr.strip() or proc.stdout.strip()}"
            )
        return window_name

    def send_keys(
        self,
        session_name: str,
        window_name: str,
        keys: str,
        enter_count: int = 1,
        force_bracketed_paste: bool = False,
        submit_delay: float = 0.3,
    ) -> None:
        """Send keys to window using tmux paste-buffer for instant delivery.

        Uses load-buffer + paste-buffer instead of chunked send-keys to avoid
        slow character-by-character input and special character interpretation.
        The -p flag enables bracketed paste mode so multi-line content is treated
        as a single input rather than submitting on each newline.

        Args:
            session_name: Name of tmux session
            window_name: Name of window in session
            keys: Text to send
            enter_count: Number of Enter keys to send after pasting (default 1).
                Some TUIs enter multi-line mode after bracketed paste,
                requiring 2 Enters to submit.
            force_bracketed_paste: If True, unconditionally wrap content in
                bracketed paste sequences (\x1b[200~...\x1b[201~) instead of
                relying on paste-buffer -p. Use for message delivery to TUIs.
                Do NOT use for shell commands sent to bash during initialization
                (bash 4.x does not support bracketed paste and will inject the
                escape sequences literally into the command line).
        """
        # Defence-in-depth: re-validate at the sink even though callers
        # validate at the API/MCP boundary. Both halves flow into a
        # tmux subprocess argument (-t target), and tmux itself parses
        # ':' / '.' as target delimiters, so any leak past upstream
        # validation could pivot to a different pane. Validating here
        # also clears the CodeQL py/command-line-injection data flow.
        validated_session = validate_tmux_name(session_name, "session_name")
        validated_window = validate_tmux_name(window_name, "window_name")
        target = f"{validated_session}:{validated_window}"
        buf_name = f"cao_{uuid.uuid4().hex[:8]}"
        try:
            # Log metadata only at INFO: the payload is the full launch
            # command / message, which can include MCP env values (API
            # tokens from a profile's mcpServers.env) and entire system
            # prompts. This matches send_keys_via_paste, which logs only
            # the text length at INFO. Full content additionally remains
            # available here at DEBUG for local delivery troubleshooting.
            logger.info(f"send_keys: {target} - keys length: {len(keys)}")
            logger.debug(f"send_keys: {target} - keys: {keys}")
            if force_bracketed_paste:
                # Wrap unconditionally and use -r (no newline→CR conversion).
                # paste-buffer -p only adds bracketed sequences if tmux tracks
                # ?2004h for the pane — some TUIs (e.g. current Kiro) don't
                # send ?2004h so -p is a no-op and \n becomes CR (Enter).
                buf_content = b"\x1b[200~" + keys.encode() + b"\x1b[201~"
                paste_flag = "-r"
            else:
                buf_content = keys.encode()
                paste_flag = "-p"
            subprocess.run(
                ["tmux", "load-buffer", "-b", buf_name, "-"],
                input=buf_content,
                check=True,
            )
            subprocess.run(
                ["tmux", "paste-buffer", paste_flag, "-b", buf_name, "-t", target],
                check=True,
            )
            # Delay to let the TUI process the bracketed paste end sequence before
            # sending Enter. Without enough delay, some TUIs (e.g. the newest
            # Claude Code Ink renderer) swallow the Enter that immediately follows
            # paste-buffer, leaving the message unsubmitted. The duration is
            # provider-tunable via ``submit_delay`` (BaseProvider.paste_submit_delay).
            time.sleep(submit_delay)
            for i in range(enter_count):
                if i > 0:
                    # Delay between Enter presses for TUIs that need time to
                    # process the previous Enter (e.g., Ink adding a newline)
                    # before the next Enter triggers form submission.
                    time.sleep(0.5)
                subprocess.run(
                    ["tmux", "send-keys", "-t", target, "Enter"],
                    check=True,
                )
            logger.debug(f"Sent keys to {target}")
        except Exception as e:
            logger.error(f"Failed to send keys to {target}: {e}")
            raise
        finally:
            subprocess.run(
                ["tmux", "delete-buffer", "-b", buf_name],
                check=False,
            )

    def send_keys_via_paste(self, session_name: str, window_name: str, text: str) -> None:
        """Send text to window via tmux paste buffer with bracketed paste mode.

        Uses tmux set-buffer + paste-buffer -p to send text as a bracketed paste,
        which bypasses TUI hotkey handling. Essential for Ink-based CLIs and
        other TUI apps where individual keystrokes may trigger hotkeys.

        After pasting, sends C-m (Enter) to submit the input.

        Args:
            session_name: Name of tmux session
            window_name: Name of window in session
            text: Text to paste into the pane
        """
        try:
            logger.info(
                f"send_keys_via_paste: {session_name}:{window_name} - text length: {len(text)}"
            )

            session = self.server.sessions.get(session_name=session_name)
            if not session:
                raise ValueError(f"Session '{session_name}' not found")

            window = session.windows.get(window_name=window_name)
            if not window:
                raise ValueError(f"Window '{window_name}' not found in session '{session_name}'")

            pane = window.active_pane
            if pane:
                buf_name = "cao_paste"

                # Load text into tmux buffer
                self.server.cmd("set-buffer", "-b", buf_name, text)

                # Paste with bracketed paste mode (-p flag).
                # This wraps the text in \x1b[200~ ... \x1b[201~ escape sequences,
                # telling the TUI "this is pasted text" so it bypasses hotkey handling.
                pane.cmd("paste-buffer", "-p", "-b", buf_name)

                time.sleep(0.3)

                # Send Enter to submit the pasted text
                pane.send_keys("C-m", enter=False)

                # Clean up the paste buffer
                try:
                    self.server.cmd("delete-buffer", "-b", buf_name)
                except Exception:
                    pass

                logger.debug(f"Sent text via paste to {session_name}:{window_name}")
        except Exception as e:
            logger.error(f"Failed to send text via paste to {session_name}:{window_name}: {e}")
            raise

    def send_special_key(self, session_name: str, window_name: str, key: str) -> None:
        """Send a tmux special key sequence (e.g., C-d, C-c) to a window.

        Unlike send_keys(), this sends the key as a tmux key name (not literal text)
        and does not append a carriage return. Used for control signals like Ctrl+D (EOF).

        Args:
            session_name: Name of tmux session
            window_name: Name of window in session
            key: Tmux key name (e.g., "C-d", "C-c", "Escape")
        """
        try:
            logger.info(f"send_special_key: {session_name}:{window_name} - key: {key}")

            session = self.server.sessions.get(session_name=session_name)
            if not session:
                raise ValueError(f"Session '{session_name}' not found")

            window = session.windows.get(window_name=window_name)
            if not window:
                raise ValueError(f"Window '{window_name}' not found in session '{session_name}'")

            pane = window.active_pane
            if pane:
                pane.send_keys(key, enter=False)
                logger.debug(f"Sent special key to {session_name}:{window_name}")
        except Exception as e:
            logger.error(f"Failed to send special key to {session_name}:{window_name}: {e}")
            raise

    def get_history(
        self,
        session_name: str,
        window_name: str,
        tail_lines: Optional[int] = None,
        strip_escapes: bool = False,
        full_history: bool = False,
    ) -> str:
        """Get window history.

        Args:
            session_name: Name of tmux session
            window_name: Name of window in session
            tail_lines: Number of lines to capture from end (default: TMUX_HISTORY_LINES)
            strip_escapes: If True, capture plain text without ANSI escape sequences
            full_history: If True, capture entire scrollback buffer (overrides tail_lines)
        """
        try:
            session = self.server.sessions.get(session_name=session_name)
            if not session:
                raise ValueError(f"Session '{session_name}' not found")

            window = session.windows.get(window_name=window_name)
            if not window:
                raise ValueError(f"Window '{window_name}' not found in session '{session_name}'")

            # Use cmd to run capture-pane with -e (escape sequences) and -p (print) flags
            pane = window.panes[0]
            if full_history:
                # "-S -" captures from the start of the scrollback buffer
                flags = ["-p", "-S", "-"]
            else:
                lines = tail_lines if tail_lines is not None else TMUX_HISTORY_LINES
                flags = ["-p", "-S", f"-{lines}"]
            if not strip_escapes:
                flags = ["-e"] + flags
            result = pane.cmd("capture-pane", *flags)
            # Join all lines with newlines to get complete output
            return "\n".join(result.stdout) if result.stdout else ""
        except Exception as e:
            logger.error(f"Failed to get history from {session_name}:{window_name}: {e}")
            raise

    def list_sessions(self) -> List[Dict[str, str]]:
        """List all tmux sessions."""
        try:
            sessions: List[Dict[str, str]] = []
            for session in self.server.sessions:
                # Check if session has attached clients
                is_attached = len(getattr(session, "attached_sessions", [])) > 0

                session_name = session.name if session.name is not None else ""
                sessions.append(
                    {
                        "id": session_name,
                        "name": session_name,
                        "status": "active" if is_attached else "detached",
                    }
                )

            return sessions
        except Exception as e:
            logger.error(f"Failed to list sessions: {e}")
            return []

    def get_session_windows(self, session_name: str) -> List[Dict[str, str]]:
        """Get all windows in a session."""
        try:
            session = self.server.sessions.get(session_name=session_name)
            if not session:
                return []

            windows: List[Dict[str, str]] = []
            for window in session.windows:
                window_name = window.name if window.name is not None else ""
                windows.append({"name": window_name, "index": str(window.index)})

            return windows
        except Exception as e:
            logger.error(f"Failed to get windows for session {session_name}: {e}")
            return []

    def kill_session(self, session_name: str) -> bool:
        """Kill tmux session."""
        try:
            session = self.server.sessions.get(session_name=session_name)
            if session:
                session.kill()
                logger.info(f"Killed tmux session: {session_name}")
                return True
            return False
        except Exception as e:
            logger.error(f"Failed to kill session {session_name}: {e}")
            return False

    def kill_window(self, session_name: str, window_name: str) -> bool:
        """Kill a specific tmux window within a session."""
        try:
            session = self.server.sessions.get(session_name=session_name)
            if not session:
                return False
            window = session.windows.get(window_name=window_name)
            if window:
                window.kill()
                logger.info(f"Killed tmux window: {session_name}:{window_name}")
                return True
            return False
        except Exception as e:
            logger.error(f"Failed to kill window {session_name}:{window_name}: {e}")
            return False

    def window_exists(self, session_name: str, window_name: str) -> bool:
        """Check the exact tmux window without swallowing lookup failures."""
        session = self.server.sessions.get(session_name=session_name)
        if not session:
            return False
        return session.windows.get(window_name=window_name) is not None

    def window_identity(self, session_name: str, window_name: str) -> Optional[Dict[str, str]]:
        """Server-owned immutable tmux identity of a window: its tmux-assigned
        ``window_id`` (``@N``) and active ``pane_id`` (``%N``). Unlike window
        names these are immutable for the resource's life — the only tmux
        facts an attestation may bind a terminal to."""
        try:
            session = self.server.sessions.get(session_name=session_name)
            if not session:
                return None
            window = session.windows.get(window_name=window_name)
            if not window:
                return None
            pane = window.active_pane
            pane_id = getattr(pane, "pane_id", None) if pane else None
            window_id = getattr(window, "window_id", None)
            if not pane_id or not window_id:
                return None
            return {"pane_id": str(pane_id), "window_id": str(window_id)}
        except Exception as e:
            logger.error(f"Failed to resolve window identity for {session_name}:{window_name}: {e}")
            return None

    @staticmethod
    def _descendant_processes(root_pid: int) -> Optional[List[int]]:
        """Return a bounded process tree rooted at ``root_pid``.

        A failed or oversized observation is ambiguous and therefore returns
        ``None`` rather than supplying partial ownership evidence.
        """
        ps = shutil.which("ps")
        if not ps:
            return None
        try:
            result = subprocess.run(
                [os.path.realpath(ps), "-axo", "pid=,ppid="],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        children: Dict[int, List[int]] = {}
        try:
            for line in result.stdout.splitlines():
                pid_text, parent_text = line.split()
                children.setdefault(int(parent_text), []).append(int(pid_text))
        except (ValueError, TypeError):
            return None
        observed = [root_pid]
        cursor = 0
        while cursor < len(observed):
            observed.extend(children.get(observed[cursor], ()))
            cursor += 1
            if len(observed) > 64:
                return None
        return observed

    @staticmethod
    def _process_has_terminal_id(pid: int, terminal_id: str) -> bool:
        """Read one process environment without logging it."""
        proc_environ = f"/proc/{pid}/environ"
        try:
            with open(proc_environ, "rb") as fh:
                values = fh.read().split(b"\0")
        except OSError:
            ps = shutil.which("ps")
            if not ps:
                return False
            try:
                result = subprocess.run(
                    [os.path.realpath(ps), "eww", "-p", str(pid), "-o", "command="],
                    capture_output=True,
                    timeout=2,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                return False
            if result.returncode != 0:
                return False
            values = re.split(rb"[ \0]", result.stdout)
        expected = f"CAO_TERMINAL_ID={terminal_id}".encode()
        return expected in values

    def terminal_bound_window_identity(
        self, terminal_id: str, session_name: str, window_name: str
    ) -> Optional[Dict[str, str]]:
        """Resolve a legacy pane only with live process-lineage ownership proof.

        The stored window name locates a candidate; it is never the proof.  At
        least one process rooted in the candidate pane must carry the exact
        CAO terminal identity injected when that terminal was created.
        """
        try:
            session = self.server.sessions.get(session_name=session_name)
            if not session:
                return None
            window = session.windows.get(window_name=window_name)
            if not window:
                return None
            pane = window.active_pane
            pane_id = getattr(pane, "pane_id", None) if pane else None
            pane_pid = getattr(pane, "pane_pid", None) if pane else None
            window_id = getattr(window, "window_id", None)
            if not pane_id or not pane_pid or not window_id:
                return None
            processes = self._descendant_processes(int(pane_pid))
            if processes is None or not any(
                self._process_has_terminal_id(pid, terminal_id) for pid in processes
            ):
                return None
            return {"pane_id": str(pane_id), "window_id": str(window_id)}
        except (TypeError, ValueError, AttributeError) as exc:
            logger.warning(
                "Could not prove terminal-bound identity for %s: %s",
                terminal_id,
                exc,
            )
            return None

    def list_pane_control_identities(self) -> Optional[List[PaneControlIdentity]]:
        """Every pane on the server, with the facts a control call binds to.

        Enumerates with ``list-panes -a`` and selects in Python.  A ``-t``
        target is deliberately never used to resolve identity: tmux answers
        ``display-message -t <session>:<missing-window>`` with a *different*
        pane and exit status 0, so a lookup that trusted a target could
        quietly bind a control to a pane the caller never named.

        Returns None when the observation itself failed.  An unreadable
        server is ambiguous, not empty — reporting "no panes" would let a
        caller conclude a live pane had gone away.
        """
        try:
            result = subprocess.run(
                [tmux_binary(), "list-panes", "-a", "-F", _PANE_CONTROL_FORMAT],
                capture_output=True,
                text=True,
                check=False,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            logger.warning("Could not enumerate tmux panes: %s", exc)
            return None
        if result.returncode != 0:
            logger.warning(
                "tmux could not enumerate panes: %s",
                (result.stderr or "").strip(),
            )
            return None
        records = []
        for line in (result.stdout or "").splitlines():
            record = _parse_pane_control_record(line)
            if record is not None:
                records.append(record)
        return records

    def pane_control_identity(
        self,
        *,
        pane_id: Optional[str] = None,
        session_name: Optional[str] = None,
        window_name: Optional[str] = None,
    ) -> Optional[PaneControlIdentity]:
        """Resolve exactly one pane's live identity, or None.

        Select either by immutable ``pane_id`` — the verification path,
        asking whether this exact pane still exists and its facts are
        unchanged — or by ``session_name``/``window_name``, the first-pin
        path used before any pane id is known.  The selectors are mutually
        exclusive.

        Names are compared in Python and never reach a tmux argument, so an
        unexpected name can only fail to match.

        Returns None when the pane is absent, when the observation failed,
        or when a name pair matches more than one pane: a window holding
        several panes has no single control target, and choosing one would
        be a guess rather than an observation.

        Raises:
            ValueError: No selector, or both selectors, were supplied.
        """
        by_pane = pane_id is not None
        by_name = session_name is not None or window_name is not None
        if by_pane == by_name:
            raise ValueError("Select a pane by pane_id or by session_name/window_name, not both")
        if by_name and (session_name is None or window_name is None):
            raise ValueError("session_name and window_name must be supplied together")

        records = self.list_pane_control_identities()
        if records is None:
            return None
        if by_pane:
            matches = [record for record in records if record.pane_id == pane_id]
        else:
            matches = [
                record
                for record in records
                if record.session_name == session_name and record.window_name == window_name
            ]
        if len(matches) > 1:
            logger.warning(
                "Refusing ambiguous pane lookup: %d panes match %r",
                len(matches),
                pane_id if by_pane else (session_name, window_name),
            )
            return None
        if not matches:
            return None
        return matches[0]

    def send_literal_line(self, pane_id: str, text: str, submit: bool = True) -> int:
        """Write ``text`` to ``pane_id`` as literal bytes, then one Enter.

        The control path's only write primitive.  It never loads or pastes
        a tmux buffer, so no bracketed-paste sentinel can be produced for
        any pane — whether or not that pane advertised ?2004h.  The leakage
        this path exists to remove is structurally impossible here rather
        than conditionally avoided.

        ``send-keys -l`` writes the argument byte for byte: no key-name
        lookup and no backslash-escape processing, so ``\\n`` in the text
        stays two characters.  The trailing ``--`` keeps text beginning
        with ``-`` as text instead of as an option.

        The target is always an immutable pane id.  tmux fails a send to a
        missing pane with a non-zero status and writes nothing, so a stale
        target cannot silently land in a different pane.

        Args:
            pane_id: Immutable tmux pane id (``%N``).
            text: Single-line literal text, free of ESC, CR and LF.
            submit: Send one explicit Enter after the text.

        Returns:
            The number of literal writes tmux accepted, not counting the
            Enter.  Returned rather than left for the caller to recompute
            from the chunk size: the caller journals this number as its
            record of what reached the pane, and a recomputation would
            silently stop matching the moment the chunking here changed.

        Raises:
            ValueError: The pane id or text violates the control contract.
                Nothing is written.
            TmuxLiteralSendError: tmux rejected a write, possibly part-way
                through.
        """
        # Defence-in-depth at the sink: the service layer rejects these
        # payloads with a typed outcome, but the primitive must not be
        # able to emit them even when called directly.
        if not is_valid_pane_id(pane_id):
            raise ValueError(f"Invalid pane_id: {pane_id!r}")
        if contains_bracketed_paste_sentinel(text):
            raise ValueError("Literal control text must not contain bracketed-paste sentinels")
        for char in _ILLEGAL_LITERAL_CHARS:
            if char in text:
                raise ValueError(f"Literal control text must not contain {char!r}")
        if not text and not submit:
            raise ValueError("Literal control write would emit nothing")

        # Metadata only at INFO: control text is caller-supplied and can
        # carry a prompt or an argument the operator considers private.
        # Full content stays available at DEBUG, matching send_keys.
        logger.info(
            "send_literal_line: %s - text length: %d, submit: %s",
            pane_id,
            len(text),
            submit,
        )
        logger.debug("send_literal_line: %s - text: %s", pane_id, text)

        chunks_sent = 0
        for start in range(0, len(text), _LITERAL_CHUNK_CHARS):
            self._run_literal_write(
                [
                    tmux_binary(),
                    "send-keys",
                    "-t",
                    pane_id,
                    "-l",
                    "--",
                    text[start : start + _LITERAL_CHUNK_CHARS],
                ],
                chunks_sent=chunks_sent,
                enter_attempted=False,
            )
            chunks_sent += 1
        if submit:
            self._run_literal_write(
                [tmux_binary(), "send-keys", "-t", pane_id, "Enter"],
                chunks_sent=chunks_sent,
                enter_attempted=True,
            )
        return chunks_sent

    @staticmethod
    def _run_literal_write(argv: List[str], *, chunks_sent: int, enter_attempted: bool) -> None:
        """Run one control write, converting any failure into a bounded one."""
        try:
            result = subprocess.run(argv, capture_output=True, text=True, check=False)
        except (OSError, RuntimeError, ValueError) as exc:
            raise TmuxLiteralSendError(
                f"tmux literal control write failed: {exc}",
                chunks_sent=chunks_sent,
                enter_attempted=enter_attempted,
            ) from exc
        if result.returncode != 0:
            detail = (result.stderr or "").strip() or (result.stdout or "").strip()
            raise TmuxLiteralSendError(
                f"tmux rejected a literal control write: {detail}",
                chunks_sent=chunks_sent,
                enter_attempted=enter_attempted,
            )

    def session_exists(self, session_name: str) -> bool:
        """Check if session exists."""
        try:
            session = self.server.sessions.get(session_name=session_name)
            return session is not None
        except Exception:
            return False

    def get_pane_working_directory(self, session_name: str, window_name: str) -> Optional[str]:
        """Get the current working directory of a pane."""
        try:
            session = self.server.sessions.get(session_name=session_name)
            if not session:
                return None

            window = session.windows.get(window_name=window_name)
            if not window:
                return None

            pane = window.active_pane
            if pane:
                # Get pane_current_path from tmux
                result = pane.cmd("display-message", "-p", "#{pane_current_path}")
                if result.stdout:
                    return result.stdout[0].strip()
            return None
        except Exception as e:
            logger.error(f"Failed to get working directory for {session_name}:{window_name}: {e}")
            return None

    def get_pane_current_command(self, session_name: str, window_name: str) -> Optional[str]:
        """Get the current foreground command running in a pane."""
        try:
            session = self.server.sessions.get(session_name=session_name)
            if not session:
                return None
            window = session.windows.get(window_name=window_name)
            if not window:
                return None
            pane = window.active_pane
            if pane:
                result = pane.cmd("display-message", "-p", "#{pane_current_command}")
                if result.stdout:
                    return result.stdout[0].strip()
            return None
        except Exception as e:
            logger.error(f"Failed to get pane command for {session_name}:{window_name}: {e}")
            return None

    def pipe_pane(self, session_name: str, window_name: str, file_path: str) -> None:
        """Start piping pane output to file.

        Args:
            session_name: Tmux session name
            window_name: Tmux window name
            file_path: Absolute path to log file
        """
        try:
            session = self.server.sessions.get(session_name=session_name)
            if not session:
                raise ValueError(f"Session '{session_name}' not found")

            window = session.windows.get(window_name=window_name)
            if not window:
                raise ValueError(f"Window '{window_name}' not found in session '{session_name}'")

            pane = window.active_pane
            if pane:
                pane.cmd("pipe-pane", "-o", f"cat >> {file_path}")
                logger.info(f"Started pipe-pane for {session_name}:{window_name} to {file_path}")
        except Exception as e:
            logger.error(f"Failed to start pipe-pane for {session_name}:{window_name}: {e}")
            raise

    def stop_pipe_pane(self, session_name: str, window_name: str) -> None:
        """Stop piping pane output.

        Args:
            session_name: Tmux session name
            window_name: Tmux window name
        """
        try:
            session = self.server.sessions.get(session_name=session_name)
            if not session:
                raise ValueError(f"Session '{session_name}' not found")

            window = session.windows.get(window_name=window_name)
            if not window:
                raise ValueError(f"Window '{window_name}' not found in session '{session_name}'")

            pane = window.active_pane
            if pane:
                pane.cmd("pipe-pane")
                logger.info(f"Stopped pipe-pane for {session_name}:{window_name}")
        except Exception as e:
            logger.error(f"Failed to stop pipe-pane for {session_name}:{window_name}: {e}")
            raise


# Module-level singleton
tmux_client = TmuxClient()
