"""Subprocess-based tmux client; supports psmux on Windows.

Uses direct subprocess calls instead of libtmux so that psmux (a
tmux-compatible multiplexer for Windows) is fully supported.  libtmux relies
on ``-F`` format-flag conventions that psmux 3.3 does not honour, which caused
empty session lists and crashes.  See PR #207 for full psmux compatibility
notes.
"""

import logging
import os
import subprocess
import sys
import time
import uuid
from typing import Dict, List, Optional

import libtmux  # retained so existing mock-patched tests keep working at import

from cli_agent_orchestrator.constants import TMUX_HISTORY_LINES

logger = logging.getLogger(__name__)


def pwsh_join(parts: list[str]) -> str:
    """Join argv into a PowerShell command line.

    Each part is wrapped in a PowerShell single-quoted string literal.
    Embedded single quotes are doubled — the escape rule inside '...' in PS.
    """
    return " ".join("'" + p.replace("'", "''") + "'" for p in parts)


class TmuxClient:
    """Simplified tmux client for basic operations.

    All tmux interactions use direct subprocess calls so that the client works
    with psmux on Windows as well as standard tmux on Linux/macOS.
    """

    # Directories that should never be used as working directories.
    # Prevents user-supplied paths from pointing at sensitive system locations.
    # Includes /private/* variants for macOS (where /etc -> /private/etc, etc.).
    _BLOCKED_DIRECTORIES = frozenset(
        {
            "/",
            "/bin",
            "/sbin",
            "/usr/bin",
            "/usr/sbin",
            "/etc",
            "/var",
            "/tmp",
            "/dev",
            "/proc",
            "/sys",
            "/root",
            "/boot",
            "/lib",
            "/lib64",
            "/private/etc",
            "/private/var",
            "/private/tmp",
        }
    )

    def _resolve_and_validate_working_directory(self, working_directory: Optional[str]) -> str:
        """Resolve and validate working directory.

        Canonicalizes the path (resolves symlinks, normalizes ``..``) and
        rejects paths that point to sensitive system directories.

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

        # Expand ~ to the server's home directory so clients can use
        # portable paths like ~/q/my-project without knowing the server's
        # actual home path (e.g., /home/user vs /Users/user).
        working_directory = os.path.expanduser(working_directory)

        # Step 1: Canonicalize the path via realpath to resolve symlinks
        # and .. sequences.  os.path.realpath is recognized by CodeQL as a
        # PathNormalization (transitions taint to NormalizedUnchecked).
        real_path = os.path.realpath(os.path.abspath(working_directory))

        # Step 2: Path-containment guard (CodeQL SafeAccessCheck).
        # CodeQL's py/path-injection two-state taint model requires:
        #   1. PathNormalization (realpath above) → NormalizedUnchecked
        #   2. SafeAccessCheck (isabs guard) → sanitized
        # os.path.isabs() is used instead of startswith("/") so that Windows
        # absolute paths (e.g. "C:\...") are accepted alongside Unix paths.
        # CodeQL recognizes str.startswith() as a SafeAccessCheck; we keep
        # the Unix variant as a secondary check so CodeQL still clears the
        # taint on Unix where realpath() always produces a "/"-prefixed path.
        if not os.path.isabs(real_path):
            raise ValueError(f"Working directory must be an absolute path: {working_directory}")

        # Step 3: Block sensitive system directories.
        # Only the exact listed paths are blocked — not their subdirectories.
        # This prevents launching agents in /etc, /var, /root, etc., while
        # still allowing legitimate paths like /Volumes/workplace or even
        # /var/folders (macOS temp) that happen to be under a blocked prefix.
        if real_path in self._BLOCKED_DIRECTORIES:
            raise ValueError(
                f"Working directory not allowed: {working_directory} "
                f"(resolves to blocked system path {real_path})"
            )

        # Step 4: Verify the directory actually exists
        if not os.path.isdir(real_path):
            raise ValueError(f"Working directory does not exist: {working_directory}")

        return real_path

    # ── internal subprocess helpers ──────────────────────────────────────

    @staticmethod
    def _build_windows_env_prefix(env: Dict[str, str]) -> str:
        """Build a PowerShell env-injection prefix for Windows / psmux.

        psmux stores ``-e KEY=VAL`` pairs in the session record but does NOT
        propagate them into the spawned shell's process environment.  On Windows
        the workaround is to pass a shell command to ``new-session`` (or
        ``new-window``) that sets each variable with ``$env:KEY = 'VAL'`` before
        handing off to the interactive shell.

        Values are wrapped in PowerShell single-quoted string literals.  Any
        embedded single-quote is escaped by doubling (``'`` → ``''``), which is
        the correct and safe PowerShell single-quote escape — double-quote
        interpolation (``"$..."`` expansion) is deliberately avoided.

        Args:
            env: Mapping of environment variable names to values.

        Returns:
            A ``pwsh -NoProfile -Command "…; pwsh -NoProfile"`` string suitable
            for use as the ``[shell-command]`` argument to ``tmux new-session``
            or ``tmux new-window``.  Returns an empty string when *env* is empty
            (callers should omit the shell-command argument in that case).
        """
        if not env:
            return ""
        set_stmts = "; ".join(
            "$env:{k} = '{v}'".format(k=k, v=v.replace("'", "''"))
            for k, v in env.items()
        )
        return f"pwsh -NoProfile -Command \"{set_stmts}; pwsh -NoProfile\""

    def _run(self, *args: str, check: bool = False) -> subprocess.CompletedProcess:
        """Run a tmux subcommand, capturing stdout/stderr as text."""
        return subprocess.run(["tmux", *args], capture_output=True, text=True, check=check)

    def _session_exists_raw(self, session_name: str) -> bool:
        """Return True if a tmux session with the given name exists."""
        # psmux-workaround: omits the ``=`` exact-match prefix that libtmux
        # uses (``-t =NAME``) because psmux does not support it.
        result = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            capture_output=True,
        )
        return result.returncode == 0

    # ── public API ───────────────────────────────────────────────────────

    def create_session(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        working_directory: Optional[str] = None,
    ) -> str:
        """Create detached tmux session with initial window and return window name."""
        try:
            working_directory = self._resolve_and_validate_working_directory(working_directory)

            # Filter out provider env vars that would cause "nested session"
            # errors when CAO itself runs inside a provider (e.g. Claude Code).
            # Preserve CLAUDE_CODE_USE_* and CLAUDE_CODE_SKIP_* vars needed
            # for provider authentication (Bedrock, Vertex AI, Foundry).
            blocked_prefixes = ("CLAUDE", "CODEX_")
            allowed_vars = {
                "CLAUDE_CODE_USE_BEDROCK",
                "CLAUDE_CODE_USE_VERTEX",
                "CLAUDE_CODE_USE_FOUNDRY",
                "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
                "CLAUDE_CODE_SKIP_VERTEX_AUTH",
                "CLAUDE_CODE_SKIP_FOUNDRY_AUTH",
            }
            environment = {
                k: v
                for k, v in os.environ.items()
                if k in allowed_vars or not any(k.startswith(p) for p in blocked_prefixes)
            }
            environment["CAO_TERMINAL_ID"] = terminal_id

            # Build the new-session command.  Environment variables are passed
            # via repeated ``-e KEY=VALUE`` args (one pair per flag invocation).
            # psmux-workaround: ``-e`` sets the session record but does NOT
            # propagate into the spawned shell process; inject via a pwsh prefix.
            cmd = [
                "tmux", "new-session",
                "-s", session_name,
                "-n", window_name,
                "-c", working_directory,
                "-d",
            ]
            for k, v in environment.items():
                cmd += ["-e", f"{k}={v}"]

            if sys.platform == "win32":
                win_shell = self._build_windows_env_prefix(environment)
                if win_shell:
                    cmd.append(win_shell)

            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(
                    f"tmux new-session failed (exit {result.returncode}): {result.stderr.strip()}"
                )

            logger.info(
                f"Created tmux session: {session_name} with window: {window_name}"
                f" in directory: {working_directory}"
            )

            # psmux-workaround: -F and format string must be separate argv entries;
            # concatenated form (``-F#{field}``) is silently ignored by psmux.
            lw = self._run("list-windows", "-t", session_name, "-F", "#{window_name}")
            first_window = lw.stdout.splitlines()[0].strip() if lw.stdout.strip() else None
            if first_window is None:
                raise ValueError(f"Window name is None for session {session_name}")
            return first_window
        except Exception as e:
            logger.error(f"Failed to create session {session_name}: {e}")
            raise

    def create_window(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        working_directory: Optional[str] = None,
    ) -> str:
        """Create window in session and return window name."""
        try:
            working_directory = self._resolve_and_validate_working_directory(working_directory)

            if not self._session_exists_raw(session_name):
                raise ValueError(f"Session '{session_name}' not found")

            window_env = {"CAO_TERMINAL_ID": terminal_id}
            cmd = [
                "tmux", "new-window",
                "-t", session_name,
                "-n", window_name,
                "-c", working_directory,
                "-e", f"CAO_TERMINAL_ID={terminal_id}",
            ]
            if sys.platform == "win32":
                win_shell = self._build_windows_env_prefix(window_env)
                if win_shell:
                    cmd.append(win_shell)

            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(
                    f"tmux new-window failed (exit {result.returncode}): {result.stderr.strip()}"
                )

            # Retrieve the actual window name
            # psmux-workaround: -F and format string must be separate argv entries.
            lw = self._run("list-windows", "-t", session_name, "-F", "#{window_name}")
            # The newly created window is the last one in the list
            names = [n.strip() for n in lw.stdout.splitlines() if n.strip()]
            actual_name = next((n for n in reversed(names) if n == window_name), None)
            if actual_name is None:
                # Fall back to last window
                actual_name = names[-1] if names else None
            if actual_name is None:
                raise ValueError(f"Window name is None for session {session_name}")

            logger.info(
                f"Created window '{actual_name}' in session '{session_name}'"
                f" in directory: {working_directory}"
            )
            return actual_name
        except Exception as e:
            logger.error(f"Failed to create window in session {session_name}: {e}")
            raise

    def send_keys(
        self, session_name: str, window_name: str, keys: str, enter_count: int = 1
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
        """
        target = f"{session_name}:{window_name}"
        buf_name = f"cao_{uuid.uuid4().hex[:8]}"
        try:
            logger.info(f"send_keys: {target} - keys: {keys}")
            subprocess.run(
                ["tmux", "load-buffer", "-b", buf_name, "-"],
                input=keys.encode(),
                check=True,
            )
            subprocess.run(
                ["tmux", "paste-buffer", "-p", "-b", buf_name, "-t", target],
                check=True,
            )
            # Brief delay to let the TUI process the bracketed paste end sequence
            # before sending Enter. Without this, some TUIs (e.g., Claude Code 2.x)
            # swallow the Enter that immediately follows paste-buffer -p.
            time.sleep(0.3)
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

            if not self._session_exists_raw(session_name):
                raise ValueError(f"Session '{session_name}' not found")

            # psmux-workaround: -F and format string must be separate argv entries.
            lw = self._run("list-windows", "-t", session_name, "-F", "#{window_name}")
            window_names = [n.strip() for n in lw.stdout.splitlines() if n.strip()]
            if window_name not in window_names:
                raise ValueError(f"Window '{window_name}' not found in session '{session_name}'")

            target = f"{session_name}:{window_name}"
            # psmux-workaround: use a fixed buffer name; psmux has a numeric buffer
            # stack and ignores the ``-b NAME`` flag, so named buffers are not reliable.
            buf_name = "cao_paste"

            # Load text into tmux buffer
            subprocess.run(
                ["tmux", "set-buffer", "-b", buf_name, text],
                check=True,
            )

            # Paste with bracketed paste mode (-p flag).
            # This wraps the text in \x1b[200~ ... \x1b[201~ escape sequences,
            # telling the TUI "this is pasted text" so it bypasses hotkey handling.
            subprocess.run(
                ["tmux", "paste-buffer", "-p", "-b", buf_name, "-t", target],
                check=True,
            )

            time.sleep(0.3)

            # Send Enter to submit the pasted text
            subprocess.run(
                ["tmux", "send-keys", "-t", target, "C-m"],
                check=True,
            )

            # Clean up the paste buffer
            subprocess.run(["tmux", "delete-buffer", "-b", buf_name], check=False)

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

            if not self._session_exists_raw(session_name):
                raise ValueError(f"Session '{session_name}' not found")

            # psmux-workaround: -F and format string must be separate argv entries.
            lw = self._run("list-windows", "-t", session_name, "-F", "#{window_name}")
            window_names = [n.strip() for n in lw.stdout.splitlines() if n.strip()]
            if window_name not in window_names:
                raise ValueError(f"Window '{window_name}' not found in session '{session_name}'")

            target = f"{session_name}:{window_name}"
            subprocess.run(
                ["tmux", "send-keys", "-t", target, key],
                check=True,
            )
            logger.debug(f"Sent special key to {session_name}:{window_name}")
        except Exception as e:
            logger.error(f"Failed to send special key to {session_name}:{window_name}: {e}")
            raise

    def get_history(
        self, session_name: str, window_name: str, tail_lines: Optional[int] = None
    ) -> str:
        """Get window history.

        Args:
            session_name: Name of tmux session
            window_name: Name of window in session
            tail_lines: Number of lines to capture from end (default: TMUX_HISTORY_LINES)
        """
        try:
            if not self._session_exists_raw(session_name):
                raise ValueError(f"Session '{session_name}' not found")

            # psmux-workaround: -F and format string must be separate argv entries.
            lw = self._run("list-windows", "-t", session_name, "-F", "#{window_name}")
            window_names = [n.strip() for n in lw.stdout.splitlines() if n.strip()]
            if window_name not in window_names:
                raise ValueError(f"Window '{window_name}' not found in session '{session_name}'")

            lines = tail_lines if tail_lines is not None else TMUX_HISTORY_LINES
            target = f"{session_name}:{window_name}"
            result = self._run("capture-pane", "-t", target, "-e", "-p", "-S", f"-{lines}")
            return result.stdout if result.stdout else ""
        except Exception as e:
            logger.error(f"Failed to get history from {session_name}:{window_name}: {e}")
            raise

    def list_sessions(self) -> List[Dict[str, str]]:
        """List all tmux sessions."""
        try:
            # psmux-workaround: ``list-sessions -F FORMAT`` is ignored; session names
            # are parsed from the default human-readable output instead.
            result = self._run("list-sessions")
            if result.returncode != 0:
                return []

            sessions: List[Dict[str, str]] = []
            for line in result.stdout.splitlines():
                colon_idx = line.find(": ")
                if colon_idx <= 0:
                    continue
                name = line[:colon_idx]
                is_attached = "(attached)" in line
                sessions.append(
                    {
                        "id": name,
                        "name": name,
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
            if not self._session_exists_raw(session_name):
                return []

            # psmux-workaround: -F and format string must be separate argv entries.
            result = self._run(
                "list-windows", "-t", session_name, "-F", "#{window_name}|#{window_index}"
            )
            if result.returncode != 0:
                return []

            windows: List[Dict[str, str]] = []
            for line in result.stdout.splitlines():
                line = line.strip()
                if "|" in line:
                    name, index = line.split("|", 1)
                    windows.append({"name": name, "index": index})
            return windows
        except Exception as e:
            logger.error(f"Failed to get windows for session {session_name}: {e}")
            return []

    def kill_session(self, session_name: str) -> bool:
        """Kill tmux session."""
        try:
            if not self._session_exists_raw(session_name):
                return False
            result = self._run("kill-session", "-t", session_name)
            if result.returncode == 0:
                logger.info(f"Killed tmux session: {session_name}")
                return True
            return False
        except Exception as e:
            logger.error(f"Failed to kill session {session_name}: {e}")
            return False

    def kill_window(self, session_name: str, window_name: str) -> bool:
        """Kill a specific tmux window within a session."""
        try:
            if not self._session_exists_raw(session_name):
                return False

            # psmux-workaround: -F and format string must be separate argv entries.
            lw = self._run("list-windows", "-t", session_name, "-F", "#{window_name}")
            window_names = [n.strip() for n in lw.stdout.splitlines() if n.strip()]
            if window_name not in window_names:
                return False

            target = f"{session_name}:{window_name}"
            result = self._run("kill-window", "-t", target)
            if result.returncode == 0:
                logger.info(f"Killed tmux window: {session_name}:{window_name}")
                return True
            return False
        except Exception as e:
            logger.error(f"Failed to kill window {session_name}:{window_name}: {e}")
            return False

    def session_exists(self, session_name: str) -> bool:
        """Check if session exists."""
        try:
            return self._session_exists_raw(session_name)
        except Exception:
            return False

    def get_pane_working_directory(self, session_name: str, window_name: str) -> Optional[str]:
        """Get the current working directory of a pane."""
        try:
            if not self._session_exists_raw(session_name):
                return None

            # psmux-workaround: -F and format string must be separate argv entries.
            lw = self._run("list-windows", "-t", session_name, "-F", "#{window_name}")
            window_names = [n.strip() for n in lw.stdout.splitlines() if n.strip()]
            if window_name not in window_names:
                return None

            target = f"{session_name}:{window_name}"
            result = self._run("display-message", "-p", "-t", target, "#{pane_current_path}")
            if result.stdout:
                return result.stdout.strip()
            return None
        except Exception as e:
            logger.error(f"Failed to get working directory for {session_name}:{window_name}: {e}")
            return None

    def pipe_pane(self, session_name: str, window_name: str, file_path: str) -> None:
        """Start piping pane output to file.

        Args:
            session_name: Tmux session name
            window_name: Tmux window name
            file_path: Absolute path to log file
        """
        try:
            if not self._session_exists_raw(session_name):
                raise ValueError(f"Session '{session_name}' not found")

            # psmux-workaround: -F and format string must be separate argv entries.
            lw = self._run("list-windows", "-t", session_name, "-F", "#{window_name}")
            window_names = [n.strip() for n in lw.stdout.splitlines() if n.strip()]
            if window_name not in window_names:
                raise ValueError(f"Window '{window_name}' not found in session '{session_name}'")

            target = f"{session_name}:{window_name}"
            result = self._run("pipe-pane", "-t", target, "-o", f"cat >> {file_path}")
            if result.returncode != 0:
                raise RuntimeError(
                    f"tmux pipe-pane failed (exit {result.returncode}): {result.stderr.strip()}"
                )
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
            if not self._session_exists_raw(session_name):
                raise ValueError(f"Session '{session_name}' not found")

            # psmux-workaround: -F and format string must be separate argv entries.
            lw = self._run("list-windows", "-t", session_name, "-F", "#{window_name}")
            window_names = [n.strip() for n in lw.stdout.splitlines() if n.strip()]
            if window_name not in window_names:
                raise ValueError(f"Window '{window_name}' not found in session '{session_name}'")

            target = f"{session_name}:{window_name}"
            self._run("pipe-pane", "-t", target, check=True)
            logger.info(f"Stopped pipe-pane for {session_name}:{window_name}")
        except Exception as e:
            logger.error(f"Failed to stop pipe-pane for {session_name}:{window_name}: {e}")
            raise


# Module-level singleton
tmux_client = TmuxClient()
