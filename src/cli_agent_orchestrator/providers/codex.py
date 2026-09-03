"""Codex CLI provider implementation."""

import asyncio
import logging
import os
import re
import shlex
import time
from pathlib import Path
from typing import Any, Optional

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

# Regex patterns for Codex output analysis
ANSI_CODE_PATTERN = r"\x1b\[[0-9;]*m"
IDLE_PROMPT_PATTERN = r"(?:❯|›|»|codex>)"
# Number of lines from the bottom of capture to check for the idle prompt.
# With --no-alt-screen, codex output is inline (scrollback contains history),
# so we can't anchor to \Z. Instead, check the last few lines where the prompt
# and status bar appear.
IDLE_PROMPT_TAIL_LINES = 5
# The idle prompt character ❯ (U+276F) is rendered on-screen by capture-pane
# but is NOT written to the raw output stream captured by pipe-pane.  Instead,
# the TUI footer text "? for shortcuts" is reliably present whenever the TUI
# is active.  This is intentionally permissive — _has_idle_pattern() is a
# lightweight pre-check; the real status decision is made by get_status()
# which uses capture-pane (rendered screen).
# Match assistant response start: "assistant:/codex:/agent:" (label style from synthetic
# test fixtures) or "•" bullet point (real Codex interactive output format).
# [^\S\n]* matches horizontal whitespace only (not newlines) so the match anchors
# on the actual bullet line — using \s* would let the match start on a blank
# line above the bullet, breaking per-line tool-call filtering downstream.
ASSISTANT_PREFIX_PATTERN = r"^(?:(?:assistant|codex|agent)\s*:|[^\S\n]*•)"
# MCP tool call marker emitted by Codex when invoking a tool, e.g.
# "• Called cao-mcp-server.load_skill({...})". The body that follows
# (└ ... lines) is the tool's return value, not the model's reply.
# Used to skip these markers when locating the actual response start.
# The "<server>.<tool>(" shape (identifier.identifier followed by an open
# paren) is required so legitimate model bullets like "• Called attention
# to the bug" don't get filtered as tool calls.
MCP_TOOL_CALL_PATTERN = r"^[^\S\n]*•\s+Called\s+[\w-]+\.[\w-]+\("
# Match user input: "You ..." (label style), "› text" (older Codex interactive
# prompt), or "» text" (Codex 0.149+ interactive prompt). The prompt alternative
# requires a non-whitespace character on the same line to distinguish user input
# from an empty idle prompt.
# [^\S\n] matches horizontal whitespace only (spaces/tabs), preventing the pattern
# from crossing newline boundaries into subsequent lines.
USER_PREFIX_PATTERN = r"^(?:You\b|[›»][^\S\n]*\S)"
# Strict idle prompt pattern for extraction: matches empty prompt lines only.
# Distinguishes "› " (idle) from "› user message" (user input with text).
IDLE_PROMPT_STRICT_PATTERN = r"^\s*(?:❯|›|»|codex>)\s*$"

PROCESSING_PATTERN = r"\b(thinking|working|running|executing|processing|analyzing)\b"
WAITING_PROMPT_PATTERN = r"^(?:Approve|Allow)\b.*\b(?:y/n|yes/no|yes|no)\b"
ERROR_PATTERN = r"^(?:Error:|ERROR:|Traceback \(most recent call last\):|panic:)"

# Codex TUI footer indicators (status bar below the idle prompt).
# Used to detect when the bottom lines contain TUI chrome rather than user input.
# v0.110 and earlier: "? for shortcuts" and "N% context left"
# v0.111+: "model · N% left · path" (PR #13202 restored draft footer hints)
# v0.136+: "model · path" (the "N% left" segment was removed)
# The "·\s+[~/]" alternative anchors on the path component of the footer,
# which is shared across v0.111 and v0.136 status bars.
TUI_FOOTER_PATTERN = r"(?:\?\s+for shortcuts|context left|\d+%\s+left|·\s+[~/])"
# Codex TUI progress spinner: "• Working (0s • esc to interrupt)",
# "• Working (1m 00s ...)", "• Working (1h 00m 00s ...)", or dynamic
# prefixes such as "• Starting script creation (10s • esc to interrupt)".
# Codex expands the elapsed value at the minute and hour boundaries.
# Appears inline with --no-alt-screen when the agent is actively processing.
# Must be checked before COMPLETED to avoid false positives (the • matches
# ASSISTANT_PREFIX_PATTERN and the TUI footer › matches idle prompt).
TUI_PROGRESS_PATTERN = r"•[^\n]*\((?:(?:\d+h\s+)?\d+m\s+)?\d+s\s*•\s*esc to interrupt\)"

# Workspace trust/approval prompt shown when Codex opens a new directory.
# Two known variants:
#   v0.98+: "allow Codex to work in this folder"
#   v0.130+ (git worktree): "Do you trust the contents of this directory?"
# Both indicate the TUI is blocked waiting for user input.
TRUST_PROMPT_PATTERN = r"allow Codex to work in this folder"
TRUST_PROMPT_PATTERN_V2 = r"Do you trust the contents of this directory\?"
TRUST_PROMPT_FOOTER = r"Press enter to continue"

# First-run auth menu, shown when no OpenAI/Codex credentials are configured yet:
#   Welcome to Codex, OpenAI's command-line coding agent
#   Sign in with ChatGPT to use Codex as part of your paid plan
#   or connect an API key for usage-based billing
#   > 1. Sign in with ChatGPT
#     2. Sign in with Device Code
#     3. Provide your own API key
#   Press enter to continue
# Unlike the trust/update-available dialogs above, this one cannot be auto-dismissed --
# it requires a real human to actually complete OAuth or supply a real API key, which is
# squarely an operator task, not something this provider can or should fabricate. Before
# this was recognized, `initialize()`'s own wait_until_status(..., {IDLE, COMPLETED}, ...)
# had no way to ever succeed for an account with no credentials configured yet: the pane
# would sit at this exact, correctly-rendered screen -- process alive, output real, nothing
# actually broken -- but never reach IDLE/COMPLETED, so the 60s init timeout would always
# fire and CAO would tear the terminal down before an operator had any real chance to open
# the session and complete login themselves. Bottom-anchored (last 15 lines) requiring BOTH
# the menu text and the footer together, same shape as TRUST_PROMPT_PATTERN_V2 immediately
# above, to avoid a false match on this text surviving in scrollback from earlier output.
LOGIN_MENU_PATTERN = r"Sign in with ChatGPT"
LOGIN_MENU_FOOTER = TRUST_PROMPT_FOOTER

# Startup "Update available!" dialog. Codex shows this at startup when a newer
# release exists, with a numbered menu whose cursor default is option 1:
#   ✨ Update available! 0.142.5 -> 0.144.5
#   1. Update now (runs npm install -g @openai/codex)
#   2. Skip
#   3. Skip until next version
#   Press enter to continue
# A blind Enter would run a GLOBAL npm install that swaps the codex binary under
# every other running CAO worker. We suppress with -c check_for_update_on_startup=false
# at launch AND detect+dismiss with '3'+Enter as defense-in-depth.
UPDATE_DIALOG_PATTERN = r"Update available!\s+\S+\s+->\s+\S+"
UPDATE_DIALOG_MENU_PATTERN = r"Skip until next version"
UPDATE_DIALOG_FOOTER = TRUST_PROMPT_FOOTER
STARTUP_PROMPT_BOTTOM_LINES = 15
STARTUP_ACTIVITY_PATTERN = r"^\s*•[^\S\n]+\S"
# Codex's runtime approval prompt as actually rendered by codex-cli 0.147.0,
# verified against a live tmux capture (test/providers/fixtures/
# codex_approval_modal_raw.txt):
#
#     Would you like to run the following command?
#
#     Environment: local
#
#     $ mkdir -p /private/tmp/codex-work-567
#
#   › 1. Yes, proceed (y)
#     2. Yes, and don't ask again for commands that start with `mkdir -p ...` (p)
#     3. No, and tell Codex what to do differently (esc)
#
#     Press enter to confirm or esc to cancel
#
# It is NOT a box-drawn modal and carries no "[a] Accept"/"[d] Decline" keys: it
# is a numbered menu with a `›` selection cursor and a confirm footer.
#
# THE TITLE IS NOT THE HOOK. An earlier revision of this detector required one of
# three enumerated question titles inside a fixed-height bottom window, and both
# halves of that were wrong:
#
#   * The enumeration is incomplete, and cannot be completed. `strings` over the
#     0.147.0 native binary turns up at least two more approval titles driving the
#     same menu -- `Do you want to approve network access to "<host>"?` (emitted
#     when features.network_proxy is on, with its own `Yes, and allow this host in
#     ...` / `No, and block this host in the future` options) and `Approve app tool
#     call?` -- so any title list is a list of the variants someone happened to
#     have already seen.
#   * The fixed window fails OPEN. The command preview between the title and the
#     menu is the verbatim command, untruncated by the renderer, so a multi-line
#     heredoc pushes the title out of any fixed row budget; the detector then found
#     no title and returned IDLE for a hard-blocked pane, which is the dangerous
#     direction to be wrong in.
#
# So detection is structural and bottom-up instead -- see
# :func:`_has_approval_prompt_in_bottom`. These title patterns survive only as the
# permissive NEGATIVE gate in STARTUP_BLOCKING_INPUT_PATTERN below, where an extra
# match merely keeps the startup poll going and is therefore free; the network
# title is folded in there for the same reason. Nothing positive is classified off
# a title any more.
APPROVAL_PROMPT_PATTERN = (
    r"(?:Would you like to (?:run the following command"
    r"|make the following edits"
    r"|grant these permissions)\?"
    r"|Do you want to approve network access to)"
)
# The footer under a blocking list menu is OPTIONAL CORROBORATION, not an
# anchor: both list actions are unbindable (`tui.keymap.list.accept = []` --
# an empty list explicitly unbinds; config/src/tui_keymap.rs at rust-v0.147.0),
# and accept_cancel_hint_line (tui/src/bottom_pane/popup_consts.rs) renders one
# of FOUR shapes accordingly:
#   both bound     -> "Press <a> to confirm or <c> to cancel"
#   accept unbound -> "Press <c> to cancel"
#   cancel unbound -> "Press <a> to confirm"
#   both unbound   -> no footer line at all
# The approval overlay may append " or <k> to open thread"
# (approval_overlay.rs), which is also the WHOLE line when both list actions
# are unbound, and the generic popups (model picker) say "to go back" where
# the approval says "to cancel". So this pattern's job is only to recognise a
# footer line as part of the menu block wherever one is rendered.
#
# The key labels are emitted as separately styled spans -- the sentence is not
# one literal in the binary -- and a label is NOT a single token: a two-stroke
# chord such as "ctrl-x ctrl-s" renders via ShortcutHint::display_label() as
# `ctrl + x ctrl + s` (strokes joined by a space, modifiers by " + ";
# key_hint.rs). Worst legal label is a chord whose strokes each carry
# ctrl+shift+alt: 7 tokens per stroke, 14 in total, so a label is matched as
# 1-15 whitespace-separated tokens. The bound keeps a same-line prose sentence
# from bridging an unrelated "Press" to a distant "to confirm".
APPROVAL_PROMPT_FOOTER = (
    r"Press \S+(?:[^\S\n]+\S+){0,14} to (?:confirm|cancel|go back|open thread)\b"
)
# One numbered menu option: "› 1. Yes, proceed (y)", "  2. No, ... (esc)". The
# selection cursor is optional here because it sits on exactly one option at a
# time and moves as the operator arrows around.
APPROVAL_MENU_OPTION_PATTERN = r"^[^\S\n]*(?:›[^\S\n]+)?\d+\.[^\S\n]+\S"
# The selected option with its cursor flush at column 0, which is where Codex
# draws every transcript gutter marker. This is the same left-margin argument
# _modal_line_content makes for the boxed modal: quoted or continuation prose is
# indented under its bullet, so a menu the model merely PASTED into its own reply
# carries its cursor at column >= 2 and fails this while a live one passes.
APPROVAL_MENU_CURSOR_PATTERN = r"^›[^\S\n]+\d+\.[^\S\n]+\S"
# A wrapped option's continuation row. ListSelectionView renders wrapped rows by
# default (SelectionRowDisplay::Wrapped, list_selection_view.rs at
# rust-v0.147.0), and word_wrap_line indents every continuation to the option
# TEXT column -- the width of the "{prefix} {n}. " gutter, so 5 columns for a
# single-digit menu and one more per extra digit (build_rows sets wrap_indent to
# the prefix width; wrap_standard_row feeds it to subsequent_indent). The
# question, command preview, and footer rows all sit at 2 columns of indent, so
# >= 5 columns directly under an option row is the menu's own wrapping, never
# new content. Only honoured while inside an option (see the below-anchor scan)
# -- indented prose elsewhere still disqualifies the block.
APPROVAL_MENU_CONTINUATION_PATTERN = r"^[^\S\n]{5,}\S"
# Minimum numbered options required above the footer. Two is the floor for a
# genuine approval (accept and decline); requiring specific option COPY instead
# would reintroduce exactly the enumeration fragility described above.
APPROVAL_MENU_MIN_OPTIONS = 2

# Codex's boxed command-approval modal:
#   ╭─ Command Approval Required ─╮
#   │ [a] Accept  [d] Decline     │
#   ╰─────────────────────────────╯
# WARNING: this copy is NOT emitted by codex-cli 0.147.0. `strings` over the
# vendored native binary finds zero occurrences of "Command Approval Required",
# "] Accept", or "] Decline" -- the live prompt is APPROVAL_PROMPT_PATTERN above.
# The two patterns are kept because this copy predates the numbered menu and is
# already load-bearing in STARTUP_BLOCKING_INPUT_PATTERN below, so dropping them
# would silently un-guard whichever older Codex builds still render it. Treat
# _has_approval_modal_in_bottom as legacy/defensive: APPROVAL_PROMPT_PATTERN is
# what fires on current Codex.
#
# Split into header and choice-key halves because the two paths that consume
# them need different strictness. The startup path (_has_startup_idle_composer)
# uses the permissive OR below as a NEGATIVE gate — any one token vetoes
# "ready", and a false veto merely keeps polling, so over-matching is free.
# get_status() uses them as a POSITIVE classifier where over-matching would
# strand a healthy pane in WAITING_USER_ANSWER, so it corroborates the two
# halves separately (see _has_approval_modal_in_bottom). Box-drawing characters
# are deliberately NOT required: the frame chrome has changed across Codex
# releases while this copy has not.
APPROVAL_MODAL_HEADER_PATTERN = r"Command Approval Required"
APPROVAL_MODAL_CHOICE_PATTERN = r"(?:\[[aA]\]\s+Accept\b|\[[dD]\]\s+Decline\b)"
# Box-drawing frame and padding stripped from a modal line before matching, so a
# framed line ("│ [a] Accept  [d] Decline     │") reduces to its text content.
# Stripped as a character SET from both ends, hence no ordering assumption about
# corner/edge glyphs. Light, heavy, and double variants are all covered because
# only light glyphs have been observed and the frame style is not contractual.
#
# ASCII frame characters (+ - |) are deliberately EXCLUDED. They are markdown
# table syntax, so including them would let a table the model wrote in its own
# reply ("| Command Approval Required |" / "| [a] Accept | [d] Decline |")
# reduce to the exact modal shape. No Codex release has been observed using
# ASCII frames, so that trade buys a hypothetical false negative at the cost of
# a plausible false positive.
#
# Note this set also strips leading whitespace, so an INDENTED plain-text quote
# reduces to the modal shape too. That look-alike is excluded positionally
# instead — see _has_approval_modal_in_bottom.
MODAL_FRAME_CHARS = "─│╭╮╰╯├┤━┃┏┓┗┛┣┫═║╔╗╚╝╠╣ \t"
# The same set minus padding, used to tell "this line began with box chrome"
# from "this line began with a prose indent".
MODAL_FRAME_GLYPHS = frozenset(MODAL_FRAME_CHARS) - frozenset(" \t")
STARTUP_BLOCKING_INPUT_PATTERN = (
    rf"(?:{APPROVAL_MODAL_HEADER_PATTERN}|{APPROVAL_MODAL_CHOICE_PATTERN}|"
    rf"{APPROVAL_PROMPT_PATTERN}|{APPROVAL_PROMPT_FOOTER}|{TRUST_PROMPT_FOOTER})"
)
STARTUP_IDLE_PLACEHOLDER_PATTERN = (
    rf"^\s*{IDLE_PROMPT_PATTERN}[^\S\n]+(?:"
    r"Explain this codebase|"
    r"Summarize recent commits|"
    r"Implement \{feature\}|"
    r"Find and fix a bug in @filename|"
    r"Write tests for @filename|"
    r"Improve documentation in @filename|"
    r"Run /review on my current changes|"
    r"Use /skills to list available skills|"
    r"Ask Codex to do anything"
    r")\s*$"
)

# Codex welcome banner indicating normal startup (no trust prompt)
CODEX_WELCOME_PATTERN = r"OpenAI Codex"


def _compute_tui_footer_cutoff(all_lines: list) -> int:
    """Compute the character position where the TUI footer area starts.

    Scans backward from the last line to find the TUI footer status bar
    (matches TUI_FOOTER_PATTERN), then continues upward to include any
    blank lines and the suggestion hint line (› with text) that appear
    above the status bar as part of the footer area.

    Returns the character position in the joined text (``'\\n'.join(all_lines)``)
    where the footer starts. Returns ``len('\\n'.join(all_lines))`` if no
    footer is found.
    """
    n = len(all_lines)
    footer_start_idx = n

    # Find the status bar line (last TUI_FOOTER_PATTERN match in the bottom area)
    for i in range(n - 1, max(n - IDLE_PROMPT_TAIL_LINES - 1, -1), -1):
        if re.search(TUI_FOOTER_PATTERN, all_lines[i]):
            footer_start_idx = i
            break

    if footer_start_idx == n:
        return len("\n".join(all_lines))

    # Scan upward from the status bar to include blank lines and the
    # suggestion hint (› with text) that are part of the TUI footer chrome.
    for j in range(footer_start_idx - 1, max(footer_start_idx - 4, -1), -1):
        line = all_lines[j]
        if not line.strip():
            footer_start_idx = j
        elif re.match(rf"\s*{IDLE_PROMPT_PATTERN}", line):
            footer_start_idx = j
            break
        else:
            break

    return len("\n".join(all_lines[:footer_start_idx]))


def _toml_scalar(value: Any) -> str:
    """Serialize a Python scalar to a TOML literal for a ``-c key=<value>`` override.

    Strings become quoted TOML basic strings (backslash, quote, tab, CR, and newline escaped so
    tmux ``send_keys`` keeps the launch command on one line); bools become
    ``true``/``false``; ints and floats are emitted bare. Non-scalar values (dict/list/None) raise ``TypeError`` so a misconfigured profile fails fast. ``bool`` is checked
    before ``int`` because ``bool`` is a subclass of ``int`` in Python, so the
    order here is load-bearing — a flipped order would render ``True`` as ``1``.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if not isinstance(value, str):
        raise TypeError(
            "codexConfig values must be scalars (str, bool, int, or float); "
            f"got {type(value).__name__}"
        )
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\t", "\\t")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )
    return f'"{escaped}"'


# codexConfig keys are dotted CONFIG PATHS ("features.fast_mode") — dots are
# the path separator and intentional. MCP server names and env keys are single
# TOML BARE KEYS: a dot there would silently create a NESTED table
# (mcp_servers.my.srv.command → mcp_servers['my']['srv'], not
# mcp_servers['my.srv']), so codex would never find the server.
_CODEX_CONFIG_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_CODEX_BARE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_config_key(key: Any, *, source: str, allow_dots: bool = False) -> str:
    """Validate a key that is interpolated into a Codex ``-c`` override path.

    Spaces, ``=``, quotes, or control characters are rejected so a
    misconfigured profile fails fast with a clear error instead of silently
    emitting a malformed ``-c`` override (an unescaped quote or newline in the
    KEY half would corrupt the TOML the same way an unescaped value would).

    ``allow_dots=True`` permits dotted config paths (codexConfig keys like
    ``features.fast_mode``). MCP server names and env keys must be single
    TOML bare keys: a dot there would nest the entry under the wrong TOML
    table (see pattern comment above). ``source`` names the profile field
    for the error message.
    """
    if allow_dots:
        pattern = _CODEX_CONFIG_KEY_PATTERN
        expected = "a dotted config path over [A-Za-z0-9_.-] (e.g. 'features.fast_mode')"
    else:
        pattern = _CODEX_BARE_KEY_PATTERN
        expected = (
            "a single TOML bare key over [A-Za-z0-9_-] (no dots -- a dot "
            "would nest the entry under the wrong TOML table)"
        )
    # fullmatch, not match: with ``$`` alone, re.match accepts a TRAILING
    # newline ("srv\n" passes ^...$), which is exactly the bug class this
    # validation exists to close.
    if not isinstance(key, str) or not pattern.fullmatch(key):
        raise ValueError(f"Invalid {source} key {key!r}: must be {expected}")
    return key


def _toml_override(key: str, value: Any) -> str:
    """Build one ``key=<toml-scalar>`` Codex ``-c`` override, validating the key.

    Key validation is delegated to :func:`_validate_config_key`.
    Value-serialization failures from :func:`_toml_scalar` are re-raised with
    the offending key for context.
    """
    _validate_config_key(key, source="codexConfig", allow_dots=True)
    try:
        return f"{key}={_toml_scalar(value)}"
    except TypeError as exc:
        raise TypeError(f"codexConfig key '{key}': {exc}") from exc


def _has_update_dialog_in_bottom(clean_output: str) -> bool:
    """Return True when Codex's update-available dialog is active in the bottom region."""
    bottom = "\n".join(clean_output.splitlines()[-STARTUP_PROMPT_BOTTOM_LINES:])
    return (
        re.search(UPDATE_DIALOG_PATTERN, bottom) is not None
        and re.search(UPDATE_DIALOG_MENU_PATTERN, bottom) is not None
        and re.search(UPDATE_DIALOG_FOOTER, bottom) is not None
    )


def _modal_line_content(line: str) -> Optional[str]:
    """Reduce one line to its modal text, or None if the line reads as prose.

    Strips frame glyphs and padding so ``"│ [a] Accept  [d] Decline   │"``
    reduces to ``"[a] Accept  [d] Decline"``. Returns None when the leading run
    removed was whitespace ONLY while being non-empty — i.e. the line is
    indented plain text.

    That indent test is the discriminator against the model quoting a modal
    transcript back in its own reply:

        • The terminal output showed:
            Command Approval Required
            [a] Accept  [d] Decline
          so it was waiting on approval.

    Those quoted lines reproduce the modal's per-line structure exactly, so
    line structure alone cannot separate them. Position can: Codex draws the
    modal box flush at the left margin, whereas quoted or continuation prose is
    indented under its bullet. So a leading run of frame glyphs is accepted, a
    leading run of spaces/tabs is not, and column 0 is accepted either way
    (an unframed modal would still start there).
    """
    content = line.strip(MODAL_FRAME_CHARS)
    if not content:
        return None
    lead = line[: len(line) - len(line.lstrip(MODAL_FRAME_CHARS))]
    if lead and not (MODAL_FRAME_GLYPHS & set(lead)):
        return None
    return content


def _is_frame_padding(line: str) -> bool:
    """Return True when ``line`` carries nothing but frame glyphs and padding.

    True of a box's top/bottom rule ("╰────╯"), of an empty interior row
    ("│      │"), and of a blank line (space is in ``MODAL_FRAME_CHARS``).
    """
    return not line.strip(MODAL_FRAME_CHARS)


def _is_chrome_only(line: str) -> bool:
    """Return True when ``line`` is frame or TUI chrome rather than content.

    The union of what may legitimately sit BELOW a live modal: the box's own
    closing rule and interior padding, blank filler, the empty composer line
    ("›" with nothing typed), and the status-bar footer. Anything else -- a
    prose bullet, a spinner, a typed draft -- is content, which means the
    modal is no longer the bottom of the pane.

    The empty-composer and footer cases are matched explicitly rather than
    folded into :func:`_is_frame_padding` because neither ``›`` nor the footer
    text reduces to empty under ``MODAL_FRAME_CHARS``.
    """
    if _is_frame_padding(line):
        return True
    if re.fullmatch(rf"\s*{IDLE_PROMPT_PATTERN}\s*", line):
        return True
    return re.search(TUI_FOOTER_PATTERN, line) is not None


def _is_transcript_marker(line: str) -> bool:
    """Return True when ``line`` opens a new transcript cell (``›`` user / ``•`` bullet).

    Used as the upward bound on the header search: Codex draws the modal as ONE
    cell, so a user line or an assistant bullet is a hard boundary that the box
    cannot span. This replaces a fixed line count, which could not express
    "same box" and therefore failed open on a modal taller than the window.
    """
    return bool(
        re.match(USER_PREFIX_PATTERN, line, re.IGNORECASE)
        or re.match(ASSISTANT_PREFIX_PATTERN, line, re.IGNORECASE)
    )


def _has_approval_modal_in_bottom(clean_output: str) -> bool:
    """Return True when Codex's boxed command-approval modal is active at the bottom.

    NOTE: this detects the LEGACY "Command Approval Required" / "[a] Accept"
    modal, which codex-cli 0.147.0 does not render — see
    APPROVAL_MODAL_HEADER_PATTERN's comment and
    :func:`_has_approval_prompt_in_bottom` for the copy that is live today.

    Anchored BOTTOM-UP on the last choice line, because the thing being tested
    is an invariant about the bottom of the pane, not about a region of it: a
    live modal blocks the TUI, so it must BE the bottom, with only frame rows
    and footer chrome after it. Four guards:

    1. **Anchor.** The LAST line that reduces to a choice key. Taking the last
       rather than the first is what lets an already-answered modal sitting in
       scrollback above a live one be ignored instead of vetoing it.
    2. **Nothing but chrome below the anchor.** See :func:`_is_chrome_only`.
       This subsumes the older spinner test (a spinner is not chrome) and also
       rejects a modal transcript the model quoted mid-reply, since the reply
       continues below the quote. It replaces "footer must NOT appear below",
       which would have false-negatived every real modal: with
       ``--no-alt-screen`` the footer renders at the bottom regardless.
    3. **Corroborating header above the anchor,** found by walking up and
       stopping at the first :func:`_is_transcript_marker` — the box is one
       transcript cell, so the header must be inside it. No fixed window, so an
       arbitrarily tall modal still resolves; previously a >15-line modal lost
       its header and failed open to COMPLETED.
    4. **Line structure and left-margin position.** Each half must own its line
       (header an exact match, choice line a prefix match) and sit at the box's
       margin rather than under a prose indent — see :func:`_modal_line_content`.

    Known residual: a framed modal quote that ENDS a reply, with only the empty
    composer and footer after it, satisfies all four guards and reads as live.
    Distinguishing it needs semantics this detector does not have; it costs a
    spurious WAITING_USER_ANSWER (work withheld) rather than a COMPLETED (work
    pasted into a blocked pane), which is the safe direction to be wrong in.
    """
    lines = clean_output.splitlines()

    choice_idx = None
    for index in range(len(lines) - 1, -1, -1):
        content = _modal_line_content(lines[index])
        if content is not None and re.match(APPROVAL_MODAL_CHOICE_PATTERN, content, re.IGNORECASE):
            choice_idx = index
            break
    if choice_idx is None:
        return False

    if not all(_is_chrome_only(line) for line in lines[choice_idx + 1 :]):
        return False

    for index in range(choice_idx - 1, -1, -1):
        line = lines[index]
        content = _modal_line_content(line)
        if content is not None and re.fullmatch(
            APPROVAL_MODAL_HEADER_PATTERN, content, re.IGNORECASE
        ):
            return True
        if _is_transcript_marker(line):
            return False
    return False


def _has_approval_prompt_in_bottom(clean_output: str) -> bool:
    """Return True when Codex's runtime approval prompt is active at the bottom.

    This is the prompt codex-cli 0.147.0 actually renders (verified against three
    live captures). Detection is STRUCTURAL and bottom-up — the numbered menu
    itself, not the question title and not the footer — because none of the
    alternatives can be relied on: the title list cannot be completed, a fixed
    row budget fails open on a long command preview (see
    APPROVAL_PROMPT_PATTERN), and the footer is absent entirely when the list
    actions are unbound (`tui.keymap.list.accept = []` renders the same blocking
    menu with only "Press esc to cancel", or with no footer line at all — see
    APPROVAL_PROMPT_FOOTER).

    Three guards, in the order they are cheapest to refute:

    1. **Menu-cursor anchor.** The LAST line whose selection cursor sits flush
       at column 0 (APPROVAL_MENU_CURSOR_PATTERN). Taking the last, not the
       first, lets an already-answered prompt in scrollback be ignored rather
       than shadow a live one below it. Column 0 is where Codex draws every
       transcript gutter marker, so quoted or continuation prose — a menu the
       model merely PASTED into a reply — carries its cursor at column >= 2 and
       fails this.
    2. **Nothing below the anchor but the rest of the menu block**: the
       remaining (non-cursor) option rows — each an option-start row plus any
       wrapped continuation rows at the option text column
       (APPROVAL_MENU_CONTINUATION_PATTERN; the renderer wraps long options by
       default, so a narrow pane splits one option across lines) — at most one
       footer hint in any of its rendered forms, and chrome
       (:func:`_is_chrome_only`, shared with the boxed-modal detector).
       Continuations are honoured only while inside an option; indented prose
       after the footer or after blank filler still disqualifies. A live
       prompt blocks the TUI, so its menu must BE the bottom of the pane. This
       is the guard that rejects an ordinary COMPLETED reply which quotes the
       menu while a live composer and more prose sit underneath — the sticky
       WAITING_USER_ANSWER that case used to latch would wedge a ready worker.
    3. **Menu size.** At least APPROVAL_MENU_MIN_OPTIONS option-START rows
       counted contiguously around the anchor, stepping over wrapped
       continuation rows (a genuine approval always offers accept and
       decline). Contiguity matters: counting across the question or the
       command preview would let a numbered list INSIDE a quoted command
       inflate the tally.

    Deliberately NOT required: any particular question title, any particular
    option copy, and the footer. The footer, when present in any of its four
    rendered shapes, is accepted as part of the block; its absence proves
    nothing because unbinding the accept action removes it while the menu still
    blocks. Firing on Codex's other blocking numbered menus (the model picker,
    for one) is correct rather than tolerated — those panes are equally blocked
    on a keystroke, and WAITING_USER_ANSWER is the right answer for them too.

    Residual risks, disclosed:

    - The column-0 cursor test is what separates a live menu from one pasted
      into a reply, so a future renderer that indents the cursor would fail
      open to IDLE. The live captures in test/providers/fixtures/
      (codex_approval_{modal,edits,long_preview}_raw.txt) pin the current
      rendering against that.
    - A USER message that is itself a numbered list ("1. foo\\n2. bar") renders
      with the same column-0 gutter marker ("› 1. foo" over "  2. bar"), so if
      it is the last transcript cell with only the idle composer below — codex
      interrupted before replying, say — it now reads WAITING rather than IDLE.
      That errs toward withholding work, never toward pasting into a blocked
      pane, and clears as soon as codex renders any activity below the cell.
      The footer-anchored version rejected this shape, but only by failing open
      to IDLE on every unbound-keymap approval, which is the dangerous
      direction to be wrong in.
    """
    lines = clean_output.splitlines()

    anchor = None
    for index in range(len(lines) - 1, -1, -1):
        if re.match(APPROVAL_MENU_CURSOR_PATTERN, lines[index]):
            anchor = index
            break
    if anchor is None:
        return False

    options = 1  # the anchor row
    footer_seen = False
    in_option = True  # the anchor row itself may wrap onto the next line
    for line in lines[anchor + 1 :]:
        if not footer_seen and re.match(APPROVAL_MENU_OPTION_PATTERN, line):
            options += 1
            in_option = True
            continue
        if in_option and re.match(APPROVAL_MENU_CONTINUATION_PATTERN, line):
            continue
        if not footer_seen and re.search(APPROVAL_PROMPT_FOOTER, line):
            footer_seen = True
            in_option = False
            continue
        if _is_chrome_only(line):
            in_option = False
            continue
        return False

    for index in range(anchor - 1, -1, -1):
        line = lines[index]
        if re.match(APPROVAL_MENU_OPTION_PATTERN, line):
            options += 1
            continue
        if re.match(APPROVAL_MENU_CONTINUATION_PATTERN, line):
            # Walking up, a continuation belongs to the option row above it;
            # step over it and let that row (or anything else) decide.
            continue
        break

    return options >= APPROVAL_MENU_MIN_OPTIONS


def _has_startup_idle_composer(clean_output: str) -> bool:
    """Return True when the bottom of the pane shows Codex's idle composer."""
    all_lines = clean_output.splitlines()
    tail_lines = all_lines[-STARTUP_PROMPT_BOTTOM_LINES:]
    tail_output = "\n".join(tail_lines)

    if re.search(STARTUP_ACTIVITY_PATTERN, tail_output, re.MULTILINE):
        return False
    if re.search(WAITING_PROMPT_PATTERN, tail_output, re.IGNORECASE | re.MULTILINE):
        return False
    if re.search(STARTUP_BLOCKING_INPUT_PATTERN, tail_output, re.IGNORECASE):
        return False

    legacy_tail = all_lines[-IDLE_PROMPT_TAIL_LINES:]
    if any(re.match(IDLE_PROMPT_STRICT_PATTERN, line) for line in legacy_tail):
        return True

    # Codex 0.145 renders placeholder text inside the idle composer instead of
    # an empty prompt. Match only known placeholder copy and require its status
    # footer below it so typed drafts and ordinary output are not treated as ready.
    for index in range(len(tail_lines) - 1, -1, -1):
        if re.match(STARTUP_IDLE_PLACEHOLDER_PATTERN, tail_lines[index]):
            return any(re.search(TUI_FOOTER_PATTERN, line) for line in tail_lines[index + 1 :])
    return False


def _find_assistant_marker(text: str) -> Optional[re.Match[str]]:
    """Find the first ASSISTANT_PREFIX_PATTERN match in ``text`` whose line
    is not an MCP tool-call marker.

    Codex emits ``• Called <server>.<tool>(...)`` when invoking an MCP tool;
    that bullet matches ASSISTANT_PREFIX_PATTERN but is followed by tool
    output, not the model's reply. Anchoring on it would conflate tool
    output with the model response (status: false COMPLETED;
    extraction: skill-body leak).
    """
    for m in re.finditer(ASSISTANT_PREFIX_PATTERN, text, re.IGNORECASE | re.MULTILINE):
        line_end = text.find("\n", m.start())
        if line_end == -1:
            line_end = len(text)
        line = text[m.start() : line_end]
        if re.match(MCP_TOOL_CALL_PATTERN, line):
            continue
        return m
    return None


def _find_response_marker(text: str) -> Optional[re.Match[str]]:
    """Find the first model-reply marker after a structural activity prelude.

    Native Codex activity cells have a ``•`` summary followed by a ``└`` tree
    continuation.  Require at least two complete cells before advancing the
    response boundary: a single tree-formatted group may be a legitimate
    answer, while two consecutive cells are strong evidence of TUI activity.
    Compact bullet groups remain ambiguous and are preserved.  This trades a
    rare false positive for avoiding silent truncation of ordinary replies and
    deliberately avoids matching English verbs such as ``Read`` or ``Called``.
    """

    def line_end(start: int) -> int:
        newline = text.find("\n", start)
        return len(text) if newline == -1 else newline

    matches = []
    for match in re.finditer(ASSISTANT_PREFIX_PATTERN, text, re.IGNORECASE | re.MULTILINE):
        if not re.match(MCP_TOOL_CALL_PATTERN, text[match.start() : line_end(match.start())]):
            matches.append(match)

    if not matches:
        return None

    complete_cells = []
    prose_start = None
    for index, match in enumerate(matches):
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        cell_tail = text[line_end(match.start()) : next_start]
        continuation = re.search(r"^[^\S\n]*└[^\n]*(?:\n|$)", cell_tail, re.MULTILINE)
        contains_mcp_call = re.search(MCP_TOOL_CALL_PATTERN, cell_tail, re.MULTILINE)
        if continuation and not contains_mcp_call:
            complete_cells.append(index)
            if index == len(matches) - 1:
                remaining = cell_tail[continuation.end() :]
                separator = re.search(r"^[^\S\n]*\n", remaining, re.MULTILINE)
                following = re.search(r"\S", remaining[separator.end() :]) if separator else None
                if separator and following:
                    candidate = (
                        line_end(match.start())
                        + continuation.end()
                        + separator.end()
                        + following.start()
                    )
                    if text[candidate] != "›":
                        prose_start = candidate

    if len(complete_cells) >= 2:
        last_cell = complete_cells[-1]
        if last_cell + 1 < len(matches):
            return matches[last_cell + 1]
        if prose_start is not None:
            return re.compile("").match(text, prose_start)

    return matches[0]


class ProviderError(Exception):
    """Exception raised for provider-specific errors."""

    pass


class CodexProvider(BaseProvider):
    """Provider for Codex CLI tool integration."""

    # Codex redraws its inline TUI in place. The append-only pipe-pane stream
    # therefore retains transient progress frames (notably MCP startup) after
    # they have been erased from the visible terminal. Route status detection
    # through StatusMonitor's pyte-composited viewport so get_status() sees only
    # the live frame rather than stale redraw history.
    supports_screen_detection = True

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        agent_profile: Optional[str] = None,
        allowed_tools: Optional[list] = None,
        skill_prompt: Optional[str] = None,
        model: Optional[str] = None,
    ):
        """Initialize provider state."""
        super().__init__(terminal_id, session_name, window_name, allowed_tools, skill_prompt)
        self._initialized = False
        self._agent_profile = agent_profile
        # Explicit per-call override for profile.model, see _build_codex_command.
        self._model = model

    @property
    def blocks_orchestrated_input_while_waiting_user_answer(self) -> bool:
        """The first-run login/auth menu consumes pasted text as a menu selection.

        Now that ``initialize()`` accepts ``WAITING_USER_ANSWER`` as a successful init
        outcome (for a credential-less account parked on the login menu), an assign/handoff
        launch that lands there must not have its orchestrated task text pasted into the
        live menu -- it would be read as an option selection, not delivered as a task.
        Opting in (matching hermes.py/antigravity_cli.py) makes terminal_service's send_input
        guard hold orchestrated delivery until the prompt clears, while still allowing an
        explicit answer_user_prompt call through.
        """
        return True

    def _developer_instructions_file_path(self) -> Path:
        """Path of this terminal's developer_instructions temp file.

        Single source of truth for the path -- both `_build_codex_command` (which
        writes it) and `cleanup` (which removes it) call this instead of each
        re-deriving the same `CAO_HOME_DIR / "tmp" / f"{...}.codex_developer_instructions"`
        expression independently, which would let the two silently drift apart.
        """
        return CAO_HOME_DIR / "tmp" / f"{self.terminal_id}.codex_developer_instructions"

    def _build_codex_command(self) -> str:
        """Build Codex command with agent profile if provided.

        Returns properly escaped shell command string that can be safely sent via tmux.
        Uses codex's -c developer_instructions flag to inject agent system prompts.
        """
        # --yolo (alias for --dangerously-bypass-approvals-and-sandbox)
        # is the default because CAO runs codex non-interactively in tmux
        # where approval prompts would block handoff/assign. Profiles can
        # opt out via `codexProfile` (names a [profiles.<name>] block in
        # ~/.codex/config.toml), unless unrestricted allowed tools are enabled.
        # In practice, allowed_tools containing "*" is treated as yolo mode
        # and overrides codexProfile in the same way as an explicit yolo launch.
        yolo = bool(self._allowed_tools and "*" in self._allowed_tools)

        profile = None
        if self._agent_profile is not None:
            try:
                profile = load_agent_profile(self._agent_profile)
            except Exception as e:
                raise ProviderError(f"Failed to load agent profile '{self._agent_profile}': {e}")

        if profile and profile.codexProfile and not yolo:
            command_parts = ["codex", "--profile", profile.codexProfile]
        else:
            if profile and profile.codexProfile:
                # The operator asked for containment and is not getting it. Naming a
                # codexProfile is the ONLY way to keep Codex's sandbox and approval
                # policy in effect under CAO, and allowed_tools containing "*"
                # discards it. Silently dropping an explicit safety setting is worse
                # than not offering one, so say so at the point it happens (#707).
                logger.warning(
                    "Terminal %s: profile '%s' sets codexProfile=%r, but allowed_tools "
                    "contains '*', which forces --yolo and discards it. This worker "
                    "runs with Codex approvals AND sandbox bypassed. Remove '*' from "
                    "allowed_tools to keep codexProfile in effect.",
                    self.terminal_id,
                    self._agent_profile,
                    profile.codexProfile,
                )
            else:
                # --yolo is --dangerously-bypass-approvals-and-sandbox, so NOTHING in
                # ~/.codex/config.toml's [sandbox_*] tables applies to this worker --
                # including network_access. Operators have relied on that setting as a
                # containment control (#707); it is inert here. Logged on every default
                # codex launch because that is exactly when the expectation is set.
                logger.info(
                    "Terminal %s: launching codex with --yolo "
                    "(--dangerously-bypass-approvals-and-sandbox). Codex sandbox "
                    "settings in ~/.codex/config.toml, including "
                    "[sandbox_workspace_write].network_access, do NOT apply. Set "
                    "codexProfile on the agent profile to launch under a named "
                    "[profiles.<name>] block instead.",
                    self.terminal_id,
                )
            command_parts = ["codex", "--yolo"]
        command_parts.extend(["--no-alt-screen", "--disable", "shell_snapshot"])

        # self._model is an explicit per-call override (handoff/assign's own
        # `model` parameter) and wins over the profile's own static model
        # field when both are given; applies even with no profile at all.
        resolved_model = self._model or (profile.model if profile else None)
        if resolved_model:
            command_parts.extend(["--model", resolved_model])

        # Set below, only when there is a non-empty system_prompt to inject -- appended, raw and
        # deliberately unquoted by shlex, after the shlex.join() of everything else at the very
        # end of this method. See the long comment at its assignment site for why.
        developer_instructions_fragment: Optional[str] = None

        if profile is not None:
            system_prompt = profile.system_prompt if profile.system_prompt is not None else ""
            system_prompt = self._apply_skill_prompt(system_prompt)

            # Prepend security constraints for soft enforcement (Codex has no
            # native tool restriction mechanism). Only applied when tool
            # restrictions are active (not unrestricted "*").
            if self._allowed_tools and "*" not in self._allowed_tools:
                from cli_agent_orchestrator.constants import SECURITY_PROMPT

                tools_list = ", ".join(self._allowed_tools)
                tool_constraint = f"\nYou only have access to these tools: {tools_list}\n"
                system_prompt = SECURITY_PROMPT + tool_constraint + system_prompt

            if system_prompt:
                # Codex accepts developer_instructions via -c config override.
                # This is injected as a developer role message before AGENTS.md content.
                # Escape backslashes, double quotes, and newlines for TOML basic string.
                # Newlines must become literal \n to prevent tmux send_keys from
                # splitting the command across multiple lines.
                #
                # The escaped value is written to a CAO-owned temp file and referenced via a
                # shell command substitution ($(cat <file>)) instead of being inlined directly,
                # so the LAUNCH LINE ITSELF (what actually gets typed/pasted into the tmux pane)
                # stays short regardless of how long the instructions text is. A real profile
                # combining a security preamble, the caller's own system prompt, and the full
                # skill-list prompt (see _apply_skill_prompt) commonly produces several KB of
                # escaped text -- observed live at 8+KB. At launch time the pane is still a bare
                # shell (codex has not started yet), which correctly does not get bracketed-paste
                # framing (see clients/tmux.py's BRACKETED_PASTE_INCOMPATIBLE_SHELLS) since a bare
                # shell does not understand those escape sequences. But WITHOUT that framing, a
                # single pasted/typed line longer than the tty's canonical-mode line-length limit
                # (MAX_CANON, 4096 bytes on Linux) is silently truncated/dropped by the kernel's
                # tty line discipline before the shell ever sees a complete, valid command --
                # this manifests as the shell hanging at an unclosed-quote continuation prompt
                # forever (confirmed live: zero codex process ever spawned under the pane's shell,
                # even after an explicit trailing Enter), until CAO's own init-timeout eventually
                # fires with a generic "Codex initialization timed out" that gives no hint of the
                # real cause. $(cat <file>) is expanded internally by the shell BEFORE exec'ing
                # codex -- that internal expansion is not subject to the tty's per-line INPUT
                # limit at all, only the typed/pasted command line is. Wrapped in double quotes
                # (not left bare, not single-quoted) so the substitution still happens (command
                # substitution is disabled inside single quotes) while word-splitting/globbing of
                # the substituted content is suppressed (it is not inside single quotes either).
                # The file's own content is `_toml_scalar`'s output verbatim, already including
                # its own surrounding TOML double-quotes -- appended as a raw, deliberately
                # UNquoted-by-shlex fragment after the main shlex.join() below (shlex.join would
                # otherwise single-quote the whole "developer_instructions=$(cat ...)" fragment as
                # one opaque token, disabling the substitution it depends on).
                #
                # Same underlying instructions/skills length problem does not affect Claude Code
                # or Kimi CLI providers -- both already write the system prompt to a temp file and
                # pass a short file-path flag instead of inlining it (see claude_code.py's
                # --append-system-prompt-file, kimi_cli.py's system_prompt_path: YAML field).
                # Codex has no direct equivalent of that "arbitrary absolute path" flag (its only
                # file-loading mechanism, --profile, resolves names relative to $CODEX_HOME, which
                # this provider has no reliable way to resolve per-account from here) -- this
                # command-substitution approach reaches the same practical outcome (a short launch
                # line) without needing that.
                #
                # Deliberate, documented shell-scope trade-off (not an oversight): $(...) command
                # substitution is POSIX and works identically on every shell CAO's own
                # BRACKETED_PASTE_INCOMPATIBLE_SHELLS (constants.py) already tracks as a shell
                # class *except* csh/tcsh, which use `cmd` backticks instead and do not recognize
                # `$(` as substitution syntax at all -- launching codex from a pane whose bare
                # shell is csh/tcsh would break outright with this fragment malformed/rejected by
                # the shell, not merely degrade. bash/zsh/dash/sh/ksh/mksh/ash/fish are all fine.
                # No code here detects or special-cases the pane's shell before writing this
                # fragment (unlike BRACKETED_PASTE_INCOMPATIBLE_SHELLS' own runtime
                # #{pane_current_command} probe) -- csh/tcsh support, if ever needed, is scoped
                # out of this fix rather than silently assumed to already work.
                #
                # Not covering here (disclosed, not silently assumed away): the other -c overrides
                # below (per-MCP-server config, codexConfig) are NOT routed through this same
                # mechanism and remain inlined directly -- they are typically far smaller than
                # developer_instructions, but a profile configuring many MCP servers could in
                # theory still accumulate enough inline -c overrides to hit the same limit. Left
                # as a known, scoped-out follow-up rather than expanding this fix's surface.
                developer_instructions_file = self._developer_instructions_file_path()
                developer_instructions_file.parent.mkdir(parents=True, exist_ok=True)
                # Open with mode 0o600 baked into the O_CREAT call itself (rather than
                # write_text() followed by a separate chmod()) so the file is never
                # briefly world/group-readable between creation and permission-tightening --
                # the permissions are correct from the very first byte written.
                fd = os.open(
                    developer_instructions_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
                )
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(_toml_scalar(system_prompt))
                developer_instructions_fragment = f'-c "developer_instructions=$(cat {shlex.quote(str(developer_instructions_file))})"'

            # Add MCP servers via -c config overrides (per-session, no global config changes).
            # Each server field is set via dotted path: mcp_servers.<name>.<field>=<value>
            if profile.mcpServers:
                for server_name, server_config in profile.mcpServers.items():
                    # Codex-only validation: the server name becomes part of
                    # the -c override PATH (a TOML dotted path), so it must be
                    # a single bare key — a quote/newline would corrupt the
                    # TOML and a dot would nest the server under the wrong
                    # table. Other providers write JSON configs where any
                    # string key is valid, so they don't need this.
                    _validate_config_key(server_name, source="mcpServers name")
                    prefix = f"mcp_servers.{server_name}"
                    if isinstance(server_config, dict):
                        cfg = dict(server_config)
                    else:
                        cfg = server_config.model_dump(exclude_none=True)
                    # Resolve the bundled cao-mcp-server console script to a
                    # PATH-independent invocation.
                    cfg = resolve_mcp_server_config(cfg)
                    if "command" in cfg:
                        command_parts.extend(
                            ["-c", f"{prefix}.command={_toml_scalar(cfg['command'])}"]
                        )
                    if "args" in cfg:
                        args_toml = "[" + ", ".join(_toml_scalar(a) for a in cfg["args"]) + "]"
                        command_parts.extend(["-c", f"{prefix}.args={args_toml}"])
                    if "env" in cfg and cfg["env"]:
                        for env_key, env_val in cfg["env"].items():
                            _validate_config_key(env_key, source="mcpServers env")
                            command_parts.extend(
                                ["-c", f"{prefix}.env.{env_key}={_toml_scalar(str(env_val))}"]
                            )
                    # Forward CAO_TERMINAL_ID so MCP servers (e.g. cao-mcp-server)
                    # can identify the current session for handoff/assign operations.
                    # Codex does not forward env vars to MCP subprocesses by default;
                    # env_vars lists names to inherit from the parent shell environment.
                    env_vars = cfg.get("env_vars", [])
                    if "CAO_TERMINAL_ID" not in env_vars:
                        env_vars = list(env_vars) + ["CAO_TERMINAL_ID"]
                    env_vars_toml = "[" + ", ".join(_toml_scalar(v) for v in env_vars) + "]"
                    command_parts.extend(["-c", f"{prefix}.env_vars={env_vars_toml}"])
                    # Set a generous tool timeout for MCP calls like handoff, which
                    # create a new terminal, initialize the provider, send a message,
                    # wait for the agent to complete, and extract the output.
                    # Codex defaults to 60s which is too short for multi-step operations.
                    # Value MUST be a TOML float (600.0, not 600) because Codex
                    # deserializes tool_timeout_sec via Option<f64>; a TOML integer
                    # is silently rejected and falls back to the 60s default.
                    if "tool_timeout_sec" not in cfg:
                        command_parts.extend(["-c", f"{prefix}.tool_timeout_sec=600.0"])

            # Inline Codex config overrides (-c key=value). Lets a profile set
            # per-agent Codex knobs — reasoning effort, service tier, fast mode,
            # etc. — without editing the global ~/.codex/config.toml or
            # maintaining named profile files. Keys may be dotted config paths
            # (e.g. "features.fast_mode"); values are serialized to TOML
            # scalars. Emitted last so they take precedence over CAO's own
            # overrides and the profile/config defaults on key conflicts.
            if profile.codexConfig:
                for key, value in profile.codexConfig.items():
                    command_parts.extend(["-c", _toml_override(key, value)])

        # Suppress the startup update dialog at the source. Placed last so it
        # wins even if a profile sets check_for_update_on_startup=true.
        command_parts.extend(["-c", "check_for_update_on_startup=false"])

        command = shlex.join(command_parts)
        if developer_instructions_fragment is not None:
            command = f"{command} {developer_instructions_fragment}"
        return command

    async def _handle_trust_prompt(self, timeout: float = 20.0) -> None:
        """Dismiss startup prompts that block readiness.

        Handles two classes of blocking dialog in a single poll loop:

        1. Workspace trust prompt (two variants):
             v0.98+: "allow Codex to work in this folder"
             v0.130+ (git worktree): "Do you trust the contents of this directory?"
           Dismissed with Enter (default = allow).

        2. Update-available dialog (defense-in-depth; normally suppressed via
           -c check_for_update_on_startup=false at launch):
             "Update available! X -> Y" with numbered menu.
           Dismissed with '3'+Enter ("Skip until next version"). A blind Enter
           would select "1. Update now" (global npm install).
        """
        start_time = time.time()
        trust_dismissed = False
        update_dismissed = False
        while time.time() - start_time < timeout:
            output = get_backend().get_history(self.session_name, self.window_name)
            if not output:
                await asyncio.sleep(1.0)
                continue

            clean_output = strip_terminal_escapes(re.sub(ANSI_CODE_PATTERN, "", output))

            if not trust_dismissed and re.search(TRUST_PROMPT_PATTERN, clean_output):
                from cli_agent_orchestrator.services.status_monitor import status_monitor

                logger.info("Codex workspace trust prompt (v1) detected, auto-accepting")
                status_monitor.notify_input_sent(self.terminal_id)
                get_backend().send_special_key(self.session_name, self.window_name, "Enter")
                trust_dismissed = True
                await asyncio.sleep(1.0)
                continue

            bottom_region = "\n".join(clean_output.splitlines()[-STARTUP_PROMPT_BOTTOM_LINES:])

            if (
                not trust_dismissed
                and re.search(TRUST_PROMPT_PATTERN_V2, bottom_region)
                and re.search(TRUST_PROMPT_FOOTER, bottom_region)
            ):
                from cli_agent_orchestrator.services.status_monitor import status_monitor

                logger.info("Codex workspace trust prompt (v2) detected, auto-accepting")
                status_monitor.notify_input_sent(self.terminal_id)
                get_backend().send_special_key(self.session_name, self.window_name, "Enter")
                trust_dismissed = True
                await asyncio.sleep(1.0)
                continue

            if not update_dismissed and _has_update_dialog_in_bottom(clean_output):
                from cli_agent_orchestrator.services.status_monitor import status_monitor

                logger.info(
                    "Codex update-available dialog detected, selecting " "'Skip until next version'"
                )
                status_monitor.notify_input_sent(self.terminal_id)
                get_backend().send_keys(self.session_name, self.window_name, "3", enter_count=0)
                # TUI rendering latency: '3' highlights the menu item, Enter confirms.
                await asyncio.sleep(0.3)
                get_backend().send_special_key(self.session_name, self.window_name, "Enter")
                update_dismissed = True
                await asyncio.sleep(1.0)
                continue

            # Exit when the bottom region shows the idle composer prompt AND no
            # dialog is active. The welcome banner alone is insufficient — it
            # renders as normal startup chrome BEFORE a late update dialog appears.
            has_idle = _has_startup_idle_composer(clean_output)
            has_dialog = (
                re.search(TRUST_PROMPT_PATTERN, bottom_region)
                or (
                    re.search(TRUST_PROMPT_PATTERN_V2, bottom_region)
                    and re.search(TRUST_PROMPT_FOOTER, bottom_region)
                )
                or _has_update_dialog_in_bottom(clean_output)
            )
            if has_idle and not has_dialog:
                logger.info("Codex started — idle prompt visible, no blocking dialog")
                return

            await asyncio.sleep(1.0)

        pane_tail = ""
        try:
            output = get_backend().get_history(self.session_name, self.window_name)
            if output:
                pane_tail = "\n".join(output.splitlines()[-10:])
        except Exception:
            pass
        logger.error(
            "Codex startup prompt handler timed out — no prompt or welcome banner detected. "
            "Pane tail:\n%s",
            pane_tail,
        )

    async def initialize(self) -> bool:
        """Initialize Codex provider by starting codex command."""
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        init_timeout = get_server_settings()["provider_init_timeout"]
        if not await wait_for_shell(self.terminal_id, timeout=init_timeout):
            raise TimeoutError(f"Shell initialization timed out after {init_timeout}s")

        # Capture the shell process name before launching codex — used later to
        # detect when codex has exited and the pane is back to a bare shell.
        self.shell_baseline = get_backend().get_pane_current_command(
            self.session_name, self.window_name
        )

        # Send a warm-up command before launching codex.
        # Codex exits immediately in freshly-created tmux sessions where the shell
        # has not yet processed a full interactive command cycle.
        # Arm the StatusMonitor stickiness gate: each send_keys here represents
        # external input that must be allowed to drive PROCESSING transitions
        # past any previously-latched ready state.
        status_monitor.notify_input_sent(self.terminal_id)
        get_backend().send_keys(self.session_name, self.window_name, "echo ready")
        await asyncio.sleep(2.0)

        # Build command with flags and agent profile (developer_instructions).
        # --no-alt-screen: run in inline mode so output stays in normal scrollback,
        #   making tmux capture-pane reliable.
        # --disable shell_snapshot: avoid TTY input conflicts (SIGTTIN) in tmux
        #   caused by the shell_snapshot subprocess inheriting stdin.
        command = self._build_codex_command()
        status_monitor.notify_input_sent(self.terminal_id)
        get_backend().send_keys(self.session_name, self.window_name, command)

        # Handle workspace trust prompt if it appears (new/untrusted directories)
        await self._handle_trust_prompt(timeout=20.0)

        # WAITING_USER_ANSWER is included here specifically for the first-run login/auth
        # menu (see LOGIN_MENU_PATTERN's own comment) — an account with no credentials
        # configured yet is a real, expected state at this exact point (trust/update
        # dialogs above are already auto-dismissed by _handle_trust_prompt, so nothing
        # else should legitimately produce WAITING_USER_ANSWER this early), not a failure.
        # Without this, initialize() had no way to ever succeed for such an account: the
        # pane would sit at a correctly-rendered, fully-alive login screen forever without
        # reaching IDLE/COMPLETED, and CAO would tear the terminal down on every single
        # attempt before an operator had any real chance to open the session and complete
        # login themselves.
        #
        # CodexProvider now overrides `blocks_orchestrated_input_while_waiting_user_answer`
        # (see the property above) specifically so this WAITING_USER_ANSWER init-success
        # path can't let an assign/handoff paste the orchestrated task into the live login
        # menu -- the same composition hazard PR #539's review flagged for ClaudeCodeProvider's
        # own choice-prompt widening, fixed here the same way rather than left open.
        if not await wait_until_status(
            self.terminal_id,
            {TerminalStatus.IDLE, TerminalStatus.COMPLETED, TerminalStatus.WAITING_USER_ANSWER},
            timeout=float(get_server_settings()["provider_init_timeout"]),
            polling_interval=1.0,
        ):
            raise TimeoutError("Codex initialization timed out after 60 seconds")

        self._initialized = True
        return True

    def get_status(self, output: str) -> TerminalStatus:
        # Native status (herdr): trust the backend's agent state when available;
        # on herdr the buffer is never fed, so buffer parsing can't leave UNKNOWN.
        native = self._resolve_native_status(output)
        if native is not None:
            return native

        # herdr never pushes a buffer (pipe_pane is a no-op there); read live
        # pane content instead of falling through to "no output" on every call.
        output = self._resolve_buffer(output)
        if not output:
            return TerminalStatus.UNKNOWN

        # Detect when the codex process has exited and the pane is back to a
        # bare shell. The pane's current command will revert to the shell
        # (e.g. "zsh") that was running before we launched codex. Returning
        # ERROR prevents the inbox service from typing a queued message into
        # the shell — which would execute it as arbitrary commands.
        if self._initialized and self.shell_baseline:
            current_cmd = get_backend().get_pane_current_command(
                self.session_name, self.window_name
            )
            if current_cmd == self.shell_baseline:
                return TerminalStatus.ERROR

        # Strip the RAW pipe-pane escapes (cursor positioning, in-place redraws),
        # not just SGR colour codes — otherwise cursor sequences survive and the
        # idle ``›`` prompt / structural checks below misfire on the raw stream.
        clean_output = strip_terminal_escapes(output)
        tail_output = "\n".join(clean_output.splitlines()[-25:])

        # Search for user messages, excluding the Codex TUI footer when present.
        # The TUI footer (idle prompt hint like "› Summarize recent commits" +
        # status bar "? for shortcuts / context left") can contain › followed by
        # suggestion text, which USER_PREFIX_PATTERN would incorrectly match as
        # user input, preventing COMPLETED detection.
        # Only apply the cutoff when TUI footer indicators are actually present
        # to avoid over-excluding in short outputs or test fixtures.
        all_lines = clean_output.splitlines()
        tui_footer_detected = any(
            re.search(TUI_FOOTER_PATTERN, line) for line in all_lines[-IDLE_PROMPT_TAIL_LINES:]
        )
        if tui_footer_detected:
            cutoff_pos = _compute_tui_footer_cutoff(all_lines)
        else:
            cutoff_pos = len(clean_output)

        last_user = None
        for match in re.finditer(USER_PREFIX_PATTERN, clean_output, re.IGNORECASE | re.MULTILINE):
            if match.start() < cutoff_pos:
                last_user = match

        output_after_last_user = clean_output[last_user.start() :] if last_user else clean_output
        # Skip MCP tool-call markers — those mark "model invoked a tool", not
        # "model has replied", and shouldn't gate WAITING/ERROR detection.
        assistant_after_last_user = bool(
            last_user and _find_assistant_marker(output_after_last_user) is not None
        )

        # Check trust prompt early — the trust menu uses › which matches the idle prompt
        # pattern, and PROCESSING_PATTERN matches "running" in "You are running Codex in..."
        if re.search(TRUST_PROMPT_PATTERN, clean_output):
            return TerminalStatus.WAITING_USER_ANSWER

        # V2 trust dialog ("Do you trust the contents of this directory?" / "Press enter
        # to continue"). Only classify as WAITING when BOTH the question AND the footer
        # appear in the bottom region — avoids false positives if the question text
        # appears in scrollback from a previous model response.
        bottom_region = "\n".join(clean_output.splitlines()[-15:])
        if re.search(TRUST_PROMPT_PATTERN_V2, bottom_region) and re.search(
            TRUST_PROMPT_FOOTER, bottom_region
        ):
            return TerminalStatus.WAITING_USER_ANSWER

        # Update-available dialog. Bottom-anchored like trust-v2 to avoid false
        # positives from scrollback. Never let this fall through to IDLE/COMPLETED
        # where a queued message or blind Enter could select "Update now".
        # Eager inbox delivery is not a vector: accepts_input_while_processing=False.
        if _has_update_dialog_in_bottom(clean_output):
            return TerminalStatus.WAITING_USER_ANSWER

        # First-run login/auth menu (no credentials configured yet). Bottom-anchored like
        # trust-v2, same reasoning. See LOGIN_MENU_PATTERN's own comment for why this can't
        # be auto-dismissed the way trust/update dialogs are, and why classifying it here
        # (rather than leaving it unrecognized) matters for initialize()'s own timeout.
        if re.search(LOGIN_MENU_PATTERN, bottom_region) and re.search(
            LOGIN_MENU_FOOTER, bottom_region
        ):
            return TerminalStatus.WAITING_USER_ANSWER

        # Boxed command-approval modal ("Command Approval Required" / "[a] Accept"
        # / "[d] Decline"). Reuses the copy that STARTUP_BLOCKING_INPUT_PATTERN
        # already vetoes readiness on at startup — the same modal can appear at
        # RUNTIME under any approval-prompting codexProfile, and only the startup
        # path used to notice it.
        #
        # Bottom-anchored like trust-v2 and the update dialog, and placed BEFORE
        # the idle/COMPLETED classification for the same reason: the TUI composer
        # and status bar keep rendering while the modal is up, so the idle-prompt
        # check below would otherwise report COMPLETED (or PROCESSING when the
        # composer has scrolled off) for a pane that is hard-blocked on a
        # keystroke. A COMPLETED there is the dangerous case — it tells the
        # conductor the agent is free and invites more work into a dead pane.
        #
        # NOT gated on `not assistant_after_last_user` (unlike WAITING_PROMPT_PATTERN
        # below): the modal is raised mid-turn, after the model has already emitted
        # bullets, so that gate would suppress every real occurrence. Prose that
        # merely quotes the copy is excluded structurally instead — see
        # _has_approval_modal_in_bottom.
        if _has_approval_modal_in_bottom(clean_output):
            return TerminalStatus.WAITING_USER_ANSWER

        # Runtime approval prompt as codex-cli 0.147.0 actually renders it -- a
        # numbered menu, not the boxed modal above. This is the check that fires on
        # a current-Codex approval; without it a live prompt classified as IDLE
        # (verified against the live capture in
        # test/providers/fixtures/codex_approval_modal_raw.txt), because the
        # prompt's own "› 1. Yes, proceed (y)" cursor line is both the last
        # USER_PREFIX_PATTERN match and an idle-prompt match, so the classification
        # below saw a user message with no reply after it. IDLE is as dangerous as
        # COMPLETED here: both tell the conductor the pane is free.
        #
        # Placed after the legacy modal check and before the idle classification,
        # for the same reason: the composer and status bar keep rendering while the
        # prompt is up, so the idle-prompt check cannot see the block.
        #
        # Structural, not title-driven: see _has_approval_prompt_in_bottom. Both
        # this buffer path and get_status_from_screen's rendered-screen path reach
        # it through this one call, so the two cannot disagree.
        if _has_approval_prompt_in_bottom(clean_output):
            return TerminalStatus.WAITING_USER_ANSWER

        # Check bottom of captured output for idle prompt.
        # With --no-alt-screen, scrollback contains history so we can't anchor
        # to end-of-string. Instead, check only the last few lines.
        bottom_lines = clean_output.strip().splitlines()[-IDLE_PROMPT_TAIL_LINES:]
        has_idle_prompt_at_end = any(
            re.match(rf"\s*{IDLE_PROMPT_PATTERN}", line, re.IGNORECASE) for line in bottom_lines
        )

        # Only treat ERROR/WAITING prompts as actionable if they appear after the last user message
        # and are not part of an assistant response.
        if last_user is not None:
            if not assistant_after_last_user:
                if re.search(
                    WAITING_PROMPT_PATTERN,
                    output_after_last_user,
                    re.IGNORECASE | re.MULTILINE,
                ):
                    return TerminalStatus.WAITING_USER_ANSWER
                if re.search(
                    ERROR_PATTERN,
                    output_after_last_user,
                    re.IGNORECASE | re.MULTILINE,
                ):
                    return TerminalStatus.ERROR
        else:
            if re.search(WAITING_PROMPT_PATTERN, tail_output, re.IGNORECASE | re.MULTILINE):
                return TerminalStatus.WAITING_USER_ANSWER
            if re.search(ERROR_PATTERN, tail_output, re.IGNORECASE | re.MULTILINE):
                return TerminalStatus.ERROR
        if has_idle_prompt_at_end:
            # Check for TUI progress indicator ("• Working (0s • esc to interrupt)").
            # With --no-alt-screen, the TUI footer (› hint + status bar) is always
            # rendered at the bottom, even during processing. The • in the progress
            # spinner matches ASSISTANT_PREFIX_PATTERN, causing a false COMPLETED.
            # Detect the spinner and return PROCESSING before checking for COMPLETED.
            if re.search(TUI_PROGRESS_PATTERN, tail_output, re.MULTILINE):
                return TerminalStatus.PROCESSING

            # Consider COMPLETED only if we see an assistant marker (skipping
            # MCP tool-call markers) after the last user message. Without the
            # tool-call filter, "• Called <server>.<tool>(...)" emitted before
            # the model has actually replied would trip COMPLETED prematurely.
            if last_user is not None:
                if _find_assistant_marker(clean_output[last_user.start() :]) is not None:
                    return TerminalStatus.COMPLETED

                return TerminalStatus.IDLE

            # No user-message marker in the cleaned buffer. Two cases:
            # - Fresh init: no assistant content either → IDLE.
            # - Long-running response: the › user marker has been evicted from
            #   the rolling state buffer by the time the response settles, but an
            #   assistant bullet is still visible. Without this branch we'd
            #   return IDLE forever and ``wait_for_status(completed)`` in the
            #   e2e tests would time out.
            # Search above the TUI footer cutoff so the › suggestion-hint and
            # status-bar lines aren't confused with a model reply.
            if _find_assistant_marker(clean_output[:cutoff_pos]) is not None:
                return TerminalStatus.COMPLETED
            return TerminalStatus.IDLE

        # If we're not at an idle prompt and we don't see explicit errors/permission prompts,
        # assume the CLI is still producing output.
        return TerminalStatus.PROCESSING

    def get_status_from_screen(self, screen_lines: list[str]) -> TerminalStatus:
        """Detect status from the current pyte-composited Codex viewport.

        Codex's existing detector is line-oriented and already understands its
        trust/update dialogs, progress spinner, idle composer, and completed
        response markers. Remove pyte's blank padding rows and reuse that
        detector against the rendered screen; cursor-erased startup frames are
        absent here, which prevents a stale spinner from pinning PROCESSING.
        """
        rows = [line.rstrip() for line in screen_lines if line.strip()]
        if not rows:
            return TerminalStatus.UNKNOWN
        return self.get_status("\n".join(rows))

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract Codex's final response from terminal output.

        Supports two output formats:
        - Label style: "You ...\\nassistant: response\\n❯" (synthetic/test format)
        - Bullet style: "› user message\\n• response\\n›" (real Codex interactive mode)

        Primary approach: find the last user message and extract everything between
        the end of that line and the next empty idle prompt.
        Fallback: use assistant marker based extraction when no user message is found.
        """
        # Strip ALL terminal escape sequences, not just SGR colour codes. The
        # narrow ANSI_CODE_PATTERN (``\x1b[...m``) leaves cursor-movement (H),
        # erase (K), and scroll CSI sequences in place; codex's TUI emits those
        # heavily, so an SGR-only strip returned raw escape garbage
        # (``[49;2H[K[38;2;...m``) as the "response", failing extraction. Use
        # the shared strip which also normalises \r and column-1 cursor moves to
        # newlines — this is fed a tmux capture-pane render (already laid out),
        # so the line-based extraction below still anchors correctly.
        clean_output = strip_terminal_escapes(script_output)

        # Primary: find last user message, extract response between it and idle prompt.
        # Exclude the Codex TUI footer from user-message matching when detected.
        all_lines = clean_output.splitlines()
        tui_footer_detected = any(
            re.search(TUI_FOOTER_PATTERN, line) for line in all_lines[-IDLE_PROMPT_TAIL_LINES:]
        )
        if tui_footer_detected:
            cutoff_pos = _compute_tui_footer_cutoff(all_lines)
        else:
            cutoff_pos = len(clean_output)

        user_matches = [
            m
            for m in re.finditer(USER_PREFIX_PATTERN, clean_output, re.IGNORECASE | re.MULTILINE)
            if m.start() < cutoff_pos
        ]

        if user_matches:
            last_user = user_matches[-1]

            # Extraction uses a stricter anchor than status detection: skip MCP
            # calls and at least two complete native activity cells before the
            # model's actual reply, while preserving ambiguous compact groups.
            asst_after_user = _find_response_marker(clean_output[last_user.start() :])

            if asst_after_user:
                response_start = last_user.start() + asst_after_user.start()
            else:
                # No assistant marker found; fall back to skipping one line
                user_line_end = clean_output.find("\n", last_user.start())
                if user_line_end == -1:
                    user_line_end = len(clean_output)
                response_start = user_line_end + 1

            # Find extraction boundary: empty idle prompt or TUI footer area.
            # With --no-alt-screen, the TUI footer (› hint + status bar) has no
            # empty idle prompt. Use cutoff_pos as the boundary when TUI is present.
            idle_after = re.search(
                IDLE_PROMPT_STRICT_PATTERN,
                clean_output[response_start:],
                re.MULTILINE,
            )
            if idle_after:
                end_pos = response_start + idle_after.start()
            elif tui_footer_detected:
                end_pos = cutoff_pos
            else:
                end_pos = len(clean_output)

            response_text = clean_output[response_start:end_pos].strip()

            if response_text:
                # Strip "assistant:" prefix if present (label format)
                response_text = re.sub(
                    r"^(?:assistant|codex|agent)\s*:\s*",
                    "",
                    response_text,
                    count=1,
                    flags=re.IGNORECASE,
                )
                return response_text.strip()

        # Fallback: assistant marker based extraction (no user message found).
        # Filter out "• Called <tool>(...)" MCP tool call markers so we anchor
        # on the model's actual reply, not tool output.
        all_matches = list(
            re.finditer(ASSISTANT_PREFIX_PATTERN, clean_output, re.IGNORECASE | re.MULTILINE)
        )
        matches = []
        for m in all_matches:
            line_end = clean_output.find("\n", m.start())
            if line_end == -1:
                line_end = len(clean_output)
            line = clean_output[m.start() : line_end]
            if re.match(MCP_TOOL_CALL_PATTERN, line):
                continue
            matches.append(m)

        if not matches:
            raise ValueError("No Codex response found - no assistant marker detected")

        last_match = matches[-1]
        start_pos = last_match.end()

        idle_after = re.search(
            IDLE_PROMPT_STRICT_PATTERN,
            clean_output[start_pos:],
            re.MULTILINE,
        )
        end_pos = start_pos + idle_after.start() if idle_after else len(clean_output)

        final_answer = clean_output[start_pos:end_pos].strip()

        if not final_answer:
            raise ValueError("Empty Codex response - no content found")

        return final_answer

    def exit_cli(self) -> str:
        """Get the command to exit Codex CLI."""
        return "/exit"

    def cleanup(self) -> None:
        """Clean up Codex CLI provider."""
        self._initialized = False
        # Remove the developer_instructions temp file written by _build_codex_command, if any --
        # same convention claude_code.py's own cleanup() uses for its analogous .prompt file.
        # Path comes from _developer_instructions_file_path() (single source of truth shared
        # with _build_codex_command) so the write site and the cleanup site can't drift apart.
        try:
            self._developer_instructions_file_path().unlink(missing_ok=True)
        except OSError:
            pass
