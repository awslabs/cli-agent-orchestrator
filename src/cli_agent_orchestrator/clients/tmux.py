"""Simplified tmux client as module singleton."""

import logging
import os
import subprocess
import time
import uuid
from typing import Dict, List, Optional

import libtmux

from cli_agent_orchestrator.constants import TMUX_HISTORY_LINES
from cli_agent_orchestrator.utils.path_validation import (
    BLOCKED_SYSTEM_DIRECTORIES,
    resolve_and_validate_path,
)
from cli_agent_orchestrator.utils.terminal import validate_tmux_name

logger = logging.getLogger(__name__)


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
            logger.info(f"send_keys: {target} - keys: {keys}")
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
