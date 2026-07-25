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

    def window_exists(self, session_name: str, window_name: str) -> bool:
        return self._client.window_exists(session_name, window_name)

    def window_identity(self, session_name: str, window_name: str) -> Optional[Dict[str, str]]:
        return self._client.window_identity(session_name, window_name)

    def terminal_bound_window_identity(
        self, terminal_id: str, session_name: str, window_name: str
    ) -> Optional[Dict[str, str]]:
        return self._client.terminal_bound_window_identity(terminal_id, session_name, window_name)

    @property
    def supports_pane_identity(self) -> bool:
        return True

    def observe_pane_identity(self, pane_id: str) -> Optional[Dict[str, str]]:
        # Enumerated, then matched here, so that "the server answered and
        # this pane is not on it" stays distinguishable from "the server
        # did not answer". A single ``-t`` lookup collapses the two, and
        # collapsing them would let an unreadable server reap live rows.
        records = self._client.list_pane_control_identities()
        if records is None:
            return {"outcome": "unreadable"}
        matches = [record for record in records if record.pane_id == pane_id]
        if len(matches) > 1:
            # One id matching several panes is not an observation of
            # either of them.
            return {"outcome": "unreadable"}
        if not matches:
            return {"outcome": "absent"}
        observed = matches[0]
        identity = {
            "outcome": "observed",
            "pane_id": observed.pane_id,
            "window_id": observed.window_id,
            "session_id": observed.session_id,
            "pane_pid": str(observed.pane_pid),
            "session_name": observed.session_name,
            "window_name": observed.window_name,
            "dead": "1" if observed.dead else "0",
        }
        # Absent rather than null when the owning server could not be
        # proven, so a comparison against a recorded socket has nothing to
        # pass against instead of comparing None to None and agreeing.
        if observed.server_socket_path is not None:
            identity["server_socket_path"] = observed.server_socket_path
        return identity

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

    def send_special_key(self, session_name: str, window_name: str, key: str) -> None:
        self._client.send_special_key(session_name, window_name, key)

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

    def prepare_web_attach(self, session_name: str, window_name: str) -> List[str]:
        """Return the tmux command used by the browser PTY WebSocket."""
        return ["tmux", "-u", "attach-session", "-t", f"{session_name}:{window_name}"]

    # --- Pipe-pane ---

    def pipe_pane(self, session_name: str, window_name: str, file_path: str) -> None:
        self._client.pipe_pane(session_name, window_name, file_path)

    def stop_pipe_pane(self, session_name: str, window_name: str) -> None:
        self._client.stop_pipe_pane(session_name, window_name)
