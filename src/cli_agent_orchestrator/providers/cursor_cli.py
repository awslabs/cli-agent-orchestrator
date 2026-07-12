"""Cursor CLI provider implementation.

This module provides the CursorCliProvider class for integrating with the
Cursor CLI (https://cursor.com/cli), Anysphere's terminal-native AI coding
assistant. The CLI is invoked via the ``cursor-agent`` binary (preferred;
unambiguous name shared only with the Cursor CLI) and falls back to the
primary ``agent`` name (Cursor's documented top-level command per
https://cursor.com/docs/cli/overview) when the legacy alias is missing.
When the primary ``agent`` name is selected the provider runs a
``agent --version`` probe to confirm the resolved binary is the Cursor
CLI (a number of unrelated tools also install an ``agent`` binary).

Cursor CLI v2026.06.15 (the version ``curl https://cursor.com/install | bash``
ships today) has dropped or moved several flags the provider previously
relied on. The v2026 launch command the provider builds:

- ``--force`` auto-approves tool calls so the agent does not block on
  per-tool approval prompts during orchestration.
- ``--model`` selects a specific model (e.g. ``gpt-5``, ``sonnet-4``,
  ``composer-2.5``).
- ``--plugin-dir <path>`` injects MCP server configuration. v2026
  removed the ``--mcp <json>`` flag in favour of ``--plugin-dir``
  pointing at a directory holding Cursor plugin manifests. The
  provider synthesises that directory from the agent profile's
  ``mcpServers`` map at launch time.
- ``--approve-mcps`` pre-approves MCP servers declared via
  ``--plugin-dir`` so the REPL does not block on a per-server
  approval dialog.

Flags deliberately omitted for v2026.06.15:

- ``--system-prompt <file>`` is omitted because v2026.06.15's backend
  rejects every request that carries a ``--system-prompt <file>``
  payload with ``[invalid_argument] unknown option '--system-prompt'``
  regardless of the file's contents (a 3-character file reproduces
  the bug). Multi-turn inbox still works because the CAO role context
  reaches the agent via the ``cao-mcp-server`` MCP tool's handoff /
  assign payloads. The preserved ``_write_system_prompt_file`` helper
  is ready to re-enable this path when Cursor ships a fixed client.
- ``--agent <name>`` is omitted because v2026 removed the flag
  (issue #300).
- ``--trust`` is omitted because v2026 rejects it in interactive
  REPL mode ("only works with --print/headless mode").
- ``--mcp <json>`` is replaced by ``--plugin-dir`` as noted above.

Skill catalog injection: the provider forwards the skill catalog via
the shared ``_apply_skill_prompt`` helper from :class:`BaseProvider`
to the (currently disabled) system prompt path. When ``--system-prompt``
is re-enabled, the catalog will be appended at launch.

Status detection is pattern-based. v2026+ uses a full Ink/TUI; the
provider detects the "ctrl+c to stop" hint Cursor renders on the
input-box line every frame the agent is working on a turn. Older
text-mode builds are still classified by the legacy ``❯`` prompt
and ``─────`` separator regex suite (mirrors the Claude Code
provider's structural fix for the stale-spinner bug).

Status detection is pattern-based. Cursor CLI uses an Ink-style interactive
prompt (``❯``) for IDLE / COMPLETED, spinner text with ellipsis for
PROCESSING, and a structural "thinking-before-separator" check borrowed
from the Claude Code provider to avoid stale-spinner false positives.
"""

import json
import logging
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.services.settings_service import get_server_settings
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.mcp_resolution import resolve_mcp_server_config
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status
from cli_agent_orchestrator.utils.text import strip_terminal_escapes

logger = logging.getLogger(__name__)


class ProviderError(Exception):
    """Exception raised for provider-specific errors."""

    pass


# =============================================================================
# Regex Patterns for Cursor CLI Output Analysis
# =============================================================================

# ANSI escape code pattern for stripping terminal colors.
ANSI_CODE_PATTERN = r"\x1b\[[0-9;]*m"

# Cursor CLI uses the same spinner glyph vocabulary as Claude Code while
# generating a response. Match a spinner char + text + ellipsis on a single
# line. Examples: "⠋ Thinking…", "✶ Reasoning… (esc to interrupt)".
PROCESSING_PATTERN = r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏✶✢✽✻✳·][^\n]*\u2026"

# Cursor CLI's REPL prompt is a "❯" character (right arrow) with optional
# space / non-breaking space, identical in shape to Claude Code's prompt.
# The pattern is anchored to the start of a line (with optional SGR colour
# codes before the prompt) so it does NOT match the leading "❯ " on echoed
# user input lines (e.g. "❯ Summarize…") or any "> " inside response
# content. This matches the claude_code provider's _SOL_IDLE_RE pattern.
IDLE_PROMPT_PATTERN = r"^\s*(?:\x1b\[[0-9;]*m)*[❯>](?:\x1b\[[0-9;]*m)*[\s\xa0]"

# Same pattern for log files (no ANSI involved). Still start-of-line
# anchored so the log pre-check is consistent with live status detection.
IDLE_PROMPT_PATTERN_LOG = r"^\s*[❯>][\s\xa0]"

# Footer shown while a TUI selection widget is active (mode picker, model
# picker, file completion overlay).
WAITING_USER_ANSWER_PATTERN = r"↑/↓ to navigate"

# v2026+ TUI processing indicator. When the agent is actively working
# on a turn, Cursor renders the input-box line with the placeholder
# text followed by "ctrl+c to stop" on the same line — this is the
# "you can interrupt me with Ctrl+C" hint. The indicator is absent
# on every other state (idle, completed, freshly launched, brand-new
# TUI). The placeholder itself ("Add a follow-up" / "Plan, search,
# build anything") is ALWAYS present in v2026 regardless of state,
# so it cannot distinguish idle from processing on its own — the
# presence / absence of "ctrl+c to stop" is the reliable signal.
TUI_PROCESSING_INDICATOR_PATTERN = r"ctrl\+c to stop"

# Workspace trust dialog (Cursor asks once per directory the first time the
# agent is launched there).
TRUST_PROMPT_PATTERN = r"do you trust (?:the )?files? in this folder|confirm folder trust"

# Permission / approval dialog that appears when the agent wants to run a
# shell command or edit a file without ``--force`` enabled.
PERMISSION_PROMPT_PATTERN = (
    r"(?:do you want to (?:allow|run)|approve this action|\[\s*y\s*/\s*n\s*\])"
)

# Separator regex. Matches a contiguous run of at least 20 box-drawing
# horizontal characters (U+2500), with an optional CSI escape between any
# two consecutive dashes. This is the correct "CSI interleaved with the
# separator" pattern: Cursor's TUI re-renders the separator in place
# with new colour escapes on every prompt, so the byte stream looks
# like:
#   ─\x1b[0m──\x1b[38;5;245m──\x1b[0m──…
# The pattern is anchored to a full line (``^…$``) so we don't match
# a stray dash sequence inside response content.
# Intermediate bytes are restricted to the ECMA-48 param range
# (0x30-0x3F) so a stray ``ESC [`` introducer is not consumed.
SEPARATOR_PATTERN = (
    r"^(?:\x1b\[[\x30-\x3F]*[\x40-\x7E])?(?:\u2500(?:\x1b\[[\x30-\x3F]*[\x40-\x7E])?){20,}$"
)

# Cursor CLI v2026+ runs a full Ink/TUI in interactive mode. The text-mode
# `❯` prompt and the `─────` separator are no longer emitted as text in
# the pipe-pane buffer — they are rendered as TUI widgets. The only
# stable, plain-text signal the TUI emits into the scrollback is the
# input-box placeholder plus the status bar ("Composer 2.5 Fast" /
# "Run Everything"). Cursor REPLACES the placeholder with the user's
# text on submit and only redraws it once the agent has finished a
# turn, so the presence/absence of this placeholder is a reliable
# idle/processing signal even though the placeholder itself is no
# longer "an idle prompt" in the text-mode sense.
#
# The exact placeholder text varies by conversation state:
#
#   * fresh launch, no prior turns:    ``Plan, search, build anything``
#   * after the first user turn:       ``Add a follow-up``
#
# Both are emitted as a single string in the input-box line; either
# indicates the input box is empty and the agent is ready for the
# next turn. We match either of them. The pattern is intentionally
# case-insensitive: a few builds render "plan" lower-case depending
# on the locale.
TUI_PLACEHOLDER_PATTERN = r"(?:Plan, search, build anything|Add a follow-up)"
TUI_STATUS_BAR_PATTERN = r"Run Everything|Composer \d"


class CursorCliProvider(BaseProvider):
    """Provider for the Cursor CLI (``agent`` / ``cursor-agent``).

    The provider launches Cursor with the primary ``agent`` command
    (Cursor's documented top-level entrypoint per
    https://cursor.com/docs/cli/overview). The ``cursor-agent`` alias is
    still shipped for backward compatibility and resolves to the same
    binary.

    Manages the lifecycle of a Cursor CLI REPL session inside a tmux
    window: initialization, status detection, response extraction, and
    cleanup.

    Attributes:
        terminal_id: Unique identifier for this terminal instance.
        session_name: Name of the tmux session containing this terminal.
        window_name: Name of the tmux window for this terminal.
        _agent_profile: Optional Cursor agent name (e.g. ``"developer"``).
        _model: Optional model override forwarded as ``--model``.
    """

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
        """Initialize the Cursor CLI provider.

        Args:
            terminal_id: Unique identifier for this terminal.
            session_name: Name of the tmux session.
            window_name: Name of the tmux window.
            agent_profile: Optional Cursor agent name (e.g. ``"developer"``).
            allowed_tools: Optional list of CAO tool names the agent is
                allowed to use. Cursor CLI does not expose a native
                ``--disallowedTools`` flag, so restrictions are enforced
                softly via the ``SECURITY_PROMPT`` (see
                :data:`cli_agent_orchestrator.constants.SECURITY_PROMPT`).
            model: Optional model override (e.g. ``"gpt-5"``, ``"sonnet-4"``).
            skill_prompt: Optional skill catalog text built by the service
                layer. Appended to the system prompt at launch.
        """
        super().__init__(terminal_id, session_name, window_name, allowed_tools, skill_prompt)
        self._initialized = False
        self._agent_profile = agent_profile
        self._model = model
        # Temp paths the provider has created under the CAO tmp dir.
        # ``cleanup()`` deletes every entry in this list so the
        # per-session files (system prompt + plugin dir) do not
        # accumulate on disk. Initialised lazily on the first
        # ``_build_cursor_command`` so providers that never get
        # launched (e.g. because the binary is missing) leave the
        # tmp dir untouched.
        self._tmp_paths: list[Path] = []
        # Turn counter. ``get_status`` returns ``IDLE`` while
        # ``_turns == 0`` (fresh spawn, no user input yet) and
        # ``COMPLETED`` once at least one turn has been delivered
        # and the agent is back in a non-processing state. This
        # lets the UI badge distinguish "just spawned, waiting for
        # first prompt" from "last turn delivered, ready for next".
        # Incremented from :meth:`mark_input_received`, which the
        # terminal service calls after every ``send_input``.
        self._turns: int = 0

    @property
    def paste_enter_count(self) -> int:
        """Cursor CLI submits on a single Enter after bracketed paste."""
        return 1

    def _build_cursor_command(self) -> str:
        """Build the ``agent`` (Cursor CLI) launch command.

        Cursor's primary command per the official documentation is
        ``agent`` (https://cursor.com/docs/cli/overview). The legacy
        ``cursor-agent`` binary resolves to the same REPL; we prefer
        the primary name so newly-installed machines work out of the
        box.

        Flags used (v2026.06.15):
        - ``--force`` auto-approves tool calls so the agent does not
          block on per-tool approval prompts during orchestration.
        - ``--model`` selects a specific model (when configured).
        - ``--system-prompt <path>`` is **deliberately omitted** —
          v2026.06.15 has a confirmed bug where any request that
          carries a ``--system-prompt <file>`` payload is rejected
          by the backend with ``[invalid_argument] unknown option
          '--system-prompt'`` regardless of file contents. The
          CAO role context still reaches the agent via the
          ``cao-mcp-server`` MCP tool's handoff / assign payloads.
        - ``--plugin-dir`` injects MCP server configuration. v2026
          removed the ``--mcp <json>`` flag in favour of
          ``--plugin-dir <path>`` pointing at a directory that holds
          Cursor plugin / MCP server manifests. We synthesise the
          directory from the agent profile's ``mcpServers`` map at
          launch time and keep it under the CAO tmp dir.
        - ``--approve-mcps`` pre-approves MCP servers declared via
          ``--plugin-dir`` so the REPL does not block on a per-server
          approval dialog.

        The CAO agent profile is no longer passed as ``--agent <name>``
        — that flag was removed in v2026. The profile's identity is
        preserved via the system-prompt content (the profile markdown
        body) and the synthesised plugin-dir layout. Multi-agent
        orchestration (handoff / assign) continues to work because the
        inbox / MCP tools are the same across all profiles.

        Returns a properly escaped shell command string suitable for
        :func:`tmux_client.send_keys`. Uses :func:`shlex.join` to handle
        multiline strings and special characters correctly.
        """
        profile = None
        if self._agent_profile is not None:
            try:
                profile = load_agent_profile(self._agent_profile)
            except Exception as exc:
                raise ProviderError(f"Failed to load agent profile '{self._agent_profile}': {exc}")

        # Resolve the binary. ``agent`` is a common name and a
        # number of unrelated tools (e.g. the Linux ``gpg-agent``)
        # install an ``agent`` binary on $PATH; we cannot tell
        # whether a given ``agent`` is Cursor without inspecting it.
        # We prefer the unambiguous ``cursor-agent`` first (which
        # only the Cursor CLI ships) and fall back to the documented
        # primary ``agent`` name when only that is installed. When
        # we do pick the ``agent`` path, we run a quick
        # ``agent --version`` probe to confirm the resolved binary
        # identifies as Cursor — if it does not, we raise
        # ``ProviderError`` so the operator can either uninstall
        # the conflicting tool or symlink the Cursor binary to
        # ``cursor-agent``. The e2e skip fixture in
        # ``test/e2e/conftest.py::require_cursor`` accepts either
        # name, so the launch behaves consistently when the probe
        # passes.
        cursor_agent = shutil.which("cursor-agent")
        generic_agent = shutil.which("agent")
        binary, path = None, None
        if cursor_agent:
            binary, path = "cursor-agent", cursor_agent
        elif generic_agent:
            binary, path = "agent", generic_agent
        else:
            raise ProviderError(
                "Cursor CLI not found: neither 'agent' nor 'cursor-agent' is on $PATH. "
                "Install from https://cursor.com/cli"
            )

        # The ``agent`` binary name is shared with a number of
        # unrelated tools. Validate it really is the Cursor CLI
        # before launching — saves the operator a confusing 500
        # from the spawned tmux pane when the wrong tool rejects
        # the Cursor-only flags. We do the probe via
        # ``subprocess.run`` with a short timeout so a slow or
        # hung binary does not block ``_build_cursor_command``.
        if binary == "agent":
            try:
                probe = subprocess.run(  # noqa: S603 — binary is on $PATH
                    [binary, "--version"],
                    capture_output=True,
                    text=True,
                    timeout=3.0,
                )
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
                raise ProviderError(
                    f"Could not probe '{binary}' at {path}: {exc}. "
                    "If this is a non-Cursor tool installed under the same name, "
                    "either uninstall it or symlink the Cursor binary to 'cursor-agent'."
                ) from exc
            # Cursor's ``agent --version`` prints
            # ``agent <semver>`` (e.g. ``agent 2026.06.15-...``).
            # The version string is the only reliable signal that
            # we can match without hard-coding the binary's
            # internal name; we accept the version line as
            # evidence the resolved binary really is the Cursor
            # CLI. The probe is intentionally lenient: any line
            # that contains the word ``agent`` and a 4-digit year
            # (the semver convention Cursor ships) is treated as a
            # match. If the binary returns a different banner the
            # operator sees a clear error rather than a 500 from
            # the launched agent.
            banner = (probe.stdout or probe.stderr or "").strip()
            if not re.search(r"agent\s+\d{4}\.\d+\.\d+", banner):
                raise ProviderError(
                    f"'{binary}' at {path} does not identify as Cursor CLI "
                    f"(version probe returned: {banner!r}). "
                    "If this is a non-Cursor tool installed under the same name, "
                    "either uninstall it or symlink the Cursor binary to 'cursor-agent'."
                )

        command_parts = [binary]

        # Approval flag. We always pass --force when running under
        # CAO so per-tool approval prompts do not block handoff /
        # assign flows. --trust was removed because v2026 rejects it
        # in interactive REPL mode ("only works with --print/headless
        # mode"); the CAO launch flow already confirms workspace
        # trust, and the interactive REPL has no per-directory trust
        # dialog that --trust would skip.
        command_parts.append("--force")

        # Model override (--model, when explicitly set or supplied
        # by the agent profile's ``model`` field). Profile.model
        # takes precedence when set, then the constructor-provided
        # ``self._model``.
        model = self._model
        if profile is not None and profile.model:
            model = profile.model
        if model:
            command_parts.extend(["--model", model])

        # System prompt injection is intentionally omitted for
        # Cursor CLI v2026.06.15 — the backend (https://agentn.global.api5.cursor.sh)
        # rejects every request that carries a ``--system-prompt <file>``
        # payload with ``[invalid_argument] unknown option
        # '--system-prompt'`` regardless of the file's contents. The bug
        # is reproducible via both ``--print`` and the interactive
        # TUI; the full Cursor log at /tmp/cursor-agent-logs/session-*.log
        # shows the request failing on every retry. See
        # https://github.com/awslabs/cli-agent-orchestrator/issues/299
        # for the upstream investigation.
        #
        # CAO's stance: drop --system-prompt entirely. Multi-turn
        # inbox still works because the CAO role context reaches the
        # agent via the ``cao-mcp-server`` MCP tool's handoff /
        # assign payloads (on the wire, not via Cursor's launch
        # arguments). The agent still has the right capabilities and
        # the right inbox tools; only the role body is not pre-loaded
        # as a system prompt.
        #
        # When Cursor ships a fixed v2026.x client, re-introduce the
        # system-prompt injection here using the preserved
        # ``_write_system_prompt_file`` helper and update the
        # ``extract_last_message_from_script`` docstring to point at
        # the new behaviour.

        # MCP server injection via --plugin-dir. v2026 removed
        # ``--mcp <json>``; the replacement is ``--plugin-dir
        # <path>`` pointing at a directory containing plugin / MCP
        # server manifests. We synthesise the directory from the
        # agent profile's ``mcpServers`` map at launch time, inject
        # CAO_TERMINAL_ID into each server's env so MCP tools can
        # identify the current terminal for handoff / assign
        # operations, and keep the layout under the CAO tmp dir so
        # it is removed with the session.
        if profile is not None and profile.mcpServers:
            plugin_dir = self._write_plugin_dir(profile.mcpServers)
            command_parts.extend(["--plugin-dir", plugin_dir])
            # --approve-mcps is required to skip per-server approval
            # dialogs on first run; otherwise the REPL blocks.
            command_parts.append("--approve-mcps")

        return shlex.join(command_parts)

    def _write_system_prompt_file(self, system_prompt: str) -> str:
        """Write the system prompt to a per-session temp file.

        Cursor CLI v2026.06.15 takes a *file path* for
        ``--system-prompt`` rather than the inline text that older
        builds accepted. The provider used to escape newlines into
        ``\\n`` and pass the prompt as a single shell argument; that
        form now triggers ``error: failed to read --system-prompt
        file: <prompt text>``.

        We write the prompt to a file under the CAO tmp dir keyed by
        the terminal id, return the absolute path, and register the
        file in ``self._tmp_paths`` so ``cleanup()`` deletes it when
        the session is torn down. The file is created lazily on the
        first launch and overwritten on subsequent launches (e.g.
        after a ``reset_buffer`` for a fallback mode).

        Args:
            system_prompt: The fully-resolved system prompt text
                (profile body + skill catalog + optional SECURITY_PROMPT).

        Returns:
            Absolute path to the file. Pass this to
            ``--system-prompt <path>``.
        """
        import os

        prompt_path = self._cao_tmp_dir() / f"{self.terminal_id}-system-prompt.md"
        prompt_path.write_text(system_prompt, encoding="utf-8")
        self._register_tmp_path(prompt_path)
        return str(prompt_path)

    def _write_plugin_dir(self, mcp_servers) -> str:
        """Materialise a Cursor plugin directory for the session's MCP servers.

        Cursor CLI v2026.06.15 dropped the ``--mcp <json>`` flag in
        favour of ``--plugin-dir <path>``: a directory that holds
        Cursor plugin manifests. We translate the CAO agent
        profile's ``mcpServers`` map into a minimal manifest layout
        so the same MCP servers (cao-mcp-server, ops-mcp-server,
        etc.) start transparently under the new flag.

        The synthesised directory lives under the CAO tmp dir keyed
        by the terminal id and is registered in ``self._tmp_paths``
        so ``cleanup()`` deletes it (and the per-session manifest
        inside it) when the session ends.

        Args:
            mcp_servers: The agent profile's ``mcpServers`` map. Keys
                are server names; values are either plain dicts (from
                YAML) or Pydantic models (from programmatic install).

        Returns:
            Absolute path to the plugin directory. Pass this to
            ``--plugin-dir <path>``.
        """
        plugin_dir = self._cao_tmp_dir() / f"{self.terminal_id}-cursor-plugins"
        plugin_dir.mkdir(parents=True, exist_ok=True)

        # CAO_TERMINAL_ID is forwarded into every server's env so
        # MCP tools (cao-mcp-server, ops-mcp-server) can resolve
        # the current terminal for handoff / assign operations. We
        # do not override an explicit preset — the same rule the
        # --mcp <json> path used to follow (issue #300 is the v2026
        # equivalent).
        servers: dict = {}
        for server_name, server_config in mcp_servers.items():
            if isinstance(server_config, dict):
                servers[server_name] = dict(server_config)
            else:
                servers[server_name] = server_config.model_dump(exclude_none=True)
            # Resolve the bundled cao-mcp-server console script to a
            # PATH-independent invocation.
            servers[server_name] = resolve_mcp_server_config(servers[server_name])
            env = servers[server_name].get("env", {})
            if "CAO_TERMINAL_ID" not in env:
                env["CAO_TERMINAL_ID"] = self.terminal_id
                servers[server_name]["env"] = env

        # Cursor v2026 plugin manifests are JSON files inside the
        # plugin dir. The exact schema is undocumented in the help
        # text we captured; the minimum that the CLI accepts is a
        # ``mcpServers`` object matching the well-known MCP config
        # layout, written as ``plugin.json`` at the root. If the
        # schema proves more strict in a later v2026 point release
        # this layout can be tweaked without touching the rest of
        # the provider.
        manifest = {"mcpServers": servers}
        (plugin_dir / "plugin.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        self._register_tmp_path(plugin_dir)
        return str(plugin_dir)

    def _cao_tmp_dir(self) -> Path:
        """Resolve the CAO tmp directory and create it on demand.

        Honours the ``CAO_TMP_DIR`` env var so tests can redirect
        temp output to ``/tmp/cao_test`` instead of polluting the
        user's ``~/.aws/cli-agent-orchestrator/tmp``. Defaults to
        ``~/.aws/cli-agent-orchestrator/tmp`` for production.
        """
        import os

        cao_tmp = Path(
            os.environ.get(
                "CAO_TMP_DIR", str(Path.home() / ".aws" / "cli-agent-orchestrator" / "tmp")
            )
        )
        cao_tmp.mkdir(parents=True, exist_ok=True)
        return cao_tmp

    def _register_tmp_path(self, path: Path) -> None:
        """Track a per-session temp path so ``cleanup()`` can remove it.

        A single path can be a file (the system prompt) or a
        directory (the plugin dir). If the path is already in the
        registry we skip the add; if a previous run wrote a stale
        file at the same path we let the new write overwrite it but
        keep the registry entry for the new one.
        """
        if path in self._tmp_paths:
            return
        self._tmp_paths.append(path)

    async def initialize(self) -> bool:
        """Initialize the Cursor CLI provider by starting ``agent``.

        This method:
        1. Waits for the shell prompt to appear in the tmux window.
        2. Sends the ``agent`` command with the configured agent
           profile, model, system prompt, and MCP config.
        3. Waits for the agent to reach IDLE / COMPLETED state.

        Returns:
            True if initialization was successful.

        Raises:
            TimeoutError: If shell or Cursor CLI initialization times out.
        """
        init_timeout = get_server_settings()["provider_init_timeout"]
        if not await wait_for_shell(self.terminal_id, timeout=init_timeout):
            raise TimeoutError(f"Shell initialization timed out after {init_timeout}s")

        command = self._build_cursor_command()
        # Arm the StatusMonitor stickiness gate so the launching
        # command can drive a fresh PROCESSING transition past any
        # stale ready latch. Without this, a previously-latched
        # IDLE/COMPLETED would suppress the genuine PROCESSING
        # transition that follows once Cursor starts loading.
        # Imported lazily to avoid a circular import: the
        # status_monitor module imports provider_manager, which
        # imports this module.
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        status_monitor.notify_input_sent(self.terminal_id)
        get_backend().send_keys(self.session_name, self.window_name, command)

        # Wait for Cursor CLI to fully initialize. Accept both IDLE
        # and COMPLETED — some versions render a startup message that
        # get_status() interprets as a completed response.
        if not await wait_until_status(
            self.terminal_id,
            {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
            timeout=float(get_server_settings()["provider_init_timeout"]),
        ):
            raise TimeoutError("Cursor CLI initialization timed out after 30 seconds")

        self._initialized = True
        return True

    def get_status(self, output: Optional[str]) -> TerminalStatus:
        """Get Cursor CLI status by analyzing terminal output.

        Called by StatusMonitor with the accumulated terminal output
        buffer (the raw pipe-pane byte stream). Status detection checks
        patterns in priority order:

        1. Empty / None output → UNKNOWN
        2. PROCESSING — Cursor CLI v2026+ TUI: ``ctrl+c to stop``
           indicator on the input-box line. Cursor renders this
           every frame the agent is working on a turn; it disappears
           once the response is fully delivered and the input box
           is back to the placeholder alone.
        3. PROCESSING — structural spinner-before-separator check
           (older text-mode Cursor builds).
        4. PROCESSING — fallback spinner visible with no separator.
        5. WAITING_USER_ANSWER — TUI selection footer or trust /
           permission prompt.
        6. IDLE / COMPLETED — TUI placeholder present (v2026+) OR
           idle prompt present (older text-mode builds).
        7. UNKNOWN — fallback when no marker matches.

        Args:
            output: Raw terminal output (rolling buffer, up to ~8KB).

        Returns:
            Current TerminalStatus.
        """
        # Native status (herdr): trust the backend's agent state when available;
        # on herdr the buffer is never fed, so buffer parsing can't leave UNKNOWN.
        native = self._resolve_native_status()
        if native is not None:
            return native

        if not output:
            return TerminalStatus.UNKNOWN

        # Strip the RAW pipe-pane escapes (cursor positioning, in-place
        # redraws), not just SGR colour codes — otherwise cursor
        # sequences survive and the structural checks below misfire on
        # the raw stream.
        clean = strip_terminal_escapes(output)

        # =================================================================
        # v2026+ TUI detection — primary signal: "ctrl+c to stop".
        # =================================================================
        # Cursor CLI v2026.x runs a full Ink/TUI in interactive mode.
        # The `❯` prompt and the `─────` separator that older builds
        # emitted as plain text are now rendered as TUI widgets and
        # never reach the pipe-pane buffer (issue #299).
        #
        # The TUI *does* still emit two plain-text signals into the
        # pipe-pane stream:
        #
        #   * the input-box placeholder — "Plan, search, build
        #     anything" on a fresh launch, "Add a follow-up" after
        #     the first turn. Always present in v2026 regardless of
        #     agent state, so it cannot distinguish idle from
        #     processing on its own.
        #   * the "ctrl+c to stop" hint, which Cursor renders on the
        #     same line as the placeholder while the agent is
        #     actively working on a turn. Absent on every other
        #     state (idle, completed, freshly launched, brand-new
        #     TUI). This is the reliable processing signal.
        #
        # We check the *tail* of the buffer (last ~1KB) so a long
        # response that scrolls older processing markers out of the
        # rolling 8KB window does not flip us back to IDLE during
        # processing. The 1KB window is well below the 8KB buffer
        # cap, and the input-box line is rendered in the last few
        # hundred bytes on every Cursor TUI frame, so the indicator
        # is always present in the tail whenever the agent is
        # working.
        TUI_TAIL_WINDOW = 1024
        tail = clean[-TUI_TAIL_WINDOW:]
        processing_indicator_in_tail = (
            re.search(TUI_PROCESSING_INDICATOR_PATTERN, tail, re.IGNORECASE) is not None
        )
        placeholder_in_tail = re.search(TUI_PLACEHOLDER_PATTERN, tail, re.IGNORECASE) is not None

        if processing_indicator_in_tail:
            # Primary v2026+ PROCESSING signal: the "ctrl+c to stop"
            # hint is on the input-box line. Cursor renders this
            # every frame the agent is working on a turn.
            #
            # We still let the WAITING_USER_ANSWER / trust /
            # permission checks below run first — those are
            # interactive prompts that take precedence over a
            # plain processing state.
            tui_processing = True
        else:
            tui_processing = False

        # PRIMARY PROCESSING check (older text-mode Cursor builds):
        # walk backwards from the *last* separator. If a spinner
        # line appears before another separator, the agent is
        # actively processing. If we hit another separator first,
        # the spinner is from a completed task and should be
        # ignored. Mirrors the Claude Code provider's structural
        # fix for the stale-spinner bug.
        # The separator regex is anchored to a full line (``^…$``);
        # the ``MULTILINE`` flag is required so ``^`` and ``$``
        # match at every line start/end, not just the buffer
        # start/end.
        _sep_re = re.compile(SEPARATOR_PATTERN, re.MULTILINE)
        _sep_positions = [m.start() for m in _sep_re.finditer(clean)]
        if _sep_positions:
            pre_sep_lines = clean[: _sep_positions[-1]].rstrip("\n").split("\n")
            for line in reversed(pre_sep_lines):
                if re.search(r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏✶✢✽✻✳·][^\n]*\u2026", line):
                    return TerminalStatus.PROCESSING
                if _sep_re.search(line):
                    break

        # Find the LAST occurrence of each marker for fallback
        # position checks. ``IDLE_PROMPT_PATTERN`` is line-anchored
        # (see its definition) so re.findall across the full buffer
        # only matches true prompt lines, never the leading
        # ``❯ <text>`` of an echoed user input line.
        last_processing = None
        for m in re.finditer(PROCESSING_PATTERN, clean):
            last_processing = m

        last_idle = None
        for m in re.finditer(IDLE_PROMPT_PATTERN, clean, re.MULTILINE):
            last_idle = m

        # FALLBACK PROCESSING: spinner visible AND no separator follows
        # it yet (early in execution before the separator appears).
        if last_processing and not _sep_re.search(clean):
            if last_idle is None or last_processing.start() > last_idle.start():
                return TerminalStatus.PROCESSING

        # Check for active TUI selection widgets (mode picker, model
        # picker, etc.) which show a ↑/↓ navigation footer. Exclude
        # the trust/permission dialogs, which are separate states.
        if (
            re.search(WAITING_USER_ANSWER_PATTERN, clean)
            and not re.search(TRUST_PROMPT_PATTERN, clean, re.IGNORECASE)
            and not re.search(PERMISSION_PROMPT_PATTERN, clean, re.IGNORECASE)
        ):
            return TerminalStatus.WAITING_USER_ANSWER

        # Trust / permission dialogs are an interactive prompt that
        # blocks the agent until the operator accepts. Treat them as
        # WAITING_USER_ANSWER.
        if re.search(TRUST_PROMPT_PATTERN, clean, re.IGNORECASE) or re.search(
            PERMISSION_PROMPT_PATTERN, clean, re.IGNORECASE
        ):
            return TerminalStatus.WAITING_USER_ANSWER

        # v2026+ TUI: "ctrl+c to stop" is on the input-box line.
        # This is the primary PROCESSING signal. We also require
        # the status bar to be present so a half-initialised TUI
        # does not false-positive.
        if tui_processing and re.search(TUI_STATUS_BAR_PATTERN, clean):
            return TerminalStatus.PROCESSING

        # IDLE / COMPLETED: an idle prompt at the bottom of the
        # buffer indicates the agent has finished its turn (or is
        # freshly initialized and waiting for input). We
        # differentiate IDLE vs COMPLETED on the turn counter: a
        # fresh spawn (no user input ever delivered) is IDLE
        # regardless of what the TUI looks like, and a turn
        # that has finished is COMPLETED. The TUI's status bar +
        # absence of "ctrl+c to stop" is the same in both states
        # (no good way to tell them apart from the buffer alone
        # in v2026), so the turn counter is the authoritative
        # source for that split.
        if last_idle:
            return TerminalStatus.COMPLETED if self._turns > 0 else TerminalStatus.IDLE

        # v2026+ TUI IDLE: no "ctrl+c to stop" indicator on the
        # input-box line AND the status bar is visible. The
        # placeholder is always present in v2026 regardless of
        # state, so it is the *absence* of the processing
        # indicator that distinguishes idle / completed from
        # processing. We require the status bar to be present
        # too so we don't false-positive on a half-initialised
        # TUI. Same IDLE-vs-COMPLETED split via the turn counter
        # as above.
        if not processing_indicator_in_tail and re.search(TUI_STATUS_BAR_PATTERN, clean):
            return TerminalStatus.COMPLETED if self._turns > 0 else TerminalStatus.IDLE

        return TerminalStatus.UNKNOWN

    def get_idle_pattern_for_log(self) -> str:
        """Return Cursor CLI IDLE prompt pattern for log files."""
        return IDLE_PROMPT_PATTERN_LOG

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract the last assistant response from the terminal output.

        Cursor CLI does not emit a single canonical response marker
        (unlike Claude Code's ``⏺``), so we use the structural
        separator + trailing prompt pattern. The terminal buffer has
        the shape::

            ──────────────────────────
            ❯ <user question>
            ──────────────────────────
            <assistant response>
            ──────────────────────────
            ❯

        The assistant response is the text between the *second* and
        *third* separators (or equivalently, between the user's
        ``❯ <text>`` question and the next ``❯`` idle prompt).

        The separator regex tolerates SGR colour codes interleaved
        between the ``─`` characters (Cursor's TUI redraws the
        separator in place with new colour escapes on every prompt).
        All remaining terminal escapes (cursor positioning, OSC, \r
        redraws) are stripped from the response region before
        returning so the extracted text is the rendered output, not
        the raw byte stream. We do NOT use
        :func:`cli_agent_orchestrator.utils.text.strip_terminal_escapes`
        because that function normalises ``\\r`` to ``\\n`` and would
        split single-line spinner frames into multiple lines — see
        its docstring. Extraction operates on rendered (capture-pane)
        output, not the raw FIFO stream.

        Raises:
            ValueError: When no response boundary is detected.
        """
        # Match a separator line, with ANY CSI sequence (not just
        # SGR) interleaved between the box-drawing characters.
        # Cursor re-renders the separator with new colour escapes
        # on every prompt, so the byte stream looks like
        # ``\x1b[38;5;245m──\x1b[0m──\x1b[38;5;245m──`` — the
        # pattern must accept CSI inside the dash run, not just
        # before it. Intermediate bytes are restricted to the
        # ECMA-48 param range (0x30-0x3F) so a stray ``ESC [`` is
        # not consumed. The pattern is anchored to a full line
        # (``^...$``) so a stray dash sequence inside response
        # content is not matched.
        # The separator regex is anchored to a full line (``^…$``);
        # the ``MULTILINE`` flag is required so ``^`` and ``$``
        # match at every line start/end, not just the buffer
        # start/end.
        _sep_re = re.compile(SEPARATOR_PATTERN, re.MULTILINE)
        separators = list(_sep_re.finditer(script_output))
        idle_matches = list(re.finditer(IDLE_PROMPT_PATTERN, script_output, re.MULTILINE))

        if not separators or not idle_matches:
            raise ValueError(
                "No Cursor CLI response found - no separator / idle prompt boundary detected"
            )

        # Anchor on the trailing idle prompt (the last ❯ in the
        # buffer). The response ends at this prompt. Walk back to
        # find the separator that immediately precedes the response.
        # The earlier check guarantees that ``separators`` and
        # ``idle_matches`` are both non-empty, so at least one
        # separator is guaranteed to be before the trailing
        # prompt (an idle prompt at position 0 would be paired
        # with no separators, which the earlier check rejects).
        final_prompt = idle_matches[-1]

        # Find the last separator that comes BEFORE the trailing
        # prompt. That separator marks the end of the response.
        end_sep: Optional[re.Match[str]] = None
        for sep in reversed(separators):
            if sep.start() < final_prompt.start():
                end_sep = sep
                break

        assert end_sep is not None  # see comment above

        # The response starts at the separator before end_sep (which
        # marks the start of the response region) — or at the start
        # of the buffer if there is no such separator. This avoids
        # leaking the user's question into the extracted response.
        start_sep: Optional[re.Match[str]] = None
        for sep in reversed(separators):
            if sep.start() < end_sep.start():
                start_sep = sep
                break

        start = start_sep.end() if start_sep is not None else 0
        response = script_output[start : end_sep.start()]

        # Strip ALL terminal escape sequences from the extracted
        # region (not just SGR colours). Cursor CLI re-renders
        # cursor-positioning sequences inside the response area
        # during long generations, and OSC title updates can leak
        # into the captured text. We deliberately do not use
        # ``strip_terminal_escapes`` here because that function
        # normalises ``\r`` → ``\n`` (suitable for status detection
        # but destructive for response extraction).
        #
        # The regex follows the ECMA-48 escape grammar:
        #   CSI:  ESC [  <param-bytes 0x30-0x3F>*  <final-byte 0x40-0x7E>
        #   OSC:  ESC ]  <payload>        BEL  |  ESC \
        #   2-byte ESC: ESC <0x20-0x2F>+  <0x30-0x7E>
        # Intermediate bytes are restricted to 0x20-0x3F so that
        # plain ``ESC [`` (e.g. CSI introducer with no params) is
        # only consumed when followed by a real final byte.
        _full_esc_re = re.compile(
            r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
            r"|\x1b\[[\x30-\x3F]*[\x40-\x7E]"
            r"|\x1b[\x20-\x2F]+[\x30-\x7E]"
        )
        response = _full_esc_re.sub("", response).strip()

        if not response:
            raise ValueError("Empty Cursor CLI response - no content found between separators")

        return response

    def exit_cli(self) -> str:
        """Get the command to exit Cursor CLI.

        Cursor CLI exits on ``/exit`` (slash command) or Ctrl+D
        (double-press). ``/exit`` is the more reliable programmatic
        path and matches the convention used by the other providers.
        """
        return "/exit"

    def cleanup(self) -> None:
        """Clean up Cursor CLI provider state.

        Resets the initialised flag and removes every per-session
        temp file the provider has created under the CAO tmp dir
        (system prompt file, plugin directory). Removing the
        plugin directory also drops the per-session ``plugin.json``
        manifest that forwards ``CAO_TERMINAL_ID`` into the MCP
        server env.

        Errors during cleanup are logged and swallowed — the
        session is already going away at this point and we do not
        want to mask the original error path with a transient
        ``OSError`` from a stale file. The registry is cleared
        either way so a future reuse of the same provider instance
        (rare; providers are typically one-shot) does not try to
        re-delete paths that no longer exist.
        """
        import logging
        import shutil

        logger = logging.getLogger(__name__)
        self._initialized = False
        for path in self._tmp_paths:
            try:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                elif path.exists() or path.is_symlink():
                    path.unlink()
            except FileNotFoundError:
                # Already removed by something else (e.g. operator
                # manually cleaned the tmp dir). Not an error.
                pass
            except OSError as exc:
                logger.warning(
                    "CursorCliProvider cleanup: failed to remove %s: %s",
                    path,
                    exc,
                )
        self._tmp_paths = []

    def mark_input_received(self) -> None:
        """Record that a turn has been delivered to the agent.

        Called by the terminal service after every ``send_input``.
        Bumps the internal turn counter so :meth:`get_status` can
        distinguish a fresh spawn (``IDLE``) from a finished
        turn (``COMPLETED``) — the v2026 TUI looks the same in
        both states (status bar visible, no ``ctrl+c to stop``
        hint), so the counter is the only authoritative signal.

        Called on the *delivery* path, not on the response path:
        we increment the moment the input is sent to tmux, not
        when the agent finishes processing it. That means the
        status will be ``COMPLETED`` after the next
        ``get_status`` call once the agent has finished the
        turn, not before — which is what the StatusMonitor's
        stickiness gate expects.
        """
        super().mark_input_received()
        self._turns += 1
