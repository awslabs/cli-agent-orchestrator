"""HerdrAppBackend: TerminalBackend on herdr-core, shown in the Herdr app.

Each CAO session is one Herdr space and each CAO terminal one tab. Placement
uses the app's documented labels (herdr-app defaults/AGENTS.md: "A terminal
with those labels shows up there ... An id Herdr doesn't know becomes a new
space or tab"). Names are set with a version-checked write to the core's
``herdr-app`` layout document, the only place they live.
"""

import copy
import hashlib
import logging
import os
import secrets
import time
from typing import Any, Callable, Dict, List, Optional

from cli_agent_orchestrator.backends.base import TerminalBackend, TerminalBackendError
from cli_agent_orchestrator.clients.hc_rpc import HcProtocolError, HcRpcClient, HcRpcError
from cli_agent_orchestrator.constants import HC_READ_MAX_LINES
from cli_agent_orchestrator.models.terminal import TerminalStatus

logger = logging.getLogger(__name__)

LAYOUT_DOC = "herdr-app"
DOC_WRITE_ATTEMPTS = 5
DOC_RETRY_DELAY_S = 0.1
MANAGED = {"cao.managed": "1"}

HC_STATUS_TO_TERMINAL_STATUS: Dict[str, TerminalStatus] = {
    "working": TerminalStatus.PROCESSING,
    "blocked": TerminalStatus.WAITING_USER_ANSWER,
    "done": TerminalStatus.COMPLETED,
    "idle": TerminalStatus.IDLE,
    "error": TerminalStatus.ERROR,
}


def space_id_for(session_name: str) -> str:
    """Stable Herdr space id for a CAO session (no storage needed)."""
    return "sp" + hashlib.sha256(f"cao:{session_name}".encode("utf-8")).hexdigest()[:32]


def _terminals_in(node: Any) -> List[str]:
    """Terminal addresses in a tab's split tree."""
    if not isinstance(node, dict):
        return []
    content = node.get("content")
    found = [content["terminal"]] if isinstance(content, dict) and "terminal" in content else []
    for child in node.get("children") or []:
        found.extend(_terminals_in(child))
    return found


class HerdrAppBackend(TerminalBackend):
    """TerminalBackend for the Herdr app (herdr-core)."""

    def __init__(
        self, herdr_app_session: str = "cao", client: Optional[HcRpcClient] = None
    ) -> None:
        self._session = herdr_app_session
        self._client = client
        self._connect()

    def _connect(self) -> None:
        from cli_agent_orchestrator.backends.factory import ConfigurationError

        if self._client is None:
            self._client = HcRpcClient(self._session)
            self._client.start_server()
        try:
            self._client.hello()
        except HcProtocolError as exc:
            raise ConfigurationError(str(exc)) from exc

    @property
    def herdr_app_session(self) -> str:
        return self._session

    def _call(self, method: str, **params: Any) -> Any:
        assert self._client is not None
        return self._client.call(method, params)

    # --- Environment and creation ---

    def _env(
        self, terminal_id: str, session_name: str, extra_env: Optional[Dict[str, str]]
    ) -> Dict[str, str]:
        """Same policy as HerdrBackend._build_env_args, as a dict (values travel as JSON)."""
        from cli_agent_orchestrator.clients.tmux import TmuxClient
        from cli_agent_orchestrator.utils.mcp_resolution import cao_server_env

        env: Dict[str, str] = dict(cao_server_env())
        for key, value in (extra_env or {}).items():
            if TmuxClient._is_blocked_env_key(key):
                logger.warning("Dropping forwarded env var with blocked key: %s", key)
                continue
            if len(value.encode("utf-8")) >= TmuxClient._MAX_ENV_VALUE_BYTES:
                logger.warning("Dropping forwarded env var %s -- exceeds byte cap", key)
                continue
            env[key] = value
        env["CAO_TERMINAL_ID"] = terminal_id  # identity last, so it always wins
        env["CAO_SESSION_NAME"] = session_name
        env["HISTFILE"] = "/dev/null"
        return env

    def _run_terminal(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        working_directory: Optional[str],
        window_shell: Optional[str],
        extra_env: Optional[Dict[str, str]],
    ) -> str:
        cwd = working_directory or os.getcwd()
        if not os.path.isdir(cwd):
            raise ValueError(f"working_directory does not exist: {cwd}")
        params: Dict[str, Any] = {
            "name": f"cao-{terminal_id}",
            "no_parent": True,
            "no_notify": True,
            "cwd": cwd,
            "env": self._env(terminal_id, session_name, extra_env),
            "labels": {
                "herdr.space": space_id_for(session_name),
                "herdr.tab": "tb" + secrets.token_hex(16),
                **MANAGED,
                "cao.terminal_id": terminal_id,
                "cao.session": session_name,
                "cao.window": window_name,
            },
        }
        if window_shell:
            params["line"] = window_shell
        try:
            self._call("terminal.run", **params)
        except HcRpcError as exc:
            raise TerminalBackendError(f"could not start terminal {terminal_id}: {exc}") from exc
        self.sync_layout_names()
        return window_name

    def create_session(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        working_directory: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> str:
        return self._run_terminal(
            session_name, window_name, terminal_id, working_directory, None, extra_env
        )

    def create_window(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        working_directory: Optional[str] = None,
        window_shell: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> str:
        return self._run_terminal(
            session_name, window_name, terminal_id, working_directory, window_shell, extra_env
        )

    def restore_session_env(self, session_name: str, key: str, launch_value: str) -> None:
        """Each terminal gets its own env, so later windows never inherit a first-window value."""
        return None

    # --- Layout document ---

    def update_layout(self, mutate: Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]) -> bool:
        """Read-mutate-write the layout doc with version checks. ``mutate`` returns None for no write."""
        for attempt in range(DOC_WRITE_ATTEMPTS):
            current = self._call("doc.get", name=LAYOUT_DOC)
            value = current.get("value")
            if not isinstance(value, dict) or not isinstance(value.get("spaces"), list):
                return False  # app never wrote it, or a shape we don't know
            new_value = mutate(copy.deepcopy(value))
            if new_value is None:
                return False
            try:
                self._call("doc.set", name=LAYOUT_DOC, value=new_value, version=current["version"])
                return True
            except HcRpcError as exc:
                if exc.kind != "conflict":
                    raise
                if attempt + 1 < DOC_WRITE_ATTEMPTS:
                    time.sleep(DOC_RETRY_DELAY_S)
        logger.warning("herdr-app: could not name spaces after %d conflicts", DOC_WRITE_ATTEMPTS)
        return False

    def sync_layout_names(self) -> None:
        """Name every CAO space and tab after its CAO session and window. Never raises."""
        try:
            live = self._call("terminal.list", labels=MANAGED)["terminals"]
            spaces = {
                space_id_for(t["labels"]["cao.session"]): t["labels"]["cao.session"] for t in live
            }
            windows = {t["address"]: t["labels"]["cao.window"] for t in live}

            def mutate(doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
                changed = False
                for space in doc["spaces"]:
                    if not isinstance(space, dict) or space.get("id") not in spaces:
                        continue
                    if space.get("name") != spaces[space["id"]]:
                        space["name"] = spaces[space["id"]]
                        changed = True
                    for tab in space.get("tabs") or []:
                        owned = [windows[a] for a in _terminals_in(tab.get("root")) if a in windows]
                        if owned and tab.get("name") != owned[0]:
                            tab["name"] = owned[0]
                            changed = True
                return doc if changed else None

            self.update_layout(mutate)
        except Exception as exc:  # noqa: BLE001 - naming must never fail a caller
            logger.warning("herdr-app: could not name spaces and tabs: %s", exc)

    # --- Filled in by Task 4 ---

    def session_exists(self, session_name: str) -> bool:
        raise NotImplementedError

    def list_sessions(self) -> List[Dict[str, str]]:
        raise NotImplementedError

    def kill_session(self, session_name: str) -> bool:
        raise NotImplementedError

    def kill_window(self, session_name: str, window_name: str) -> bool:
        raise NotImplementedError

    def send_keys(
        self,
        session_name: str,
        window_name: str,
        keys: str,
        enter_count: int = 1,
        force_bracketed_paste: bool = False,
        submit_delay: float = 0.3,
    ) -> None:
        raise NotImplementedError

    def send_special_key(self, session_name: str, window_name: str, key: str) -> None:
        raise NotImplementedError

    def get_history(
        self,
        session_name: str,
        window_name: str,
        tail_lines: Optional[int] = None,
        strip_escapes: bool = False,
        full_history: bool = False,
        visible_only: bool = False,
        join_wrapped: bool = False,
    ) -> str:
        raise NotImplementedError

    @property
    def max_history_lines(self) -> int:
        return HC_READ_MAX_LINES

    def get_pane_working_directory(self, session_name: str, window_name: str) -> Optional[str]:
        raise NotImplementedError

    def get_pane_current_command(self, session_name: str, window_name: str) -> Optional[str]:
        raise NotImplementedError

    def attach_session(self, session_name: str) -> None:
        raise NotImplementedError

    def prepare_web_attach(self, session_name: str, window_name: str) -> List[str]:
        raise NotImplementedError

    def pipe_pane(self, session_name: str, window_name: str, file_path: str) -> None:
        return None

    def stop_pipe_pane(self, session_name: str, window_name: str) -> None:
        return None
