"""Terminal backend abstract base class.

Defines the contract that all terminal backends (tmux, herdr, etc.) must satisfy.
Core services depend only on this ABC, never on a concrete backend directly.
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional

from cli_agent_orchestrator.models.terminal import TerminalStatus


class TerminalBackendError(Exception):
    """Base exception for terminal backend operations."""

    pass


class TerminalNotFoundError(TerminalBackendError):
    """Raised when a terminal/pane cannot be found or resolved."""

    def __init__(self, terminal_id: str, message: Optional[str] = None):
        self.terminal_id = terminal_id
        super().__init__(message or f"Terminal not found: {terminal_id}")


class TerminalBackend(ABC):
    """Abstract base class defining the terminal backend contract.

    All terminal operations CAO requires are declared here. Concrete backends
    (TmuxBackend, HerdrBackend) implement these methods using their respective
    multiplexer APIs.
    """

    # --- Session lifecycle ---

    @abstractmethod
    def create_session(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        working_directory: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> str:
        """Create a new terminal session with an initial window.

        Args:
            session_name: Name for the session (e.g., "cao-my-project")
            window_name: Name for the initial window/tab
            terminal_id: Unique terminal identifier to inject into the environment
            working_directory: Optional starting directory

        Returns:
            The actual window name assigned by the backend

        Raises:
            TerminalBackendError: If session creation fails
            ValueError: If working_directory is invalid
        """
        ...

    @abstractmethod
    def session_exists(self, session_name: str) -> bool:
        """Check if a session exists.

        Args:
            session_name: Session name to check

        Returns:
            True if the session exists
        """
        ...

    @abstractmethod
    def list_sessions(self) -> List[Dict[str, str]]:
        """List all sessions managed by this backend.

        Returns:
            List of dicts with keys: id, name, status
        """
        ...

    @abstractmethod
    def kill_session(self, session_name: str) -> bool:
        """Kill/destroy a session.

        Args:
            session_name: Session to kill

        Returns:
            True if session was killed, False if not found
        """
        ...

    # --- Window/tab lifecycle ---

    @abstractmethod
    def create_window(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        working_directory: Optional[str] = None,
        window_shell: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> str:
        """Create a new window/tab in an existing session.

        Args:
            session_name: Session to add the window to
            window_name: Name for the new window
            terminal_id: Unique terminal identifier to inject into the environment
            working_directory: Optional starting directory
            window_shell: Optional shell command to run instead of default shell

        Returns:
            The actual window name assigned by the backend

        Raises:
            TerminalBackendError: If window creation fails
            ValueError: If session not found or working_directory is invalid
        """
        ...

    @abstractmethod
    def kill_window(self, session_name: str, window_name: str) -> bool:
        """Kill a specific window within a session.

        Args:
            session_name: Session containing the window
            window_name: Window to kill

        Returns:
            True if window was killed, False if not found
        """
        ...

    @abstractmethod
    def window_exists(
        self,
        session_name: str,
        window_name: str,
        *,
        deadline_monotonic: Optional[float] = None,
    ) -> bool:
        """Return whether the exact window exists, raising on lookup failure.

        ``deadline_monotonic`` is optional so ordinary lifecycle callers keep
        their existing behavior.  Identity-bound control paths supply it and
        implementations must not outlive that absolute deadline.
        """
        ...

    def window_identity(
        self,
        session_name: str,
        window_name: str,
        *,
        deadline_monotonic: Optional[float] = None,
    ) -> Optional[Dict[str, str]]:
        """Server-owned immutable tmux identity (pane_id/window_id) of a
        window, or None when the backend cannot resolve one."""
        return None

    def terminal_bound_window_identity(
        self, terminal_id: str, session_name: str, window_name: str
    ) -> Optional[Dict[str, str]]:
        """Return an immutable window identity only when the backend can prove
        the live pane is still bound to ``terminal_id``."""
        return None

    @property
    def supports_pane_identity(self) -> bool:
        """Whether this backend can observe a pane's live identity and target
        a write at an immutable pane id.

        Default ``False``. Callers that enforce the identity boundary must
        test it with ``is True`` rather than for truthiness: a backend double
        that answers every attribute with a stand-in object would otherwise
        appear to declare a capability it does not have, and the boundary
        would start refusing writes on evidence nobody actually gathered.
        Declaring the capability is a positive act.
        """
        return False

    def observe_pane_identities(self) -> Optional[Dict[str, Dict[str, str]]]:
        """Every observable pane's identity, keyed by pane id, in one read.

        ``None`` means the server could not be read — distinct from an
        empty mapping, which means it was read and holds no panes.

        Exists so that a view rendering many terminals asks once rather
        than once per row. Per-row probing is not merely slower: the rows
        would be answered at different instants, so a listing could show
        one terminal as live and another as dead on the strength of two
        different moments.
        """
        return None

    def observe_pane_identity(self, pane_id: str) -> Optional[Dict[str, str]]:
        """The live facts of exactly ``pane_id``.

        Always carries ``outcome``, which is the part callers must branch
        on, because "the pane is gone" and "we could not look" are
        different facts and only one of them is evidence:

        ``observed``   the pane exists; the record also carries
                       ``pane_id``, ``window_id``, ``session_id``,
                       ``pane_pid``, ``session_name``, ``window_name`` and
                       ``dead``, plus ``server_socket_path`` when the
                       owning server can be proven (absent, never null,
                       when it cannot).
        ``absent``     the server answered and this pane is not on it. The
                       pane is provably gone.
        ``unreadable`` the server could not be read, or the pane id matched
                       more than once. Nothing was learned, so nothing may
                       be concluded — in particular this is not a licence
                       to reap the row or to fall back to a name.

        ``None`` means the backend does not implement the probe at all.
        """
        return None

    def create_window_with_argv(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        argv: List[str],
        working_directory: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> str:
        """Create a window running ``argv`` directly as the pane process (no
        shell, zero keystrokes). Backends that cannot provide atomic process
        creation MUST fail closed by raising — managed launch never degrades
        to typing a command into a shell."""
        raise TerminalBackendError(
            f"{type(self).__name__} does not support atomic process window creation"
        )

    # --- Input ---

    @abstractmethod
    def send_keys(
        self,
        session_name: str,
        window_name: str,
        keys: str,
        enter_count: int = 1,
        force_bracketed_paste: bool = False,
        submit_delay: float = 0.3,
        pane_id: Optional[str] = None,
    ) -> None:
        """Send text input to a window.

        Args:
            session_name: Target session
            window_name: Target window
            keys: Text to send
            enter_count: Number of Enter keys to send after the text
            force_bracketed_paste: If True, wrap in bracketed paste sequences
            submit_delay: Seconds to wait after pasting before sending Enter, so
                a TUI (e.g. Claude Code's Ink renderer) finishes processing the
                paste before submission. Backends without a paste step may ignore.
            pane_id: Immutable pane id the caller has already verified. When
                supplied it is the target, and the two names are diagnostic
                only. A backend that cannot address a pane id must raise
                rather than fall back to the names: the caller passed an id
                precisely because it does not accept a name-resolved target,
                and a silent downgrade would reinstate the delivery it asked
                to rule out.
        """
        ...

    @abstractmethod
    def send_special_key(
        self,
        session_name: str,
        window_name: str,
        key: str,
        pane_id: Optional[str] = None,
    ) -> None:
        """Send a special key (e.g., C-c, C-d, Enter) to a window.

        Unlike send_keys(), this sends the key as a control/special key name
        and does not append a carriage return.

        Args:
            session_name: Target session
            window_name: Target window
            key: Key name (e.g., "C-d", "C-c", "Escape", "Enter", "")
            pane_id: Verified immutable pane id. When supplied it is the
                target and the names are context. A backend whose own ids
                are not tmux pane ids must refuse it rather than fall back
                to the names — see ``send_input`` for the same rule.
        """
        ...

    # --- Output ---

    @abstractmethod
    def get_history(
        self,
        session_name: str,
        window_name: str,
        tail_lines: Optional[int] = None,
        strip_escapes: bool = False,
        full_history: bool = False,
    ) -> str:
        """Get terminal output/history from a window.

        Args:
            session_name: Target session
            window_name: Target window
            tail_lines: Number of lines from the end (None = backend default)
            strip_escapes: If True, strip ANSI escape sequences
            full_history: If True, capture entire scrollback

        Returns:
            Terminal output as a string
        """
        ...

    @abstractmethod
    def get_pane_working_directory(self, session_name: str, window_name: str) -> Optional[str]:
        """Get the current working directory of a pane.

        Args:
            session_name: Target session
            window_name: Target window

        Returns:
            Working directory path, or None if unavailable
        """
        ...

    @abstractmethod
    def get_pane_current_command(self, session_name: str, window_name: str) -> Optional[str]:
        """Get the current foreground command running in a pane.

        Args:
            session_name: Target session
            window_name: Target window

        Returns:
            Command name, or None if unavailable
        """
        ...

    # --- Attach ---

    @abstractmethod
    def attach_session(self, session_name: str) -> None:
        """Attach to a session (for interactive use).

        Args:
            session_name: Session to attach to
        """
        ...

    @abstractmethod
    def prepare_web_attach(
        self,
        session_name: str,
        window_name: str,
        *,
        pane_id: Optional[str] = None,
        server_socket_path: Optional[str] = None,
    ) -> List[str]:
        """Prepare a browser PTY attachment and return its subprocess argv.

        Backends may perform routing work before returning, such as focusing a
        Herdr workspace/tab. The caller owns the PTY and subprocess lifecycle.

        Args:
            session_name: Target session
            window_name: Target window
            pane_id: A caller-verified immutable pane identity. When
                present it is the target and the names are diagnostic
                only. A backend that cannot address one may ignore it if
                its own addressing is already identity-based (herdr
                resolves its tabs by id), but must never use it as a hint
                and then resolve by name anyway.
            server_socket_path: The server that owns ``pane_id``, so the
                attach cannot land on a different one.

        Returns:
            Subprocess argv for the interactive backend client

        Raises:
            TerminalBackendError: If the backend cannot prepare the attachment
        """
        ...

    # --- Pipe-pane (logging) ---

    @abstractmethod
    def pipe_pane(self, session_name: str, window_name: str, file_path: str) -> None:
        """Start piping pane output to a file.

        For backends that don't support pipe-pane (e.g., herdr), this is a no-op
        since inbox delivery uses a different mechanism.

        Args:
            session_name: Target session
            window_name: Target window
            file_path: Absolute path to the log file
        """
        ...

    @abstractmethod
    def stop_pipe_pane(self, session_name: str, window_name: str) -> None:
        """Stop piping pane output.

        For backends that don't support pipe-pane, this is a no-op.

        Args:
            session_name: Target session
            window_name: Target window
        """
        ...

    # --- Capability queries ---

    def supports_event_inbox(self) -> bool:
        """Whether this backend uses event-based inbox delivery (e.g., socket events).

        When True, terminals should be registered with an event-based inbox service
        instead of using pipe-pane file watching.

        Default is False (pipe-pane based delivery).
        """
        return False

    def get_pane_id(self, terminal_id: str, session_name: str = "", window_name: str = "") -> str:
        """Resolve terminal_id to backend-specific pane identifier.

        Only meaningful for backends that use event-based inbox delivery.
        Default raises NotImplementedError.

        Args:
            terminal_id: CAO terminal identifier
            session_name: Optional session name for window-based fallback lookup
            window_name: Optional window name for window-based fallback lookup

        Returns:
            Backend-specific pane identifier

        Raises:
            NotImplementedError: If backend does not support pane ID resolution
        """
        raise NotImplementedError(f"{type(self).__name__} does not support get_pane_id()")

    def get_native_status(self, session_name: str, window_name: str) -> Optional[TerminalStatus]:
        """Query native agent status if the backend has agent awareness.

        Returns None if unsupported — caller falls back to pane content parsing.
        """
        return None
