"""TmuxBackend — concrete TerminalBackend implementation wrapping TmuxClient.

This backend delegates all operations to the existing TmuxClient, preserving
identical behavior for all callers. It serves as the default backend when
no alternative is configured.
"""

import logging
from typing import Dict, List, Optional

from cli_agent_orchestrator.backends.base import TerminalBackend, TerminalBackendError
from cli_agent_orchestrator.clients.tmux import (
    DEFAULT_PANE_LAYOUT,
    PaneSpawnUnavailable,
    TmuxClient,
)

logger = logging.getLogger(__name__)

# The window pane-mode terminals share. Named rather than "the current window"
# so a spawn triggered from anywhere lands in one place; the first pane-mode
# terminal in a session creates it.
DEFAULT_PANE_WINDOW = "cao-agents"

# Anything else is a typo, and a typo must not silently turn the feature off.
SPAWN_MODES = frozenset({"window", "pane"})


class TmuxBackend(TerminalBackend):
    """TerminalBackend implementation backed by tmux via TmuxClient."""

    def __init__(
        self,
        client: Optional[TmuxClient] = None,
        spawn_mode: str = "window",
        pane_window: str = DEFAULT_PANE_WINDOW,
        pane_layout: str = DEFAULT_PANE_LAYOUT,
    ) -> None:
        """Initialize with an optional TmuxClient (defaults to module singleton).

        ``spawn_mode="pane"`` puts every terminal after the first into
        ``pane_window`` as a pane, so a supervisor watches its whole fleet in
        one view instead of one window per agent. ``pane_layout`` is how that
        window is re-arranged after each spawn.
        """
        if client is None:
            if spawn_mode == "pane":
                # Its own client, not the module singleton: pane mode changes how
                # a terminal is addressed, and nothing else sharing that singleton
                # asked for it.
                client = TmuxClient(pane_mode=True)
            else:
                from cli_agent_orchestrator.clients.tmux import tmux_client

                client = tmux_client
        self._client = client
        self._spawn_mode = spawn_mode
        self._pane_window = pane_window
        self._pane_layout = pane_layout

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

    def session_exists_strict(self, session_name: str) -> bool:
        return self._client.session_exists_strict(session_name)

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
            if self._spawn_mode == "pane":
                try:
                    return self._client.create_pane(
                        session_name,
                        self._pane_window,
                        window_name,
                        terminal_id,
                        working_directory,
                        window_shell,
                        extra_env=extra_env,
                        pane_layout=self._pane_layout,
                    )
                except PaneSpawnUnavailable as e:
                    # Only this case falls back: tmux has no room for another
                    # pane in the host window. A duplicate name or a refused
                    # directory must still fail.
                    logger.warning(f"Pane spawn for '{window_name}' fell back to a window: {e}")
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

    # --- Input ---

    def send_keys(
        self,
        session_name: str,
        window_name: str,
        keys: str,
        enter_count: int = 1,
        force_bracketed_paste: bool = False,
        submit_delay: float = 0.3,
        use_paste_buffer: bool = True,
    ) -> None:
        kwargs = {
            "enter_count": enter_count,
            "force_bracketed_paste": force_bracketed_paste,
            "submit_delay": submit_delay,
        }
        # Only forward when opting out of paste-buffer; the client default is True.
        if not use_paste_buffer:
            kwargs["use_paste_buffer"] = False
        self._client.send_keys(session_name, window_name, keys, **kwargs)

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
        visible_only: bool = False,
    ) -> str:
        return self._client.get_history(
            session_name,
            window_name,
            tail_lines=tail_lines,
            strip_escapes=strip_escapes,
            full_history=full_history,
            visible_only=visible_only,
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
        if self._spawn_mode != "pane":
            return ["tmux", "-u", "attach-session", "-t", f"{session_name}:{window_name}"]
        # Only pane mode has to ask tmux where the terminal is; the window-mode
        # target is a pure function of its arguments and stays one.
        return self._client.attach_command(session_name, window_name)

    # --- Pipe-pane ---

    def pipe_pane(self, session_name: str, window_name: str, file_path: str) -> None:
        self._client.pipe_pane(session_name, window_name, file_path)

    def stop_pipe_pane(self, session_name: str, window_name: str) -> None:
        self._client.stop_pipe_pane(session_name, window_name)
