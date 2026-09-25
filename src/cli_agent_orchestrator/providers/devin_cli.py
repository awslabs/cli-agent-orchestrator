"""Devin CLI provider implementation."""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import tempfile
from pathlib import Path
from typing import Optional

from cli_agent_orchestrator.agent_plugins.mcp_delivery import with_plugin_mcp as _with_plugin_mcp
from cli_agent_orchestrator.constants import SECURITY_PROMPT
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.mcp_resolution import resolve_mcp_server_config
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status

logger = logging.getLogger(__name__)

ANSI_CODE_PATTERN = r"\x1b\[[0-?]*[ -/]*[@-~]"
OSC_PATTERN = r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
CONTROL_CHARS_PATTERN = r"[\x00-\x08\x0b-\x1f\x7f]"

# Devin TUI layout:
#   > user message        <- user input prefix
#   Response text         <- agent reply
#   ────────────────────  <- horizontal rule (U+2500–U+257F)
#   #                     <- input prompt (fixed chrome — NEVER disappears)
#   ────────────────────  <- horizontal rule
#   Mode: ... Model: ...  <- status bar

STATUS_BAR_PATTERN = r"Mode:.*Model:"

# Horizontal rule: one or more chars in Unicode box-drawing range U+2500–U+257F
HORIZONTAL_RULE_PATTERN = r"^[\u2500-\u257f]{3,}"

# User input lines are prefixed with "> " (with content after the space).
USER_INPUT_PATTERN = r"^>\s+\S"

# Devin shows a "#" prompt when idle and waiting for input
IDLE_PROMPT_PATTERN = r"^[\s]*#[\s]*$"

# Sentinel marking "no prior entry under this name" in _mcp_owned_servers.
_PRIOR_ABSENT = object()

# Processing state indicators (take priority over the fixed `#` prompt)
PROCESSING_PATTERNS = [
    r"Running tools",
    r"esc to interrupt",
    r"Running:",
    r"Executing:",
    r"Reading file",
    r"Writing to",
    r"Editing file",
]

# Explicit error indicators from Devin CLI or the underlying runtime.
# These are matched only when the TUI prompt is not visible, to avoid
# treating an agent response that mentions an error as a failure.
ERROR_PATTERNS = [
    r"^Error:",
    r"^Traceback \(most recent call last\):",
    r"^panic:",
    r"^(?:\s*)?(?:Devin CLI )?(?:authentication|login|credentials?|auth).{0,20}(?:failed|invalid|error|denied)",
    r"Devin CLI (?:crashed|exited|failed|error)",
]


class DevinCliProvider(BaseProvider):
    """Provider for Devin CLI (https://cli.devin.ai/)."""

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        agent_profile: Optional[str] = None,
        allowed_tools: Optional[list] = None,
        skill_prompt: Optional[str] = None,
    ):
        """Initialize provider with terminal context."""
        super().__init__(terminal_id, session_name, window_name, allowed_tools, skill_prompt)
        self._initialized = False
        self._agent_profile = agent_profile
        self._temp_prompt_file: Optional[str] = None
        self._mcp_config_path: Optional[Path] = None
        # name -> (entry we wrote, entry that was there before — _PRIOR_ABSENT
        # when the name did not exist). Ownership cannot be inferred from the
        # name alone on a shared file, so both halves are recorded.
        self._mcp_owned_servers: dict = {}
        self._mcp_file_preexisted: bool = False
        self._cached_profile: Optional[AgentProfile] = None

    def _load_profile(self) -> Optional[AgentProfile]:
        """Load and cache the agent profile, logging failures clearly.

        The profile is needed by both ``_build_command()`` (path maps,
        prompt/config) and ``initialize()`` (init timeout), so it is cached
        after the first successful load.
        """
        if self._agent_profile is None:
            return None
        if self._cached_profile is not None:
            return self._cached_profile

        try:
            self._cached_profile = _with_plugin_mcp(
                load_agent_profile(self._agent_profile), "devin_cli"
            )
        except Exception as e:
            logger.warning(
                "Failed to load agent profile '%s': %s",
                self._agent_profile,
                e,
            )
            raise RuntimeError(f"Failed to load agent profile '{self._agent_profile}': {e}") from e
        return self._cached_profile

    @property
    def paste_enter_count(self) -> int:
        """Devin CLI needs a single Enter after pasted input."""
        return 1

    @property
    def use_paste_buffer(self) -> bool:
        """Devin CLI doesn't support paste-buffer - use send-keys instead."""
        return False

    @staticmethod
    def _clean(output: str) -> str:
        cleaned = (output or "").replace("\r\n", "\n").replace("\r", "\n")
        # Remove ANSI codes and OSC sequences
        cleaned = re.sub(ANSI_CODE_PATTERN, "", cleaned)
        cleaned = re.sub(OSC_PATTERN, "", cleaned)
        cleaned = re.sub(CONTROL_CHARS_PATTERN, "", cleaned)
        return cleaned

    def _cleanup_temp_files(self) -> None:
        """Clean up any existing temporary files before creating new ones."""
        for attr in ("_temp_prompt_file",):
            path = getattr(self, attr)
            if path:
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError as e:
                    logger.warning("Failed to delete temp file %s: %s", path, e)
                    # Keep the path recorded so a later cleanup can retry.
                    continue
                setattr(self, attr, None)

    def _build_security_constraint(self) -> str:
        """Build security constraint prompt for allowed tools."""
        if self._allowed_tools is None:
            return ""
        tools_list = ", ".join(self._allowed_tools)
        return (
            f"{SECURITY_PROMPT}\n"
            f"## ALLOWED TOOLS\n"
            f"You are restricted to only use the following tools: {tools_list}\n"
        )

    def _write_temp_file(self, content: str, prefix: str, suffix: str) -> str:
        """Write ``content`` to a securely created temporary file and return its path."""
        fd: Optional[int] = None
        path: Optional[str] = None
        try:
            fd, path = tempfile.mkstemp(prefix=prefix, suffix=suffix)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                fd = None
                f.write(content)
            # mkstemp creates the file with a restrictive mode on POSIX, but
            # enforce 0o600 explicitly for portability (including Windows).
            os.chmod(path, 0o600)
            return path
        except Exception:
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass
            raise
        finally:
            if fd is not None:
                os.close(fd)

    def _write_prompt_file(self, content: str) -> None:
        """Write prompt content to a temporary file and store the path."""
        self._temp_prompt_file = self._write_temp_file(
            content,
            prefix="cao_devin_prompt_",
            suffix=".md",
        )

    def _terminal_workdir(self) -> Path:
        """The directory Devin launches in — where it discovers ``.devin/`` config."""
        try:
            from cli_agent_orchestrator.clients import database as _db

            metadata = _db.get_terminal_metadata(self.terminal_id) or {}
            workdir = metadata.get("working_directory")
            if workdir:
                return Path(workdir)
        except Exception as exc:
            logger.debug("Could not resolve working directory for %s: %s", self.terminal_id, exc)
        return Path.cwd()

    def _deliver_mcp_servers(self, mcp_servers: dict) -> None:
        """Write profile/plugin MCP servers to ``.devin/mcp_config.local.json``.

        Since Devin CLI v3000.3, MCP servers are discovered only from the
        dedicated ``mcp_config`` files (user ``~/.config/devin/mcp_config.json``,
        project ``.devin/mcp_config.json``, local ``.devin/mcp_config.local.json``);
        ``--config`` overrides the main settings file and ignores ``mcpServers``
        there (verified against 3000.11.3). The local project file is the
        supported per-workspace delivery path — it is gitignored by convention
        and outranks the other two scopes.

        ``CAO_TERMINAL_ID`` is emitted as a ``${env:...}`` reference, which Devin
        expands from the spawned server's environment — the pane environment
        carries the real value, so one shared file still stamps each terminal's
        own identity into its own servers.
        """
        config_path = self._terminal_workdir() / ".devin" / "mcp_config.local.json"

        base: dict = {}
        self._mcp_file_preexisted = config_path.is_file()
        if self._mcp_file_preexisted:
            try:
                loaded = json.loads(config_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    base = loaded
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Could not read existing %s: %s", config_path, e)

        existing = base.get("mcpServers")
        prior_servers = dict(existing) if isinstance(existing, dict) else {}
        self._merge_mcp_servers(base, mcp_servers)

        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(base, indent=2) + "\n", encoding="utf-8")
        os.chmod(config_path, 0o600)
        self._mcp_config_path = config_path
        delivered = base["mcpServers"]
        for name in mcp_servers:
            if name in delivered:
                self._mcp_owned_servers[name] = (
                    delivered[name],
                    prior_servers.get(name, _PRIOR_ABSENT),
                )

    @staticmethod
    def _is_cao_owned_entry(entry: object) -> bool:
        """Whether an MCP entry was written by CAO (it carries our env marker)."""
        return isinstance(entry, dict) and "CAO_TERMINAL_ID" in (entry.get("env") or {})

    def _has_live_siblings(self, workdir: Path) -> bool:
        """Whether another live CAO terminal launches in ``workdir``.

        A sibling's MCP entries in the shared file can be byte-identical to
        ours, so the file must be left alone while one is alive — entry values
        carry no per-terminal identity by which to split ownership.
        """
        try:
            from cli_agent_orchestrator.clients import database as _db

            terminals = _db.list_all_terminals()
        except Exception as exc:
            logger.debug("Could not list terminals for %s: %s", self.terminal_id, exc)
            return False
        target = str(workdir)
        return any(
            isinstance(t, dict)
            and t.get("terminal_id") != self.terminal_id
            and t.get("working_directory") == target
            for t in terminals
        )

    def _cleanup_mcp_config(self) -> None:
        """Restore the delivered MCP config to what it was before this terminal.

        Removes entries this terminal created and restores entries it
        overwrote — an operator-authored server under a name we reused must
        come back, not vanish with our cleanup. An entry someone else rewrote
        since delivery is left alone: it is no longer ours to restore. While
        another live terminal shares the working directory the file is left
        untouched entirely, because its entries may be serving the sibling.
        """
        config_path = self._mcp_config_path
        owned = self._mcp_owned_servers
        self._mcp_config_path = None
        self._mcp_owned_servers = {}
        if config_path is None or not owned:
            return
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            servers = data.get("mcpServers")
            if not isinstance(servers, dict):
                return
            if self._has_live_siblings(config_path.parent.parent):
                return
            for name, (written, prior) in owned.items():
                if servers.get(name) != written:
                    continue
                if prior is _PRIOR_ABSENT or self._is_cao_owned_entry(prior):
                    servers.pop(name, None)
                else:
                    servers[name] = prior
            if servers or self._mcp_file_preexisted:
                config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            else:
                config_path.unlink(missing_ok=True)
                try:
                    config_path.parent.rmdir()
                except OSError:
                    pass
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Failed to clean MCP config %s: %s", config_path, e)

    # CAO ``streamable-http`` maps to Devin's ``http`` transport; ``sse`` and
    # ``http`` pass through unchanged (``mcp_config`` accepts ``"http"|"sse"``).
    _DEVIN_TRANSPORTS = {"streamable-http": "http", "http": "http", "sse": "sse"}

    def _normalize_mcp_server_for_devin(self, resolved: dict) -> dict:
        """Translate a CAO MCP server config into Devin CLI's ``mcp_config`` schema.

        Devin's dedicated ``mcp_config*.json`` files expect ``command``/``args``/
        ``env`` for stdio servers and ``url``/``transport`` for remote servers.
        CAO-specific keys such as ``type`` and ``timeout`` are dropped to avoid
        confusing the CLI.
        """
        normalized: dict = {}
        if resolved.get("url"):
            normalized["url"] = resolved["url"]
            transport = resolved.get("transport") or resolved.get("type") or "http"
            normalized["transport"] = self._DEVIN_TRANSPORTS.get(transport, transport)
            if resolved.get("headers"):
                normalized["headers"] = dict(resolved["headers"])
            for key in ("oauthClientId", "oauthClientSecret", "oauthResource"):
                if key in resolved and resolved[key] is not None:
                    normalized[key] = resolved[key]
        else:
            command = resolved.get("command")
            if command:
                normalized["command"] = command
            if resolved.get("args"):
                normalized["args"] = list(resolved["args"])

            env = resolved.get("env") or {}
            if not isinstance(env, dict):
                env = {}
            if "CAO_TERMINAL_ID" not in env:
                # An ${env:...} reference rather than a literal: the pane
                # environment carries the real value, so one shared config file
                # still delivers each terminal's own id to its own servers.
                env["CAO_TERMINAL_ID"] = "${env:CAO_TERMINAL_ID}"
            if env:
                normalized["env"] = env

        if resolved.get("disabled"):
            normalized["disabled"] = True

        return normalized

    def _merge_mcp_servers(self, base_config: dict, mcp_servers: dict) -> None:
        """Merge profile MCP servers into a ``{"mcpServers": {...}}`` document."""
        # Ensure mcpServers is a dict in base_config
        if not isinstance(base_config.get("mcpServers"), dict):
            base_config["mcpServers"] = {}

        existing_mcp = base_config.get("mcpServers", {})
        for server_name, server_config in mcp_servers.items():
            if isinstance(server_config, dict):
                resolved = resolve_mcp_server_config(dict(server_config))
            else:
                resolved = resolve_mcp_server_config(server_config.model_dump(exclude_none=True))
            existing_mcp[server_name] = self._normalize_mcp_server_for_devin(resolved)
        base_config["mcpServers"] = existing_mcp

    def _build_command(self) -> str:
        """Build Devin CLI command with agent profile if provided.

        Returns properly escaped shell command string for tmux.
        """
        self._cleanup_temp_files()

        command_parts = ["devin"]

        # Load the agent profile (cached) so we can use it for path translation
        # and prompt/config construction below.
        profile = self._load_profile()

        # Only use dangerous permission mode when allowed_tools is unrestricted
        # This follows the pattern of other providers (e.g., kiro_cli.py:250)
        if self._allowed_tools is not None and "*" in self._allowed_tools:
            command_parts.extend(
                [
                    "--permission-mode",
                    "dangerous",
                    "--respect-workspace-trust",
                    "false",
                ]
            )

        # Handle allowed_tools restrictions
        if self._allowed_tools is not None and "*" not in self._allowed_tools:
            security_constraint = self._build_security_constraint()
            self._write_prompt_file(security_constraint)
            assert self._temp_prompt_file is not None
            command_parts.extend(["--prompt-file", self._temp_prompt_file])

        if profile is not None:
            # Devin supports --prompt-file for system prompt injection
            system_prompt = profile.system_prompt if profile.system_prompt else ""
            # Apply skill prompt if provided
            system_prompt = self._apply_skill_prompt(system_prompt)
            if system_prompt:
                # If we already have a prompt-file from allowed_tools, append the system prompt AFTER security constraint
                if self._temp_prompt_file:
                    with open(self._temp_prompt_file, "r", encoding="utf-8") as f:
                        existing_content = f.read()
                    combined_prompt = f"{existing_content}\n\n{system_prompt}"
                    with open(self._temp_prompt_file, "w", encoding="utf-8") as f:
                        f.write(combined_prompt)
                else:
                    self._write_prompt_file(system_prompt)
                    assert self._temp_prompt_file is not None
                    command_parts.extend(["--prompt-file", self._temp_prompt_file])

            # Deliver MCP servers through the dedicated project-local file.
            # Since Devin CLI v3000.3, ``--config`` overrides the main settings
            # file and cannot carry ``mcpServers`` — only the dedicated
            # ``mcp_config`` files are consulted.
            if profile.mcpServers:
                self._deliver_mcp_servers(profile.mcpServers)

        # For containerized profiles, translate host temp-file paths to guest paths.
        if (
            profile is not None
            and getattr(profile, "container", None) is not None
            and isinstance(profile.container.path_maps, list)
            and profile.container.path_maps
        ):
            for i, part in enumerate(command_parts):
                if i > 0 and command_parts[i - 1] in ("--prompt-file",):
                    command_parts[i] = self._translate_path(part, profile)

        return shlex.join(command_parts)

    async def initialize(self) -> bool:
        """Initialize Devin CLI provider."""
        try:
            # Wait for shell prompt to appear in the tmux window
            if not await wait_for_shell(self.terminal_id, timeout=10.0):
                raise TimeoutError("Shell initialization timed out after 10 seconds")

            command = self._build_command()
            from cli_agent_orchestrator.backends.registry import get_backend
            from cli_agent_orchestrator.services.status_monitor import status_monitor

            # Arm the StatusMonitor stickiness gate before launching the CLI so
            # the PROCESSING and IDLE/COMPLETED transitions during init are
            # honored past any previously-latched ready state.
            status_monitor.notify_input_sent(self.terminal_id)
            get_backend().send_keys(
                self.session_name,
                self.window_name,
                command,
                use_paste_buffer=self.use_paste_buffer,
            )

            # Resolve the initialization timeout from the profile or server settings.
            profile = self._load_profile()
            init_timeout = float(self.get_init_timeout(profile))

            if not await wait_until_status(
                self.terminal_id,
                {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
                timeout=init_timeout,
            ):
                raise TimeoutError(
                    f"Devin CLI initialization timed out after {init_timeout} seconds"
                )

            self._initialized = True
            return True
        finally:
            # The prompt temp file has been consumed by the Devin CLI on
            # successful initialization (or the launch was aborted); remove it
            # to avoid leaving security constraints on disk. The delivered MCP
            # config persists for the life of the session — Devin re-reads it
            # when spawning servers — and is removed by cleanup().
            self._cleanup_temp_files()

    @staticmethod
    def _is_processing(lines: list[str]) -> bool:
        """Return True if an active processing spinner/status is visible.

        Only the most recent viewport lines are checked, and each pattern must
        appear at the start of a line.  This prevents a completed response that
        happens to contain phrases like "Reading file" from keeping the state
        stuck in PROCESSING.
        """
        for line in lines[-10:]:
            stripped = line.strip()
            for pattern in PROCESSING_PATTERNS:
                if re.match(pattern, stripped, re.IGNORECASE):
                    return True
        return False

    @staticmethod
    def _find_last_input_prompt(lines: list[str]) -> Optional[int]:
        """Return the index of the last active `#` prompt, or None.

        The Devin TUI always places a horizontal rule immediately before the `#`
        prompt.  Requiring this context avoids false positives from Markdown
        headings (e.g. ``# Title``) that appear inside agent responses.
        """
        tail = lines[-20:]
        for idx in range(len(tail) - 1, -1, -1):
            line = tail[idx]
            if not re.match(IDLE_PROMPT_PATTERN, line):
                continue
            # Verify the closest preceding non-empty line is a horizontal rule.
            preceding = [line for line in tail[:idx] if line.strip()]
            if preceding and re.match(HORIZONTAL_RULE_PATTERN, preceding[-1].strip()):
                return len(lines) - len(tail) + idx
        return None

    @staticmethod
    def _has_user_input(lines: list[str]) -> bool:
        """Return True if at least one user-input line (`> text`) is visible."""
        for line in lines:
            if re.match(USER_INPUT_PATTERN, line):
                return True
        return False

    @staticmethod
    def _is_error(lines: list[str]) -> bool:
        """Return True if the output contains an explicit error/crash indicator."""
        combined = "\n".join(lines[-50:])
        for pattern in ERROR_PATTERNS:
            if re.search(pattern, combined, re.IGNORECASE | re.MULTILINE):
                return True
        return False

    def get_status(self, buffer: str) -> TerminalStatus:
        """Detect Devin CLI state from terminal output.

        Args:
            buffer: Raw terminal output buffer from pipe-pane

        Returns:
            TerminalStatus based on pattern matching
        """
        native = self._resolve_native_status(buffer)
        if native is not None:
            return native

        # herdr never pushes a buffer (pipe-pane is a no-op); read live pane
        # content so pattern matching runs against real output instead of
        # returning UNKNOWN on an empty pushed buffer.
        buffer = self._resolve_buffer(buffer)
        if not buffer:
            return TerminalStatus.UNKNOWN

        # Strip ANSI codes for clean matching
        clean_output = self._clean(buffer)

        if not clean_output.strip():
            return TerminalStatus.UNKNOWN

        lines = clean_output.splitlines()

        # 1. Active spinner / status indicators take priority over the fixed
        # input prompt.  Devin can display a prompt line while a "Running tools"
        # spinner is still visible above it.
        if self._is_processing(lines):
            return TerminalStatus.PROCESSING

        # 2. Find the active # prompt.  If a crash or explicit error appears
        # after it, the prompt is stale from an earlier turn and the current
        # state is ERROR.  Otherwise the prompt means the turn finished.
        prompt_idx = self._find_last_input_prompt(lines)
        if prompt_idx is not None:
            if self._is_error(lines[prompt_idx + 1 :]):
                return TerminalStatus.ERROR
            # Check for user input to distinguish IDLE from COMPLETED.
            # If a task was dispatched and the user-input line has scrolled out
            # of the buffer, the visible prompt means completion.
            if self._has_user_input(lines) or self._task_dispatched:
                return TerminalStatus.COMPLETED
            return TerminalStatus.IDLE

        # 3. With no active prompt and no active spinner, explicit Devin CLI /
        # runtime crashes are reported as ERROR.
        if self._is_error(lines):
            return TerminalStatus.ERROR

        # 4. Initial Devin CLI welcome screen (before first # prompt)
        # Look for "Ask Devin to build features", "I'm ready to help", or "SWE-1.6"
        if (
            "Ask Devin to build features" in clean_output
            or "I'm ready to help" in clean_output
            or "SWE-1.6" in clean_output
        ):
            return TerminalStatus.IDLE

        # 5. Ambiguous output (no prompt, no processing, no error): keep polling.
        return TerminalStatus.UNKNOWN

    def get_idle_pattern_for_log(self) -> str:
        return IDLE_PROMPT_PATTERN

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract agent response between last user-input line and horizontal rule."""
        clean_output = self._clean(script_output)
        lines = clean_output.splitlines()

        # Find the last user-input line ("> text")
        last_user_idx = -1
        for idx, line in enumerate(lines):
            if re.match(USER_INPUT_PATTERN, line):
                last_user_idx = idx

        if last_user_idx < 0:
            raise ValueError("No user input found")

        # Collect lines between the last user input and the next horizontal rule
        # that is immediately followed by the standalone `#` input prompt.
        # A box-drawing separator inside the response is not a terminator.
        # The status bar is an additional fallback terminator.
        response_lines = []
        remaining = lines[last_user_idx + 1 :]
        for i, line in enumerate(remaining):
            if re.search(STATUS_BAR_PATTERN, line):
                break
            if re.match(HORIZONTAL_RULE_PATTERN, line.strip()):
                following = [ln for ln in remaining[i + 1 :] if ln.strip()]
                if following and re.match(IDLE_PROMPT_PATTERN, following[0]):
                    break
            # Preserve all lines including empty ones for paragraph formatting
            response_lines.append(line)

        if not response_lines:
            raise ValueError("No response found")

        return "\n".join(response_lines).strip()

    def exit_cli(self) -> str:
        return "/exit"

    def cleanup(self) -> None:
        """Clean up temp files and delivered MCP config entries."""
        self._cleanup_temp_files()
        self._cleanup_mcp_config()
