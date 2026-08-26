"""Official xAI Grok Build CLI provider implementation.

Observed with ``grok 1.0.0 (3cd0d0cbce) [stable]`` in ``--no-alt-screen``
mode.  The empty composer remains visible while a turn is running, so status
detection gives the live ``Waiting for response…`` / ``[stop]`` /
``Esc:cancel`` markers priority.  Completed turns end at ``Worked for <time>``.
Grok reads MCP servers from ``$GROK_HOME/config.toml`` and exits with
``/quit``.  Tool approval pickers expose a ``N/M:select`` footer.

Each CAO terminal receives a private ``GROK_HOME``.  Its MCP configuration is
written directly and atomically (never through ``grok mcp add``, which rewrites
the file as mode 0664), and authentication is reused through a narrow symlink
to the user's existing ``~/.grok/auth.json`` rather than copying credentials.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import signal
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, Literal, Optional

import psutil

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.constants import CAO_HOME_DIR
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.mcp_resolution import resolve_mcp_server_config
from cli_agent_orchestrator.utils.terminal import wait_for_shell
from cli_agent_orchestrator.utils.text import strip_terminal_escapes

logger = logging.getLogger(__name__)


class ProviderError(Exception):
    """Exception raised for Grok CLI provider-specific errors."""


# Render-stable current-turn signals from Grok Build 1.0.0.
PROCESSING_PATTERN = re.compile(
    r"Waiting for response…|\[stop\]|Esc:cancel|"
    r"[\u2800-\u28ff][^\n]*(?:Waiting for response|\u2026)",
    re.IGNORECASE,
)
# Do not bake the footer into this expression.  In the raw pipe-pane stream
# Grok writes a block cursor and cursor-positioning redraws between the
# ``Worked for`` text and ``Ctrl+x:shortcuts``; after escape normalization that
# is no longer whitespace (the proximity check in ``get_status`` handles it).
COMPLETION_PATTERN = re.compile(r"Worked\s+for\s+\d+(?:\.\d+)?[sm]")
# A visible completion marker occupies its own indented status line.  The raw
# pipe-pane equivalent is a cursor-positioned draw (optionally styled dim).
# Do not treat an arbitrary use of "Worked for" in assistant prose as chrome.
RENDERED_COMPLETION_PATTERN = re.compile(r"(?m)^[ \t]{2,}(Worked\s+for\s+\d+(?:\.\d+)?[sm])\s*$")
RAW_COMPLETION_PATTERN = re.compile(
    r"\x1b\[\d+;\d+H(?:\x1b\[[0-9;]*m)*(Worked\s+for\s+\d+(?:\.\d+)?[sm])"
)
QUERY_PATTERN = re.compile(r"^\s*❯\s+\S.*$", re.MULTILINE)
# The rendered pane is a normal ``│ ❯ │`` line, but Grok's raw pipe-pane
# stream positions each cell independently (e.g. CUP row/column sequences).
# ``strip_terminal_escapes`` removes those horizontal cursor moves, producing
# ``│❯│`` glued into a larger logical redraw line. Match that structural box
# anywhere in the recent tail; current processing markers still take priority.
IDLE_COMPOSER_PATTERN = re.compile(r"│\s*❯\s*│")
# Completed raw redraws commonly emit only ``Ctrl+x:shortcuts`` after the
# completion marker; ``Shift+Tab:mode`` may have been overwritten in place.
# Active turns instead show ``Esc:cancel``/``[stop]``, which are checked first.
READY_FOOTER_PATTERN = re.compile(r"Ctrl\+x:shortcuts", re.IGNORECASE)
WAITING_USER_PATTERN = re.compile(
    r"(?:\d+/\d+:select|Tab:next option|Ctrl\+c:cancel|"
    r"Waiting for approval\.\.\.|Approve in your browser to finish signing in|"
    r"Yes, proceed|No, reject \(type to add feedback\))",
    re.IGNORECASE,
)
# A fresh private GROK_HOME has no folder-trust decision.  This dialog would
# grant project-local MCP, LSP, and hooks permission to run repository-defined
# code, so it is not an ordinary picker CAO may auto-answer.
DIRECTORY_TRUST_PATTERN = re.compile(
    r"Do you trust the contents of this directory\?.*?"
    r"Grok Build may run or modify contents in this directory",
    re.IGNORECASE | re.DOTALL,
)
ERROR_PATTERN = re.compile(
    r"^(?:Error:|ERROR:|panic:|Traceback \(most recent call last\):|"
    r"Authentication failed|Failed to (?:start|connect|load)|"
    r"Unknown model|Model .* (?:not found|unavailable))",
    re.IGNORECASE | re.MULTILINE,
)

_STATUS_TAIL_CHARS = 8192
_COMPLETION_TO_READY_MAX_CHARS = 4096
_HOME_PROCESS_TERM_GRACE_SECONDS = 1.0
_HOME_PROCESS_KILL_GRACE_SECONDS = 1.0
_TIMESTAMP_SUFFIX = re.compile(r"\s{2,}\d{1,2}:\d{2}\s+(?:AM|PM)\s*$", re.IGNORECASE)
_THOUGHT_LINE = re.compile(r"^\s*◆\s+Thought\b", re.IGNORECASE)
_TOOL_LINE = re.compile(r"^\s*┃")
_TELEMETRY_LINE = re.compile(
    r"Help improve Grok|Off by default\. Opt-in|Read Terms and Privacy Policy",
    re.IGNORECASE,
)
_CHROME_LINE = re.compile(
    r"(?:Shift\+Tab:mode|Ctrl\+x:shortcuts|Esc:cancel|\[stop\]|"
    r"Grok\s+\S+.*always-approve|Clipboard may be unreachable)",
    re.IGNORECASE,
)
# Grok treats a server name before ``__`` as a literal identifier.  Accept the
# conventional MCP names used by existing profiles (including dots and a
# leading digit), but never accept pattern syntax or structural punctuation.
_MCP_SERVER_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _toml_string(value: Any) -> str:
    """Serialize a scalar as a TOML-compatible basic string."""

    return json.dumps(str(value), ensure_ascii=False)


class GrokCliProvider(BaseProvider):
    """Provider for the official ``grok`` interactive TUI."""

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        agent_profile: Optional[str] = None,
        allowed_tools: Optional[list] = None,
        model: Optional[str] = None,
        skill_prompt: Optional[str] = None,
    ):
        super().__init__(terminal_id, session_name, window_name, allowed_tools, skill_prompt)
        self._agent_profile = agent_profile
        self._model = model
        self._initialized = False
        self._turns = 0
        self._grok_home: Optional[Path] = None
        # Keep the root used at prepare time so cleanup remains safe and
        # testable if a caller relocates CAO_HOME_DIR between prepare/cleanup.
        # A normal server process never changes that constant; restart recovery
        # intentionally uses the current deterministic root instead.
        self._grok_home_root: Optional[Path] = None
        self._awaiting_turn_activity = False
        # The terminal screen can retain the previous completion after CAO has
        # dispatched another prompt. Keep an identity for that completion and
        # its position in a monotonic view of StatusMonitor's rolling stream.
        # The raw buffer is bounded, so a buffer-relative offset must never be
        # used as a completion identity: eviction shifts an old marker even
        # when no new completion has happened.
        self._last_completion_identity: Optional[str] = None
        self._last_completion_stream_offset: Optional[int] = None
        self._last_completion_buffer_epoch: Optional[int] = None
        self._turn_activity_seen = False
        self._status_buffer_epoch = 0
        self._last_status_buffer: Optional[str] = None
        self._last_status_buffer_stream_start = 0

    @property
    def paste_enter_count(self) -> int:
        """Grok submits bracketed-paste input with one Enter."""

        return 1

    @property
    def paste_submit_delay(self) -> float:
        """Live probing found 0.4s sufficient for Grok's composer."""

        return 0.4

    @property
    def blocks_orchestrated_input_while_waiting_user_answer(self) -> bool:
        """Approval and login pickers would consume orchestrated task text."""

        return True

    @property
    def grok_home(self) -> Optional[Path]:
        """The CAO-managed private home, exposed read-only for diagnostics/tests."""

        return self._grok_home

    def _try_load_profile(self):
        if self._agent_profile is None:
            return None
        try:
            return load_agent_profile(self._agent_profile)
        except Exception:
            return None

    def _load_profile(self):
        if self._agent_profile is None:
            return None
        try:
            return load_agent_profile(self._agent_profile)
        except FileNotFoundError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Failed to load agent profile '{self._agent_profile}': {exc}"
            ) from exc

    def _home_path(self) -> Path:
        digest = hashlib.sha256(self.terminal_id.encode("utf-8")).hexdigest()[:12]
        slug = re.sub(r"[^A-Za-z0-9_.-]", "_", self.terminal_id).strip("._") or "terminal"
        return self._managed_home_root() / f"{slug[:48]}-{digest}"

    @staticmethod
    def _managed_home_root() -> Path:
        """Return the only directory containing CAO-owned Grok homes."""

        return CAO_HOME_DIR / "grok" / "terminals"

    def _is_managed_home(self, home: Path) -> bool:
        """Return whether ``home`` is this terminal's deterministic home.

        ``cleanup()`` is intentionally willing to run on a provider recreated
        after a server restart, where ``_grok_home`` was never populated.  Do
        not turn that recovery path into a general recursive-delete primitive:
        the target must be exactly the deterministic child of CAO's managed
        Grok terminal root, rather than merely somewhere below it.
        """

        expected = self._home_path()
        current_root = self._managed_home_root()
        if home == expected and home.parent == current_root:
            root = current_root
        # Tests and a deliberately relocated CAO home can prepare a terminal
        # before the process-wide constant is changed.  Preserve that valid
        # in-process lifecycle, but only for the exact private path prepared
        # by this instance; restart recovery always takes the branch above.
        elif (
            home == self._grok_home
            and self._grok_home_root is not None
            and home.parent == self._grok_home_root
            and home.name == expected.name
        ):
            root = self._grok_home_root
        else:
            return False

        # ``Path`` equality is lexical.  An attacker-controlled symlink in the
        # managed path (for example ``.../grok/terminals -> /tmp``) would still
        # make the lexical check above pass, then cause ``rmtree(home)`` to
        # delete outside CAO's state directory.  CAO_HOME_DIR is resolved at
        # import in production, but inspect every managed ancestor as well so
        # cleanup remains safe with a relocated/test home and if an ancestor is
        # replaced between terminal creation and deletion.  The terminal home
        # itself is intentionally excluded: it may be a symlink and is safe to
        # unlink without following its target.
        base = root.parent.parent
        if root.name != "terminals" or root.parent.name != "grok":
            return False
        ancestors = (base, base / "grok", root)
        try:
            if any(ancestor.is_symlink() for ancestor in ancestors):
                return False
            # Defense in depth for platform-specific path normalization.  The
            # parent must resolve to this exact non-symlink managed root.
            return home.parent.resolve(strict=False) == root.resolve(strict=False)
        except OSError:
            return False

    @staticmethod
    def _server_dict(server: Any) -> dict[str, Any]:
        if isinstance(server, dict):
            return dict(server)
        if hasattr(server, "model_dump"):
            return dict(server.model_dump(exclude_none=True))
        raise ProviderError(f"Unsupported MCP server configuration: {type(server).__name__}")

    def _render_mcp_config(self, mcp_servers: Optional[dict[str, Any]]) -> str:
        lines = ["# Managed by CLI Agent Orchestrator. Do not edit."]
        for name, raw_server in (mcp_servers or {}).items():
            config = self._server_dict(raw_server)
            if "command" in config:
                config = resolve_mcp_server_config(config)
                env = dict(config.get("env") or {})
                env["CAO_TERMINAL_ID"] = self.terminal_id
                config["env"] = env

            table = f"mcp_servers.{_toml_string(name)}"
            lines.extend(["", f"[{table}]"])
            if config.get("url"):
                transport = config.get("type")
                if transport is not None:
                    if transport not in {"http", "sse"}:
                        raise ProviderError(
                            f"MCP server '{name}' has unsupported URL transport "
                            f"{transport!r}; Grok supports 'http' and 'sse'"
                        )
                    # Grok defaults an untyped URL to HTTP.  SSE requires an
                    # explicit type, so preserve the profile transport rather
                    # than silently changing an SSE server into HTTP.
                    lines.append(f"type = {_toml_string(transport)}")
                lines.append(f"url = {_toml_string(config['url'])}")
            elif config.get("command"):
                lines.append(f"command = {_toml_string(config['command'])}")
                args = config.get("args") or []
                serialized_args = ", ".join(_toml_string(arg) for arg in args)
                lines.append(f"args = [{serialized_args}]")
            else:
                raise ProviderError(f"MCP server '{name}' has neither command nor url")
            lines.append(f"enabled = {'true' if config.get('enabled', True) else 'false'}")
            if config.get("timeout") is not None:
                # CAO's common MCP schema exposes one timeout knob. Grok has
                # separate startup/tool fields; applying the explicit value to
                # both preserves the profile's cap without relying on Grok's
                # provider-specific defaults.
                timeout = int(config["timeout"])
                lines.append(f"startup_timeout_sec = {timeout}")
                lines.append(f"tool_timeout_sec = {timeout}")

            env = config.get("env") or {}
            if env:
                lines.extend(["", f"[{table}.env]"])
                for key, value in env.items():
                    lines.append(f"{_toml_string(key)} = {_toml_string(value)}")

            headers = config.get("headers") or {}
            if headers:
                lines.extend(["", f"[{table}.headers]"])
                for key, value in headers.items():
                    lines.append(f"{_toml_string(key)} = {_toml_string(value)}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _permitted_mcp_server_refs(
        allowed_tools: list, mcp_servers: Optional[dict[str, Any]]
    ) -> list[str]:
        """Return configured MCP server names safely referenced by ``allowedTools``.

        Grok's ``MCPTool(server__*)`` permission language accepts a pattern.
        Never interpolate an arbitrary ``@...`` CAO entry into that pattern:
        only a conventional server name that is actually configured for this
        profile (or CAO's built-in orchestration server) may grant MCP access.
        ``@builtin`` is a CAO vocabulary marker, not an MCP server reference.
        Unknown or malformed entries remain denied by the enclosing dontAsk
        policy instead of widening it.
        """
        configured = {"cao-mcp-server"}
        if isinstance(mcp_servers, dict):
            configured.update(name for name in mcp_servers if isinstance(name, str))

        return sorted(
            {
                name
                for tool_ref in allowed_tools
                if isinstance(tool_ref, str)
                and tool_ref.startswith("@")
                and (name := tool_ref[1:]) != "builtin"
                and _MCP_SERVER_REF.fullmatch(name)
                and name in configured
            }
        )

    @staticmethod
    def _atomic_write_private(path: Path, content: str) -> None:
        """Atomically publish UTF-8 text at mode 0600 in ``path``'s directory."""

        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temp_path = Path(temp_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = -1
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
            os.chmod(path, 0o600)
            if stat.S_IMODE(path.stat().st_mode) != 0o600:
                raise ProviderError(f"Could not secure Grok config at {path}")
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

    def _prepare_grok_home(self, mcp_servers: Optional[dict[str, Any]]) -> Path:
        home = self._home_path()
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(home, 0o700)

        configured_home = os.environ.get("GROK_HOME", "").strip()
        auth_source = (
            Path(configured_home).expanduser() if configured_home else Path.home() / ".grok"
        ) / "auth.json"
        auth_link = home / "auth.json"
        if auth_source.is_file() and not auth_link.exists():
            auth_link.symlink_to(auth_source)

        self._atomic_write_private(home / "config.toml", self._render_mcp_config(mcp_servers))
        self._grok_home = home
        self._grok_home_root = home.parent
        return home

    def _build_grok_command(self) -> str:
        binary = shutil.which("grok")
        if not binary:
            raise ProviderError(
                "Grok Build CLI not found: 'grok' is not on $PATH. "
                "Install the official xAI Grok Build CLI first."
            )

        profile = self._load_profile()
        mcp_servers = profile.mcpServers if profile is not None else None
        home = self._prepare_grok_home(mcp_servers)

        # CAO owns child profiles, terminal accounting, callbacks, and tool
        # boundaries. ``--no-subagents`` alone only blocks spawn_subagent;
        # disable Grok's workflow and /goal engines too, because either can
        # launch native workers outside CAO accounting. A profile must opt in
        # explicitly; unrestricted allowedTools is a tool policy, not consent
        # to bypass CAO orchestration.
        native_workflows = bool(profile and profile.grokNativeWorkflows)
        command_parts = [
            "env",
            f"GROK_HOME={home}",
            f"GROK_SUBAGENTS={int(native_workflows)}",
            f"GROK_WORKFLOWS={int(native_workflows)}",
            f"GROK_GOAL={int(native_workflows)}",
            binary,
            "--no-alt-screen",
        ]
        if not native_workflows:
            command_parts.append("--no-subagents")

        # Explicit launch/assign/handoff model wins, then profile model.
        model = self._model or (profile.model if profile is not None else None)
        if model:
            command_parts.extend(["--model", model])

        rules = self._apply_skill_prompt(profile.system_prompt if profile is not None else "")
        if rules:
            command_parts.extend(["--rules", rules])

        if self._allowed_tools is not None and "*" not in self._allowed_tools:
            from cli_agent_orchestrator.utils.tool_mapping import (
                get_allowed_tools,
                get_disallowed_tools,
            )

            # Grok 1.0.0's documented automation mode keeps deny rules in
            # force, but live CAO probes observed an intermittent Bash escape
            # under --always-approve.  Use Grok's deny-by-default mode for a
            # restricted CAO profile instead: explicitly allow only native
            # capabilities and CAO MCP tools, then retain native denials as a
            # defense-in-depth guard. This remains unattended: a non-allowed
            # Bash call is denied by dontAsk before it can prompt or execute.
            command_parts.extend(["--permission-mode", "dontAsk"])
            if self._allowed_tools:
                for tool in get_allowed_tools("grok_cli", self._allowed_tools):
                    command_parts.extend(["--allow", tool])
                for server_name in self._permitted_mcp_server_refs(
                    self._allowed_tools, mcp_servers
                ):
                    command_parts.extend(["--allow", f"MCPTool({server_name}__*)"])

                for tool in get_disallowed_tools("grok_cli", self._allowed_tools):
                    command_parts.extend(["--deny", tool])
            else:
                # dontAsk still grants Grok's built-in read-only operations;
                # an explicit empty CAO list must deny every tool class.
                command_parts.extend(["--deny", "*"])
            if "web_fetch" not in self._allowed_tools:
                command_parts.append("--disable-web-search")
        else:
            command_parts.append("--always-approve")

        return shlex.join(command_parts)

    async def initialize(self) -> bool:
        profile = self._try_load_profile()
        init_timeout = float(self.get_init_timeout(profile))
        try:
            if not await wait_for_shell(self.terminal_id, timeout=init_timeout):
                raise TimeoutError(f"Shell initialization timed out after {init_timeout:g}s")

            command = await asyncio.to_thread(self._build_grok_command)
            from cli_agent_orchestrator.services.status_monitor import status_monitor

            status_monitor.notify_input_sent(self.terminal_id)
            await asyncio.to_thread(
                get_backend().send_keys, self.session_name, self.window_name, command
            )
            await self._wait_for_startup_ready(init_timeout)

            # Grok preserves an existing config mode. Repair defensively after
            # startup in case a future release rewrites it during migration.
            if self._grok_home is not None:
                config_path = self._grok_home / "config.toml"
                if config_path.exists():
                    await asyncio.to_thread(os.chmod, config_path, 0o600)
            self._initialized = True
            return True
        except Exception:
            await asyncio.to_thread(self.cleanup)
            raise

    async def _wait_for_startup_ready(self, timeout: float) -> None:
        """Wait for the composer, failing explicitly rather than granting trust.

        Selecting ``No`` at Grok's directory-trust dialog quits and selecting
        ``Yes`` permits repository-controlled MCP/LSP/hooks under the terminal
        user's privileges. CAO must do neither in an unattended launch.
        """

        from cli_agent_orchestrator.services.status_monitor import status_monitor

        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            output = status_monitor.get_buffer(self.terminal_id)
            if DIRECTORY_TRUST_PATTERN.search(strip_terminal_escapes(output)):
                raise ProviderError(
                    "Grok Build is waiting for directory trust. CAO does not automatically "
                    "trust repository-local MCP, LSP, or hooks. Review and remove the "
                    "project-local configuration (for example .mcp.json or .grok/) before "
                    "launching this CAO terminal."
                )
            if status_monitor.get_status(self.terminal_id) in {
                TerminalStatus.IDLE,
                TerminalStatus.COMPLETED,
            }:
                return
            await asyncio.sleep(1.0)

        raise TimeoutError(f"Grok CLI initialization timed out after {timeout:g}s")

    def _observe_status_buffer(self, output: str, *, at_rolling_capacity: bool) -> tuple[int, bool]:
        """Return the monotonic stream start for a rolling normalized buffer.

        StatusMonitor appends FIFO chunks and then retains only a suffix. Most
        consecutive observations therefore either share a prefix (no eviction)
        or a suffix/prefix overlap (eviction). Preserve that overlap to make a
        completion position stable across buffer eviction. A short no-overlap
        replacement is a rendered-test/snapshot discontinuity, not trustworthy
        evidence of a new turn; a full rolling buffer with no overlap can only
        happen after enough fresh output to evict the prior buffer and is safe
        to advance as a new stream segment.
        """

        previous = self._last_status_buffer
        if previous is None:
            self._last_status_buffer = output
            return self._last_status_buffer_stream_start, True
        if output == previous:
            return self._last_status_buffer_stream_start, True

        if output.startswith(previous):
            stream_start = self._last_status_buffer_stream_start
            contiguous = True
        else:
            # Retaining an overlap is what lets a fixed-size StatusMonitor
            # buffer slide without changing the absolute position of retained
            # completion chrome. KMP finds the longest suffix/prefix overlap
            # in linear time; a descending slice comparison becomes quadratic
            # when a full 32 KiB buffer has no overlap.
            sequence: list[object] = [*output, object(), *previous]
            prefix = [0] * len(sequence)
            for index in range(1, len(sequence)):
                candidate = prefix[index - 1]
                while candidate and sequence[index] != sequence[candidate]:
                    candidate = prefix[candidate - 1]
                if sequence[index] == sequence[candidate]:
                    candidate += 1
                prefix[index] = candidate
            overlap = min(prefix[-1], len(previous), len(output))
            if overlap:
                stream_start = self._last_status_buffer_stream_start + len(previous) - overlap
                contiguous = True
            else:
                if at_rolling_capacity:
                    # A bounded FIFO buffer can lose every byte of its prior
                    # view if one burst exceeds the cap. It is fresh output,
                    # not a stale completion moved to a different offset.
                    stream_start = self._last_status_buffer_stream_start + len(previous)
                    contiguous = True
                else:
                    # Do not manufacture a new generation from a non-append
                    # snapshot. This is especially important after a visible
                    # processing frame, where an old completed screen may be
                    # supplied again by a renderer.
                    stream_start = self._last_status_buffer_stream_start
                    contiguous = False

        self._last_status_buffer = output
        self._last_status_buffer_stream_start = stream_start
        return stream_start, contiguous

    def notify_status_buffer_reset(self, epoch: int) -> None:
        """Start observing a fresh StatusMonitor byte-buffer generation.

        Retain the previous completion identity so a delayed redraw can still
        be rejected, but discard only the overlap-derived stream view.  The
        monitor invokes this while its lock is held, before it can append the
        first chunk for the newly dispatched turn.
        """

        if epoch <= self._status_buffer_epoch:
            return
        self._status_buffer_epoch = epoch
        self._last_status_buffer = None
        self._last_status_buffer_stream_start = 0

    def get_status(self, output: Optional[str]) -> TerminalStatus:
        native = self._resolve_native_status(output)
        if native is not None:
            return native

        output = self._resolve_buffer(output)
        if not output:
            return TerminalStatus.UNKNOWN

        # Completion positions below are measured in normalized terminal text,
        # so observe the same representation.  The capacity check remains on
        # raw bytes because StatusMonitor bounds the raw FIFO buffer before
        # ``strip_terminal_escapes`` removes cursor-control sequences.
        raw_output = output
        clean = strip_terminal_escapes(raw_output)
        from cli_agent_orchestrator.services.settings_service import get_server_settings

        stream_start, stream_contiguous = self._observe_status_buffer(
            clean,
            at_rolling_capacity=len(raw_output) >= get_server_settings()["state_buffer_max"],
        )
        tail_start = max(0, len(clean) - _STATUS_TAIL_CHARS)
        prefix = clean[:tail_start]
        tail = clean[tail_start:]

        last_waiting = max(
            (match.start() for match in WAITING_USER_PATTERN.finditer(tail)), default=-1
        )
        last_processing = max(
            (match.start() for match in PROCESSING_PATTERN.finditer(tail)), default=-1
        )
        last_ready = max(
            (
                match.start()
                for pattern in (READY_FOOTER_PATTERN, IDLE_COMPOSER_PATTERN)
                for match in pattern.finditer(tail)
            ),
            default=-1,
        )
        last_footer = max(
            (match.start() for match in READY_FOOTER_PATTERN.finditer(tail)), default=-1
        )
        # ``Worked for`` also occurs in normal assistant prose.  It becomes a
        # completion boundary only when Grok's current ready footer follows it
        # closely.  This deliberately accepts raw redraw cells (including the
        # visible block cursor) between the two markers rather than requiring
        # whitespace-only adjacency.
        raw_completion_ordinals: dict[str, set[int]] = {}
        raw_counts: dict[str, int] = {}
        raw_completion_starts = {
            match.start(1) for match in RAW_COMPLETION_PATTERN.finditer(output)
        }
        for match in COMPLETION_PATTERN.finditer(output):
            marker = match.group()
            ordinal = raw_counts.get(marker, 0)
            raw_counts[marker] = ordinal + 1
            if match.start() in raw_completion_starts:
                raw_completion_ordinals.setdefault(marker, set()).add(ordinal)

        rendered_completion_starts = {
            match.start(1) for match in RENDERED_COMPLETION_PATTERN.finditer(tail)
        }
        # Tail ordinals must continue the full-buffer sequence. Restarting at
        # zero lets an evicted raw ``Worked for`` marker match later prose and
        # falsely complete the current turn.
        clean_counts: dict[str, int] = {}
        for match in COMPLETION_PATTERN.finditer(prefix):
            marker = match.group()
            clean_counts[marker] = clean_counts.get(marker, 0) + 1
        completion_matches = []
        for match in COMPLETION_PATTERN.finditer(tail):
            marker = match.group()
            ordinal = clean_counts.get(marker, 0)
            clean_counts[marker] = ordinal + 1
            structurally_rendered = match.start() in rendered_completion_starts
            structurally_raw = ordinal in raw_completion_ordinals.get(marker, set())
            has_current_footer = 0 <= last_footer - match.end() <= _COMPLETION_TO_READY_MAX_CHARS
            has_later_query = any(
                query.start() > match.end() and query.start() < last_footer
                for query in QUERY_PATTERN.finditer(tail)
            )
            if (
                (structurally_rendered or structurally_raw)
                and has_current_footer
                and not has_later_query
            ):
                completion_matches.append(match)
        last_completion = completion_matches[-1].start() if completion_matches else -1
        last_error = max((match.start() for match in ERROR_PATTERN.finditer(tail)), default=-1)

        # Pickers/login are bottom-of-screen blocking surfaces. Position guards
        # keep a dismissed prompt retained in scrollback from pinning status.
        if last_waiting > max(last_completion, last_ready):
            return TerminalStatus.WAITING_USER_ANSWER

        if last_processing > last_completion:
            if self._awaiting_turn_activity:
                self._turn_activity_seen = True
            return TerminalStatus.PROCESSING

        if last_error > max(last_completion, last_ready, last_processing):
            return TerminalStatus.ERROR

        if last_ready >= 0:
            if last_completion >= 0 and self._turns > 0:
                completion_match = completion_matches[-1]
                completion_start = len(clean) - len(tail) + completion_match.start()
                completion_end = len(clean) - len(tail) + completion_match.end()
                completion_stream_offset = stream_start + completion_start
                query_matches = list(QUERY_PATTERN.finditer(clean[:completion_start]))
                turn_start = query_matches[-1].start() if query_matches else completion_start
                fingerprint = hashlib.sha256(
                    clean[turn_start:completion_end].encode("utf-8")
                ).hexdigest()
                same_completion = (
                    self._last_completion_identity == fingerprint
                    and self._last_completion_buffer_epoch == self._status_buffer_epoch
                    and self._last_completion_stream_offset == completion_stream_offset
                )
                if self._awaiting_turn_activity and same_completion:
                    return TerminalStatus.PROCESSING

                # A byte-identical completion can be legitimate on a new turn.
                # Accept it only when its stable stream position has advanced.
                # A short discontinuous snapshot has no such proof and must
                # remain processing; otherwise a stale completion seen after a
                # processing frame could complete the new task.
                if (
                    self._awaiting_turn_activity
                    and self._last_completion_identity == fingerprint
                    and self._last_completion_buffer_epoch == self._status_buffer_epoch
                    and (
                        not stream_contiguous
                        or self._last_completion_stream_offset is None
                        or completion_stream_offset <= self._last_completion_stream_offset
                    )
                ):
                    return TerminalStatus.PROCESSING

                # Before accepting a completion after dispatch, require a
                # current-turn signal.  A live processing marker is the usual
                # signal.  For very fast turns, the new query can be the first
                # observable signal; use the full transcript rather than the
                # 8 KiB status tail so long answers retain that evidence.
                if (
                    self._awaiting_turn_activity
                    and self._last_completion_identity is not None
                    and not self._turn_activity_seen
                ):
                    # A fresh buffer generation makes a processing marker that
                    # precedes the completion reliable current-turn evidence,
                    # even when Grok emits both frames in one FIFO chunk.  Do
                    # not accept an identical completion without that marker:
                    # a delayed old completed screen after clear must remain
                    # PROCESSING.
                    if (
                        self._last_completion_identity == fingerprint
                        and self._last_completion_buffer_epoch != self._status_buffer_epoch
                        and 0 <= last_processing < last_completion
                    ):
                        self._turn_activity_seen = True
                    else:
                        latest_query_start = query_matches[-1].start() if query_matches else None
                        if (
                            latest_query_start is None
                            or self._last_completion_stream_offset is not None
                            and stream_start + latest_query_start
                            <= self._last_completion_stream_offset
                        ):
                            return TerminalStatus.PROCESSING
                        self._turn_activity_seen = True
                self._awaiting_turn_activity = False
                self._last_completion_identity = fingerprint
                self._last_completion_stream_offset = completion_stream_offset
                self._last_completion_buffer_epoch = self._status_buffer_epoch
                return TerminalStatus.COMPLETED
            # After dispatch, do not mistake the previous empty composer for
            # instant completion before Grok has rendered this turn.
            if self._turns > 0:
                return TerminalStatus.PROCESSING
            return TerminalStatus.IDLE

        return TerminalStatus.UNKNOWN

    def get_idle_pattern_for_log(self) -> str:
        return r"Shift\+Tab:mode[^\n]*Ctrl\+x:shortcuts"

    def extract_last_message_from_script(self, script_output: str) -> str:
        clean = strip_terminal_escapes(script_output)
        completions = list(COMPLETION_PATTERN.finditer(clean))
        if not completions:
            raise ValueError("No Grok CLI completion boundary found")
        completion = completions[-1]

        queries = [match for match in QUERY_PATTERN.finditer(clean[: completion.start()])]
        if not queries:
            raise ValueError("No Grok CLI user query found before completion")
        query = queries[-1]

        body = clean[query.end() : completion.start()].splitlines()
        response_lines: list[str] = []
        for line in body:
            line = _TIMESTAMP_SUFFIX.sub("", line).rstrip()
            if _THOUGHT_LINE.search(line) or _TOOL_LINE.search(line):
                continue
            if _TELEMETRY_LINE.search(line) or _CHROME_LINE.search(line):
                continue
            response_lines.append(line)

        while response_lines and not response_lines[0].strip():
            response_lines.pop(0)
        while response_lines and not response_lines[-1].strip():
            response_lines.pop()

        nonempty_indents = [
            len(line) - len(line.lstrip()) for line in response_lines if line.strip()
        ]
        if nonempty_indents:
            common_indent = min(nonempty_indents)
            if common_indent:
                response_lines = [
                    line[common_indent:] if line.strip() else "" for line in response_lines
                ]

        response = "\n".join(response_lines).strip()
        if not response:
            raise ValueError("Empty Grok CLI response between query and completion")
        return response

    def exit_cli(self) -> str:
        return "/quit"

    @staticmethod
    def _pids_using_home(home: Path) -> Optional[set[int]]:
        """Return same-user processes whose exact ``GROK_HOME`` is ``home``.

        Grok 1.0.0 can leave its updater as an orphan after the tmux pane is
        killed.  That process keeps writing into the private home, so deleting
        the directory first merely lets it recreate a partial tree.  Inspect
        the process environment rather than command lines: the exact,
        deterministic home is the capability boundary and avoids touching a
        user's ordinary Grok process.
        """

        current_pid = os.getpid()
        pids: set[int] = set()
        try:
            process_ids = psutil.pids()
        except psutil.Error as exc:
            logger.warning("Cannot enumerate processes before cleaning Grok home %s: %s", home, exc)
            # A failed inspection is not evidence of a CAO-owned process.  In
            # particular, cleanup may run in the server process, so never
            # synthesize and signal our own PID. Retain state for a later retry.
            return None

        for pid in process_ids:
            if pid == current_pid:
                continue
            uses_home = GrokCliProvider._pid_uses_home(pid, home)
            if uses_home is None:
                # ``_pid_uses_home`` returns uncertainty only after identifying
                # a same-user Grok/cao-mcp candidate whose environment cannot
                # be read.  Retain the home rather than race that process.
                return None
            if uses_home:
                pids.add(pid)
        return pids

    @staticmethod
    def _pid_uses_home(pid: int, home: Path) -> Optional[bool]:
        """Check exact home ownership immediately before a signal.

        A PID found by the initial scan can exit and be reused before cleanup
        signals it. Re-reading same-user process metadata and its environment
        prevents a signal from crossing that PID-reuse boundary. ``None`` is
        uncertainty (for example a protected same-user environment), which is
        deliberately fail-closed for private-home removal.
        """

        inspected = GrokCliProvider._inspect_home_process(pid, home)
        if inspected is None:
            return None
        return not isinstance(inspected, bool)

    @staticmethod
    def _inspect_home_process(pid: int, home: Path) -> psutil.Process | Literal[False] | None:
        """Return a verified, PID-reuse-safe process owning ``home``.

        A returned ``psutil.Process`` has already cached its creation time.
        Calling ``send_signal`` on that same object makes psutil re-check the
        PID/create-time identity before signalling, rather than issuing a raw
        ``kill(pid, ...)`` after a separate verification step.
        """

        try:
            proc = psutil.Process(pid)
            # Seed psutil's pid+creation-time identity before examining the
            # process. Its send_signal() then refuses a PID reused between this
            # verification and delivery.
            proc.create_time()
            if proc.uids().effective != os.geteuid():
                return False
        except psutil.NoSuchProcess:
            return False
        except psutil.AccessDenied as exc:
            # We cannot establish same-user ownership, so this can still be a
            # Grok/updater process with this private home.  Retain it for a
            # later retry rather than racing a live writer.
            logger.warning("Cannot inspect process %s before Grok cleanup: %s", pid, exc)
            return None
        except psutil.Error as exc:
            logger.warning("Cannot inspect process %s before Grok cleanup: %s", pid, exc)
            return False

        try:
            # ``exe()`` can be protected even for an unrelated same-user
            # service (for example macOS login helpers). Its short process
            # name is enough to reject those before an unavailable executable
            # turns every cleanup into a retry. Python is a possible name for
            # the CAO MCP child, so it deliberately remains a candidate.
            process_name = proc.name().lower()
            possible_owner = (
                process_name == "grok"
                or process_name.startswith("grok-")
                or process_name == "cao-mcp-server"
                or process_name.startswith("python")
            )
            if not possible_owner:
                return False
            executable = proc.exe()
        except psutil.NoSuchProcess:
            return False
        except psutil.AccessDenied as exc:
            # At this point the process is same-user and has a Grok/CAO
            # candidate name, but its identity cannot be fully verified.
            # Fail closed so cleanup never removes a home a live updater may
            # recreate.
            logger.warning("Cannot inspect process %s before Grok cleanup: %s", pid, exc)
            return None
        except psutil.Error as exc:
            logger.warning("Cannot inspect process %s before Grok cleanup: %s", pid, exc)
            return None

        try:
            # Avoid treating unrelated same-user services (which can protect
            # their environment) as Grok cleanup
            # candidates. A Grok main process/updater always has ``grok`` in
            # its executable basename. Its CAO MCP child is Python, so accept
            # only its observed launcher shape: a native binary in argv[0],
            # or a Python interpreter with the exact console-script path in
            # argv[1]. Do not accept an arbitrary later argument/source string.
            executable_name = Path(executable).name.lower()
            # Accept the documented CLI binary and its updater naming shape,
            # but not an arbitrary executable which merely contains ``grok``
            # somewhere in its filename.
            is_grok = executable_name == "grok" or executable_name.startswith("grok-")
            argv = proc.cmdline()
            argv0 = os.path.basename(argv[0]) if argv else ""
            is_cao_mcp = (argv0 == "cao-mcp-server" and executable_name == "cao-mcp-server") or (
                len(argv) >= 2
                and argv0.startswith("python")
                and os.path.basename(argv[1]) == "cao-mcp-server"
            )
            if not (is_grok or is_cao_mcp):
                return False
            environ = proc.environ()
        except psutil.NoSuchProcess:
            return False
        except psutil.AccessDenied as exc:
            logger.warning("Cannot inspect process %s before Grok cleanup: %s", pid, exc)
            return None
        except psutil.Error as exc:
            logger.warning("Cannot inspect process %s before Grok cleanup: %s", pid, exc)
            return None
        return proc if environ.get("GROK_HOME") == str(home) else False

    @classmethod
    def _stop_home_processes(cls, home: Path) -> bool:
        """Stop residual processes before removing a private Grok home.

        The normal terminal path kills the tmux window first.  This additional
        confirmation handles Grok's updater, which may have escaped that pane
        process group.  On any uncertainty retain the private directory for a
        later cleanup rather than racing a live process and leaking it again.
        """

        def stop(pids: set[int], sig: signal.Signals) -> bool:
            delivered = True
            for pid in pids:
                inspected = cls._inspect_home_process(pid, home)
                if inspected is None:
                    delivered = False
                    continue
                if inspected is False:
                    continue
                try:
                    inspected.send_signal(sig)
                except psutil.NoSuchProcess:
                    continue
                except psutil.Error as exc:
                    logger.warning("Cannot signal Grok-home process %s for %s: %s", pid, home, exc)
                    delivered = False
            return delivered

        pids = cls._pids_using_home(home)
        if pids is None:
            return False
        if not pids:
            return True
        logger.info("Stopping residual Grok processes %s before cleanup of %s", sorted(pids), home)
        if not stop(pids, signal.SIGTERM):
            return False

        deadline = time.monotonic() + _HOME_PROCESS_TERM_GRACE_SECONDS
        while time.monotonic() < deadline:
            remaining = cls._pids_using_home(home)
            if remaining is None:
                return False
            if not remaining:
                return True
            time.sleep(0.05)

        pids = cls._pids_using_home(home)
        if pids is None:
            return False
        if not pids:
            return True
        logger.warning("Grok-home processes did not exit after SIGTERM: %s", sorted(pids))
        if not stop(pids, signal.SIGKILL):
            return False

        deadline = time.monotonic() + _HOME_PROCESS_KILL_GRACE_SECONDS
        while time.monotonic() < deadline:
            remaining = cls._pids_using_home(home)
            if remaining is None:
                return False
            if not remaining:
                return True
            time.sleep(0.05)
        remaining = cls._pids_using_home(home)
        logger.warning(
            "Retaining Grok home %s because processes remain alive or cannot be inspected: %s",
            home,
            sorted(remaining) if remaining is not None else "unknown",
        )
        return False

    def cleanup(self) -> bool:
        """Remove this terminal's private home when no owner can recreate it.

        ``False`` is intentionally a retryable outcome, not an exception: the
        caller must retain terminal metadata and the provider mapping so a
        subsequent DELETE can finish cleanup after a protected/orphaned process
        becomes inspectable or exits.
        """
        self._initialized = False
        # The provider object is reconstructed after a cao-server restart, so
        # `_grok_home` only describes the happy in-process lifecycle.  The
        # terminal id deterministically identifies the CAO-owned directory and
        # lets cleanup recover that state without persisting credentials.
        home = self._grok_home or self._home_path()
        if not self._is_managed_home(home):
            logger.warning("Refusing to remove non-managed Grok home %s", home)
            return False
        if not self._stop_home_processes(home):
            # A later terminal deletion/retry can safely revisit the exact
            # deterministic path.  Do not erase it while a process can still
            # recreate private files after cleanup.
            return False
        try:
            # rmtree refuses a symlink root, which is safe but leaves the
            # managed entry behind.  Removing the link itself never follows
            # its target; this is also the same property that protects the
            # auth.json symlink inside a normal managed home.
            if home.is_symlink():
                home.unlink()
            else:
                shutil.rmtree(home)
        except FileNotFoundError:
            self._grok_home = None
            self._grok_home_root = None
            return True
        except OSError as exc:
            logger.warning("Failed to remove Grok home %s: %s", home, exc)
            return False
        else:
            self._grok_home = None
            self._grok_home_root = None
            return True

    def mark_input_received(self) -> None:
        super().mark_input_received()
        self._turns += 1
        self._awaiting_turn_activity = True
        self._turn_activity_seen = False
