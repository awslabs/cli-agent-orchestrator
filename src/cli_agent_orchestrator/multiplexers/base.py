"""Backend-neutral pane/session control surface for CAO."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence


@dataclass(frozen=True)
class LaunchSpec:
    """Concrete process spawn request for a new pane/window.

    argv:
        Exact argv to execute as the pane's initial process. When None, start
        the backend's default interactive shell.
    env:
        Extra environment variables to inject into the spawned process.
    provider:
        Optional provider key used by backend-specific launch templating and
        executable resolution.
    """

    argv: Optional[Sequence[str]] = None
    env: Optional[Mapping[str, str]] = None
    provider: Optional[str] = None


class BaseMultiplexer(ABC):
    """Backend-neutral pane/session control surface for CAO."""

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

    def _resolve_and_validate_working_directory(
        self, working_directory: Optional[str]
    ) -> str:
        """Canonicalize, validate, and return a safe working directory.

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
        #   2. SafeAccessCheck (startswith guard) → sanitized
        # CodeQL recognizes str.startswith() as a SafeAccessCheck; when
        # the true branch flows to filesystem ops, the path is cleared.
        # The "/" prefix is always true after realpath(), but this
        # explicit guard satisfies CodeQL and rejects relative paths.
        if not real_path.startswith("/"):
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

    @abstractmethod
    def create_session(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        working_directory: Optional[str] = None,
        launch_spec: Optional[LaunchSpec] = None,
    ) -> str:
        """Create a detached CAO session/workspace and return the actual window name."""

    @abstractmethod
    def create_window(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        working_directory: Optional[str] = None,
        launch_spec: Optional[LaunchSpec] = None,
    ) -> str:
        """Create another CAO window/pane inside an existing session."""

    def send_keys(
        self, session_name: str, window_name: str, keys: str, enter_count: int = 1
    ) -> None:
        """Paste text, wait for the TUI to settle, then submit Enter separately."""
        self._paste_text(session_name, window_name, keys)
        self._submit_input(session_name, window_name, enter_count=enter_count)

    @abstractmethod
    def _paste_text(self, session_name: str, window_name: str, text: str) -> None:
        """Inject literal text without submitting it."""

    @abstractmethod
    def _submit_input(
        self, session_name: str, window_name: str, enter_count: int = 1
    ) -> None:
        """Submit already-pasted input with one or more Enter presses."""

    @abstractmethod
    def send_special_key(
        self,
        session_name: str,
        window_name: str,
        key: str,
        *,
        literal: bool = False,
    ) -> None:
        """Send a control key or a literal VT sequence without paste semantics."""

    @abstractmethod
    def get_history(
        self, session_name: str, window_name: str, tail_lines: Optional[int] = None
    ) -> str:
        """Return normalized pane text for provider regex/status parsing."""

    @abstractmethod
    def list_sessions(self) -> list[dict[str, str]]:
        """List CAO-visible sessions as {id, name, status}."""

    @abstractmethod
    def kill_session(self, session_name: str) -> bool:
        """Terminate a session and all owned panes/windows."""

    @abstractmethod
    def kill_window(self, session_name: str, window_name: str) -> bool:
        """Terminate one CAO window/pane."""

    @abstractmethod
    def session_exists(self, session_name: str) -> bool:
        """Return True when the named session/workspace exists."""

    @abstractmethod
    def get_pane_working_directory(
        self, session_name: str, window_name: str
    ) -> Optional[str]:
        """Return the active pane's working directory when the backend exposes it."""

    @abstractmethod
    def pipe_pane(self, session_name: str, window_name: str, file_path: str) -> None:
        """Start backend-specific output capture into a CAO log file."""

    @abstractmethod
    def stop_pipe_pane(self, session_name: str, window_name: str) -> None:
        """Stop backend-specific output capture for a CAO log file."""
