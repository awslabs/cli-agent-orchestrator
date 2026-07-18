"""Claude Code provider implementation."""

import asyncio
import json
import logging
import os
import re
import shlex
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, List, Optional

if TYPE_CHECKING:
    from cli_agent_orchestrator.models.agent_profile import AgentProfile

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.constants import CAO_HOME_DIR
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.services.settings_service import get_server_settings
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.mcp_resolution import resolve_mcp_server_config
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status
from cli_agent_orchestrator.utils.text import strip_terminal_escapes

logger = logging.getLogger(__name__)

# Sentinel so _build_claude_command can tell "caller passed no profile, load it"
# from "caller explicitly passed None" (native/missing profile). initialize()
# loads the profile once and passes it in; direct callers omit it and get a
# load. NOT a candidate for removal in favor of a plain `None` default: that
# would make _build_claude_command reload the profile from disk a SECOND time
# whenever initialize() already resolved it to None (no CAO profile found),
# reintroducing the double-disk-read this sentinel exists to prevent.
_UNSET: Any = object()


# Custom exception for provider errors
class ProviderError(Exception):
    """Exception raised for provider-specific errors."""

    pass


# Regex patterns for Claude Code output analysis
ANSI_CODE_PATTERN = r"\x1b\[[0-9;]*m"
RESPONSE_PATTERN = r"⏺(?:\x1b\[[0-9;]*m)*\s+"  # Handle any ANSI codes between marker and text
# Shared shape of the reasoning-effort footer's tail, e.g. "high · /effort" or
# "xhigh · /effort". "\w+" matches the effort level generically (any name, not
# just "high") rather than enumerating known levels. End-anchored so it only
# describes a line that IS ENTIRELY this shape; a genuine response that merely
# mentions "/effort" in prose (e.g. "Run /effort to change settings") does not
# end the line with "<word> · /effort" and is unaffected.
#
# _ANSI_OPT is spliced between every token: real capture-pane -e output (used
# by extraction, per this module's docstring guidance) re-renders the pane's
# SGR color state, so this exact chrome line arrives wrapped in color codes,
# e.g. "\x1b[38;5;246m● high · /effort\x1b[39m" — a trailing reset directly
# after "/effort" (GH #459 follow-up). Without this, the reset defeats the
# "[ \t]*$" end anchor and the exclusion silently stops firing on styled
# output, even though it passes on the plain-text unit fixtures.
_ANSI_OPT = r"(?:\x1b\[[0-9;]*m)*"
_EFFORT_FOOTER_TAIL = (
    _ANSI_OPT
    + r"\w+"
    + _ANSI_OPT
    + r"[ \t]*·[ \t]*"
    + _ANSI_OPT
    + r"/effort"
    + _ANSI_OPT
    + r"[ \t]*$"
)
# Response marker at the START of a line, for message EXTRACTION only (not
# status detection). Matches the legacy "⏺" (U+23FA) and the newest TUI's
# "●" (U+25CF) response glyphs. Anchored to line start (MULTILINE) so a
# mid-line "●" — e.g. the footer effort indicator "… esc to interrupt ● high
# · /effort" — is NOT mistaken for a response marker.
#
# On Claude Code v2.1.212+ the same footer can instead render on its OWN line
# at column 0 — "● high · /effort" — where the line-start anchor no longer
# protects against it (GH #459: this false-matched as a response marker, so
# get_status() reported COMPLETED while the worker was still processing,
# causing handoff to paste a premature "/exit"). The negative lookahead below
# guards that exact shape. It sits right after the glyph (+ optional ANSI),
# BEFORE the trailing "\s+" — placing it AFTER "\s+" instead lets that greedy
# "\s+" backtrack by one space to dodge the lookahead whenever the footer has
# more than one space after the glyph, silently reopening the false match.
#
# Kept separate from RESPONSE_PATTERN so get_status's legacy ⏺-COMPLETED check
# is unaffected (adding "●" there could fire COMPLETED mid-stream while a
# response is still rendering).
EXTRACTION_RESPONSE_PATTERN = re.compile(
    r"^[ \t]*(?:\x1b\[[0-9;]*m)*[⏺●](?:\x1b\[[0-9;]*m)*(?![ \t]*" + _EFFORT_FOOTER_TAIL + r")\s+",
    re.MULTILINE,
)
# Own-line effort-footer, e.g. "● high · /effort" (glyph + the tail shape
# above). Standalone (not a lookahead) so get_status()'s box-walk can skip past
# this exact chrome line while searching for a live spinner (GH #459). Shares
# _EFFORT_FOOTER_TAIL with EXTRACTION_RESPONSE_PATTERN instead of duplicating
# the shape.
EFFORT_FOOTER_LINE_PATTERN = re.compile(r"^[ \t]*[⏺●][ \t]*" + _EFFORT_FOOTER_TAIL, re.MULTILINE)
# Match Claude Code processing spinners:
# - Old format: "✽ Cooking… (esc to interrupt)" / "✶ Thinking… (esc to interrupt)"
# - New format: "✽ Cooking… (6s · ↓ 174 tokens · thinking)"
# - Minimal format: "✻ Orbiting…" (no parenthesized status)
# Common: spinner char + text + ellipsis, optionally followed by parenthesized status
# The leading class includes the ASCII asterisk "*" (U+002A): the newest
# Claude Code TUI cycles its spinner glyph through "· ✢ * ✶ ✻ ✽", so ~1 in 6
# captured frames shows a bare "*". Omitting it left a live "* Cultivating…"
# frame invisible to every processing detector (false IDLE/COMPLETED mid-turn).
PROCESSING_PATTERN = r"[✶✢✽✻✳·*].*\u2026"
# Structural PROCESSING indicator (reference pattern — get_status uses an
# inline last-separator-anchored version to avoid false positives from
# mid-conversation compaction events like "✢ Compacting conversation…"):
# a spinner line (spinner char + … ) immediately before the ────────
# separator, allowing 0–2 blank lines between them.
THINKING_BEFORE_SEPARATOR_PATTERN = re.compile(
    r"[^\n]*[✶✢✽✻✳·][^\n]*\u2026[^\n]*\n(?:[^\n]*\n){0,2}(?:\x1b\[[0-9;]*m)*\u2500{20,}",
    re.MULTILINE,
)
IDLE_PROMPT_PATTERN = r"[>❯][\s\xa0]"  # Handle both old ">" and new "❯" prompt styles
WAITING_USER_ANSWER_PATTERN = (
    r"↑/↓ to navigate"  # Ink TUI footer shown only while a selection widget is active
)
TRUST_PROMPT_PATTERN = r"Yes, I trust this folder"  # Workspace trust dialog
BYPASS_PROMPT_PATTERN = r"Yes, I accept"  # Bypass permissions confirmation dialog
IDLE_PROMPT_PATTERN_LOG = r"[>❯][\s\xa0]"  # Same pattern for log files
# New Claude Code TUI completion summary, e.g. "✻ Sautéed for 1s" /
# "✶ Cultivated for 12s". Unlike the active spinner (PROCESSING_PATTERN, which
# always ends with the … ellipsis), the summary is past-tense + "for Ns" with NO
# ellipsis. The newest TUI shows this (above an empty ❯ box) after a finished
# turn INSTEAD of the old ⏺ response marker, so it is the COMPLETED signal there.
# The ``·`` glyph is intentionally excluded from the leading class so footer
# lines like "high · /effort" cannot false-match.
COMPLETION_SUMMARY_PATTERN = r"[✶✢✽✻✳][^\n…]*\bfor\s+\d+(?:\.\d+)?\s*s\b"
# get_status completion detection tolerates the duration being CLIPPED off by
# the raw redraw ("✻ Crunched for " with no "Ns"): past-tense glyph + "for",
# no ellipsis (so a live "…ing…" spinner never matches). · and * stay excluded
# so footer lines ("high · /effort") cannot false-match. Looser than
# COMPLETION_SUMMARY_PATTERN (which extraction keeps strict to trim only real
# stat lines), and only ever turns IDLE/PROCESSING into COMPLETED — the safe
# direction — and only after the live-spinner PROCESSING checks have passed.
GET_STATUS_COMPLETION_PATTERN = r"[✶✢✽✻✳][^\n…]*\bfor\b"
# Background-task wait line, e.g. "✻ Waiting for 1 dynamic workflow to finish".
# The newest TUI renders this while a backgrounded task (Workflow tool, bash
# task) keeps running AFTER the turn's text response has printed and the input
# box is already idle — the terminal looks "finished" (response + empty ❯ box)
# except for this one line. It carries NO ellipsis, so every spinner-based
# PROCESSING check misses it, and it satisfies GET_STATUS_COMPLETION_PATTERN's
# lenient glyph+"for" match ("✻ Waiting *for* 1 …"), so without special
# handling the whole frame reads COMPLETED while work continues (GH #392:
# the Runs board showed "Done" with the TUI footer at "2/3 agents done").
#
# Match shape (review-hardened, PR #393):
# - Start-of-line anchor + a REQUIRED tail keyword (workflow/task/background/
#   "to finish"), so a markdown bullet in a settled response body
#   ("* Waiting for review") can never match — an over-match here pins the
#   terminal at PROCESSING via the ready-latch (denial-of-progress).
# - The glyph class DELIBERATELY keeps "·" and "*", unlike
#   GET_STATUS_COMPLETION_PATTERN: the TUI cycles the glyph through
#   "· ✢ * ✶ ✻ ✽", and a single missed frame here would false-COMPLETE and
#   latch (the exact #392 bug) — the tail keywords carry the disambiguation
#   the completion pattern gets from excluding those glyphs.
# - "\xa0" is allowed as the gap, matching IDLE_PROMPT_PATTERN's handling of
#   the TUI's non-breaking-space rendering.
BACKGROUND_WAIT_PATTERN = re.compile(
    r"(?m)^[ \t\xa0]*[✶✢✽✻✳·*][ \t\xa0]+Waiting for\b"
    r"(?=[^\n]*\b(?:workflows?|tasks?|to finish|background)\b)"
)
# The newest Claude Code TUI renders the ❯ input prompt BOXED between two
# horizontal separator lines (the older TUI used a single separator ABOVE ❯).
# Detecting this box GATES the new-TUI status logic so legacy output is
# unaffected. The ❯ line must be essentially empty (just the prompt) so a
# response/echo line like "❯ my task" does NOT match.
#
# The interior `(?:[ \t\xa0]*\n){0,2}` tolerates up to two WHITESPACE-ONLY
# lines between each separator and the ❯ line. This is required against the
# RAW pipe-pane stream: get_status runs strip_terminal_escapes first, which
# converts the newest TUI's in-place CUU/CHA redraw escapes into newlines, so
# the box arrives as "─…\n\n❯\xa0\n\n─…" (one blank line each side) rather than
# the immediately-adjacent form a tmux-rendered snapshot would show. Only blank
# lines are tolerated (not arbitrary content), so a "❯ my task" echo or a
# "⏺ response"/compaction line between separators still cannot match, and the
# {0,2} bound keeps the match local so it cannot span two distinct separators.
NEW_TUI_BOX_PATTERN = re.compile(
    r"─{8,}[^\n]*\n(?:[ \t\xa0]*\n){0,2}[ \t]*[>❯][ \t\xa0]*\n(?:[ \t\xa0]*\n){0,2}[ \t]*─{8,}",
    re.MULTILINE,
)
# Live spinner in the new TUI: spinner glyph + a gerund ("…ing") + the … ellipsis,
# e.g. "✻ Cultivating…", "· Swirling…". Tighter than PROCESSING_PATTERN so the
# version status bar ("· latest:…") is not mistaken for a live spinner.
NEW_TUI_SPINNER_PATTERN = r"[✶✢✽✻✳·*][^\n]*ing…"
# Spinner on the line DIRECTLY ABOVE the new-TUI input box. Tighter than
# NEW_TUI_SPINNER_PATTERN: the FIRST word after the leading glyph must be a
# gerund (ends in "ing"); the … ellipsis may follow later on the line so the
# real multi-word compaction spinner "✢ Compacting conversation…" still matches.
# Requiring the gerund as the FIRST word rejects a response bullet
# ("* Remember to deploy…") or the version footer ("· latest: … update…")
# sitting directly above the box from being misread as a live spinner.
NEW_TUI_BOX_SPINNER_PATTERN = re.compile(r"^[ \t]*[✶✢✽✻✳·*][ \t]+\w*ing\b.*…")


class ClaudeCodeProvider(BaseProvider):
    """Provider for Claude Code CLI tool integration."""

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        agent_profile: Optional[str] = None,
        allowed_tools: Optional[list] = None,
        skill_prompt: Optional[str] = None,
    ):
        """Initialize provider state."""
        super().__init__(terminal_id, session_name, window_name, allowed_tools, skill_prompt)
        self._initialized = False
        self._agent_profile = agent_profile
        # Native-status dispatch tracking (_task_dispatched + flush-wait timers)
        # lives on BaseProvider and is consumed by _resolve_native_status().

    def _load_profile(self) -> Optional["AgentProfile"]:
        """Load this terminal's CAO agent profile from disk, if any.

        Returns None when no profile name was given or the named profile does
        not exist (the "pass --agent <name> to the native store" path). Raises
        ProviderError on a genuine load/parse failure so a broken profile is not
        silently ignored.

        ``self._agent_profile`` is a profile *name string*, not an object.
        """
        if self._agent_profile is None:
            return None
        try:
            return load_agent_profile(self._agent_profile)
        except FileNotFoundError:
            return None
        except Exception as e:
            raise ProviderError(f"Failed to load agent profile '{self._agent_profile}': {e}")

    def _build_claude_command(self, profile: Optional["AgentProfile"] = _UNSET) -> str:
        """Build Claude Code command with agent profile if provided.

        Returns properly escaped shell command string that can be safely sent via tmux.
        Uses shlex.join() to handle multiline strings and special characters correctly.

        Three routing paths based on agent profile state:
        1. Profile with native_agent field -> pass --agent <native_agent> directly
           (thin wrapper: Claude Code handles all config)
        2. No CAO profile found -> pass --agent <name> directly to Claude Code's
           native agent store (~/.claude/agents/)
        3. Full CAO profile -> decompose into CLI flags (model, prompt, MCP, etc.)

        Args:
            profile: Pre-loaded profile. When omitted, the profile is loaded from
                disk here. initialize() loads it once and passes it in so the
                profile is not read from disk twice per launch.
        """
        # --dangerously-skip-permissions: bypass the workspace trust dialog and
        # tool permission prompts. CAO already confirms workspace access during
        # `cao launch` (or `--yolo`), so re-prompting each spawned agent
        # (supervisor and worker) is redundant and blocks handoff/assign flows.
        yolo = bool(self._allowed_tools and "*" in self._allowed_tools)

        if profile is _UNSET:
            profile = self._load_profile()

        # Determine permission mode for the base command.
        # Priority: explicit permissionMode > yolo/root detection > default yolo.
        #
        # Root/sudo guard: Claude Code rejects --dangerously-skip-permissions when
        # running as root. We only omit it for yolo+root; non-root yolo still needs
        # the flag so Claude won't prompt for tool approval inside a headless tmux
        # pane and silently block handoff/assign flows.
        is_root = getattr(os, "geteuid", lambda: -1)() == 0

        if profile and profile.permissionMode:
            command_parts = ["claude", "--permission-mode", profile.permissionMode]
        elif yolo and is_root:
            # Root users cannot use --dangerously-skip-permissions; omit it entirely.
            command_parts = ["claude"]
        else:
            command_parts = ["claude", "--dangerously-skip-permissions"]

        # Route based on profile state
        native = getattr(profile, "native_agent", None) if profile else None
        if profile is not None and isinstance(native, str) and native:
            # Thin wrapper: CAO profile maps to a native Claude Code agent.
            # Let Claude Code handle all config (MCP servers, hooks, tools, model).
            # CAO_TERMINAL_ID propagates via tmux pane env inheritance.
            command_parts.extend(["--agent", native])
        elif self._agent_profile is not None and profile is None:
            # No CAO profile exists — pass agent name directly to Claude Code's
            # native agent store (~/.claude/agents/). Same thin-orchestrator
            # pattern as the Kiro CLI provider.
            command_parts.extend(["--agent", self._agent_profile])
        elif profile is not None:
            # Full CAO profile with config decomposition
            if profile.model:
                command_parts.extend(["--model", profile.model])

            # Add system prompt - escape newlines to prevent tmux chunking issues
            system_prompt = profile.system_prompt if profile.system_prompt is not None else ""
            system_prompt = self._apply_skill_prompt(system_prompt)
            if system_prompt:
                tmp_dir = CAO_HOME_DIR / "tmp"
                tmp_dir.mkdir(parents=True, exist_ok=True)
                prompt_file = tmp_dir / f"{self.terminal_id}.prompt"
                prompt_file.write_text(system_prompt, encoding="utf-8")
                try:
                    prompt_file.chmod(0o600)
                except OSError:
                    pass
                command_parts.extend(
                    ["--append-system-prompt-file", self._translate_path(str(prompt_file), profile)]
                )

            # Add MCP config if present.
            # Forward CAO_TERMINAL_ID so MCP servers (e.g. cao-mcp-server)
            # can identify the current terminal for handoff/assign operations.
            # Claude Code does not automatically forward parent shell env vars
            # to MCP subprocesses, so we inject it explicitly via the env field.
            if profile.mcpServers:
                mcp_config = {}
                for server_name, server_config in profile.mcpServers.items():
                    if isinstance(server_config, dict):
                        mcp_config[server_name] = dict(server_config)
                    else:
                        mcp_config[server_name] = server_config.model_dump(exclude_none=True)

                    # Resolve the bundled cao-mcp-server console script to a
                    # PATH-independent invocation.
                    mcp_config[server_name] = resolve_mcp_server_config(mcp_config[server_name])

                    env = mcp_config[server_name].get("env", {})
                    if "CAO_TERMINAL_ID" not in env:
                        env["CAO_TERMINAL_ID"] = self.terminal_id
                        mcp_config[server_name]["env"] = env

                tmp_dir = CAO_HOME_DIR / "tmp"
                tmp_dir.mkdir(parents=True, exist_ok=True)
                mcp_file = tmp_dir / f"{self.terminal_id}.mcp.json"
                mcp_file.write_text(json.dumps({"mcpServers": mcp_config}), encoding="utf-8")
                try:
                    mcp_file.chmod(0o600)
                except OSError:
                    pass
                command_parts.extend(
                    [
                        "--mcp-config",
                        self._translate_path(str(mcp_file), profile),
                        "--strict-mcp-config",
                    ]
                )

        # Apply tool restrictions via --disallowedTools flags.
        # --dangerously-skip-permissions bypasses prompts but --disallowedTools
        # still prevents the agent from using the blocked tools entirely.
        if self._allowed_tools and "*" not in self._allowed_tools:
            from cli_agent_orchestrator.utils.tool_mapping import get_disallowed_tools

            disallowed = get_disallowed_tools("claude_code", self._allowed_tools)
            for tool in disallowed:
                command_parts.extend(["--disallowedTools", tool])

        # Use shlex.join() for proper shell escaping of all arguments
        # This correctly handles multiline strings, quotes, and special characters
        claude_cmd = shlex.join(command_parts)

        # When cao-server runs inside a Claude Code session, CLAUDE* env vars
        # leak into spawned tmux panes (via the tmux server's global env).
        # Claude Code detects these and refuses to start ("nested session").
        # Unset all matching vars except CLAUDE_CODE_USE_*,
        # CLAUDE_CODE_SKIP_*_AUTH (needed for provider authentication:
        # Bedrock, Vertex AI, Foundry), and CLAUDE_CODE_EFFORT_LEVEL (user pref).
        unset_cmd = (
            "unset $(env | sed -n 's/^\\(CLAUDE[A-Z_]*\\)=.*/\\1/p'"
            " | grep -v -E 'CLAUDE_CODE_USE_(BEDROCK|VERTEX|FOUNDRY)"
            "|CLAUDE_CODE_SKIP_(BEDROCK|VERTEX|FOUNDRY)_AUTH"
            "|CLAUDE_CODE_EFFORT_LEVEL'"
            ") 2>/dev/null"
        )
        return f"{unset_cmd}; {claude_cmd}"

    @staticmethod
    def _ensure_skip_bypass_prompt_setting() -> None:
        """Ensure ``skipDangerousModePermissionPrompt`` is set in settings.

        Claude Code (v2.1.41+) shows a bypass permissions confirmation dialog
        on every launch with ``--dangerously-skip-permissions`` unless
        ``skipDangerousModePermissionPrompt: true`` is persisted in
        ``~/.claude/settings.json``.  CAO already uses the flag intentionally,
        so the confirmation is redundant and blocks initialization.
        """
        settings_path = Path.home() / ".claude" / "settings.json"
        settings: dict = {}
        if settings_path.exists():
            try:
                with open(settings_path) as f:
                    settings = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass

        if settings.get("skipDangerousModePermissionPrompt") is True:
            return

        settings["skipDangerousModePermissionPrompt"] = True
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        with open(settings_path, "w") as f:
            json.dump(settings, f, indent=2)
        logger.info("Set skipDangerousModePermissionPrompt in ~/.claude/settings.json")

    def _handle_startup_prompts(
        self, idle_gap: Optional[float] = None, outer_timeout: Optional[float] = None
    ) -> None:
        """Auto-accept startup prompts that may appear before the REPL is ready.

        Claude Code may show up to two prompts during startup:

        1. **Bypass permissions confirmation** (``--dangerously-skip-permissions``)
           – shows "Yes, I accept" as option 2; requires ``Down`` + ``Enter``.
           The settings-based fix (``_ensure_skip_bypass_prompt_setting``) prevents
           this in most cases; this handler is a defensive fallback.
        2. **Workspace trust dialog** – shows "Yes, I trust this folder";
           requires ``Enter``.

        Idle-gap semantics (see issue #400): a cold or containerized start can
        render these dialogs LATE and in sequence, past the old fixed ~20s
        window. Instead of a total-window budget, ``idle_gap`` is the maximum
        quiet stretch tolerated BETWEEN prompts: the loop keeps polling and
        resets the idle timer every time it answers a prompt, exiting only once
        no new prompt appears for ``idle_gap`` seconds. Total runtime is still
        hard-capped by ``outer_timeout`` so a wedged start cannot hang init
        indefinitely.

        The idle-gap exit is gated on having handled at least one prompt.
        ``last_prompt_time`` has no real "last prompt" to measure from until
        the FIRST one arrives, so treating the handler's start time as if it
        were one let a first dialog later than ``idle_gap`` (e.g. issue #400's
        own cold-node-plus-gateway-connect scenario) get missed entirely — the
        loop would exit at the idle-gap boundary having never seen it. Before
        any prompt has been observed, only ``outer_timeout`` can end the loop;
        the idle-gap clock starts only once a prompt has actually been handled.

        Args:
            idle_gap: Seconds of no-new-prompt quiet that ends the loop. Defaults
                to the ``startup_prompt_handler_timeout`` setting.
            outer_timeout: Hard cap (seconds) on total handler runtime. Defaults
                to the ``provider_init_timeout`` setting; initialize() passes the
                per-profile-resolved value so a containerized profile's longer
                init budget also governs this handler.
        """
        if idle_gap is None:
            idle_gap = get_server_settings()["startup_prompt_handler_timeout"]
        if outer_timeout is None:
            outer_timeout = get_server_settings()["provider_init_timeout"]
        outer_deadline = time.monotonic() + outer_timeout
        last_prompt_time = time.monotonic()
        any_prompt_handled = False
        bypass_accepted = False
        while True:
            now = time.monotonic()
            if now >= outer_deadline:
                logger.warning("Startup prompt handler hit provider_init_timeout outer cap")
                return
            if any_prompt_handled and now - last_prompt_time >= idle_gap:
                return  # no new prompt within the idle gap — startup settled

            output = get_backend().get_history(self.session_name, self.window_name)
            if not output:
                time.sleep(1.0)
                continue

            clean_output = re.sub(ANSI_CODE_PATTERN, "", output)

            # 1) Handle bypass permissions prompt (appears before trust prompt).
            #    Only act once — the text stays in the buffer after dismissal.
            if not bypass_accepted and re.search(BYPASS_PROMPT_PATTERN, clean_output):
                from cli_agent_orchestrator.services.status_monitor import status_monitor

                logger.info("Bypass permissions prompt detected, auto-accepting")
                # Send Down arrow to move cursor to "Yes, I accept", then Enter.
                status_monitor.notify_input_sent(self.terminal_id)
                get_backend().send_keys(
                    self.session_name, self.window_name, "\x1b[B", enter_count=0
                )
                time.sleep(0.5)
                status_monitor.notify_input_sent(self.terminal_id)
                get_backend().send_special_key(self.session_name, self.window_name, "Enter")
                bypass_accepted = True
                any_prompt_handled = True
                last_prompt_time = time.monotonic()  # reset idle timer — trust prompt may follow
                time.sleep(1.0)
                continue

            # 2) Handle workspace trust prompt
            if re.search(TRUST_PROMPT_PATTERN, clean_output):
                from cli_agent_orchestrator.services.status_monitor import status_monitor

                logger.info("Workspace trust prompt detected, auto-accepting")
                status_monitor.notify_input_sent(self.terminal_id)
                get_backend().send_special_key(self.session_name, self.window_name, "Enter")
                return

            # 3) Claude Code fully started — no prompts needed.
            #    The version banner is the ONLY reliable "ready" signal here: it
            #    renders only once the REPL is up and cannot appear in the echoed
            #    launch command. The old bare IDLE_PROMPT_PATTERN ("> "/"❯ ") check
            #    was removed: the injected --append-system-prompt text contains
            #    "> `memory_store`" (start of a line), which the echoed command
            #    surfaces in the capture buffer within ~300ms and false-matches as
            #    "idle". The handler then returned BEFORE the workspace-trust dialog
            #    rendered, leaving it unaccepted; initialize() then blocked on
            #    {IDLE, COMPLETED} for 30s and the session was killed. Trust/bypass
            #    dialogs are handled explicitly above; if no banner ever appears the
            #    loop just waits out its idle gap and the downstream
            #    wait_until_status() remains the real readiness gate.
            if re.search(r"Welcome to|Claude Code v\d+", clean_output):
                logger.info("Claude Code started without prompts")
                return

            time.sleep(1.0)

    async def initialize(self) -> bool:
        """Initialize Claude Code provider by starting claude command."""
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        # Load the profile once so the per-profile provider_init_timeout override
        # (if any) governs every wait below, and reuse it for the command build
        # so the profile is not read from disk twice.
        profile = self._load_profile()
        init_timeout = self.get_init_timeout(profile)

        # Wait for shell prompt to appear in the tmux window
        if not await wait_for_shell(self.terminal_id, timeout=init_timeout):
            raise TimeoutError(f"Shell initialization timed out after {init_timeout}s")

        # Prevent bypass permissions dialog from appearing (settings-based fix).
        self._ensure_skip_bypass_prompt_setting()

        # Build properly escaped command string
        command = self._build_claude_command(profile)

        # Send Claude Code command using the backend. Arm the StatusMonitor
        # stickiness gate so the launching command can drive a fresh
        # PROCESSING transition past any stale ready latch.
        status_monitor.notify_input_sent(self.terminal_id)
        get_backend().send_keys(self.session_name, self.window_name, command)

        # Handle startup prompts (bypass permissions + workspace trust).
        # Pass the resolved timeout as the outer cap so a containerized profile's
        # longer init budget also governs the startup-prompt handler.
        self._handle_startup_prompts(outer_timeout=init_timeout)

        # Wait for Claude Code prompt to be ready.
        # Accept both IDLE and COMPLETED — some CLI versions show a startup
        # message that get_status() interprets as a completed response.
        # The StatusMonitor push pipeline (FifoReader -> get_status(buffer))
        # drives wait_until_status; it only fires once the provider's own
        # get_status returns IDLE/COMPLETED on Claude-rendered content, so the
        # old stale-zsh-prompt false-IDLE guard is no longer needed.
        if not await wait_until_status(
            self.terminal_id,
            {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
            timeout=init_timeout,
            polling_interval=1.0,
        ):
            raise TimeoutError(f"Claude Code initialization timed out after {init_timeout}s")

        # The status wait fires as soon as the input box RENDERS, but the Ink
        # renderer drops keystrokes for a beat after that — "box rendered" is
        # not "box accepting input". Gate on actual input readiness so the
        # first paste does not race the widget (best effort: a False return
        # proceeds anyway rather than failing init).
        await self.wait_until_input_ready()

        self._initialized = True
        return True

    async def wait_until_input_ready(self, timeout: float = 5.0) -> bool:
        """Settle-check readiness gate for the Ink input box.

        The new-TUI input box matching NEW_TUI_BOX_PATTERN appears one render
        pass before the widget accepts keystrokes. Require the rendered pane
        content to be STABLE across two consecutive captures ~0.5s apart (and
        still showing the input box) before declaring input-ready. A changing
        pane means Ink is still painting startup content (banner, tips, MCP
        status), during which the first keystrokes get dropped.

        Uses capture-pane (rendered screen) rather than the pipe-pane buffer:
        stability of the RENDERED output is the actual readiness signal.
        """
        poll = 0.5
        deadline = time.monotonic() + timeout
        previous: Optional[str] = None
        while time.monotonic() < deadline:
            try:
                current = get_backend().get_history(
                    self.session_name, self.window_name, tail_lines=40
                )
            except Exception as exc:  # backend hiccup: don't fail init for the gate
                logger.warning("input-ready settle check capture failed: %s", exc)
                return False
            if (
                previous is not None
                and current == previous
                and NEW_TUI_BOX_PATTERN.search(strip_terminal_escapes(current))
            ):
                logger.debug("input-ready settle check passed for %s", self.terminal_id)
                return True
            previous = current
            await asyncio.sleep(poll)
        logger.warning(
            "input-ready settle check timed out after %.1fs for %s; proceeding anyway",
            timeout,
            self.terminal_id,
        )
        return False

    def get_status(self, output: str) -> TerminalStatus:
        """Get Claude Code status.

        Two detection paths:

        1. Native path (herdr backend): get_native_status() returns the full
           herdr agent state as a TerminalStatus. When non-None, buffer reads are
           skipped entirely. The only ambiguous case is IDLE -- herdr reports "idle"
           both before any task has been dispatched and after a task completed when
           the user focuses the tab (resetting "done" to "idle"). _task_dispatched
           (set by mark_input_received() on first send_input()) disambiguates:
           IDLE + _task_dispatched=True -> COMPLETED, otherwise -> IDLE.

        2. Buffer path (tmux backend, or herdr backend returning None): runs
           structural regex analysis over the rolling pipe-pane buffer supplied
           by the StatusMonitor push pipeline (FifoReader -> EventBus ->
           StatusMonitor). Uses a "thinking-before-separator" check as the
           primary PROCESSING indicator, plus position-based fallbacks. This
           path never reads tmux itself -- the buffer is passed in as ``output``.

        See: https://github.com/awslabs/cli-agent-orchestrator/issues/104
        """
        # Native status (herdr): when the backend knows agent state, trust it and
        # skip buffer reads. Tmux returns None -- falls through to buffer analysis.
        native = self._resolve_native_status(output)
        if native is not None:
            return native

        # herdr never pushes a buffer (pipe_pane is a no-op there); read live
        # pane content instead of falling through to "no output" on every call.
        output = self._resolve_buffer(output)
        if not output:
            return TerminalStatus.UNKNOWN

        # The StatusMonitor feeds the RAW pipe-pane buffer (cursor-positioning
        # escapes, in-place redraws, OSC titles) — not a tmux-rendered snapshot.
        # Strip escapes / normalize cursor moves to newlines so the structural
        # checks below see clean, line-oriented text. On already-clean input
        # (unit fixtures, capture-pane output) this is a near no-op.
        output = strip_terminal_escapes(output)
        if not output.strip():
            return TerminalStatus.UNKNOWN

        # PRIMARY PROCESSING check: walk backwards from the *last* separator.
        _sep_re = re.compile(r"(?:\x1b\[[0-9;]*m)*\u2500{20,}")
        _sep_positions = [m.start() for m in _sep_re.finditer(output)]
        # If a completion summary ("✻ <Verb>ed for Ns") appears AFTER the last
        # separator, the newest TUI has repainted the finished turn BOXLESS below
        # the last box's bottom border (its own separators flattened out of the
        # cleaned buffer). The spinner still sitting above that separator is then
        # stale, so suppress the spinner-before-separator walk and let the
        # COMPLETED branch win. During genuine processing nothing but the footer
        # follows the last separator, so this never hides a live turn.
        _boxless_completion_tail = False
        if _sep_positions:
            _tail = output[_sep_positions[-1] :]
            _last_summary = None
            for _m in re.finditer(GET_STATUS_COMPLETION_PATTERN, _tail):
                if "Waiting" in _m.group(0):
                    continue  # background-wait line, not a completion summary (GH #392)
                _last_summary = _m
            # The summary only marks the turn finished if no LIVE spinner
            # renders after it. Claude prints interim summaries mid-turn
            # ("✻ Pondered for 8s") and then keeps working — e.g. a handoff
            # MCP call shows "● Calling cao-mcp-server…" with a fresh
            # "✢ Misting… (33s · ↑ 332 tokens)" spinner below the interim
            # summary. Treating that tail as completed mis-reports an active
            # MCP call as COMPLETED (and the StatusMonitor ready-latch then
            # pins it until the next input).
            if _last_summary is not None and not re.search(
                r"[✶✢✽✻✳·*][ \t]+\w*ing\b[^\n]*…", _tail[_last_summary.end() :]
            ):
                _boxless_completion_tail = True
        if _sep_positions and not _boxless_completion_tail:
            pre_sep_lines = output[: _sep_positions[-1]].rstrip("\n").split("\n")
            for line in reversed(pre_sep_lines):
                if re.search(r"[✶✢✽✻✳·][^\n]*\u2026", line):
                    return TerminalStatus.PROCESSING  # spinner before another separator
                if _sep_re.search(line):
                    break  # hit another separator first -- spinner is from a completed task

        # Find the LAST occurrence of each marker for fallback position checks.
        last_processing = None
        for m in re.finditer(PROCESSING_PATTERN, output):
            last_processing = m

        last_idle = None
        for m in re.finditer(IDLE_PROMPT_PATTERN, output):
            last_idle = m

        last_response = None
        for m in re.finditer(RESPONSE_PATTERN, output):
            last_response = m

        # New-TUI completion summary ("✻ Sautéed for 1s"): the newest Claude Code
        # drops the ⏺ marker and shows this past-tense summary above the ❯ box
        # after a finished turn.
        last_completion = None
        for m in re.finditer(GET_STATUS_COMPLETION_PATTERN, output):
            if "Waiting" in m.group(0):
                continue  # background-wait line, not a completion summary (GH #392)
            last_completion = m

        # FALLBACK PROCESSING: spinner visible AND no separator follows it yet
        if last_processing and not re.search(r"\u2500{20,}", output):
            if last_idle is None or last_processing.start() > last_idle.start():
                return TerminalStatus.PROCESSING

        # Check for waiting user answer via the active Ink selection footer.
        if (
            re.search(WAITING_USER_ANSWER_PATTERN, output)
            and not re.search(TRUST_PROMPT_PATTERN, output)
            and not re.search(BYPASS_PROMPT_PATTERN, output)
        ):
            return TerminalStatus.WAITING_USER_ANSWER

        # New Claude Code TUI PROCESSING: the input prompt is BOXED between two
        # separators, and the live spinner renders on the line DIRECTLY ABOVE the
        # box's top border — where the structural "spinner-before-separator" walk
        # above cannot see it (it breaks at the box's top separator). Anchor to
        # the box that actually CONTAINS the last ❯ prompt, then require a spinner
        # on the freshest non-blank line immediately above it. This rejects two
        # false positives the prior "spinner anywhere + any box" gate allowed:
        #   1. a stale spinner left above a response by an interrupted/finished
        #      turn (the line above the box is the response, not the spinner), and
        #   2. a mid-buffer separator-framed region (e.g. a markdown blockquote)
        #      that is not the real input box (it does not contain the last ❯).
        # Older builds (no box) fall through to the legacy ⏺-based logic unchanged.
        input_box = None
        if last_idle is not None:
            for m in NEW_TUI_BOX_PATTERN.finditer(output):
                if m.start() <= last_idle.start() < m.end():
                    input_box = m
        if input_box is not None:
            # Walk up from the box past footer chrome — "⎿ Tip: …" hint lines,
            # blanks, and (GH #459) an own-line effort footer ("● high ·
            # /effort") — render BETWEEN the live spinner and the box's top
            # border, so checking only the single line above the box misses
            # an active spinner (false COMPLETED during MCP calls, or IDLE
            # once fix #1 above stops the footer from false-matching COMPLETED
            # via EXTRACTION_RESPONSE_PATTERN).
            above_lines = output[: input_box.start()].rstrip("\n").split("\n")
            for line in reversed(above_lines[-4:]):
                if (
                    not line.strip()
                    or line.lstrip().startswith("⎿")
                    or EFFORT_FOOTER_LINE_PATTERN.match(line)
                ):
                    continue
                if NEW_TUI_BOX_SPINNER_PATTERN.search(line):
                    return TerminalStatus.PROCESSING
                break

        # COMPLETED: the finished turn left output behind — a "✻ <Verb>ed for Ns"
        # completion summary OR a start-of-line response marker (legacy ⏺ or the
        # newest TUI's ●) — and the input prompt is visible. This is reached only
        # AFTER all the PROCESSING checks above, so any spinner still in the
        # rolling buffer is a STALE frame, not the live turn; no spinner-freshness
        # guard is applied here. (Such a guard wrongly pinned a finished turn at
        # IDLE when the newest TUI clipped the completion summary's duration —
        # "✻ Crunched for " — or rendered the summary on a · / * glyph frame that
        # COMPLETION_SUMMARY_PATTERN excludes; the ● response marker is the robust
        # fallback.) The ● is matched at line start only, so the mid-line
        # footer indicator "… esc to interrupt ● high · /effort" is never
        # counted. The footer's own-line variant ("● high · /effort" at
        # column 0) starts at line start too — that case is excluded by the
        # negative lookahead in EXTRACTION_RESPONSE_PATTERN, not by the
        # line-start anchor (GH #459).
        last_sol_response = None
        for m in re.finditer(EXTRACTION_RESPONSE_PATTERN, output):
            last_sol_response = m

        # BACKGROUND TASK still running (GH #392): the newest TUI prints the
        # turn's text response, shows an idle ❯ box, and renders
        # "✻ Waiting for N dynamic workflow(s) to finish" — a frame that looks
        # COMPLETED to the signature check below while work continues. Two
        # containment guards (review-hardened):
        # 1. Region: the live wait line renders just above the input box, i.e.
        #    within the last few lines of the rolling buffer — restrict the
        #    match to the buffer's final 20 lines so response-body text higher
        #    up can never trigger it.
        # 2. Recency: honor the wait line only while it is the NEWEST activity
        #    marker — once the task finishes, Claude prints a fresh response
        #    and/or completion summary BELOW it, re-enabling the normal
        #    COMPLETED path (a stale wait line can never pin PROCESSING).
        tail_region_start = len(output) - len("\n".join(output.split("\n")[-20:]))
        last_bg_wait = None
        for m in BACKGROUND_WAIT_PATTERN.finditer(output, tail_region_start):
            last_bg_wait = m
        if last_bg_wait is not None and not any(
            marker.start() > last_bg_wait.start()
            for marker in (last_completion, last_response, last_sol_response)
            if marker is not None
        ):
            return TerminalStatus.PROCESSING

        if last_idle is not None and (
            last_completion is not None
            or last_sol_response is not None
            or last_response is not None
        ):
            return TerminalStatus.COMPLETED

        # IDLE: shell prompt visible but no response yet (e.g. just initialized).
        if last_idle:
            return TerminalStatus.IDLE

        return TerminalStatus.UNKNOWN

    # Opt in to pyte rendered-screen detection (gated by CAO_PYTE_STATUS). The
    # detector below is tuned for a COMPOSITED viewport, not the raw stream.
    supports_screen_detection = True

    def get_status_from_screen(self, screen_lines: List[str]) -> TerminalStatus:
        """Detect status from a pyte-composited viewport (escape-free rows).

        Anchors on the bottom of the rendered screen — exactly what a human
        sees — rather than scanning a raw redraw stream. The StatusMonitor only
        calls this on settled / rising-edge frames (quiescence debounce), so it
        does not need to tolerate mid-repaint frames.

        Precedence: a live spinner in the bottom region wins (PROCESSING); then
        the Ink selection footer (WAITING_USER_ANSWER); then, if the BOXED input
        prompt is visible, COMPLETED when a response/completion-summary is on
        screen above it, else IDLE.

        The prompt must be the real input box — a ``❯``/``>`` line adjacent to a
        ``────`` separator — NOT any line containing ``> ``. During launch the
        echoed command (whose system-prompt text contains ``> ``) would
        otherwise read as an idle prompt and declare the terminal ready before
        Claude's TUI has even rendered, breaking init (observed live).
        """
        rows = [ln.rstrip() for ln in screen_lines if ln.strip()]
        if not rows:
            return TerminalStatus.UNKNOWN
        joined = "\n".join(rows)
        bottom = rows[-25:]

        # Live spinner: "✻ <gerund>… (…)" — the boxed-prompt spinner or a bare
        # spinner line. Visible in a composited frame ⇒ genuinely working.
        #
        # Use ONLY the gerund-first NEW_TUI_BOX_SPINNER_PATTERN, not the looser
        # NEW_TUI_SPINNER_PATTERN. The loose pattern ([glyph][^\n]*ing…) is
        # documented (see its definition) as too permissive precisely because
        # its glyph class includes the markdown bullets "·"/"*", so a settled
        # response bullet ending in a gerund + ellipsis ("* …after deploying…")
        # in the bottom region reads as a live spinner — a false PROCESSING that
        # then latches and starves InboxService (delivers only on IDLE/COMPLETED).
        # The raw get_status() path already switched to the tight pattern for the
        # same reason; the screen path must match.
        if any(NEW_TUI_BOX_SPINNER_PATTERN.search(ln) for ln in bottom):
            return TerminalStatus.PROCESSING

        bottom_joined = "\n".join(bottom)
        if (
            re.search(WAITING_USER_ANSWER_PATTERN, bottom_joined)
            and not re.search(TRUST_PROMPT_PATTERN, joined)
            and not re.search(BYPASS_PROMPT_PATTERN, joined)
        ):
            return TerminalStatus.WAITING_USER_ANSWER

        # Background task still running (GH #392): "✻ Waiting for N dynamic
        # workflow(s)…" has no spinner ellipsis, so the spinner check above
        # misses it, while the response + boxed-prompt signature below would
        # read COMPLETED. Checked AFTER the Ink selection footer on purpose: a
        # permission prompt co-rendering with a wait line must surface as
        # WAITING_USER_ANSWER (a security gate the user has to answer), never
        # be masked as "working" — mirrors the raw path's precedence. The
        # composited screen shows only LIVE content — the line disappears on
        # the finished repaint — so presence in the bottom region is a safe
        # PROCESSING signal (no staleness risk, unlike the raw buffer path).
        if any(BACKGROUND_WAIT_PATTERN.search(ln) for ln in bottom):
            return TerminalStatus.PROCESSING

        # Real input box: a prompt line with a "────" rail BOTH within 2 rows
        # above AND within 2 rows below — the "──── / ❯ / ────" box Claude pins
        # to the bottom of the viewport.
        sep_idx = [i for i, ln in enumerate(rows) if re.search(r"─{8,}", ln)]
        # Prompt line: ❯/> followed by whitespace OR alone at end-of-line. The
        # bare-glyph case matters because rows are rstrip()ed: an EMPTY prompt
        # box renders as "❯" + pyte's space padding, which rstrip reduces to a
        # bare "❯" that IDLE_PROMPT_PATTERN (glyph + whitespace) cannot match.
        # We deliberately do NOT require the prompt line to be empty — a ready
        # box carries placeholder text (❯ Try "fix typecheck errors").
        prompt_idx = [i for i, ln in enumerate(rows) if re.search(r"[>❯](?:[\s\xa0]|$)", ln)]
        # Require BOTH rails, not just one nearby separator: during launch the
        # echoed "> " system-prompt quote can land within 2 rows of a single
        # early-painted ──── rule, which a one-sided adjacency misread as a ready
        # prompt (premature IDLE on init — the first task then hits a not-ready
        # agent). The real box always has a rail above AND below the prompt.
        boxed_prompt = any(
            any(0 < pi - si <= 2 for si in sep_idx) and any(0 < si - pi <= 2 for si in sep_idx)
            for pi in prompt_idx
        )
        if boxed_prompt:
            if re.search(
                GET_STATUS_COMPLETION_PATTERN, joined
            ) or EXTRACTION_RESPONSE_PATTERN.search(joined):
                return TerminalStatus.COMPLETED
            return TerminalStatus.IDLE

        return TerminalStatus.UNKNOWN

    @property
    def paste_submit_delay(self) -> float:
        # The newest Claude Code Ink TUI needs noticeably longer than the 0.3s
        # default to settle a bracketed paste before an Enter counts as "submit"
        # rather than a literal newline; a too-early Enter is swallowed and the
        # message sits unsubmitted in the prompt box (observed on Claude Code with
        # the "/effort" + shift+tab bypass UI). 2.0s is conservative.
        return 2.0

    @property
    def accepts_input_while_processing(self) -> bool:
        """Claude Code's Ink TUI buffers pasted input during processing.

        Only true after initialization completes — during startup the REPL
        isn't ready to accept input even though get_status() sees PROCESSING.
        """
        return self._initialized

    def get_idle_pattern_for_log(self) -> str:
        """Return Claude Code IDLE prompt pattern for log files."""
        return IDLE_PROMPT_PATTERN_LOG

    # Start-of-line idle prompt for extraction: ❯ or > at the beginning of a line
    # (after optional ANSI codes).  Mid-line ">" in Java generics, git diffs, HTML
    # etc. must NOT trigger the stop condition.
    _SOL_IDLE_RE = re.compile(r"^\s*(?:\x1b\[[0-9;]*m)*[>❯](?:\x1b\[[0-9;]*m)*[\s\xa0]")

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract Claude's final response message using the ⏺/● response marker."""
        # Find all matches of the response pattern (legacy ⏺ or newest-TUI ●).
        matches = list(re.finditer(EXTRACTION_RESPONSE_PATTERN, script_output))

        if not matches:
            raise ValueError("No Claude Code response found - no ⏺/● pattern detected")

        # Get the last match (final answer)
        last_match = matches[-1]
        start_pos = last_match.end()

        # Extract everything after the last marker until:
        # 1. A start-of-line idle prompt (❯ or >) — the definitive boundary
        # 2. A separator line (the box border above the input prompt)
        # 3. A completion stat line ("✻ Sautéed for 14s" / "✻ Worked for 3s") —
        #    the newest TUI renders this between the response and the prompt box.
        # Using start-of-line anchor avoids false stops on ">" inside
        # response content (Java generics, git diffs, HTML tags, etc.).
        remaining_text = script_output[start_pos:]

        # Split by lines and extract response
        lines = remaining_text.split("\n")
        response_lines = []

        for line in lines:
            clean_line = re.sub(ANSI_CODE_PATTERN, "", line).strip()
            if self._SOL_IDLE_RE.match(line):
                break
            # Match full-width Claude UI separator (20+ U+2500 dashes spanning the line).
            # Table borders also contain ──── runs but always pair with other box-drawing
            # chars (corners, intersections U+2501-U+257F). Claude's separator uses only
            # U+2500 dashes plus optional text — no other box-drawing chars present.
            if re.search(r"─{20,}", clean_line) and not re.search("[━-╿]", clean_line):
                break
            if re.search(COMPLETION_SUMMARY_PATTERN, clean_line):
                break
            # GH #459 follow-up: the exclusion lookahead in
            # EXTRACTION_RESPONSE_PATTERN stops the own-line effort footer from
            # being mistaken for a SECOND response marker, but does nothing
            # about the footer's own text once collection has started from an
            # earlier, real marker — that footer line would otherwise be
            # appended verbatim as trailing garbage on the extracted answer.
            # clean_line is already ANSI-stripped, so this matches on the
            # footer's plain-text shape regardless of surrounding SGR codes.
            if EFFORT_FOOTER_LINE_PATTERN.match(clean_line):
                break

            response_lines.append(clean_line)

        if not response_lines or not any(line.strip() for line in response_lines):
            raise ValueError("Empty Claude Code response - no content found after ⏺/●")

        # Join lines and clean up
        final_answer = "\n".join(response_lines).strip()
        # Remove ANSI codes from the final message
        final_answer = re.sub(ANSI_CODE_PATTERN, "", final_answer)
        return final_answer.strip()

    def exit_cli(self) -> str:
        """Get the command to exit Claude Code."""
        return "/exit"

    def cleanup(self) -> None:
        """Clean up Claude Code provider."""
        self._initialized = False
        # Remove temp files created during initialization
        tmp_dir = CAO_HOME_DIR / "tmp"
        for suffix in (".prompt", ".mcp.json"):
            tmp_file = tmp_dir / f"{self.terminal_id}{suffix}"
            try:
                tmp_file.unlink(missing_ok=True)
            except OSError:
                pass
