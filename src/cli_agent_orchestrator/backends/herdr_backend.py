"""HerdrBackend — TerminalBackend implementation using the herdr CLI.

Herdr is a Rust-based terminal multiplexer with native agent-awareness.
This backend maps CAO operations to herdr CLI commands.

Design decisions:
- One herdr session, workspaces per CAO session (labeled cao-<name>)
- terminal_id is the stable identifier; pane_id is resolved before each operation
- Resolution cache with 5s TTL reduces redundant herdr pane list calls
- CAO_TERMINAL_ID and CAO_SESSION_NAME injected via command prefix
"""

import json
import logging
import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, cast

from cli_agent_orchestrator.backends.base import (
    TerminalBackend,
    TerminalBackendError,
    TerminalNotFoundError,
)
from cli_agent_orchestrator.models.terminal import TerminalStatus

logger = logging.getLogger(__name__)

# Herdr CLI subcommands that _run_herdr is allowed to invoke.
_HERDR_ALLOWED_SUBCOMMANDS = frozenset(
    {
        "workspace",
        "tab",
        "pane",
        "session",
    }
)

# Pattern for safe structural argument values passed to herdr.  The goal is
# preventing argument injection (crafted --flags) under shell=False, NOT shell
# injection (which list-form subprocess already prevents).  Rejects control
# characters and NUL bytes; allows printable characters needed for filesystem
# paths, UUIDs, labels, and JSON snippets.
_SAFE_ARG_RE = re.compile(r"^[\w\-./: =,@(){}\[\]\"'\\~+#]+$", re.UNICODE)

# Flags that _run_herdr is allowed to pass to the herdr CLI.  Any argument
# starting with "--" that is not in this set is rejected to prevent argument
# injection (e.g. a crafted ``--session other`` overriding the backend's
# session selection).
_HERDR_ALLOWED_FLAGS = frozenset(
    {
        "--cwd",
        "--format",
        "--label",
        "--lines",
        "--source",
        "--workspace",
    }
)


def _sanitize_herdr_args(args: List[str]) -> List[str]:
    """Validate herdr CLI arguments and return a shallow copy.

    Checks that all structural arguments (subcommand, flags, identifiers) are
    safe before they reach subprocess.run().  Returns a new list so static
    analysis tools see the subprocess receiving values that passed through
    this validation gate rather than the original caller-provided references.

    herdr is invoked with shell=False (list form) so shell injection is not
    possible, but argument injection (e.g. injecting ``--session other``) could
    redirect commands to unintended targets.  This sanitizer ensures that:
    1. The first positional arg is a known herdr subcommand.
    2. All structural arguments match a safe character set.
    3. Any ``--flag`` is in the allowed set (``--session`` is excluded since
       ``_run_herdr`` injects it from a trusted instance attribute).
    Terminal input payloads (the text body of ``pane send-text`` / ``pane run``)
    are exempt because they are literal content typed into a terminal pane, not
    arguments that alter herdr's own behavior.
    """
    if not args:
        raise ValueError("herdr args must not be empty")
    subcommand = args[0]
    if subcommand not in _HERDR_ALLOWED_SUBCOMMANDS:
        raise ValueError(
            f"herdr subcommand '{subcommand}' not in allowlist: "
            f"{sorted(_HERDR_ALLOWED_SUBCOMMANDS)}"
        )
    # Determine how many args are structural (subcommand + action + flags/ids).
    # ``pane send-text <pane_id> <text>`` and ``pane run <pane_id> <cmd>``
    # carry a terminal-input / shell-command payload at index 3+ that is
    # exempt from validation (it is content, not an argument that alters
    # herdr's own routing or behavior).
    if len(args) >= 2 and args[0] == "pane" and args[1] in ("send-text", "run"):
        structural_args = args[:3]
    else:
        structural_args = args
    for arg in structural_args:
        if not _SAFE_ARG_RE.fullmatch(arg):
            raise ValueError(f"herdr argument contains unsafe characters: {arg!r}")
        if arg.startswith("--") and arg not in _HERDR_ALLOWED_FLAGS:
            raise ValueError(
                f"herdr flag '{arg}' not in allowlist: " f"{sorted(_HERDR_ALLOWED_FLAGS)}"
            )
    return list(args)


# Cache TTL for pane_id resolution (seconds).
# Used by get_pane_id() (fast-path, reads the cache populated at create time) and
# _resolve_workspace_id(). _resolve_pane_id_from_window() never caches pane_ids —
# herdr renumbers panes on deletion, so it resolves the pane fresh every call.
_PANE_CACHE_TTL = 5.0


class HerdrBackend(TerminalBackend):
    """TerminalBackend implementation using herdr CLI commands.

    Maps CAO concepts to herdr:
    - CAO session → herdr workspace (labeled cao-<name>)
    - CAO terminal/window → herdr tab within workspace
    - terminal_id → stable identifier stored in CAO DB
    - pane_id → compact ID resolved via herdr pane list before each operation
    """

    def __init__(self, send_delay_ms: int = 0, herdr_session: str = "cao") -> None:
        """Initialize HerdrBackend.

        Args:
            send_delay_ms: Milliseconds to sleep between send-text and send-keys Enter.
                Configurable per-provider for bracketed paste timing.
            herdr_session: Name of the herdr session CAO operates in.
                Maps to ``herdr --session <name>``. Defaults to ``"cao"`` so CAO
                runs isolated from the user's personal herdr session.
        """
        self._send_delay_ms = send_delay_ms
        self._herdr_session = herdr_session
        # Resolution cache: terminal_id → (pane_id, timestamp)
        self._pane_cache: Dict[str, tuple[str, float]] = {}
        # Workspace cache: session_name → (workspace_id, timestamp)
        self._workspace_cache: Dict[str, tuple[str, float]] = {}
        self._ensure_session_running()

    @property
    def herdr_session(self) -> str:
        """The herdr session name this backend operates in."""
        return self._herdr_session

    def _run_herdr(self, args: List[str], check: bool = True) -> subprocess.CompletedProcess:
        """Run a herdr CLI command and return the result.

        Args:
            args: Command arguments (without 'herdr' prefix)
            check: If True, raise TerminalBackendError on non-zero exit

        Returns:
            CompletedProcess result

        Raises:
            TerminalBackendError: If check=True and command fails, or if args
                contain unsafe characters or unknown subcommands.
        """
        try:
            sanitized = _sanitize_herdr_args(args)
        except ValueError as e:
            raise TerminalBackendError(f"herdr argument validation failed: {e}") from e
        cmd = ["herdr", "--session", self._herdr_session] + sanitized
        # Redact only send-text/run payloads from error messages to avoid
        # leaking sensitive terminal input. Other commands keep full args
        # for debuggability.
        has_payload = (
            len(sanitized) >= 3 and sanitized[0] == "pane" and sanitized[1] in ("send-text", "run")
        )
        if has_payload:
            cmd_display = cmd[:6] + ["<redacted>"]
        else:
            cmd_display = cmd
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if check and result.returncode != 0:
                raise TerminalBackendError(
                    f"herdr command failed: {' '.join(cmd_display)}\n"
                    f"stderr: {result.stderr.strip()}"
                )
            return result
        except subprocess.TimeoutExpired as e:
            raise TerminalBackendError(f"herdr command timed out: {' '.join(cmd_display)}") from e
        except FileNotFoundError as e:
            raise TerminalBackendError(
                "herdr CLI not found. Install herdr to use terminal_backend='herdr'."
            ) from e

    def _parse_herdr_json(self, stdout: str) -> dict:
        """Parse herdr CLI JSON output, handling the envelope format.

        Herdr wraps responses in {"id":..., "result": {...}} envelopes.
        """
        data = json.loads(stdout)
        if isinstance(data, dict) and "result" in data:
            return cast(dict, data["result"])
        return cast(dict, data)

    def _resolve_workspace_id(self, session_name: str) -> str:
        """Resolve session_name (workspace label) to workspace ID.

        Uses _workspace_cache with the same TTL as pane cache.

        Args:
            session_name: CAO session name (used as workspace label)

        Returns:
            Workspace ID

        Raises:
            TerminalBackendError: If workspace not found
        """
        # Check cache
        if session_name in self._workspace_cache:
            workspace_id, cached_at = self._workspace_cache[session_name]
            if time.time() - cached_at < _PANE_CACHE_TTL:
                return workspace_id

        result = self._run_herdr(["workspace", "list"])
        try:
            data = self._parse_herdr_json(result.stdout)
            workspaces = data.get("workspaces", []) if isinstance(data, dict) else data
        except json.JSONDecodeError as e:
            raise TerminalBackendError(f"Failed to parse herdr workspace list output: {e}") from e

        for ws in workspaces:
            if ws.get("label") == session_name:
                ws_id = str(ws["workspace_id"])
                self._workspace_cache[session_name] = (ws_id, time.time())
                return ws_id

        raise TerminalBackendError(f"Workspace with label '{session_name}' not found")

    # --- Session lifecycle ---

    def create_session(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        working_directory: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> str:
        """Create a herdr workspace (= CAO session) with an initial tab."""
        import os

        working_directory = working_directory or os.getcwd()

        args = ["workspace", "create", "--label", session_name]
        if working_directory:
            args.extend(["--cwd", working_directory])

        result = self._run_herdr(args)

        # Parse workspace ID and root tab_id from output for cache
        workspace_id = ""
        root_tab_id = ""
        try:
            ws_data = self._parse_herdr_json(result.stdout)
            root_pane = ws_data.get("root_pane", {})
            workspace_id = str(root_pane.get("workspace_id", ""))
            root_tab_id = str(root_pane.get("tab_id", ""))
            if workspace_id:
                self._workspace_cache[session_name] = (workspace_id, time.time())
        except (json.JSONDecodeError, KeyError):
            pass  # Non-fatal; we can resolve later

        # Parse root pane_id from the create response for env injection
        new_pane_id = self._parse_new_pane_id(result.stdout)

        # Label the root tab so it shows the CAO window name in herdr TUI.
        if root_tab_id:
            self._run_herdr(["tab", "rename", root_tab_id, window_name], check=False)

        # Inject CAO env vars into the initial pane so agents and cao info can
        # identify the terminal/session (mirrors TmuxClient env injection).
        self._inject_env_vars(
            session_name, window_name, terminal_id, pane_id=new_pane_id, extra_env=extra_env
        )

        logger.info(f"Created herdr workspace: {session_name} in {working_directory}")
        return window_name

    def session_exists(self, session_name: str) -> bool:
        """Check if a workspace with the given label exists."""
        result = self._run_herdr(["workspace", "list"], check=False)
        if result.returncode != 0:
            return False
        try:
            data = self._parse_herdr_json(result.stdout)
            workspaces = data.get("workspaces", []) if isinstance(data, dict) else data
            return any(ws.get("label") == session_name for ws in workspaces)
        except (json.JSONDecodeError, KeyError):
            return False

    def list_sessions(self) -> List[Dict[str, str]]:
        """List all herdr workspaces as sessions."""
        result = self._run_herdr(["workspace", "list"], check=False)
        if result.returncode != 0:
            return []
        try:
            data = self._parse_herdr_json(result.stdout)
            workspaces = data.get("workspaces", []) if isinstance(data, dict) else data
            return [
                {
                    "id": ws.get("label", str(ws.get("workspace_id", ""))),
                    "name": ws.get("label", str(ws.get("workspace_id", ""))),
                    "status": "active",
                }
                for ws in workspaces
            ]
        except (json.JSONDecodeError, KeyError):
            return []

    def kill_session(self, session_name: str) -> bool:
        """Close a herdr workspace by workspace_id (herdr only accepts id, not --label)."""
        try:
            workspace_id = self._resolve_workspace_id(session_name)
        except TerminalBackendError:
            logger.warning("kill_session: workspace not found")
            return False
        result = self._run_herdr(["workspace", "close", workspace_id], check=False)
        if result.returncode == 0:
            self._workspace_cache.pop(session_name, None)
            logger.info("Killed herdr workspace")
            return True
        return False

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
        """Create a new tab in the workspace."""
        import os

        working_directory = working_directory or os.getcwd()

        # Resolve workspace ID
        workspace_id = self._resolve_workspace_id(session_name)

        args = ["tab", "create", "--workspace", workspace_id, "--label", window_name]
        if working_directory:
            args.extend(["--cwd", working_directory])

        result = self._run_herdr(args)

        # Parse the new pane_id directly from the create response
        new_pane_id = self._parse_new_pane_id(result.stdout)

        # Inject CAO env vars using the known pane_id (no list scan needed)
        self._inject_env_vars(
            session_name, window_name, terminal_id, pane_id=new_pane_id, extra_env=extra_env
        )

        if window_shell is not None and new_pane_id is not None:
            # Wait for shell startup before sending the initial command.
            time.sleep(0.5)
            try:
                self._run_herdr(["pane", "run", new_pane_id, window_shell])
            except TerminalBackendError as e:
                logger.warning(f"create_window: pane run failed (non-fatal): {e}")

        logger.info("Created herdr tab in workspace")
        return window_name

    def kill_window(self, session_name: str, window_name: str) -> bool:
        """Kill a pane by resolving session_name:window_name to its pane_id."""
        try:
            pane_id = self._resolve_pane_id_from_window(session_name, window_name)
        except TerminalBackendError:
            logger.warning(f"kill_window: could not resolve pane for {session_name}:{window_name}")
            return False

        result = self._run_herdr(["pane", "close", pane_id], check=False)

        if result.returncode == 0:
            logger.info(f"Killed herdr pane {pane_id} for {session_name}:{window_name}")
            return True
        return False

    # --- Input ---

    def send_keys(
        self,
        session_name: str,
        window_name: str,
        keys: str,
        enter_count: int = 1,
        force_bracketed_paste: bool = False,
        submit_delay: float = 0.3,
        use_paste_buffer: bool = True,  # Ignored for Herdr
    ) -> None:
        """Send text to a pane via herdr pane send-text + send-keys Enter.

        When force_bracketed_paste=True, wraps content in \\x1b[200~...\\x1b[201~
        so Claude Code's Ink TUI treats it as a paste event rather than raw
        keystrokes. Without this, multi-line prompts go into multi-line mode
        and the final Enter adds a newline instead of submitting.

        ``submit_delay`` is accepted for parity with the backend interface; herdr
        governs its own post-paste timing below (the generous 2s bracketed wait
        already covers Claude Code's Ink renderer), so the value is not used here.
        """
        # Resolve pane_id from terminal_id stored in DB metadata
        # The window_name is used as a lookup key in CAO's DB → terminal_id mapping
        # For herdr, we need the terminal_id. The service layer passes session:window
        # which maps to a terminal in the DB. We'll resolve via the pane list.
        pane_id = self._resolve_pane_id_from_window(session_name, window_name)

        # Wrap in bracketed paste sequences when requested.
        # herdr pane send-text writes raw bytes to the pty, so escape sequences
        # pass through to the running process unchanged — same behavior as tmux.
        if force_bracketed_paste:
            text = "\x1b[200~" + keys + "\x1b[201~"
        else:
            text = keys

        self._run_herdr(["pane", "send-text", pane_id, text])

        # Allow the TUI to process the pasted content before sending Enter.
        # For bracketed paste, the TUI needs time to process the end sequence
        # and enter multi-line mode; 2s is intentionally generous.
        # For non-bracketed paste, use the configurable send_delay_ms.
        if force_bracketed_paste:
            time.sleep(2.0)
        elif self._send_delay_ms > 0:
            time.sleep(self._send_delay_ms / 1000.0)

        # Send Enter key(s)
        for _ in range(enter_count):
            self._run_herdr(["pane", "send-keys", pane_id, "Enter"])

    def send_special_key(self, session_name: str, window_name: str, key: str) -> None:
        """Send a special key to a pane."""
        pane_id = self._resolve_pane_id_from_window(session_name, window_name)

        # Map key names
        if not key or key.lower() == "enter":
            self._run_herdr(["pane", "send-keys", pane_id, "Enter"])
        elif key == "C-c":
            self._run_herdr(["pane", "send-keys", pane_id, "C-c"])
        elif key == "C-d":
            self._run_herdr(["pane", "send-keys", pane_id, "C-d"])
        else:
            # Pass key name directly
            self._run_herdr(["pane", "send-keys", pane_id, key])

    # --- Output ---

    def get_history(
        self,
        session_name: str,
        window_name: str,
        tail_lines: Optional[int] = None,
        strip_escapes: bool = False,
        full_history: bool = False,
    ) -> str:
        """Read pane output via herdr pane read."""
        pane_id = self._resolve_pane_id_from_window(session_name, window_name)

        args = ["pane", "read", pane_id]
        if full_history:
            pass  # no flags — returns full scrollback
        elif tail_lines:
            args.extend(["--source", "recent", "--lines", str(tail_lines)])
        else:
            args.extend(["--source", "recent", "--lines", "500"])
        # Honor strip_escapes via herdr's native --format text (strips ANSI).
        # The TerminalBackend contract only requires that strip_escapes=True
        # yields plain text; when False we leave the format unset and take
        # herdr's default so existing provider output parsing is unchanged.
        if strip_escapes:
            args.extend(["--format", "text"])

        result = self._run_herdr(args, check=False)
        if result.returncode != 0:
            logger.warning(f"herdr pane read failed: {result.stderr}")
            return ""
        return cast(str, result.stdout)

    def get_pane_working_directory(self, session_name: str, window_name: str) -> Optional[str]:
        """Get pane CWD via herdr pane get."""
        pane_id = self._resolve_pane_id_from_window(session_name, window_name)

        result = self._run_herdr(["pane", "get", pane_id], check=False)
        if result.returncode != 0:
            return None
        try:
            data = self._parse_herdr_json(result.stdout)
            # pane get returns {"pane": {...}} inside result
            pane_info = data.get("pane", data) if isinstance(data, dict) else data
            return cast(Optional[str], pane_info.get("cwd"))
        except (json.JSONDecodeError, AttributeError):
            return None

    def get_pane_current_command(self, session_name: str, window_name: str) -> Optional[str]:
        """Get foreground process via herdr pane get."""
        pane_id = self._resolve_pane_id_from_window(session_name, window_name)

        result = self._run_herdr(["pane", "get", pane_id], check=False)
        if result.returncode != 0:
            return None
        try:
            data = self._parse_herdr_json(result.stdout)
            pane_info = data.get("pane", data) if isinstance(data, dict) else data
            return cast(Optional[str], pane_info.get("foreground_process"))
        except (json.JSONDecodeError, AttributeError):
            return None

    # --- Attach ---

    def attach_session(self, session_name: str) -> None:
        """Attach the user's terminal to the herdr UI, focused on the CAO workspace.

        Strategy:
        1. Focus the CAO workspace in the running herdr server (so the UI opens
           on the right workspace).
        2. Exec `herdr` to replace the current process with the full herdr TUI.

        This mirrors how `tmux attach-session -t <session>` works — it opens
        the multiplexer UI showing the requested session.
        """
        import os

        workspace_id = self._resolve_workspace_id(session_name)

        # Focus the workspace so herdr opens on it when we attach
        self._run_herdr(["workspace", "focus", workspace_id], check=False)

        # Replace current process with herdr TUI, targeting the CAO session.
        # Equivalent to `tmux attach-session -t <session>`.
        os.execvp("herdr", ["herdr", "--session", self._herdr_session])

    # --- Capability overrides ---

    def supports_event_inbox(self) -> bool:
        """Herdr uses socket events for inbox delivery."""
        return True

    def get_native_status(self, session_name: str, window_name: str) -> Optional[TerminalStatus]:
        """Query herdr's native agent_status for a pane.

        Uses herdr pane get to read the agent_status field directly, avoiding
        pane content parsing entirely when herdr knows the agent state.

        Mapping (all five herdr agent states):
        - working  -> PROCESSING
        - blocked  -> WAITING_USER_ANSWER
        - done     -> COMPLETED
        - idle     -> IDLE  (caller disambiguates IDLE vs COMPLETED via _task_dispatched)
        - unknown  -> ERROR  (pane exists but no agent registered yet)

        Returns None only on backend errors (command failure, parse error) so
        the caller can fall through to buffer analysis as a last resort.
        """
        try:
            pane_id = self._resolve_pane_id_from_window(session_name, window_name)
        except TerminalBackendError:
            return None

        result = self._run_herdr(["pane", "get", pane_id], check=False)
        if result.returncode != 0:
            return None

        try:
            data = self._parse_herdr_json(result.stdout)
            pane_info = data.get("pane", data) if isinstance(data, dict) else data
            agent_status = pane_info.get("agent_status", "unknown")
        except (json.JSONDecodeError, AttributeError):
            return None

        if agent_status == "working":
            return TerminalStatus.PROCESSING
        if agent_status == "blocked":
            return TerminalStatus.WAITING_USER_ANSWER
        if agent_status == "done":
            return TerminalStatus.COMPLETED
        if agent_status == "idle":
            return TerminalStatus.IDLE
        # "unknown" and any unrecognized value
        return TerminalStatus.ERROR

    def get_pane_id(self, terminal_id: str, session_name: str = "", window_name: str = "") -> str:
        """Resolve CAO terminal_id to herdr pane_id.

        Prefers the _pane_cache (populated by _inject_env_vars at create time).
        Falls back to live label-based resolution (_resolve_workspace_id ->
        _resolve_tab_id -> pane list) if session/window given.

        Args:
            terminal_id: CAO UUID terminal identifier
            session_name: Optional session name for window-based fallback lookup
            window_name: Optional window name for window-based fallback lookup

        Returns:
            Current herdr compact pane_id

        Raises:
            TerminalNotFoundError: If pane cannot be resolved
        """
        # Fast path: pane_id was cached by _inject_env_vars
        if terminal_id in self._pane_cache:
            pane_id, cached_at = self._pane_cache[terminal_id]
            if time.time() - cached_at < _PANE_CACHE_TTL:
                return pane_id

        # Fallback: resolve via window mapping if session/window provided
        if session_name and window_name:
            return self._resolve_pane_id_from_window(session_name, window_name)

        raise TerminalNotFoundError(terminal_id)

    # --- Pipe-pane (no-op for herdr) ---

    def pipe_pane(self, session_name: str, window_name: str, file_path: str) -> None:
        """No-op: herdr uses socket events for inbox delivery."""
        logger.debug(f"pipe_pane is a no-op for herdr backend (session={session_name})")

    def stop_pipe_pane(self, session_name: str, window_name: str) -> None:
        """No-op: herdr uses socket events for inbox delivery."""
        logger.debug(f"stop_pipe_pane is a no-op for herdr backend (session={session_name})")

    # --- Internal helpers ---

    def _session_socket_path(self) -> str:
        """Return the herdr socket path for the configured session.

        Mirrors HerdrInboxService._default_socket_path():
        - ``"default"`` session: ``~/.config/herdr/herdr.sock``
        - Named sessions:       ``~/.config/herdr/sessions/<name>/herdr.sock``
        """
        config_home = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
        if self._herdr_session == "default":
            return f"{config_home}/herdr/herdr.sock"
        return f"{config_home}/herdr/sessions/{self._herdr_session}/herdr.sock"

    def _ensure_session_running(self) -> None:
        """Start the herdr session server if its socket does not exist.

        Checks for the session socket file. If absent, starts the server
        headlessly and waits up to 5 seconds for the socket to appear.
        Logs a warning if the socket never appears but does not raise —
        the first actual herdr operation will produce a clear error.
        """
        socket_path = self._session_socket_path()
        if os.path.exists(socket_path):
            return

        logger.info(
            f"Herdr session '{self._herdr_session}' not running "
            f"(socket {socket_path} absent) — starting server."
        )
        subprocess.Popen(
            ["herdr", "--session", self._herdr_session, "server"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        # Give herdr a moment to create the socket file before polling.
        time.sleep(0.5)

        # Poll up to 15 seconds for the socket to appear.
        deadline = time.time() + 15.0
        while time.time() < deadline:
            if os.path.exists(socket_path):
                logger.info(f"Herdr session '{self._herdr_session}' is ready.")
                return
            time.sleep(0.1)

        logger.warning(
            f"Herdr session '{self._herdr_session}' socket did not appear within 15s "
            f"at {socket_path}. The first herdr operation will fail with a clear error."
        )

    def _parse_new_pane_id(self, stdout: str) -> Optional[str]:
        """Extract the root pane_id from a workspace/tab create response.

        Both 'herdr workspace create' and 'herdr tab create' return a
        result.root_pane.pane_id field with the newly created pane's ID.
        """
        try:
            data = self._parse_herdr_json(stdout)
            return str(data["root_pane"]["pane_id"])
        except (json.JSONDecodeError, KeyError, TypeError):
            return None

    def _inject_env_vars(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        pane_id: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> None:
        """Inject CAO env vars (and operator-forwarded vars) into the pane.

        Called after create_session/create_window to export env vars into the
        pane's shell so agents and `cao info` can identify the terminal/session.
        Operator-supplied ``extra_env`` (from ``cao launch --env``) is forwarded
        too, filtered with the same rules TmuxClient applies to its ``-e`` argv
        so the launch-env contract behaves identically across backends.

        Args:
            session_name: CAO session name
            window_name: Window name (kept for signature symmetry / logging)
            terminal_id: Terminal identifier to inject
            pane_id: Pane ID from the create response. If None, falls back to
                pane list scan (less reliable under concurrency).
            extra_env: Operator-forwarded env vars from ``cao launch --env``.
        """
        try:
            # Use provided pane_id if available (from create response)
            target_pane_id = pane_id
            if not target_pane_id:
                # Fallback: scan pane list for last pane in workspace.
                # NOTE: under concurrent creates in the same workspace this can
                # pick the wrong (most-recent) pane. It only fires when the create
                # response lacked a pane_id; the pane_id param is the primary path.
                workspace_id = self._resolve_workspace_id(session_name)
                result = self._run_herdr(["pane", "list"])
                data = self._parse_herdr_json(result.stdout)
                panes = data.get("panes", []) if isinstance(data, dict) else data
                for p in panes:
                    if p.get("workspace_id") == workspace_id:
                        target_pane_id = str(p["pane_id"])

            if target_pane_id:
                # Cache the mapping
                self._pane_cache[terminal_id] = (target_pane_id, time.time())
                # Build export command: CAO identity vars first, then any
                # operator-forwarded vars. terminal_id/session_name come from
                # CAO internals (safe); extra_env values are operator-supplied,
                # so quote them to keep the shell export injection-safe.
                exports = [
                    f"export CAO_TERMINAL_ID={terminal_id}",
                    f"export CAO_SESSION_NAME={session_name}",
                ]
                exports.extend(self._build_extra_env_exports(extra_env))
                env_cmd = "; ".join(exports)
                self._run_herdr(["pane", "send-text", target_pane_id, env_cmd])
                self._run_herdr(["pane", "send-keys", target_pane_id, "Enter"])
        except (TerminalBackendError, json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Failed to inject env vars for {terminal_id}: {e}")

    @staticmethod
    def _build_extra_env_exports(extra_env: Optional[Dict[str, str]]) -> List[str]:
        """Return shell ``export`` statements for operator-forwarded env vars.

        Applies the same safety filtering TmuxClient uses for its ``-e`` argv
        (blocked provider prefixes, per-value byte cap) so ``cao launch --env``
        forwards the same set of vars under herdr as under tmux. Values are
        shell-quoted because herdr injects via ``pane send-text`` (a shell
        command line), unlike tmux's exec-style ``-e KEY=VALUE`` argv.
        """
        if not extra_env:
            return []

        # Reuse the tmux filtering policy so the two backends cannot drift.
        from cli_agent_orchestrator.clients.tmux import TmuxClient

        exports: List[str] = []
        for key, value in extra_env.items():
            if TmuxClient._is_blocked_env_key(key):
                logger.warning("Dropping forwarded env var with blocked prefix: %s", key)
                continue
            if len(value.encode("utf-8")) >= TmuxClient._MAX_ENV_VALUE_BYTES:
                logger.warning(
                    "Dropping forwarded env var %s -- value exceeds %d bytes",
                    key,
                    TmuxClient._MAX_ENV_VALUE_BYTES,
                )
                continue
            exports.append(f"export {key}={shlex.quote(value)}")
        return exports

    def _resolve_tab_id(self, session_name: str, workspace_id: str, window_name: str) -> str:
        """Resolve window_name to its herdr tab_id in the given workspace.

        Args:
            session_name: CAO session name (used only in error messages)
            workspace_id: Herdr workspace ID to search within
            window_name: Tab label to match

        Returns:
            The tab_id of the matching tab

        Raises:
            TerminalBackendError: If no tab with label window_name exists in workspace_id
        """
        result = self._run_herdr(["tab", "list"])
        try:
            data = self._parse_herdr_json(result.stdout)
            tabs = data.get("tabs", []) if isinstance(data, dict) else data
        except json.JSONDecodeError as e:
            raise TerminalBackendError(f"Failed to parse herdr tab list: {e}") from e

        for tab in tabs:
            if tab.get("workspace_id") == workspace_id and tab.get("label") == window_name:
                return str(tab["tab_id"])

        raise TerminalBackendError(
            f"No tab labeled '{window_name}' found in workspace '{session_name}'"
        )

    def _resolve_pane_id_from_window(self, session_name: str, window_name: str) -> str:
        """Resolve a pane_id given session_name and window_name.

        Performs a fresh herdr workspace + tab + pane lookup on every call. Pane
        IDs are not stable across deletions — herdr renumbers remaining panes
        when any pane in the workspace is removed, so a cached pane_id would go
        stale and cause pane_not_found errors for live terminals. workspace_id
        resolution is cached with a short TTL inside _resolve_workspace_id as a
        latency optimization; the chain is otherwise resolved live.

        Resolution chain: workspace_id (by label) → tab_id (by label within the
        workspace) → the pane whose tab_id matches. There is no fallback: a tab
        must exist for the window and a pane must exist for the tab.

        Raises:
            TerminalNotFoundError: If the workspace, tab, or pane cannot be
                resolved for session_name:window_name.
        """
        try:
            workspace_id = self._resolve_workspace_id(session_name)
            tab_id = self._resolve_tab_id(session_name, workspace_id, window_name)

            result = self._run_herdr(["pane", "list"])
            try:
                data = self._parse_herdr_json(result.stdout)
                panes = data.get("panes", []) if isinstance(data, dict) else data
            except json.JSONDecodeError as e:
                raise TerminalBackendError(f"Failed to parse herdr pane list: {e}") from e
        except TerminalBackendError as e:
            raise TerminalNotFoundError(f"{session_name}:{window_name}") from e

        for pane in panes:
            if pane.get("tab_id") == tab_id:
                return str(pane["pane_id"])

        raise TerminalNotFoundError(f"{session_name}:{window_name}")
