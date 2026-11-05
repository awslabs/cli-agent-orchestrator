"""Kiro CLI provider implementation.

This module provides the KiroCliProvider class for integrating with Kiro CLI,
an AI-powered coding assistant that operates through a terminal interface.

Kiro CLI Features:
- Agent-based conversations with customizable profiles
- File system access and code manipulation capabilities
- Interactive permission prompts for sensitive operations
- ANSI-colored output with distinctive prompt patterns

The provider detects the following terminal states:
- IDLE: Agent is waiting for user input (shows agent prompt)
- PROCESSING: Agent is generating a response
- COMPLETED: Agent has finished responding (shows green arrow + response)
- WAITING_USER_ANSWER: Agent is waiting for permission confirmation
- ERROR: Agent encountered an error during processing
"""

import asyncio
import logging
import os
import re
import shlex
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.kiro_engine import KiroEngine, resolve_kiro_engine
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.providers.kiro_attest import (
    KiroAttestationError,
    McpServerRow,
    ToolsPage,
    ToolsPanel,
    check_mcp_panel,
    check_tools_page,
    check_tools_panel,
    join_tools_pages,
    palette_shows_only,
    parse_mcp_panel,
    parse_tools_page,
)
from cli_agent_orchestrator.providers.kiro_capabilities import (
    KiroV3LaunchError,
    build_kiro_command,
    kiro_effort,
)
from cli_agent_orchestrator.providers.kiro_identity import (
    PLAN_MODE_AGENT,
    KiroAgentIdentityError,
    fallback_notice,
    header_agents,
    header_model,
    identity_readable,
    in_plan_mode,
    require_header_identity,
)
from cli_agent_orchestrator.providers.kiro_launch_home import (
    CAO_MCP_SERVER,
    KiroLaunch,
    check_global_home_unchanged,
    kiro_home_path,
    launch_agent_grant,
    valid_terminal_id,
    verify_launch_home,
)
from cli_agent_orchestrator.providers.kiro_v3_schema import KiroV3Refusal
from cli_agent_orchestrator.providers.kiro_workspace import (
    check_kiro_helper_workspace,
    check_kiro_workspace,
)
from cli_agent_orchestrator.services.settings_service import get_server_settings
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.delegation import tools_exceeding
from cli_agent_orchestrator.utils.ephemeral_names import routes_to_ephemeral_store
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status
from cli_agent_orchestrator.utils.text import strip_terminal_escapes
from cli_agent_orchestrator.utils.tool_mapping import kiro_v3_granted_mcp_servers

logger = logging.getLogger(__name__)

# =============================================================================
# Regex Patterns for Kiro CLI Output Analysis
# =============================================================================

# Green arrow pattern indicates the start of an agent response (escape-stripped)
# Example: "> Here is the code you requested..."
GREEN_ARROW_PATTERN = r"^>\s*"

# SGR (colour) escape codes only. Used by get_status, which strips colour but
# MUST preserve carriage returns and cursor-movement sequences: the permission
# detection counts idle prompts per newline-delimited line, and Kiro renders
# active prompts with \r in-place redraws (same line, no \n). strip_terminal_
# escapes would normalise \r -> \n and split those redraws onto separate lines,
# making an active permission prompt look idle (inbox would then deliver during
# a permission prompt — see test_permission_prompt_detection).
ANSI_CODE_PATTERN = r"\x1b\[[0-9;]*m"

# Additional escape sequences that may appear in terminal output
ESCAPE_SEQUENCE_PATTERN = r"\[[?0-9;]*[a-zA-Z]"

# Control characters to strip from final output. Tab and newline stay: they
# are the layout of the reply, and without them a multi-line reply (a report
# template from a handoff, for example) came back as one line.
CONTROL_CHAR_PATTERN = r"[\x00-\x08\x0b-\x1f\x7f-\x9f]"

# Legacy-UI IDLE prompt pattern for log files (with ANSI codes)
IDLE_PROMPT_PATTERN_LOG = r"\x1b\[38;5;\d+m\[.+?\].*\x1b\[38;5;\d+m>\s*\x1b\[\d*m"

# =============================================================================
# New TUI Patterns (Kiro CLI without --legacy-ui)
# =============================================================================

# New TUI idle prompt: "Ask a question or describe a task ↵"
# Case-insensitive match; comma between "question" and "or" is optional
# (older versions used lowercase with comma, v1.29+ uses capitalized without)
NEW_TUI_IDLE_PATTERN = r"[Aa]sk a question,? or describe a task"

# New TUI IDLE prompt pattern for log files (with ANSI codes)
NEW_TUI_IDLE_PATTERN_LOG = r"[Aa]sk a question,? or describe a task"

# TUI separator line: horizontal bar (────) used to delimit sections.
# Require 20+ chars to avoid matching short markdown separators in agent output.
TUI_SEPARATOR_PATTERN = r"^[─]{20,}$"

# Non-SGR CSI sequences (erase-line \x1b[2K, cursor moves, etc.) — everything
# except colour codes ending in 'm'. Compiled once at module scope because
# get_status() is on a hot path (called per output burst); recompiling per call
# was avoidable overhead. Used in Check 6 to strip TUI chrome so the separator
# regex can anchor on the raw ─── characters.
_NON_SGR_CSI = re.compile(r"\x1b\[[0-9;?]*[^m]")

# TUI Credits line marks response completion. Two shapes so far:
#   up to 2.2x:  "▸ Credits: 0.20 • Time: 29s"
#   2.25.0:      "▸ Credits: turn 0.20 • session 0.20 | Time: 29s"
# The 2.25 form put the word "turn" between "Credits:" and the number, which
# the old pattern did not accept: every turn on 2.25 then fell through to the
# separator fallback, and that fails too because the new "Trust All Tools
# active" band sits between the last two separators with a single line of
# content. Result was IDLE forever after a finished task (never COMPLETED),
# reproduced against a live 2.25.0 pane on 2026-09-29. Accept an optional
# "turn" (and any other single word) before the first number.
TUI_CREDITS_PATTERN = r"▸\s*Credits:\s*(?:[A-Za-z]+\s+)?[\d.]+"

# 2.25's turn/session form is adopted because that release also moved the
# assistant text onto a bullet-prefixed line. Unlike the pre-2.25 layout, the
# reply is not reliably separated from the echoed prompt by a blank line, so
# extraction uses the credits anchors and the bullet line as the reply start.
TUI_225_CREDITS_PATTERN = r"▸\s*Credits:\s*turn\s+[\d.]+"
# A colored capture: bullet rows that carry a truecolor SGR.
TUI_225_COLORED_BULLET_PATTERN = r"\x1b\[38;2;\d+;\d+;\d+m•"
# A row at column 0 that starts with a bullet: a reply, a tool row, or a
# reasoning row. An echoed prompt is indented, so its bullets do not match.
TUI_225_REPLY_PATTERN = r"^•\s+\S"
# Tool rows ("• Shell ...", "• assign") and reasoning rows ("• Thought for
# 2s...") have child rows that start with "╰" (arguments, output, the thought).
# The lines of a reply are plain indented text. The colors of the rows are not
# a boundary: 2.25 drew the reply blue, and 2.29 draws it orange.
TUI_225_CHILD_ROW_PATTERN = r"^\s+╰"
# A reasoning row with its thought collapsed has no child row.
TUI_225_THOUGHT_ROW_PATTERN = r"^•\s+Thought for \d"

# Reading the pane command costs about 0.1 s of tmux calls; the exit check reads
# it at most this often (see KiroCliProvider._exited_to_shell).
EXIT_CHECK_INTERVAL_S = 2.0

# TUI processing indicator: ghost text shown while agent is working.
# kiro-cli 2.11+ replaced "Kiro is working" with "Thinking..." (with an
# optional "(esc to cancel)" suffix). Match either variant.
TUI_PROCESSING_PATTERN = r"Kiro is working|Thinking\.\.\."

# TUI initialization indicator: shown during startup before chat is ready.
# Kiro TUI renders the idle prompt placeholder ("Ask a question or describe
# a task") *before* the "● Initializing..." phase completes, which caused a
# premature IDLE verdict. "Initializing..." is cleared by Kiro once startup
# finishes, so its presence unconditionally means PROCESSING (unlike the
# "Kiro is working" ghost text, which can linger as stale after a redraw).
#
# Also covers the MCP-server boot line "M of N mcp servers initialized.
# ctrl-c to start chatting now" — Kiro shows this *before* the idle prompt
# is interactive, so a paste sent during this window is absorbed by the
# pre-prompt boot screen and silently dropped (observed during e2e
# allowed-tools tests).
#
# kiro-cli 2.8.x also shows "Initializing · type to queue a message" during
# boot (different from the "Initializing..." with three dots).
#
# ``(?<!\d)`` lets the server count start only where a run of digits starts.
# Without it the search restarted inside every digit run and re-scanned the
# rest of the run each time, so a long run of digits anywhere in the buffer
# cost time quadratic in its length (CWE-1333). The matches are unchanged:
# whenever a match exists inside a run, one exists from the run's first digit,
# and the leftmost one wins.
TUI_INITIALIZING_PATTERN = (
    r"Initializing\.\.\."
    r"|(?<!\d)\d+ of \d+ mcp servers initialized\.\s*ctrl-c to start chatting now"
    r"|Initializing\s*·\s*type to queue a message"
)

# Legacy permission prompt: "Allow this action? ... [y/n/t]:". The text between
# the question and the bracketed choice varies by version ("Use 't' to trust
# (always allow) this tool for the session."), so detection looks for the
# question and then, in order, "[", "y", "/", "n", "/", "t" and "]:" anywhere
# after it, across lines -- the subsequence the old regex
# ``Allow this action\?.*?\[.*?y.*?/.*?n.*?/.*?t.*?\]:`` (re.DOTALL) spelled
# out with six lazy gaps. Run over the whole buffer on every status poll, that
# regex tried every ordered combination of those characters whenever the
# question was present without a closing "]:" after it: polynomial of degree
# ~6 in the text that followed (CWE-1333). See _legacy_permission_prompt_spans.
_PERMISSION_PROMPT_QUESTION = "Allow this action?"
_PERMISSION_PROMPT_STEPS = ("[", "y", "/", "n", "/", "t")
_PERMISSION_PROMPT_CLOSE = "]:"


def _legacy_permission_prompt_spans(text: str) -> List[tuple[int, int]]:
    """``(start, end)`` of every legacy permission prompt in ``text``, in order.

    Returns the spans ``re.finditer`` returned for the old six-gap regex (see
    ``_PERMISSION_PROMPT_QUESTION``), in one forward pass. Taking the EARLIEST
    occurrence of each step finds exactly the regex's match, because an earlier
    choice never leaves less text for the steps after it. When the steps fail
    after one question they fail after every later question too (each later
    search sees a suffix of the same text), so the scan can stop there instead
    of retrying from each later question as the regex engine did.
    """
    spans: List[tuple[int, int]] = []
    pos = 0
    while True:
        start = text.find(_PERMISSION_PROMPT_QUESTION, pos)
        if start == -1:
            return spans
        cursor = start + len(_PERMISSION_PROMPT_QUESTION)
        for step in _PERMISSION_PROMPT_STEPS:
            cursor = text.find(step, cursor)
            if cursor == -1:
                return spans
            cursor += len(step)
        close = text.find(_PERMISSION_PROMPT_CLOSE, cursor)
        if close == -1:
            return spans
        end = close + len(_PERMISSION_PROMPT_CLOSE)
        spans.append((start, end))
        pos = end


def has_single_line_permission_prompt(text: str) -> bool:
    """True when one line of ``text`` holds a whole legacy permission prompt.

    The reading without re.DOTALL: the question and every step up to "]:" on
    the same line, which is how the AG-UI approval classifier has always
    matched it. Linear, like _legacy_permission_prompt_spans, per line.
    """
    return any(_legacy_permission_prompt_spans(line) for line in text.split("\n"))


# TUI permission prompt: shown instead of legacy [y/n/t] format.
# Requires all three options together to avoid false positives on "Yes"/"No" in agent output.
# Kiro 2.11 renders the same three-way choice with different wording for
# subagent spawning: "Yes, single permission / Trust, always allow in this
# session / No (Tab to edit)". Both alternatives anchor on the full Yes/Trust/No
# option layout so agent output that merely mentions a permission prompt (or
# quotes "subagent requires approval") can't flip status to WAITING_USER_ANSWER
# — the bare header alternative was dropped for that reason. In practice
# --trust-all-tools suppresses this prompt anyway; detection is a safety net.
TUI_PERMISSION_PATTERN = (
    r"Yes\s+No\s+Always [Aa]llow"
    r"|Yes,\s*single permission[\s\S]{0,200}?Trust,\s*always allow[\s\S]{0,200}?No"
)

# TUI trust-all-tools acceptance dialog: shown at startup when --trust-all-tools is passed.
# Matches the footer navigation chrome to avoid false positives on warning text in agent output.
# Must be anchored to bottom screen region (see get_status WAITING check) to avoid stale matches.
TUI_TRUST_ALL_TOOLS_FOOTER = r"esc to cancel · ↑↓ to navigate · ↵ to select"

# Distinctive consent-dialog body text. TUI_TRUST_ALL_TOOLS_FOOTER above is
# kiro's GENERIC list-selector chrome — an update/login/onboarding selector
# renders it identically — so before answering we require this trust-specific
# line, and require the ❯ cursor to sit on "No, exit" so Down lands on
# "Yes, I accept" (not "Yes, and don't ask again"). Anything else fails closed.
TUI_TRUST_ALL_TOOLS_BODY = r"Kiro is running in trust all tools mode"
TUI_TRUST_ALL_TOOLS_CURSOR = r"❯\s*No, exit"

# Bottom pane lines scanned for the consent dialog (mirrors codex's window).
STARTUP_PROMPT_BOTTOM_LINES = 15

# Reads of the screen for the TUI header after the prompt is ready (see
# KiroCliProvider._verify_agent_identity). A screen with no header row, only a
# partial (elided or wrapped) name, or Kiro's plan mode is read again; a screen
# whose header rows name any other agent is refused on the first read.
# Provisional (#1074): about 2 s, after which a screen without a header (a
# kiro-cli TUI change, say) is refused rather than trusted.
IDENTITY_HEADER_READS = 5
IDENTITY_HEADER_READ_INTERVAL_S = 0.5

# =============================================================================
# Kiro v3 dialogs (#1167). Used for v3 terminals only; v2 parsing is unchanged.
# =============================================================================

# Each dialog is matched as a whole block in the bottom of the screen (title,
# every option, its own footer) with no idle prompt after it, so a reply that
# quotes one cannot flip the status. The footers are the dialogs' own: v3's
# generic list chrome ("esc to close · …") alone is never enough.
#
# Every pattern here is applied to one line of agent-painted text at a time,
# and only its verdict is read, so none may cost more than linear time in the
# line (CWE-1333):
#
# * the approval title and the declined-trust line start their attempt at the
#   line start and skip to the first place the old pattern
#   (``\S.*\brequires approval\s*$``, ``Not trusted: .*Kiro exited ...``) could
#   start: its first non-blank, its first "Not trusted: ". Unanchored, the
#   search restarted at every later one and re-scanned the rest of the line
#   from each. A match from a later start on the line is also reachable from
#   the first, so text matches exactly when it did, and the match ends where
#   it did (it now begins at its line's start). ``\Z`` stands for the old
#   ``$``, which ``(?m)`` would let match before any newline: ``\s*`` takes a
#   final newline, so ``\s*$`` only ever matched at the end of the string;
# * the trust title and the footers (here and _V3_SELECTOR_FOOTER) use ``\A``,
#   which is what ``^`` means without re.MULTILINE, so their matches are
#   unchanged; unlike ``^`` it stays one start position if re.MULTILINE is
#   ever added, where ``^\s*`` would re-scan a run of blank lines from each.
#   The option rows keep ``^``: _options_follow applies them with ``.match``.
V3_DIALOG_BOTTOM_LINES = 30
_V3_APPROVAL_TITLE = re.compile(r"(?m)^[^\S\n]*+\S[^\n]*\brequires approval\s*\Z")
_V3_APPROVAL_OPTIONS = (
    re.compile(r"^\s*(?:❯\s*)?Allow once\s*$"),
    re.compile(r"^\s*(?:❯\s*)?Deny once\s*$"),
    re.compile(r"^\s*(?:❯\s*)?Create deny rule\b"),
)
_V3_APPROVAL_FOOTER = re.compile(r"\A\s*esc to close · ↑↓ to navigate · ↵ to select\b")
_V3_TRUST_TITLE = re.compile(r"\A\s*Trust this workspace\?\s*$")
_V3_TRUST_OPTIONS = (
    re.compile(r"^\s*(?:❯\s*)?Trust, continue\s*$"),
    re.compile(r"^\s*(?:❯\s*)?Don't trust, exit\s*$"),
)
_V3_TRUST_FOOTER = re.compile(r"\A\s*esc to exit · ↑↓ to navigate · ↵ to select\s*$")
_V3_TRUST_DECLINED = re.compile(
    r"(?m)^(?:(?!Not trusted: ).)*+Not trusted: .*Kiro exited without loading this folder"
)
# v2's "Switch to V3" offer, which also rewrites agent files if accepted. A
# pinned launch should never show it; if one does, nobody answers it.
_V3_UPSELL_TITLE = re.compile(r"Switch to V3 today\.")
_V3_UPSELL_OPTIONS = (
    re.compile(r"^\s*(?:❯\s*)?Switch to V3 and upgrade my configs\s*$"),
    re.compile(r"^\s*(?:❯\s*)?Remind me later\s*$"),
    re.compile(r"^\s*(?:❯\s*)?Don't ask again\s*$"),
)


def _options_follow(lines: List[str], start: int, options: tuple, window: int = 8) -> Optional[int]:
    """Index of the last option row when every option follows ``start`` in order."""
    index = start
    for option in options:
        for candidate in range(index + 1, min(len(lines), index + 1 + window)):
            if option.match(lines[candidate]):
                index = candidate
                break
        else:
            return None
    return index


def kiro_v3_dialog(output: str) -> Optional[str]:
    """Which v3 dialog the bottom of ``output`` shows, if any.

    ``approval`` (the per-call approval picker), ``trust`` (the workspace-trust
    dialog), ``trust_declined`` (Kiro exited from it), ``upsell`` (v2's Switch
    to V3 offer), or ``None``. A dialog with an idle prompt below it is history,
    not a dialog.
    """
    lines = [line.rstrip() for line in strip_terminal_escapes(output).splitlines() if line.strip()]
    lines = lines[-V3_DIALOG_BOTTOM_LINES:]

    def after_has_idle(index: int) -> bool:
        return any(re.search(NEW_TUI_IDLE_PATTERN, line) for line in lines[index + 1 :])

    for title, options, footer, name in (
        (_V3_APPROVAL_TITLE, _V3_APPROVAL_OPTIONS, _V3_APPROVAL_FOOTER, "approval"),
        (_V3_TRUST_TITLE, _V3_TRUST_OPTIONS, _V3_TRUST_FOOTER, "trust"),
    ):
        titles = [i for i, line in enumerate(lines) if title.search(line)]
        if not titles:
            continue
        last_option = _options_follow(lines, titles[-1], options)
        if last_option is None:
            continue
        footers = [i for i in range(last_option + 1, len(lines)) if footer.match(lines[i])]
        if not footers or after_has_idle(footers[0]):
            continue
        if name == "trust" and any(_V3_TRUST_DECLINED.search(l) for l in lines[footers[0] :]):
            return "trust_declined"
        return name
    upsell = [i for i, line in enumerate(lines) if _V3_UPSELL_TITLE.search(line)]
    if upsell:
        last_option = _options_follow(lines, upsell[-1], _V3_UPSELL_OPTIONS)
        if last_option is not None and not after_has_idle(last_option):
            return "upsell"
    return None


# Any v3 selector's own footer: the approval picker's and the trust dialog's
# (``esc to close`` / ``esc to exit``, then ``↑↓ to navigate · ↵ to select``).
# At startup no selector is expected and CAO answers none (#1167).
_V3_SELECTOR_FOOTER = re.compile(r"\A\s*esc to (?:close|exit) · ↑↓ to navigate · ↵ to select\b")


def kiro_v3_startup_selector(output: str) -> bool:
    """Whether the bottom of ``output`` is a v3 selector with no idle prompt below it.

    Covers dialogs CAO has no pattern for (the uncaptured v3 trust-all consent
    is the expected one): the startup wait refuses on it instead of waiting out
    the init timeout. Used at startup, and after the attestation closes a
    ``/mcp`` panel; status parsing does not read it.
    """
    lines = [line.rstrip() for line in strip_terminal_escapes(output).splitlines() if line.strip()]
    lines = lines[-V3_DIALOG_BOTTOM_LINES:]
    footers = [i for i, line in enumerate(lines) if _V3_SELECTOR_FOOTER.match(line)]
    if not footers:
        return False
    return not any(re.search(NEW_TUI_IDLE_PATTERN, line) for line in lines[footers[-1] + 1 :])


# Reads of the pane during the v3 attestation (#1167), about 2.5 s each time
# (6 reads, 5 intervals): for the typed palette, for a panel's frame after
# Enter or after a Down scrolls ``/tools``, and for the prompt after Escape
# closes ``/mcp``. Provisional.
V3_ATTEST_READS = 6
V3_ATTEST_READ_INTERVAL_S = 0.5
# v3's ``/mcp`` panel is a snapshot of Kiro's server list, taken as it opens: an
# open panel never changes, so reading it again cannot show a server that has
# since connected. A server still loading is checked again by closing ``/mcp``
# and opening it again, 1 s after the first open, then 2 s, then every 3 s. It
# refuses at the first check after ``provider_init_timeout`` has passed since
# the first open: up to one open's waits (about 7.5 s) after the timeout.
V3_MCP_REOPEN_DELAY_S = 1.0
V3_MCP_REOPEN_DELAY_MAX_S = 3.0
_V3_TOOLS_DIALOG = "a dialog appeared on /tools; it was not answered and nothing more was typed"


def _shows_v3_dialog(screen: str) -> bool:
    """Whether a v3 dialog or startup selector is on ``screen`` (never answered)."""
    return kiro_v3_dialog(screen) is not None or kiro_v3_startup_selector(screen)


@dataclass(frozen=True)
class KiroAttestation:
    """What a v3 pane's ``/tools`` and ``/mcp list`` showed once they passed (#1167)."""

    tags: tuple[str, ...]
    #: The tool count of each MCP server ``/mcp list`` showed running.
    mcp_tools: tuple[tuple[str, int], ...]
    #: The header's model column, audited and never enforced.
    model: Optional[str]


# =============================================================================
# Error Detection
# =============================================================================

# Strings that indicate the agent encountered an error
ERROR_INDICATORS = ["Kiro is having trouble responding right now"]


class KiroCliProvider(BaseProvider):
    """Provider for Kiro CLI tool integration.

    This provider manages the lifecycle of a Kiro CLI chat session within a tmux window,
    including initialization, status detection, and response extraction.

    Attributes:
        terminal_id: Unique identifier for this terminal instance
        session_name: Name of the tmux session containing this terminal
        window_name: Name of the tmux window for this terminal
        _agent_profile: Name of the Kiro agent profile to use
        _idle_prompt_pattern: Regex pattern for detecting IDLE state
    """

    @classmethod
    def honors_model(
        cls,
        agent_profile: Optional[str],
        profile: Optional["AgentProfile"],
        requested_model: Optional[str],
    ) -> bool:
        return True

    @classmethod
    def honors_effort(
        cls,
        agent_profile: Optional[str],
        profile: Optional["AgentProfile"],
    ) -> bool:
        """Only an ephemeral helper applies a decided effort (#1083, #1167 Phase 2).

        A helper runs on the v3 engine only, and its launch object carries the launch
        plan's effort, else the stored ``kiroConfig.effort``, to ``--effort``. An installed
        agent is unchanged: v2 passes no effort, and v3 reads ``kiroConfig.effort`` from
        the agent's own profile, so a decided effort would never reach it.
        """
        return agent_profile is not None and routes_to_ephemeral_store(agent_profile)

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        agent_profile: str,
        allowed_tools: Optional[list] = None,
        engine: Optional[KiroEngine] = None,
        model: Optional[str] = None,
        launch: Optional[KiroLaunch] = None,
    ):
        """Initialize Kiro CLI provider with terminal context.

        Args:
            terminal_id: Unique identifier for this terminal
            session_name: Name of the tmux session
            window_name: Name of the tmux window
            agent_profile: Name of the Kiro agent profile to use (e.g., "developer")
            allowed_tools: Optional list of CAO tool names the agent is allowed to use
            engine: Resolved Kiro engine. Terminal creation probes it before this provider exists.
            model: Explicit per-call override for profile.model (see
                _get_profile_model), e.g. a handoff/assign caller pinning a
                specific model for one worker without a dedicated profile.
            launch: An ephemeral helper's per-launch home and kiro-cli, prepared
                by terminal creation before the window existed (#1167 Phase 2).
                None for an installed agent. A helper runs on v3 only.
        """
        super().__init__(terminal_id, session_name, window_name, allowed_tools)
        self._initialized = False
        self._input_received = False
        # The cached result of the exit check (see _exited_to_shell).
        self._exit_checked_at: Optional[float] = None
        self._exited = False
        self._agent_profile = agent_profile
        self._engine = resolve_kiro_engine(persisted=engine)
        self._model = model
        # The evidence that this runtime's TUI header named the agent (#1074).
        # Only _verify_agent_identity sets it, and only once the check passed;
        # terminal_service records it on the row before any task is typed.
        # initialize() returning is not evidence on its own.
        self.identity_verified = False
        self._launch = launch
        if launch is not None and self._engine != KiroEngine.V3:
            raise ValueError("an ephemeral Kiro helper runs on the v3 engine only")
        # What the v3 attestation read, once it passed; audited for a helper.
        self.attestation: Optional[KiroAttestation] = None

        # Build dynamic prompt pattern based on agent profile
        # This pattern matches various Kiro prompt formats after ANSI stripping:
        # - [developer] >       (basic prompt)
        # - [developer] !>      (prompt with pending changes)
        # - [developer] 50% >   (prompt with progress indicator)
        # - [developer] λ >     (prompt with lambda symbol)
        # - [developer] 50% λ > (combined progress and lambda)
        self._idle_prompt_pattern = (
            rf"\[{re.escape(self._agent_profile)}\]\s*(?:\d+%\s*)?(?:\u03bb\s*)?!?>\s*"
        )
        # The legacy "Allow this action? ... [y/n/t]:" prompt is found by
        # _legacy_permission_prompt_spans (a linear scan, not a regex).

        # New TUI header pattern: "agent_name · model · ◔ N%", applied to one
        # row at a time. The attempt starts at the row start and skips to the
        # FIRST "agent_name ·" on it: unanchored, the search restarted at every
        # occurrence of the name and re-scanned the rest of the row from each,
        # and ``\s+.*`` could split the blanks after "·" every way (CWE-1333).
        # A later header on the row is reachable from the first, so a row
        # matches exactly when it did; ``\s++`` is equivalent because ``.*``
        # takes back any blanks it gave up.
        agent = re.escape(self._agent_profile)
        self._new_tui_header_pattern = (
            rf"(?m)^(?:(?!{agent}\s+·\s)[^\n])*+{agent}\s+·\s++.*·\s+◔\s*\d+%"
        )

    @property
    def paste_enter_count(self) -> int:
        """Kiro CLI 2.11 needs 2 Enters after bracketed paste to submit.

        The first Enter is consumed by the TUI (finalizes the paste), the
        second submits the message. Older kiro versions only needed 1.
        """
        return 2

    @property
    def paste_submit_delay(self) -> float:
        """Kiro 2.11's TUI needs a longer delay after bracketed paste before
        the Enter key registers as submit rather than being swallowed."""
        return 1.0

    def _exited_to_shell(self) -> bool:
        """Return True when the pane is back at the shell that ran before kiro-cli.

        Read at most every EXIT_CHECK_INTERVAL_S: the lookup costs about 0.1 s
        of tmux calls (more inside a PTY proxy), and get_status() reaches it on
        every read of a busy frame. A stale "still running" only keeps the
        terminal PROCESSING for up to that long after kiro-cli exits.
        """
        now = time.monotonic()
        if self._exit_checked_at is None or now - self._exit_checked_at >= EXIT_CHECK_INTERVAL_S:
            current_cmd = get_backend().get_pane_current_command(
                self.session_name, self.window_name
            )
            self._exited = current_cmd == self.shell_baseline
            self._exit_checked_at = now
        return self._exited

    def mark_input_received(self) -> None:
        """Track that input was sent, enabling separator-free completion detection."""
        super().mark_input_received()
        self._input_received = True

    @property
    def extraction_tail_lines(self) -> int:
        """Capture enough scrollback for no-credits extraction.

        The no-credits fallback (_extract_tui_message) needs both the
        start_separator (before the response) and end_separator (TUI frame
        before the idle prompt) in the same capture window. For long agent
        responses the start_separator can be hundreds of lines above the
        idle prompt. 2000 lines covers responses up to ~1800 lines of
        content, which exceeds any realistic single-turn agent response.
        """
        return 2000

    def _get_profile_model(self) -> Optional[str]:
        """Return the explicit per-call model override if given, else
        profile.model if the agent profile can be loaded, else None.

        Best-effort: historically the Kiro CLI provider has not required the
        CAO agent profile to be loadable at runtime (kiro-cli has its own
        agent store). A missing or unparseable profile must not block launch.
        """
        if self._model:
            return self._model
        if self._launch is not None:
            # A helper's name is reserved: load_agent_profile cannot read it.
            return self._launch.model
        try:
            profile = load_agent_profile(self._agent_profile)
        except (FileNotFoundError, RuntimeError) as exc:
            logger.debug(
                "Profile '%s' not loadable by CAO; skipping --model resolution: %s",
                self._agent_profile,
                exc,
            )
            return None
        return profile.model or None

    def _get_profile_effort(self) -> Optional[str]:
        """``kiroConfig.effort`` for a v3 launch's ``--effort`` (#1167), or None.

        Best-effort like ``_get_profile_model``: a profile that cannot be read
        sets no effort. Creation already probed the flag from the same profile.
        A helper's effort comes from its launch object.
        """
        if self._launch is not None:
            return self._launch.effort
        try:
            return kiro_effort(load_agent_profile(self._agent_profile))
        except Exception as exc:  # noqa: BLE001 - a missing profile sets no effort
            logger.debug("Profile '%s' sets no Kiro effort: %s", self._agent_profile, exc)
            return None

    async def initialize(self) -> bool:
        """Initialize Kiro CLI provider by starting kiro-cli chat command.

        This method:
        1. Waits for the shell to be ready in the tmux window
        2. Sends the kiro-cli chat command with the configured agent profile
        3. Waits for the agent to reach IDLE state (ready for input)
        4. Checks that the TUI header names the configured agent profile

        Returns:
            True if initialization was successful

        Raises:
            TimeoutError: If shell or Kiro CLI initialization times out
            KiroAgentIdentityError: If kiro-cli started a different agent (for
                example its ``kiro_default`` fallback) or no header was read
        """
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        v3 = self._engine == KiroEngine.V3

        # Step 1: Wait for shell prompt to appear in the tmux window
        # This ensures the terminal is ready before we send commands
        init_timeout = get_server_settings()["provider_init_timeout"]
        if not await wait_for_shell(self.terminal_id, timeout=init_timeout):
            raise TimeoutError(f"Shell initialization timed out after {init_timeout}s")

        # Capture the shell process name before launching kiro — used later to detect kiro exit
        self.shell_baseline = get_backend().get_pane_current_command(
            self.session_name, self.window_name
        )

        # Step 2: Start the Kiro CLI chat session.
        #
        # --trust-all-tools: bypass Kiro CLI's permission prompts when CAO
        # launches with --yolo (allowed_tools=['*']). Without this, every
        # tool invocation re-prompts, blocking assign/handoff flows.
        # --model: honor profile.model so workflows can pin a specific model.
        #
        # UI mode: always the default TUI. --legacy-ui is NOT an option — see
        # build_kiro_command's docstring: it conflicts with --agent-engine=v2
        # and otherwise silently selects the v1 engine, which exposes no MCP
        # tools, so the agent could not assign/handoff/report_outcome at all.
        model = self._get_profile_model()

        if self._launch is not None:
            # Stage C (#1167 Phase 2): the sealed home must still be exactly what
            # creation prepared before kiro-cli reads it.
            await asyncio.to_thread(verify_launch_home, self._launch.home, sealed=True)

        # kiro-cli 2.11 introduced a "subagent requires approval" prompt that
        # blocks MCP tool calls that spawn subagents (e.g. cao-mcp-server's
        # assign/handoff). Current CAO policy always bypasses Kiro's
        # interactive approval prompt regardless of the profile: CAO enforces
        # tool scoping at its own layers (profile allowedTools + MCP
        # allowlist), and there is no human at the terminal in headless
        # orchestration to answer. Without this, a supervisor invoking
        # assign() hangs indefinitely on the approval dialog.
        base_args = build_kiro_command(
            self._engine,
            self._agent_profile,
            model=model,
            yolo=True,
            effort=self._get_profile_effort() if v3 else None,
            **self._launch_command_options(),
        )
        command = shlex.join(base_args)
        # Arm the StatusMonitor stickiness gate before launching the CLI so
        # the IDLE → PROCESSING → IDLE/COMPLETED transition is honored.
        status_monitor.notify_input_sent(self.terminal_id)
        get_backend().send_keys(self.session_name, self.window_name, command)

        # Step 3: Wait for Kiro CLI to fully initialize and show the agent prompt.
        # Accept both IDLE and COMPLETED — some CLI versions show a startup
        # message that get_status() interprets as a completed response.
        # _wait_ready_accepting_trust_dialog also auto-answers the trust
        # consent dialog (see its docstring) if a wrapper version still shows
        # one; --trust-tools='*' does not raise it, so that is now a safety net.
        #
        # There is no --legacy-ui retry. It used to exist for older wrappers
        # whose TUI could not initialize headlessly, but it is no longer a
        # usable fallback: on a v2 launch kiro-cli errors out on the flag
        # combination, and a bare --legacy-ui retry would "succeed" into the v1
        # engine with no MCP tools — turning a loud timeout into an agent that
        # looks healthy and cannot orchestrate. Failing here is the honest
        # outcome.
        ready = (
            await self._wait_ready_refusing_v3_dialogs()
            if v3
            else await self._wait_ready_accepting_trust_dialog()
        )
        if not ready:
            raise TimeoutError(
                "Kiro CLI initialization timed out waiting for the agent prompt "
                f"(engine={self._engine.value}, profile={self._agent_profile!r})"
            )

        # Step 4: the prompt being ready does not mean the requested agent is
        # running. Nothing is typed into the pane until the header names it.
        await self._verify_agent_identity()

        if v3:
            # Step 5 (#1167): the header names the agent, not its tools. Hold
            # the identity back until the workspace is re-checked and the pane's
            # own /tools and /mcp panels match the installed grant.
            self.identity_verified = False
            if self._launch is not None:
                await self._recheck_helper_after_header(self._launch)
            else:
                await self._recheck_v3_workspace()
            self.attestation = await self._attest_v3_tools_and_mcp()
            self.identity_verified = True

        self._initialized = True
        return True

    async def _verify_agent_identity(self) -> None:
        """Raise unless every TUI header row names this provider's agent (#1074).

        kiro-cli answers an ``--agent`` it cannot load by starting its built-in
        ``kiro_default`` agent, which has every built-in tool, and reaches the
        same ready prompt. Called after the prompt is ready and before
        ``initialize`` returns, so on a refusal no task has been typed, and the
        pane receives no keys from here. The creation path then closes the
        window: a synchronous create rolls back, and a deferred create reclaims
        the runtime even when an external owner keeps the failure row.
        Provisional (#1074): no ``/exit`` is typed into the wrong agent either.

        Reads the rendered screen, not the scrollback, so the requested name in
        the echoed launch command is never what is matched (see
        ``kiro_identity.header_agents``). A screen without a header row is read
        again a few times, because the header can trail the prompt by a frame,
        and so is one whose header name is only partial or that shows plan mode;
        if that persists it is refused (``header_not_found``, or for plan mode
        ``identity_mismatch``). kiro-cli's fallback notice for this agent on any
        read refuses the launch too (#1167), whatever the header names.

        Raises:
            KiroAgentIdentityError: ``identity_mismatch`` or ``header_not_found``.
        """
        backend = get_backend()
        observed: List[str] = []
        plan_mode = False
        fallback: Optional[str] = None
        for attempt in range(IDENTITY_HEADER_READS):
            if attempt:
                await asyncio.sleep(IDENTITY_HEADER_READ_INTERVAL_S)
            try:
                screen = await asyncio.to_thread(
                    backend.get_history,
                    self.session_name,
                    self.window_name,
                    strip_escapes=True,
                    visible_only=True,
                )
            except Exception as exc:  # noqa: BLE001 - an unread screen is not identity
                logger.debug("kiro_cli: screen read for the identity check failed: %s", exc)
                continue
            observed = header_agents(screen) if isinstance(screen, str) else []
            if fallback is None and isinstance(screen, str):
                fallback = fallback_notice(screen, self._agent_profile)
            plan_mode = (
                bool(observed)
                and self._agent_profile != PLAN_MODE_AGENT
                and isinstance(screen, str)
                and in_plan_mode(screen)
            )
            if identity_readable(observed) and not plan_mode:
                break
        try:
            require_header_identity(
                self._agent_profile, observed, plan_mode=plan_mode, fallback=fallback
            )
        except KiroAgentIdentityError as exc:
            logger.error(
                "kiro_cli: terminal %s refused before any input: %s", self.terminal_id, exc
            )
            raise
        self.identity_verified = True

    async def _wait_ready_accepting_trust_dialog(self) -> bool:
        """Wait for the agent prompt, auto-answering the trust-all-tools dialog.

        CAO always launches kiro-cli with ``--trust-all-tools`` (there is no
        human at the terminal to answer per-tool permission prompts in headless
        orchestration; CAO enforces tool scoping at its own profile/MCP layers).
        Since kiro-cli 2.1, ``--trust-all-tools`` opens a one-time startup
        consent dialog *before* the chat prompt is interactive:

            ❯ No, exit
              Yes, I accept
              Yes, and don't ask again

        The default TUI shows this dialog on kiro-cli 2.16.1 (verified), so the
        earlier "force --legacy-ui to skip it" workaround no longer helps and
        init just times out on the dialog. This helper is applied to the
        ``--legacy-ui`` fallback path too, so if a kiro build shows the dialog
        there as well it is handled without a version check. get_status()
        classifies the dialog as WAITING_USER_ANSWER off generic selector
        chrome, so before answering we VERIFY the dialog body and the ❯ cursor
        line (fail closed on anything else), then select **"Yes, I accept"**
        (Down, Enter) — the one-line-down option. We deliberately do NOT pick
        "Yes, and don't ask again", which would persist a trust-all-tools
        bypass into the user's kiro config; CAO's acceptance is scoped to this
        ephemeral session only.

        Returns:
            True if the terminal reached IDLE or COMPLETED (dialog answered if
            it was verified and shown), False if it timed out or a
            WAITING_USER_ANSWER could not be verified as the consent dialog.
        """
        init_timeout = float(get_server_settings()["provider_init_timeout"])
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        start = time.monotonic()
        ready = await wait_until_status(
            self.terminal_id,
            {
                TerminalStatus.IDLE,
                TerminalStatus.COMPLETED,
                TerminalStatus.WAITING_USER_ANSWER,
            },
            timeout=init_timeout,
        )
        if not ready:
            return False
        # Ready-flap window: wait_until_status returned on WAITING_USER_ANSWER,
        # but get_status is re-read here rather than trusted from the wait. If it
        # has since flapped to IDLE/COMPLETED, treat the terminal as ready and do
        # NOT send dialog keys (a blind Down+Enter into a live prompt would be a
        # stray message). Low probability given the sticky latch, but explicit.
        # Off the loop: get_status() can fork a tmux capture-pane for a PROCESSING
        # terminal (status_monitor.py's stale-PROCESSING fallback).
        current = await asyncio.to_thread(status_monitor.get_status, self.terminal_id)
        if current != TerminalStatus.WAITING_USER_ANSWER:
            return True

        # WAITING_USER_ANSWER is classified from TUI_TRUST_ALL_TOOLS_FOOTER,
        # which is kiro's GENERIC list-selector chrome. Verify the dialog BODY
        # and the ❯ cursor position before answering — a blind Down+Enter on
        # some other startup selector would pick an arbitrary option. Same
        # fail-closed shape as codex's directory-trust check. On any mismatch,
        # fall through to the normal timeout/fallback path.
        backend = get_backend()
        pane = strip_terminal_escapes(
            re.sub(ANSI_CODE_PATTERN, "", backend.get_history(self.session_name, self.window_name))
        )
        bottom = "\n".join(pane.splitlines()[-STARTUP_PROMPT_BOTTOM_LINES:])
        if not (
            re.search(TUI_TRUST_ALL_TOOLS_BODY, bottom)
            and re.search(TUI_TRUST_ALL_TOOLS_CURSOR, bottom)
        ):
            logger.warning(
                "kiro_cli: WAITING_USER_ANSWER but the trust-all-tools consent "
                "dialog (body + '❯ No, exit') was not verified; not answering"
            )
            return False

        # Verified consent dialog: cursor on "No, exit", so Down lands on
        # "Yes, I accept" (session-scoped — NOT "Yes, and don't ask again").
        logger.info(
            "kiro_cli: answering --trust-all-tools startup consent dialog "
            "with 'Yes, I accept' (session-scoped)"
        )
        # Arm the PROCESSING latch before the keystrokes, like every other
        # send_special_key path, so answering the dialog isn't blocked by the
        # WAITING_USER_ANSWER we just observed.
        status_monitor.notify_input_sent(self.terminal_id)
        backend.send_special_key(self.session_name, self.window_name, "Down")
        backend.send_special_key(self.session_name, self.window_name, "Enter")
        # Bound the post-accept wait by the time already spent, so total startup
        # stays within provider_init_timeout rather than up to 2x it.
        remaining = max(0.0, init_timeout - (time.monotonic() - start))
        return await wait_until_status(
            self.terminal_id,
            {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
            timeout=remaining,
        )

    async def _wait_ready_refusing_v3_dialogs(self) -> bool:
        """Wait for a v3 prompt; refuse, sending no key, on any startup dialog (#1167).

        ``--trust-workspace`` should skip the workspace-trust dialog; if it is on
        screen anyway CAO never answers it, because "Trust, continue" would save
        the folder as trusted in the user's Kiro home and "Don't trust, exit"
        ends the session. No v3 trust-all consent text has been captured, so no
        other startup dialog is answered either.
        """
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        init_timeout = float(get_server_settings()["provider_init_timeout"])
        # Every poll also reads the pane, so a dialog the status cannot express
        # (an unrecognised selector reads PROCESSING, a declined trust dialog
        # ERROR) refuses at once instead of waiting out the timeout.
        ready = await wait_until_status(
            self.terminal_id,
            {TerminalStatus.IDLE, TerminalStatus.COMPLETED, TerminalStatus.WAITING_USER_ANSWER},
            timeout=init_timeout,
            on_poll=self._refuse_v3_startup_screen,
        )
        if not ready:
            return False
        current = await asyncio.to_thread(status_monitor.get_status, self.terminal_id)
        if current != TerminalStatus.WAITING_USER_ANSWER:
            return True
        await self._refuse_v3_startup_screen()
        raise KiroV3LaunchError(
            "kiro_v3_startup_dialog",
            "kiro-cli is waiting for an answer at startup on a screen CAO does not "
            "recognize for v3; it was not answered and no task was sent.",
        )

    async def _refuse_v3_startup_screen(self) -> None:
        """Refuse, sending no key, if the pane shows any v3 dialog at startup (#1167).

        ``kiro_v3_trust_dialog`` for the workspace-trust dialog (``--trust-workspace``
        was not honoured), also once declined (Kiro exited); ``kiro_v3_startup_dialog``
        for any other dialog or selector. The creation path then closes the window.
        """
        screen = await self._read_screen()
        dialog = kiro_v3_dialog(screen)
        if dialog == "trust":
            raise KiroV3LaunchError(
                "kiro_v3_trust_dialog",
                "kiro-cli showed the workspace-trust dialog although CAO passed "
                "--trust-workspace, so the flag was not honoured. CAO does not answer "
                "it; no task was sent.",
            )
        if dialog == "trust_declined":
            raise KiroV3LaunchError(
                "kiro_v3_trust_dialog",
                "kiro-cli showed the workspace-trust dialog although CAO passed "
                "--trust-workspace, and it was declined, so Kiro exited. CAO never "
                "answers that dialog; no task was sent.",
            )
        if dialog is not None or kiro_v3_startup_selector(screen):
            raise KiroV3LaunchError(
                "kiro_v3_startup_dialog",
                "kiro-cli showed a startup dialog CAO does not answer on v3; it was not "
                "answered and no task was sent.",
            )

    async def _read_screen(self) -> str:
        screen = await asyncio.to_thread(
            get_backend().get_history,
            self.session_name,
            self.window_name,
            strip_escapes=True,
            visible_only=True,
        )
        return screen if isinstance(screen, str) else ""

    async def _recheck_v3_workspace(self) -> None:
        """Run the workspace check again from the pane's own cwd, after the header (#1167)."""
        cwd = await asyncio.to_thread(
            get_backend().get_pane_working_directory, self.session_name, self.window_name
        )
        if not cwd:
            raise KiroV3LaunchError(
                "kiro_v3_bad_cwd", "the pane's working directory could not be read for the re-check"
            )
        await asyncio.to_thread(check_kiro_workspace, cwd)

    def _launch_command_options(self) -> dict[str, str]:
        """The absolute kiro-cli and the per-launch KIRO_HOME, for a helper only."""
        if self._launch is None:
            return {}
        return {
            "executable": self._launch.kiro_cli.path,
            "kiro_home": str(self._launch.home.home),
        }

    async def _recheck_helper_after_header(self, launch: KiroLaunch) -> None:
        """Stage D2 for a helper (#1167 Phase 2); raises before any task is typed.

        The workspace check runs again from the pane's own cwd, ``HOME``'s rule
        included; then the check after Kiro's legacy copies, which have run by
        now (a changed placeholder or a new ``~/.kiro`` marker refuses
        ``kiro_v3_global_home:changed``); then the home must still be exactly
        what creation sealed.
        """
        try:
            cwd = await asyncio.to_thread(
                get_backend().get_pane_working_directory, self.session_name, self.window_name
            )
            if not cwd:
                raise KiroV3LaunchError(
                    "kiro_v3_bad_cwd",
                    "the pane's working directory could not be read for the re-check",
                )
            user_home = launch.home.global_home.user_home
            await asyncio.to_thread(check_kiro_helper_workspace, cwd, user_home)
            await asyncio.to_thread(check_global_home_unchanged, launch.home)
            await asyncio.to_thread(verify_launch_home, launch.home, sealed=True)
        except KiroV3Refusal as exc:
            logger.error(
                "kiro_cli: terminal %s refused before any input: %s", self.terminal_id, exc
            )
            raise

    async def _open_v3_panel(self, command: str) -> str:
        """Type a slash command, confirm its palette, press Enter, and return the screen.

        Typed key by key, never as a paste, and never followed by Tab: on
        2.35.1 Tab after ``/tools`` selects ``trust-all``. Enter is pressed only
        once the palette shows the plain completion for ``command``.
        """
        backend = get_backend()
        for key in command:
            backend.send_special_key(self.session_name, self.window_name, key)
        for attempt in range(V3_ATTEST_READS):
            if attempt:
                await asyncio.sleep(V3_ATTEST_READ_INTERVAL_S)
            if palette_shows_only(await self._read_screen(), command):
                break
        else:
            backend.send_special_key(self.session_name, self.window_name, "Escape")
            code = "kiro_v3_tools_mismatch" if command == "/tools" else "kiro_v3_mcp_not_ready"
            raise KiroAttestationError(
                code, f"the {command} palette did not appear; nothing was selected"
            )
        backend.send_special_key(self.session_name, self.window_name, "Enter")
        return await self._read_screen()

    async def _wait_v3_mcp_running(self, servers: List[str]) -> Dict[str, McpServerRow]:
        """Open ``/mcp`` until no server is still loading; return its rows (#1167).

        v3's ``/mcp`` panel is a snapshot of Kiro's server list, taken as it
        opens: an open panel never changes, so reading it again cannot show a
        server that has since connected. So while a server is loading, the
        panel is closed (Escape, then the prompt must be back) and opened
        again, each time typed and checked like the first, with a short wait
        in between. The time is checked after each open's palette and panel
        reads: a server still loading at the first check after
        ``provider_init_timeout`` has passed since the first open refuses. So
        the refusal can come up to one open's waits (about 7.5 s) after the
        timeout, never later.

        Anything else that is not running (a failed, extra or missing server,
        or a layout CAO cannot read) refuses at once, with no further open. So
        do a palette that does not appear and a dialog on screen once a panel
        closes. Every refusal leaves the panel closed.
        """
        backend = get_backend()
        timeout = float(get_server_settings()["provider_init_timeout"])
        start = time.monotonic()
        delay = V3_MCP_REOPEN_DELAY_S
        opens = 0
        while True:
            opens += 1
            screen = await self._open_v3_panel("/mcp")
            try:
                rows = await self._read_v3_mcp_panel(screen)
                loading = check_mcp_panel(rows, servers)
            except KiroAttestationError:
                backend.send_special_key(self.session_name, self.window_name, "Escape")
                raise
            elapsed = time.monotonic() - start
            # Server names come from the agent file and statuses from the
            # panel's own columns; no other screen text is logged.
            logger.debug(
                "kiro_cli: terminal %s /mcp open %d at %.1f s: %s",
                self.terminal_id,
                opens,
                elapsed,
                ", ".join(
                    f"{name} {row.status.split()[-1]} ({row.tools} tools)"
                    for name, row in sorted(rows.items())
                )
                or "no servers",
            )
            if loading and elapsed >= timeout:
                backend.send_special_key(self.session_name, self.window_name, "Escape")
                raise KiroAttestationError(
                    "kiro_v3_mcp_not_ready",
                    f"MCP servers still loading after {elapsed:.1f} s "
                    f"({opens} open{'' if opens == 1 else 's'} of /mcp): {list(loading)}",
                )
            await self._close_v3_mcp_panel()
            if not loading:
                return rows
            remaining = timeout - (time.monotonic() - start)
            await asyncio.sleep(max(0.0, min(delay, remaining)))
            delay = min(delay * 2, V3_MCP_REOPEN_DELAY_MAX_S)

    async def _read_v3_mcp_panel(self, screen: str) -> Dict[str, McpServerRow]:
        """Parse the ``/mcp list`` panel just opened, reading again while its frame trails.

        Tolerance for a slow-rendering body is about 2.5 s (``V3_ATTEST_READS``
        reads), down from the old loop's ~20 s; intentional and symmetric with
        ``/tools``, and it fails closed (refuse, never accept) on a slow render.
        """
        reads = 1
        while True:
            try:
                return parse_mcp_panel(screen)
            except KiroAttestationError:
                if reads >= V3_ATTEST_READS:
                    raise
            reads += 1
            await asyncio.sleep(V3_ATTEST_READ_INTERVAL_S)
            screen = await self._read_screen()

    async def _close_v3_mcp_panel(self) -> None:
        """Press Escape on the open ``/mcp`` panel; return once the empty prompt is back.

        No other key is sent until then, so the next ``/mcp``, ``/tools`` or the
        task is typed into the prompt. A dialog on screen instead refuses at
        once and is not answered; so does a prompt that is not back within
        the short render wait.
        """
        get_backend().send_special_key(self.session_name, self.window_name, "Escape")
        for attempt in range(V3_ATTEST_READS):
            if attempt:
                await asyncio.sleep(V3_ATTEST_READ_INTERVAL_S)
            screen = await self._read_screen()
            if kiro_v3_dialog(screen) is not None or kiro_v3_startup_selector(screen):
                raise KiroAttestationError(
                    "kiro_v3_mcp_not_ready",
                    "a dialog appeared after /mcp closed; it was not answered and "
                    "nothing more was typed",
                )
            if re.search(NEW_TUI_IDLE_PATTERN, screen):
                return
        raise KiroAttestationError("kiro_v3_mcp_not_ready", "the prompt did not return after /mcp")

    async def _read_v3_tools_panel(self) -> ToolsPanel:
        """Open ``/tools``, read every screen of it, close it, and join the screens (#1167).

        A list longer than the pane scrolls: Kiro shows the rows that fit, with
        ``↓ more`` below them, and its Down key moves the list by one row. So
        while the screen just read is the open panel with ``↓ more`` on it, and
        no dialog, CAO presses Down once and reads again; the new screen must
        be the last one moved down by exactly one row. Down is the only key
        sent into the panel, never Enter or Tab. Escape closes it, on a pass
        and on every refusal, and is the last key; it is not pressed if a
        dialog is on screen by then, and a pass then refuses. The joined rows
        must hold every row the title declares, each once
        (``join_tools_pages``), before ``check_tools_panel`` sees them.

        Bounded by the declared count (a screen still showing ``↓ more`` once
        that many rows have been read refuses) and by ``provider_init_timeout``
        from the open, checked before each Down: so a refusal on time comes at
        most one screen's render wait (about 3 s) after it.
        """
        backend = get_backend()
        timeout = float(get_server_settings()["provider_init_timeout"])
        start = time.monotonic()
        screen = await self._open_v3_panel("/tools")
        try:
            page, screen = await self._read_v3_tools_page(screen, None)
            pages = [page]
            while page.more_below:
                read = len(pages[0].rows) + len(pages) - 1
                if read >= page.declared:
                    raise KiroAttestationError(
                        "kiro_v3_tools_mismatch",
                        f"the /tools panel shows more rows than the {page.declared} it declares",
                    )
                elapsed = time.monotonic() - start
                if elapsed >= timeout:
                    raise KiroAttestationError(
                        "kiro_v3_tools_mismatch",
                        f"the /tools panel was still scrolling after {elapsed:.0f} s "
                        f"({read} of {page.declared} tags read)",
                    )
                if _shows_v3_dialog(screen):
                    raise KiroAttestationError("kiro_v3_tools_mismatch", _V3_TOOLS_DIALOG)
                backend.send_special_key(self.session_name, self.window_name, "Down")
                page, screen = await self._read_v3_tools_page(await self._read_screen(), page)
                pages.append(page)
            panel = join_tools_pages(pages)
        except KiroAttestationError:
            await self._close_v3_tools_panel()
            raise
        if not await self._close_v3_tools_panel():
            raise KiroAttestationError("kiro_v3_tools_mismatch", _V3_TOOLS_DIALOG)
        # Counts only; no screen text is logged.
        logger.debug(
            "kiro_cli: terminal %s /tools read: %d tags, %d on screen at a time, %d Down",
            self.terminal_id,
            len(panel.tags) + len(panel.mcp_tools),
            len(pages[0].rows),
            len(pages) - 1,
        )
        return panel

    async def _read_v3_tools_page(
        self, screen: str, previous: Optional[ToolsPage]
    ) -> Tuple[ToolsPage, str]:
        """Parse one ``/tools`` screen, reading again while its frame trails (#1167).

        With no ``previous`` the panel has just opened: it must start at its
        first row, and a panel that fits on one screen must already be whole.
        Otherwise the screen was read after one Down on ``previous`` and must
        be ``previous`` moved down by one row (``check_tools_page``). Up to
        ``V3_ATTEST_READS`` reads, ``V3_ATTEST_READ_INTERVAL_S`` apart; then
        the last read's reason refuses. Returns the page and its screen.
        """
        reads = 1
        while True:
            try:
                page = parse_tools_page(screen)
                check_tools_page(page, previous)
                if previous is None and not page.more_below:
                    join_tools_pages([page])
                return page, screen
            except KiroAttestationError:
                if reads >= V3_ATTEST_READS:
                    raise
            reads += 1
            await asyncio.sleep(V3_ATTEST_READ_INTERVAL_S)
            screen = await self._read_screen()

    async def _close_v3_tools_panel(self) -> bool:
        """Press Escape on ``/tools`` unless the pane now shows a dialog; say whether it did.

        The screen is read first: a dialog is never answered, not even with
        Escape, and nothing more is typed into it.
        """
        if _shows_v3_dialog(await self._read_screen()):
            return False
        get_backend().send_special_key(self.session_name, self.window_name, "Escape")
        return True

    async def _attestation_grant(self) -> tuple[list[str], list[str], list[str]]:
        """The grant and servers the panels are held to, and the launch's own allowlist.

        An installed agent's come from its installed universal file. A
        helper's come from its per-launch file, read again and hash-checked,
        never from the installed store; its launch allowlist is the grant CAO
        rendered, and its only server is cao-mcp-server (#1167 Phase 2).
        """
        if self._launch is not None:
            grant, servers = await asyncio.to_thread(launch_agent_grant, self._launch.home)
            if servers != [CAO_MCP_SERVER]:
                raise KiroAttestationError(
                    "kiro_v3_mcp_not_ready", f"the helper's file configures {servers}"
                )
            return grant, servers, list(self._launch.grant)
        from cli_agent_orchestrator.services.install_service import installed_kiro_v3_grant

        launch = self._allowed_tools
        if launch is None or "*" in launch:
            raise KiroAttestationError(
                "kiro_v3_tools_mismatch", "a v3 launch needs a restricted allowlist"
            )
        installed = installed_kiro_v3_grant(self._agent_profile)
        if installed is None:
            raise KiroAttestationError(
                "kiro_v3_tools_mismatch", "the installed agent file is not a v3 universal file"
            )
        return installed[0], installed[1], list(launch)

    async def _attest_v3_tools_and_mcp(self) -> KiroAttestation:
        """Refuse unless ``/mcp list`` and ``/tools`` match the agent file's grant (#1167).

        Runs after the header check and before the identity is recorded, so on
        a refusal no task has been typed; the creation path then reclaims the
        window. Slash commands cost no credits.

        The file is read again here, after Kiro loaded it. Its grant must still
        fit this launch's and name every server it configures, so a file
        rewritten after the pre-allocation check can widen neither what the
        panels are compared with nor which servers they may show. Returns what
        the panels showed, for a helper's audit.

        ``/mcp`` is opened first, and opened again until no server is still
        loading (see ``_wait_v3_mcp_running``), and only then ``/tools``: a
        loading server has no rows on ``/tools`` yet, so a per-tool grant read
        before then would be checked against nothing. A ``/tools`` list longer
        than the pane is read screen by screen (see ``_read_v3_tools_panel``).
        """
        grant, servers, launch = await self._attestation_grant()
        exceeding = tools_exceeding(grant, launch)
        if exceeding:
            raise KiroAttestationError(
                "kiro_v3_tools_mismatch",
                f"the installed grant now exceeds this launch's: {exceeding}",
            )
        ungranted = sorted(set(servers) - set(kiro_v3_granted_mcp_servers(grant, servers)))
        if ungranted:
            raise KiroAttestationError(
                "kiro_v3_mcp_not_ready",
                f"the installed file configures ungranted servers {ungranted}",
            )
        model = header_model(await self._read_screen(), self._agent_profile)

        rows = await self._wait_v3_mcp_running(servers)

        panel = await self._read_v3_tools_panel()
        check_tools_panel(panel, grant)
        if not await wait_until_status(
            self.terminal_id,
            {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
            timeout=float(get_server_settings()["provider_init_timeout"]),
        ):
            raise KiroAttestationError("kiro_v3_mcp_not_ready", "the prompt did not return")
        return KiroAttestation(
            tags=tuple(sorted(panel.tags)),
            mcp_tools=tuple(sorted((name, row.tools) for name, row in rows.items())),
            model=model,
        )

    def get_status(self, output: str) -> TerminalStatus:
        """Get Kiro CLI status by analyzing terminal output.

        Status detection logic (in priority order):
        1. No output → UNKNOWN
        2. No IDLE prompt visible → PROCESSING (agent is generating response)
        3. Error indicators present → ERROR
        4. Permission prompt visible → WAITING_USER_ANSWER
        5. Green arrow + prompt visible → COMPLETED (response ready)
        6. Only prompt visible → IDLE (waiting for input)

        Native (herdr): if the backend can report a native agent_status, trust it
        and skip buffer parsing. On herdr the pipe-pane buffer is never fed, so
        ``output`` is empty and the regex path below can never leave UNKNOWN —
        which is why kiro never reached IDLE and init timed out. The shared
        BaseProvider helper consults the backend and disambiguates herdr's
        ambiguous "idle" via _task_dispatched (set by mark_input_received).
        """
        native = self._resolve_native_status(output)
        if native is not None:
            return native

        # herdr never pushes a buffer (pipe_pane is a no-op there); read live
        # pane content instead of falling through to "no output" on every call.
        output = self._resolve_buffer(output)
        if not output:
            return TerminalStatus.UNKNOWN

        # Strip ONLY SGR colour codes for pattern matching. Carriage returns and
        # cursor-movement sequences are intentionally preserved: the permission
        # check below counts idle prompts per "\n"-delimited line and relies on
        # \r in-place redraws staying on the same logical line (see
        # ANSI_CODE_PATTERN). Do not switch this to strip_terminal_escapes.
        clean_output = re.sub(ANSI_CODE_PATTERN, "", output)

        # Check 0 (v3 only, #1167): the approval picker, the workspace-trust
        # dialog and v2's upsell wait for an answer, whatever startup or busy
        # text is still in the buffer; never PROCESSING. A declined trust
        # dialog means Kiro exited.
        if self._engine == KiroEngine.V3:
            dialog = kiro_v3_dialog(clean_output)
            if dialog == "trust_declined":
                return TerminalStatus.ERROR
            if dialog is not None:
                return TerminalStatus.WAITING_USER_ANSWER

        # Check 0a: Detect idle prompts early — required for the position-aware
        # processing checks below.
        old_idle_matches = list(re.finditer(self._idle_prompt_pattern, clean_output))
        new_tui_idle_matches = list(re.finditer(NEW_TUI_IDLE_PATTERN, clean_output))
        has_idle_prompt = old_idle_matches[0] if old_idle_matches else None
        has_new_tui_idle = bool(new_tui_idle_matches)

        # Check 0b: TUI startup — Kiro emits "● Initializing..." or
        # "0 of N mcp servers initialized. ctrl-c to start chatting now"
        # before the prompt is interactive; pastes during this window are
        # silently absorbed by the boot screen.
        #
        # The new TUI renders an idle-prompt PLACEHOLDER ("Ask a question
        # or describe a task") even during boot, so NEW_TUI_IDLE_PATTERN
        # matching after the init line does NOT mean init has finished —
        # we must still report PROCESSING.
        #
        # In --legacy-ui (and once the new TUI is interactive), the actual
        # "[agent] N% > " idle prompt only appears AFTER init has completed.
        # The "0 of N mcp servers initialized..." line is drawn once at
        # boot and redrawn over by the TUI; under the event-driven FIFO
        # pipeline that line still sits in the rolling byte stream forever
        # (issue surfaced by yolo --legacy-ui timing out 11/11 e2e tests).
        # Treat the init line as PROCESSING only when no real ``[agent] >``
        # idle prompt appears AFTER the last init match — mirrors the
        # TUI_PROCESSING_PATTERN ghost-text guard below.
        #
        # kiro-cli 2.8.x TUI shows "● Initializing..." (animated spinner)
        # during MCP boot. Once MCP finishes, the TUI redraws completely:
        # the spinner disappears and the idle prompt appears. In the raw
        # FIFO buffer, the idle prompt text lands AFTER the last spinner
        # frame, so checking new_tui_idle_matches after last_init_pos is a
        # reliable post-init signal. During the spinner, only spinner frames
        # are written to the stream; the idle prompt only enters the buffer
        # when the TUI redraws after init completes.
        init_matches = list(re.finditer(TUI_INITIALIZING_PATTERN, clean_output))
        if init_matches:
            last_init_pos = init_matches[-1].end()
            real_idle_after_init = any(m.start() > last_init_pos for m in old_idle_matches)
            new_idle_after_init = any(m.start() > last_init_pos for m in new_tui_idle_matches)
            if not real_idle_after_init and not new_idle_after_init:
                return TerminalStatus.PROCESSING

        # Check 2: Look for TUI "Kiro is working" ghost text.
        # Kiro TUI redraws the screen in-place, so the buffer can retain a stale
        # "Kiro is working" line from an earlier render even after the agent has
        # finished and the idle prompt has appeared below it.  Only return
        # PROCESSING when no idle prompt appears *after* the last match.
        tui_working_matches = list(re.finditer(TUI_PROCESSING_PATTERN, clean_output))
        if tui_working_matches:
            last_working_pos = tui_working_matches[-1].end()
            idle_after_working = any(
                m.start() > last_working_pos for m in new_tui_idle_matches + old_idle_matches
            )
            if not idle_after_working:
                return TerminalStatus.PROCESSING

        # Check 2a: Trust-all-tools acceptance dialog at startup (no idle prompt case).
        # Must come BEFORE the "no idle prompt → PROCESSING" check below, since
        # the dialog has no idle prompt but should classify WAITING_USER_ANSWER.
        # Anchored to bottom 20 lines to avoid false positives when the warning text
        # appears in scrollback with an idle prompt below (same class as issue #405).
        lines = clean_output.split("\n")
        bottom_region = "\n".join(lines[-20:]) if len(lines) > 20 else clean_output
        footer_match = re.search(TUI_TRUST_ALL_TOOLS_FOOTER, bottom_region)
        if footer_match:
            after_footer = bottom_region[footer_match.end() :]
            has_idle_after = re.search(self._idle_prompt_pattern, after_footer) or re.search(
                NEW_TUI_IDLE_PATTERN, after_footer
            )
            if not has_idle_after:
                return TerminalStatus.WAITING_USER_ANSWER

        # Check 3: If no idle prompt found, determine if kiro is still running.
        # Compare current pane command against the shell captured before kiro launched.
        # If they match, kiro has exited and the shell is showing again → IDLE.
        #
        # Gated on self._initialized: between send_keys("kiro-cli chat ...")
        # and the moment kiro-cli exec's, the pane's current command still
        # matches shell_baseline ("zsh"), and the buffer hasn't shown any
        # idle prompt yet. Without this gate, get_status() returns IDLE
        # immediately after launch, which lets pre-init pastes get absorbed
        # by Kiro's boot screen and silently dropped.
        if not has_idle_prompt and not has_new_tui_idle:
            if self._initialized and self.shell_baseline and self._exited_to_shell():
                return TerminalStatus.IDLE
            return TerminalStatus.PROCESSING

        # Check 2: Look for known error messages in the output
        if any(indicator.lower() in clean_output.lower() for indicator in ERROR_INDICATORS):
            return TerminalStatus.ERROR

        # Check for permission prompt — legacy [y/n/t] or TUI "Yes, No, Always Allow"
        # Active prompt: 0-1 lines with idle prompt (CLI renders prompt on next line)
        # Stale prompt: 2+ lines with idle prompt (user answered, agent continued)
        # Line-based counting handles \r redraws (same line, no \n) correctly
        perm_spans = _legacy_permission_prompt_spans(clean_output)
        tui_perm_spans = [m.span() for m in re.finditer(TUI_PERMISSION_PATTERN, clean_output)]
        all_perm_spans = perm_spans + tui_perm_spans
        # Sort by position so we use the last permission prompt regardless of type
        all_perm_spans.sort(key=lambda span: span[0])
        if all_perm_spans:
            after_last_perm = clean_output[all_perm_spans[-1][1] :]
            lines_after = after_last_perm.split("\n")
            idle_lines = sum(
                1
                for line in lines_after
                if re.search(self._idle_prompt_pattern, line)
                or re.search(NEW_TUI_IDLE_PATTERN, line)
            )
            if idle_lines <= 1:
                return TerminalStatus.WAITING_USER_ANSWER

        # Check 4: Look for completed response (green arrow indicates agent output)
        # Must verify that an idle prompt appears AFTER the response
        green_arrows = list(re.finditer(GREEN_ARROW_PATTERN, clean_output, re.MULTILINE))
        if green_arrows:
            # Find if there's an idle prompt after the last green arrow
            last_arrow_pos = green_arrows[-1].end()
            idle_prompts = list(re.finditer(self._idle_prompt_pattern, clean_output))

            for prompt in idle_prompts:
                if prompt.start() > last_arrow_pos:
                    logger.debug(f"get_status: returning COMPLETED")
                    return TerminalStatus.COMPLETED

            # Also check new TUI idle pattern after the last green arrow
            for prompt in new_tui_idle_matches:
                if prompt.start() > last_arrow_pos:
                    logger.debug("get_status: returning COMPLETED (new TUI)")
                    return TerminalStatus.COMPLETED

            # Has green arrow but no prompt after it - still processing
            return TerminalStatus.PROCESSING

        # Check 5: TUI completion — Credits marker + idle prompt after it.
        # In pure TUI mode, there are no green arrows. Completion is indicated
        # by "▸ Credits:" followed by the idle prompt.
        credits_matches = list(re.finditer(TUI_CREDITS_PATTERN, clean_output))
        if credits_matches:
            last_credits_pos = credits_matches[-1].end()
            for prompt in new_tui_idle_matches:
                if prompt.start() > last_credits_pos:
                    logger.debug("get_status: returning COMPLETED (TUI credits)")
                    return TerminalStatus.COMPLETED
            for prompt in old_idle_matches:
                if prompt.start() > last_credits_pos:
                    logger.debug("get_status: returning COMPLETED (TUI credits + legacy idle)")
                    return TerminalStatus.COMPLETED
            # Credits marker found but no idle prompt after it — still processing
            return TerminalStatus.PROCESSING

        # Check 6: Kiro CLI 2.3.0+ — no Credits marker emitted. Detect completion
        # by finding the bordered "response box" — two separators with ≥2 lines
        # of non-empty content between them — followed by the idle prompt.
        #
        # kiro-cli 2.11+ keeps the "ask a question or describe a task" placeholder
        # visible in the raw buffer at all times (it's part of the TUI chrome),
        # so a bare idle-prompt match does NOT mean the agent finished. Requiring
        # a full response box (two separators + content in between) distinguishes
        # a real completion frame from the pre-response idle state where the
        # placeholder is visible but the agent hasn't produced output yet.
        # A minimal completion is user query + agent response = 2 non-empty lines
        # inside the box.
        if has_new_tui_idle:
            lines = clean_output.split("\n")

            # Strip non-SGR CSI (erase-line \x1b[2K, cursor moves, etc.) so
            # the separator regex can see the raw ─── characters. The top-of-
            # function strip only removes SGR codes ending in 'm' (color) —
            # kiro's TUI prefixes many lines with \x1b[2K which would prevent
            # the separator regex from anchoring at start-of-line.
            # (_NON_SGR_CSI is compiled once at module scope — hot path.)
            def _sep_line(line: str) -> str:
                return _NON_SGR_CSI.sub("", line).strip()

            idle_line_idx = None
            for i in range(len(lines) - 1, -1, -1):
                if re.search(NEW_TUI_IDLE_PATTERN, lines[i]):
                    idle_line_idx = i
                    break
            if idle_line_idx is not None:
                # Find the last separator BEFORE the idle line.
                last_sep_idx = None
                for i in range(idle_line_idx - 1, -1, -1):
                    if re.search(TUI_SEPARATOR_PATTERN, _sep_line(lines[i])):
                        last_sep_idx = i
                        break
                if last_sep_idx is not None:
                    # Find the previous separator, and require content between them.
                    for j in range(last_sep_idx - 1, -1, -1):
                        if re.search(TUI_SEPARATOR_PATTERN, _sep_line(lines[j])):
                            content = [l for l in lines[j + 1 : last_sep_idx] if _sep_line(l)]
                            if len(content) >= 2:
                                logger.debug(
                                    "get_status: returning COMPLETED (TUI no-credits fallback)"
                                )
                                return TerminalStatus.COMPLETED
                            break

        # Default: Agent is IDLE, waiting for user input
        return TerminalStatus.IDLE

    # The TUI's live prompt row: "› " then the first row of the text, or the
    # placeholder / status text the TUI shows there when nothing is typed.
    _COMPOSER_PROMPT_RE = re.compile(r"^\s*›(?:\s(.*)|\s*)$")
    # Text the TUI itself paints in the prompt row; none of it is user input.
    _COMPOSER_CHROME_RE = re.compile(
        rf"{NEW_TUI_IDLE_PATTERN}|{TUI_PROCESSING_PATTERN}|{TUI_INITIALIZING_PATTERN}"
        r"|type to queue a message|Type to steer"
    )

    def extract_current_composer(self, rendered_pane: str) -> Optional[str]:
        """Return the text sitting unsubmitted in the TUI's › prompt, if it is on screen.

        Issue #78 (PR #1004 review, P2-1): the inbox recovery can only send the
        bare Enter that submits a paste whose Enter was swallowed when the
        provider can read its own composer; without this method kiro_cli had
        no recovery at all and the next inbox message was pasted on top of the
        first ({MSG1}{MSG2}).

        The TUI pins its prompt to the bottom of the viewport below the last
        "────" separator and a header row (agent · model · ◔ N% · auth). The
        header text varies with the agent and the auth state, so the reader
        anchors on the last separator and the first "›" row after it; the
        composer is that row minus the glyph plus the indented continuation
        rows a wrapped or multi-line paste occupies (real captures:
        fixtures/COMPOSER_FIXTURES.md). Earlier "›" rows are transcript echoes
        above the separator and are never included. Returns "" when the row
        holds only the TUI's own text (the "ask a question" placeholder, "Kiro
        is working", "Initializing") and None for frames without the TUI
        prompt row (legacy "[agent]>" UI, boot screens).
        """
        rows = rendered_pane.splitlines()
        separators = [
            index for index, row in enumerate(rows) if re.match(TUI_SEPARATOR_PATTERN, row.strip())
        ]
        if not separators:
            return None
        prompt_index = next(
            (
                index
                for index in range(separators[-1] + 1, len(rows))
                if self._COMPOSER_PROMPT_RE.match(rows[index])
            ),
            None,
        )
        if prompt_index is None:
            return None
        match = self._COMPOSER_PROMPT_RE.match(rows[prompt_index])
        first = ((match.group(1) if match else None) or "").strip()
        if self._COMPOSER_CHROME_RE.search(first):
            return ""
        continuation = [row.strip() for row in rows[prompt_index + 1 :] if row.strip()]
        return "\n".join([first] + continuation).strip()

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract agent's final response message using green arrow indicator."""
        # Strip ANSI codes for pattern matching
        clean_output = strip_terminal_escapes(script_output)

        # Find patterns in clean output
        green_arrows = list(re.finditer(GREEN_ARROW_PATTERN, clean_output, re.MULTILINE))
        idle_prompts = list(re.finditer(self._idle_prompt_pattern, clean_output))
        new_tui_idles = list(re.finditer(NEW_TUI_IDLE_PATTERN, clean_output))

        # Slash command fallback: if the most recent interaction (between the
        # last two idle prompts) has no green arrow, it was a CLI-handled
        # command like /context or /compact. Extract that output instead.
        if len(idle_prompts) >= 2:
            last_prompt_pos = idle_prompts[-1].start()
            prev_prompt_pos = idle_prompts[-2].end()
            has_arrow_in_last_interaction = any(
                m.start() > prev_prompt_pos and m.start() < last_prompt_pos for m in green_arrows
            )
            if not has_arrow_in_last_interaction:
                between = clean_output[prev_prompt_pos:last_prompt_pos]
                # First line is the user's command text, skip it
                lines = between.split("\n", 1)
                if lines[0].lstrip().startswith("/"):
                    output = lines[1].strip() if len(lines) > 1 else ""
                    if output:
                        output = re.sub(ESCAPE_SEQUENCE_PATTERN, "", output)
                        output = re.sub(CONTROL_CHAR_PATTERN, "", output)
                        return output.strip()

        if not green_arrows:
            # Fallback: try TUI extraction (separator + Credits pattern)
            return self._extract_tui_message(clean_output, script_output)

        if not idle_prompts and not new_tui_idles:
            raise ValueError("Incomplete Kiro CLI response - no final prompt detected")

        # Find the last green arrow (response start)
        last_arrow_pos = green_arrows[-1].end()

        # Find idle prompt that comes AFTER the last green arrow (old or new TUI)
        final_prompt = None
        for prompt in idle_prompts:
            if prompt.start() > last_arrow_pos:
                final_prompt = prompt
                break
        if not final_prompt:
            for prompt in new_tui_idles:
                if prompt.start() > last_arrow_pos:
                    final_prompt = prompt
                    break

        if not final_prompt:
            raise ValueError(
                "Incomplete Kiro CLI response - no final prompt detected after response"
            )

        # Extract directly from clean output
        start_pos = last_arrow_pos
        end_pos = final_prompt.start()

        final_answer = clean_output[start_pos:end_pos].strip()

        if not final_answer:
            raise ValueError("Empty Kiro CLI response - no content found")

        return final_answer.strip()

    def _extract_tui_225_message(
        self, lines: list[str], credits_idx: int, raw_output: str | None = None
    ) -> str | None:
        """Extract a 2.25 TUI reply using turn anchors instead of paragraphs.

        The pre-2.25 layout reliably places the echoed prompt in the first
        paragraph inside the response box.  2.25 moved the assistant text to a
        bullet-prefixed line and can render a one-word reply immediately below
        the echoed prompt, so paragraph splitting cannot distinguish the two.
        The reply is the last bullet row of the turn that is not a tool or
        reasoning row (see TUI_225_CHILD_ROW_PATTERN), up to the next bullet
        row or the Credits line. ``raw_output`` is not used; the parameter is
        kept for callers.
        """
        prev_credits_idx = -1
        for i in range(credits_idx - 1, -1, -1):
            if re.search(TUI_CREDITS_PATTERN, lines[i]):
                prev_credits_idx = i
                break

        reply_start = None
        for i in range(credits_idx - 1, prev_credits_idx, -1):
            if re.search(TUI_225_REPLY_PATTERN, lines[i]) and not self._is_tui_225_chrome_row(
                lines, i, credits_idx
            ):
                reply_start = i
                break

        if reply_start is None:
            return None

        # Rows after the reply are tool or reasoning rows (the reply is the
        # last bullet that is not one), so the reply ends at the next bullet.
        reply_end = next(
            (
                i
                for i in range(reply_start + 1, credits_idx)
                if re.search(TUI_225_REPLY_PATTERN, lines[i])
            ),
            credits_idx,
        )
        response_lines = lines[reply_start:reply_end]
        response_lines[0] = re.sub(r"^•\s+", "", response_lines[0], count=1)
        final_answer = "\n".join(response_lines).strip()
        final_answer = re.sub(ESCAPE_SEQUENCE_PATTERN, "", final_answer)
        final_answer = re.sub(CONTROL_CHAR_PATTERN, "", final_answer)
        return final_answer.strip()

    @staticmethod
    def _is_tui_225_chrome_row(lines: list[str], index: int, end: int) -> bool:
        """Return True when the bullet row at ``index`` is a tool or reasoning row."""
        if re.search(TUI_225_THOUGHT_ROW_PATTERN, lines[index]):
            return True
        for line in lines[index + 1 : end]:
            if line.strip():
                return bool(re.search(TUI_225_CHILD_ROW_PATTERN, line))
        return False

    def _extract_tui_message(self, clean_output: str, raw_output: str | None = None) -> str:
        """Extract agent response from pure TUI output (no green arrows).

        TUI format:
            ────────────────────────────
              user message here

              Agent's response here.

            ▸ Credits: 0.24 - Time: 3s
            ────────────────────────────
            agent-name - model - N%
             Ask a question or describe a task

        Strategy:
            1. Find the last Credits line (response end marker)
            2. Find the previous Credits line (prior turn boundary) or start of output
            3. Find the first separator after that boundary (outer TUI separator)
               This avoids matching separators inside the agent's response.
            4. Extract text between separator and Credits
            5. Skip the first paragraph (user message) if a blank line separates it
        """
        lines = clean_output.split("\n")

        # Find the last Credits line
        credits_idx = None
        for i in range(len(lines) - 1, -1, -1):
            if re.search(TUI_CREDITS_PATTERN, lines[i]):
                credits_idx = i
                break

        if credits_idx is not None and re.search(TUI_225_CREDITS_PATTERN, lines[credits_idx]):
            response = self._extract_tui_225_message(lines, credits_idx, raw_output)
            if response is not None:
                return response
            if raw_output is not None and re.search(TUI_225_COLORED_BULLET_PATTERN, raw_output):
                raise ValueError("No Kiro CLI assistant reply found in colored 2.25 TUI output")

        if credits_idx is None:
            # Kiro CLI 2.3.0+ may not emit a Credits line. Fall back to
            # extracting content between separators around the response.
            # Only attempt this when we know input was sent — without
            # _input_received the original error contract is preserved.
            if self._input_received:
                idle_idx = None
                for i in range(len(lines) - 1, -1, -1):
                    if re.search(NEW_TUI_IDLE_PATTERN, lines[i]):
                        idle_idx = i
                        break

                if idle_idx is not None:
                    # Find the last separator before idle (TUI frame boundary)
                    end_separator_idx = None
                    for i in range(idle_idx - 1, -1, -1):
                        if re.search(TUI_SEPARATOR_PATTERN, lines[i].strip()):
                            end_separator_idx = i
                            break

                    # Find the separator before that (start of response area)
                    start_separator_idx = None
                    if end_separator_idx is not None:
                        for i in range(end_separator_idx - 1, -1, -1):
                            if re.search(TUI_SEPARATOR_PATTERN, lines[i].strip()):
                                start_separator_idx = i
                                break

                    if start_separator_idx is not None and end_separator_idx is not None:
                        content_lines = lines[start_separator_idx + 1 : end_separator_idx]
                        # Skip only the actual TUI header line (agent · model · N%)
                        content_lines = [
                            l
                            for l in content_lines
                            if not re.search(self._new_tui_header_pattern, l)
                        ]
                        # Skip first paragraph (user message echo)
                        agent_start = 0
                        found_blank = False
                        for i, line in enumerate(content_lines):
                            stripped = line.strip()
                            if not found_blank and not stripped:
                                found_blank = True
                                continue
                            if found_blank and stripped:
                                agent_start = i
                                break
                        response_lines = content_lines[agent_start:]
                        final_answer = "\n".join(response_lines).strip()
                        if final_answer:
                            final_answer = re.sub(ESCAPE_SEQUENCE_PATTERN, "", final_answer)
                            final_answer = re.sub(CONTROL_CHAR_PATTERN, "", final_answer)
                            return final_answer.strip()

            raise ValueError(
                "No Kiro CLI response found - no Credits marker or green arrow detected"
            )

        # Find the previous Credits line (prior turn's end) to establish search boundary.
        # This ensures we find the outer TUI separator, not one inside the agent's output.
        prev_credits_idx = -1
        for i in range(credits_idx - 1, -1, -1):
            if re.search(TUI_CREDITS_PATTERN, lines[i]):
                prev_credits_idx = i
                break

        # Find the first separator AFTER the previous turn boundary
        separator_idx = None
        for i in range(prev_credits_idx + 1, credits_idx):
            if re.search(TUI_SEPARATOR_PATTERN, lines[i].strip()):
                separator_idx = i
                break

        # Kiro 2.0: separator is AFTER credits_idx. Scan forward to find it.
        if separator_idx is None:
            next_credits_idx = len(lines)
            for i in range(credits_idx + 1, len(lines)):
                if re.search(TUI_CREDITS_PATTERN, lines[i]):
                    next_credits_idx = i
                    break
            for i in range(credits_idx + 1, next_credits_idx):
                if re.search(TUI_SEPARATOR_PATTERN, lines[i].strip()):
                    separator_idx = i
                    break

        if separator_idx is None:
            raise ValueError("No Kiro CLI response found - no separator found near Credits marker")

        # Extract content between separator and Credits
        if separator_idx > credits_idx:
            # Kiro 2.0: separator after Credits. Content precedes credits_idx.
            content_lines = lines[prev_credits_idx + 1 : credits_idx]
        else:
            # Pre-2.0: separator before Credits (existing behavior)
            content_lines = lines[separator_idx + 1 : credits_idx]

        # Skip the first paragraph (user message echo).
        # The user message is the first block of non-empty lines after the separator.
        # After a blank line, the agent response begins.
        agent_start = 0
        found_blank = False
        for i, line in enumerate(content_lines):
            stripped = line.strip()
            if not found_blank and not stripped:
                found_blank = True
                continue
            if found_blank and stripped:
                agent_start = i
                break

        if not found_blank:
            # No blank line found — entire content is the response
            agent_start = 0

        response_lines = content_lines[agent_start:]
        final_answer = "\n".join(response_lines).strip()

        if not final_answer:
            raise ValueError("Empty Kiro CLI response - no content found")

        # Clean up (ANSI codes already stripped from clean_output at caller)
        final_answer = re.sub(ESCAPE_SEQUENCE_PATTERN, "", final_answer)
        final_answer = re.sub(CONTROL_CHAR_PATTERN, "", final_answer)
        return final_answer.strip()

    def get_idle_pattern_for_log(self) -> str:
        """Return Kiro CLI IDLE prompt pattern for log files.

        Returns a pattern that matches either the legacy UI format
        or the new TUI format.
        """
        return rf"(?:{IDLE_PROMPT_PATTERN_LOG}|{NEW_TUI_IDLE_PATTERN_LOG})"

    def exit_cli(self) -> str:
        """Get the command to exit Kiro CLI."""
        return "/exit"

    def cleanup(self) -> Optional[bool]:
        """Clean up Kiro CLI provider; a helper also removes its per-launch home.

        The window is already killed. Every process whose ``KIRO_HOME`` is the
        helper's home is stopped, then the home is removed, Kiro's session files
        included (#1167 Phase 2). ``False`` keeps the provider and the row for a
        retry when an owner cannot be proven gone.
        """
        self._initialized = False
        from cli_agent_orchestrator.services import ephemeral_service

        if self._launch is not None:
            return ephemeral_service.remove_kiro_home(self.terminal_id, row_proof=True)
        # A helper's provider rebuilt from its row after a restart has no launch
        # object, but its home keeps its deterministic path; removal still needs
        # proof (the archive, or the ephemeral row). Installed Kiro terminals
        # have no home, so for them this is one lstat and nothing else.
        if not valid_terminal_id(self.terminal_id) or not os.path.lexists(
            kiro_home_path(self.terminal_id)
        ):
            return None
        from cli_agent_orchestrator.clients.database import is_ephemeral_terminal

        return ephemeral_service.remove_kiro_home(
            self.terminal_id, row_proof=is_ephemeral_terminal(self.terminal_id)
        )
