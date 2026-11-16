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
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from cli_agent_orchestrator.backends.base import (
    TerminalBackend,
    TerminalBackendError,
    TerminalCleanupOutcome,
    TerminalCleanupResult,
    TerminalNotFoundError,
)
from cli_agent_orchestrator.clients.hc_rpc import (
    CALLER_CORE_ENV,
    HcProtocolError,
    HcRpcClient,
    HcRpcError,
    hc_binary,
    hc_env,
)
from cli_agent_orchestrator.constants import HC_READ_MAX_LINES
from cli_agent_orchestrator.models.terminal import TerminalStatus

logger = logging.getLogger(__name__)

LAYOUT_DOC = "herdr-app"
DOC_WRITE_ATTEMPTS = 5
DOC_RETRY_DELAY_S = 0.1
MANAGED = {"cao.managed": "1"}
RPC_TIMEOUT_S = 10.0
# terminal.send / terminal.keys wait for a person's unfinished draft (up to
# ~60 s) and for output to settle before typing.
INPUT_TIMEOUT_S = 90.0
# hc key names for characters that are not their own name (hc 0.1.21).
_CHAR_KEY_NAMES = {" ": "space"}

# No "error": herdr-core keeps a terminal at `error` after a failed turn while
# the agent is back at its prompt (seen live with kiro-cli 3.3.0), and ERROR
# makes terminal_service refuse every later input. Unmapped, the provider's
# screen analysis decides, as on the herdr TUI backend.
HC_STATUS_TO_TERMINAL_STATUS: Dict[str, TerminalStatus] = {
    "working": TerminalStatus.PROCESSING,
    "blocked": TerminalStatus.WAITING_USER_ANSWER,
    "done": TerminalStatus.COMPLETED,
    "idle": TerminalStatus.IDLE,
}


def cao_home_label() -> str:
    """This server's identity among cao-servers sharing a core: the first 16 hex
    of the SHA-256 of its resolved CAO home directory (``CAO_HOME_DIR``). Two
    servers with different homes have separate databases, so neither may see,
    reconcile, or kill the other's terminals."""
    from cli_agent_orchestrator import constants

    home = Path(constants.CAO_HOME_DIR).resolve()
    return hashlib.sha256(str(home).encode("utf-8")).hexdigest()[:16]


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


def _is_provider_session_key(key: str) -> bool:
    """Provider-session variables (``CLAUDECODE``, ``CODEX_*``...) of the process
    that starts the core. Every terminal inherits the core's environment, and
    these make an agent CLI think it is nested in another session. Same prefixes
    and allowlist tmux keeps out of its sessions; the operator's own ``PATH`` and
    friends stay."""
    from cli_agent_orchestrator.clients.tmux import TmuxClient

    if key in TmuxClient._BLOCKED_PREFIX_ALLOWLIST:
        return False
    return key.startswith(TmuxClient._BLOCKED_ENV_PREFIXES)


class HerdrAppBackend(TerminalBackend):
    """TerminalBackend for the Herdr app (herdr-core)."""

    def __init__(
        self,
        herdr_app_session: str = "cao",
        client: Optional[HcRpcClient] = None,
        input_client: Optional[HcRpcClient] = None,
    ) -> None:
        self._session = herdr_app_session
        self._client = client
        # terminal.send / terminal.keys can wait up to ~60 s for a person's
        # draft. HcRpcClient serializes exchanges, so they get their own
        # connection and never stall status lookups or terminal.list. An
        # injected ``client`` (tests) doubles as the input client.
        self._input_client = input_client if input_client is not None else client
        self._input_lock = threading.Lock()
        self._home = cao_home_label()
        self._managed = {**MANAGED, "cao.home": self._home}
        self._connect()

    def _connect(self) -> None:
        from cli_agent_orchestrator.backends.factory import ConfigurationError

        if self._client is None:
            self._client = HcRpcClient(self._session)
            self._client.start_server(drop_env=_is_provider_session_key)
        try:
            self._client.hello()
        except HcProtocolError as exc:
            raise ConfigurationError(str(exc)) from exc

    @property
    def herdr_app_session(self) -> str:
        return self._session

    @property
    def home_label(self) -> str:
        """Value of this server's ``cao.home`` label."""
        return self._home

    def _call(self, method: str, _timeout: float = RPC_TIMEOUT_S, **params: Any) -> Any:
        assert self._client is not None
        return self._client.call(method, params, timeout=_timeout)

    def _input(self) -> HcRpcClient:
        """The input connection, opened on first use (same session as ``_client``)."""
        with self._input_lock:
            if self._input_client is None:
                self._input_client = HcRpcClient(self._session)
            return self._input_client

    def _input_call(self, method: str, **params: Any) -> Any:
        return self._input().call(method, params, timeout=INPUT_TIMEOUT_S)

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
                **self._managed,
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
            live = self._list()
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

    # --- Lookup ---

    def _list(self, **labels: str) -> List[Dict[str, Any]]:
        return list(self._call("terminal.list", labels={**self._managed, **labels})["terminals"])

    def _terminal(self, session_name: str, window_name: str) -> Dict[str, Any]:
        found = self._list(**{"cao.session": session_name, "cao.window": window_name})
        if not found:
            raise TerminalNotFoundError(
                f"{session_name}:{window_name}", "no live herdr-core terminal"
            )
        return found[0]

    def _address(self, session_name: str, window_name: str) -> str:
        return str(self._terminal(session_name, window_name)["address"])

    def kill_address(self, address: str) -> None:
        """Kill and remove one herdr-core terminal; a missing one is fine."""
        for method in ("terminal.kill", "terminal.rm"):
            try:
                self._call(method, terminal=address)
            except HcRpcError as exc:
                if exc.kind != "not_found":
                    raise

    def list_managed(self, include_finished: bool = False) -> List[Dict[str, Any]]:
        """This server's CAO terminals in this core (used by the inbox service).

        Live ones only, unless ``include_finished``: then also the records the
        core keeps for finished terminals (``terminal.list`` with ``all``).
        """
        if not include_finished:
            return self._list()
        return list(self._call("terminal.list", labels=dict(self._managed), all=True)["terminals"])

    # --- Sessions and windows ---

    def session_exists(self, session_name: str) -> bool:
        try:
            return self.session_exists_strict(session_name)
        except TerminalBackendError:
            return False

    def session_exists_strict(self, session_name: str) -> bool:
        return bool(self._list(**{"cao.session": session_name}))

    def list_sessions(self) -> List[Dict[str, str]]:
        try:
            names = sorted({t["labels"]["cao.session"] for t in self._list()})
        except TerminalBackendError:
            return []
        return [{"id": n, "name": n, "status": "active"} for n in names]

    def kill_window(self, session_name: str, window_name: str) -> bool:
        try:
            address = self._address(session_name, window_name)
        except TerminalNotFoundError:
            return False
        self.kill_address(address)
        return True

    def kill_session(self, session_name: str) -> bool:
        terminals = self._list(**{"cao.session": session_name})
        if not terminals:
            return False
        for t in terminals:
            self.kill_address(t["address"])
        if self._list(**{"cao.session": session_name}):
            return False
        sp = space_id_for(session_name)
        try:
            # Another cao-server (other CAO home) may use the same session name
            # and so the same space; leave the space to whoever is still in it.
            if self._call("terminal.list", labels={"herdr.space": sp})["terminals"]:
                return True
        except HcRpcError as exc:
            logger.warning("herdr-app: kept space for %s, lookup failed: %s", session_name, exc)
            return True

        def drop_space(doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            kept = [s for s in doc["spaces"] if not (isinstance(s, dict) and s.get("id") == sp)]
            if len(kept) == len(doc["spaces"]):
                return None
            doc["spaces"] = kept
            return doc

        try:
            self.update_layout(drop_space)
        except Exception as exc:  # noqa: BLE001 - the session is gone either way
            logger.warning("herdr-app: could not remove space for %s: %s", session_name, exc)
        return True

    def cleanup_terminal_exact(
        self,
        terminal_id: str,
        session_name: Optional[str] = None,
        window_name: Optional[str] = None,
        *,
        close: bool = True,
    ) -> TerminalCleanupResult:
        try:
            found = self._list(**{"cao.terminal_id": terminal_id})
        except TerminalBackendError as exc:
            return TerminalCleanupResult(TerminalCleanupOutcome.UNKNOWN, f"lookup failed: {exc}")
        if not found:
            return TerminalCleanupResult(TerminalCleanupOutcome.ABSENT)
        if len(found) > 1:
            return TerminalCleanupResult(
                TerminalCleanupOutcome.UNKNOWN, f"{len(found)} terminals carry {terminal_id}"
            )
        if not close:
            return TerminalCleanupResult(TerminalCleanupOutcome.STILL_PRESENT)
        try:
            self.kill_address(found[0]["address"])
            gone = not self._list(**{"cao.terminal_id": terminal_id})
        except TerminalBackendError as exc:
            return TerminalCleanupResult(
                TerminalCleanupOutcome.UNKNOWN, f"close not confirmed: {exc}"
            )
        return TerminalCleanupResult(
            TerminalCleanupOutcome.DELETED if gone else TerminalCleanupOutcome.STILL_PRESENT
        )

    # --- Input ---

    _KEY_NAMES = {
        "enter": "enter",
        "escape": "esc",
        "c-c": "ctrl+c",
        "c-d": "ctrl+d",
        "c-u": "ctrl+u",
    }

    def send_keys(
        self,
        session_name: str,
        window_name: str,
        keys: str,
        enter_count: int = 1,
        force_bracketed_paste: bool = False,
        submit_delay: float = 0.3,
    ) -> None:
        address = self._address(session_name, window_name)
        if enter_count <= 0:
            # hc has no paste-without-Enter; short menu answers go key by key.
            if len(keys) > 64 or not keys.isprintable():
                raise TerminalBackendError(
                    "herdr-app can only type up to 64 printable characters without Enter"
                )
            if keys:
                # hc rejects shifted punctuation as key names (invalid_params,
                # nothing typed), which surfaces as a TerminalBackendError.
                names = [_CHAR_KEY_NAMES.get(ch, ch) for ch in keys]
                self._input_call("terminal.keys", terminal=address, keys=names)
            return
        # force: CAO's own delivery gates decide when to type, as on tmux.
        self._input_call("terminal.send", terminal=address, text=keys, force=True)
        for _ in range(enter_count - 1):
            self._input_call("terminal.keys", terminal=address, keys=["enter"])

    def send_special_key(self, session_name: str, window_name: str, key: str) -> None:
        lowered = (key or "enter").lower()
        name = self._KEY_NAMES.get(lowered, lowered)
        address = self._address(session_name, window_name)
        self._input_call("terminal.keys", terminal=address, keys=[name])

    # --- Output ---

    @property
    def max_history_lines(self) -> int:
        return HC_READ_MAX_LINES

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
        """terminal.read returns plain text (screen plus ``lines`` of history)."""
        if visible_only:
            lines = 0
        elif full_history:
            lines = HC_READ_MAX_LINES
        else:
            lines = min(tail_lines or 500, HC_READ_MAX_LINES)
        result = self._call(
            "terminal.read", terminal=self._address(session_name, window_name), lines=lines
        )
        return str(result.get("text", ""))

    def get_pane_working_directory(self, session_name: str, window_name: str) -> Optional[str]:
        t = self._terminal(session_name, window_name)
        return (t.get("foreground") or {}).get("cwd") or t.get("cwd")

    def get_pane_current_command(self, session_name: str, window_name: str) -> Optional[str]:
        return (self._terminal(session_name, window_name).get("foreground") or {}).get("name")

    # --- Status ---

    def supports_event_inbox(self) -> bool:
        return True

    def get_native_status(self, session_name: str, window_name: str) -> Optional[TerminalStatus]:
        try:
            status = self._terminal(session_name, window_name).get("status")
        except TerminalBackendError:
            return None
        state = status.get("state") if isinstance(status, dict) else None
        return HC_STATUS_TO_TERMINAL_STATUS.get(state) if isinstance(state, str) else None

    def get_pane_id(self, terminal_id: str, session_name: str = "", window_name: str = "") -> str:
        found = self._list(**{"cao.terminal_id": terminal_id})
        if not found:
            raise TerminalNotFoundError(terminal_id, "no live herdr-core terminal")
        return str(found[0]["address"])

    # --- Attach ---

    def attach_session(self, session_name: str) -> None:
        found = self._list(**{"cao.session": session_name})
        if not found:
            raise TerminalNotFoundError(session_name, "no live herdr-core terminal")
        hc = hc_binary()
        os.execvpe(hc, [hc, "attach", str(found[0]["address"])], hc_env(self._session))

    def prepare_web_attach(self, session_name: str, window_name: str) -> List[str]:
        # The browser PTY inherits cao-server's environment (api/main.py), so
        # the variables that would point hc at the caller's core are unset here.
        unset = [arg for key in CALLER_CORE_ENV for arg in ("-u", key)]
        return [
            "env",
            *unset,
            f"HC_SESSION={self._session}",
            hc_binary(),
            "attach",
            self._address(session_name, window_name),
        ]

    # --- Pipe-pane: event inbox instead ---

    def pipe_pane(self, session_name: str, window_name: str, file_path: str) -> None:
        return None

    def stop_pipe_pane(self, session_name: str, window_name: str) -> None:
        return None
