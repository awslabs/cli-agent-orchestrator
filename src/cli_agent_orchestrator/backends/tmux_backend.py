"""TmuxBackend — concrete TerminalBackend implementation wrapping TmuxClient.

This backend delegates all operations to the existing TmuxClient, preserving
identical behavior for all callers. It serves as the default backend when
no alternative is configured.
"""

import logging
from typing import Dict, List, Optional

from cli_agent_orchestrator.backends.base import TerminalBackend, TerminalBackendError
from cli_agent_orchestrator.clients.tmux import TmuxClient

logger = logging.getLogger(__name__)


class TmuxBackend(TerminalBackend):
    """TerminalBackend implementation backed by tmux via TmuxClient."""

    def __init__(self, client: Optional[TmuxClient] = None) -> None:
        """Initialize with an optional TmuxClient (defaults to module singleton)."""
        if client is None:
            from cli_agent_orchestrator.clients.tmux import tmux_client

            client = tmux_client
        self._client = client

    # --- Session lifecycle ---

    def create_session(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        working_directory: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> str:
        try:
            return self._client.create_session(
                session_name, window_name, terminal_id, working_directory, extra_env=extra_env
            )
        except Exception as e:
            raise TerminalBackendError(f"Failed to create session '{session_name}': {e}") from e

    def session_exists(self, session_name: str) -> bool:
        return self._client.session_exists(session_name)

    def list_sessions(self) -> List[Dict[str, str]]:
        return self._client.list_sessions()

    def kill_session(self, session_name: str) -> bool:
        return self._client.kill_session(session_name)

    # --- Window/tab lifecycle ---

    def create_window(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        working_directory: Optional[str] = None,
        window_shell: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> str:
        try:
            return self._client.create_window(
                session_name,
                window_name,
                terminal_id,
                working_directory,
                window_shell,
                extra_env=extra_env,
            )
        except Exception as e:
            raise TerminalBackendError(
                f"Failed to create window '{window_name}' in session '{session_name}': {e}"
            ) from e

    def kill_window(self, session_name: str, window_name: str) -> bool:
        return self._client.kill_window(session_name, window_name)

    def window_exists(
        self,
        session_name: str,
        window_name: str,
        *,
        deadline_monotonic: Optional[float] = None,
    ) -> bool:
        return self._client.window_exists(
            session_name,
            window_name,
            deadline_monotonic=deadline_monotonic,
        )

    def window_identity(
        self,
        session_name: str,
        window_name: str,
        *,
        deadline_monotonic: Optional[float] = None,
    ) -> Optional[Dict[str, str]]:
        return self._client.window_identity(
            session_name,
            window_name,
            deadline_monotonic=deadline_monotonic,
        )

    def terminal_bound_window_identity(
        self, terminal_id: str, session_name: str, window_name: str
    ) -> Optional[Dict[str, str]]:
        return self._client.terminal_bound_window_identity(terminal_id, session_name, window_name)

    @property
    def supports_pane_identity(self) -> bool:
        return True

    def observe_pane_identities(self) -> Optional[Dict[str, Dict[str, str]]]:
        records = self._client.list_pane_control_identities()
        if records is None:
            return None
        identities: Dict[str, Dict[str, str]] = {}
        ambiguous = set()
        for record in records:
            if record.pane_id in identities:
                # One id answering for two panes is not an observation of
                # either. tmux does not do this within a server, but a
                # reading that assumed so and was wrong would silently pick
                # a pane rather than decline to.
                ambiguous.add(record.pane_id)
            identity = {
                "outcome": "observed",
                "pane_id": record.pane_id,
                "window_id": record.window_id,
                "session_id": record.session_id,
                "pane_pid": str(record.pane_pid),
                "session_name": record.session_name,
                "window_name": record.window_name,
                "dead": "1" if record.dead else "0",
            }
            # Absent rather than null when the owning server could not be
            # proven, so a comparison against a recorded socket has nothing
            # to pass against instead of comparing None to None and
            # agreeing.
            if record.server_socket_path is not None:
                identity["server_socket_path"] = record.server_socket_path
            identities[record.pane_id] = identity
        for pane_id in ambiguous:
            identities.pop(pane_id, None)
        return identities

    def observe_pane_identity(self, pane_id: str) -> Optional[Dict[str, str]]:
        # Enumerated, then matched here, so that "the server answered and
        # this pane is not on it" stays distinguishable from "the server
        # did not answer". A single ``-t`` lookup collapses the two, and
        # collapsing them would let an unreadable server reap live rows.
        identities = self.observe_pane_identities()
        if identities is None:
            return {"outcome": "unreadable"}
        return identities.get(pane_id) or {"outcome": "absent"}

    def create_window_with_argv(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        argv: List[str],
        working_directory: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> str:
        try:
            return self._client.create_window_with_argv(
                session_name,
                window_name,
                terminal_id,
                argv,
                working_directory,
                extra_env=extra_env,
            )
        except Exception as e:
            raise TerminalBackendError(
                f"Failed to create managed process window '{window_name}' "
                f"in session '{session_name}': {e}"
            ) from e

    # --- Input ---

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
        self._client.send_keys(
            session_name,
            window_name,
            keys,
            enter_count=enter_count,
            force_bracketed_paste=force_bracketed_paste,
            submit_delay=submit_delay,
            pane_id=pane_id,
        )

    def send_special_key(
        self,
        session_name: str,
        window_name: str,
        key: str,
        pane_id: Optional[str] = None,
    ) -> None:
        self._client.send_special_key(session_name, window_name, key, pane_id=pane_id)

    # --- Output ---

    def get_history(
        self,
        session_name: str,
        window_name: str,
        tail_lines: Optional[int] = None,
        strip_escapes: bool = False,
        full_history: bool = False,
    ) -> str:
        return self._client.get_history(
            session_name,
            window_name,
            tail_lines=tail_lines,
            strip_escapes=strip_escapes,
            full_history=full_history,
        )

    def get_pane_working_directory(self, session_name: str, window_name: str) -> Optional[str]:
        return self._client.get_pane_working_directory(session_name, window_name)

    def get_pane_current_command(self, session_name: str, window_name: str) -> Optional[str]:
        return self._client.get_pane_current_command(session_name, window_name)

    # --- Attach ---

    def attach_session(self, session_name: str) -> None:
        """Attach to tmux session via subprocess (replaces current process)."""
        import subprocess

        subprocess.run(["tmux", "attach-session", "-t", session_name], check=True)

    def prepare_web_attach(
        self,
        session_name: str,
        window_name: str,
        *,
        pane_id: Optional[str] = None,
        server_socket_path: Optional[str] = None,
    ) -> List[str]:
        """Return the tmux command used by the browser PTY WebSocket.

        Given a registered ``pane_id``, the target is that pane and the two
        names are not used at all. This is the difference between opening
        "the worker's terminal" and opening "whatever currently answers to
        that window name" — the latter is what produced ``can't find
        window:`` for deleted windows and, worse, a live attach to an
        unrelated window that had reused the name.

        Verified against the installed tmux (3.7b): ``attach-session -t``
        with a pane id selects that pane's window, as does a window id. A
        bare *session* id does not — it lands on whichever window the
        session last had current — so the pane id is the target that
        actually means what the caller intends.

        ``server_socket_path`` is required alongside ``pane_id`` and is
        not a refinement of it. A pane id is unique only within one tmux
        server, several routinely run on one host, and a restarted server
        hands out ``%0``/``%1`` again — so an unpinned id resolves against
        whichever server this process happens to reach and can name a
        different live pane. A pane id with no server is therefore refused
        outright: not downgraded to the name form, and not attached to the
        default server. Zero bytes either way.
        """
        if pane_id is not None:
            if not server_socket_path:
                raise TerminalBackendError(
                    f"refusing to attach pane {pane_id} with no recorded tmux server: "
                    "a pane id is unique only within one server, so an unpinned id "
                    "can name a different live pane"
                )
            return ["tmux", "-S", server_socket_path, "-u", "attach-session", "-t", pane_id]
        # No recorded identity at all: a row predating identity
        # persistence. It keeps the name-resolved form, which is the
        # documented edge of this change — such rows are not managed and
        # are not registered. A row holding *some* identity never reaches
        # here; the caller refuses it as unbound.
        return ["tmux", "-u", "attach-session", "-t", f"{session_name}:{window_name}"]

    # --- Pipe-pane ---

    def pipe_pane(self, session_name: str, window_name: str, file_path: str) -> None:
        self._client.pipe_pane(session_name, window_name, file_path)

    def stop_pipe_pane(self, session_name: str, window_name: str) -> None:
        self._client.stop_pipe_pane(session_name, window_name)
