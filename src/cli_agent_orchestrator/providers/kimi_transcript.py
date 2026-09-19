"""Shared semantic classifier for Kimi CLI / Kimi Code terminal transcripts.

Both the legacy MoonshotAI ``kimi-cli`` TUI and the current "Kimi Code"
(agent-core-v2) TUI render their transcript with the same broad grammar — a
bullet per assistant turn, grey styling for reasoning, a status/footer row, a
composer frame — but they disagree on the *glyphs* and on which rows are chrome:

======================  ==========================  =========================
row                     legacy ``kimi-cli``        Kimi Code (0.43.1)
======================  ==========================  =========================
response bullet         ``•`` U+2022                ``●`` U+25CF, colour 253
thinking bullet         grey ``38;5;244`` + ``•``   grey ``38;5;244`` + ``●``
live working indicator  bare moon phase ``🌑…🌘``   braille ``⠙ working…``
idle tip row            —                           ``🌕 · Tip: …`` (NOT work)
composer                ``💫`` / ``✨`` prompt       boxed ``╭─╮ │ > │ ╰─╯``
footer                  ``HH:MM yolo agent (…)``    ``context: N% (…)``
banner                  ``Welcome to Kimi Code CLI!``  ``Welcome to Kimi Code!``
======================  ==========================  =========================

Before this module every consumer (``get_status``, ``get_status_from_screen``,
``extract_last_message_from_script``, ``_extract_without_input_box``,
``extract_session_context``) re-declared its own glyph assumptions, so each new
Kimi release had to be fixed in five places and the fixes drifted apart. This
module is the single place those assumptions live.

The classifier deliberately works on the pair ``(raw_line, clean_line)``: the
clean text answers "what is this row", the ANSI-preserved text answers "how is
it styled", and the two Kimi releases are only separable when both are
available (a ``●`` is a final answer or a thinking bullet *purely* by colour).

Observed against the A0 scrubbed fixtures (Kimi 0.43.1, 200x50, tmux 3.5a)::

    fixtures/02-processing-turn.txt    ESC[38;5;111m⠙ESC[39m working… ESC[38;5;244m · Tip: …
    fixtures/03-final-answer.txt       ESC[38;5;244m● ESC[3m**Generating Fixture List**ESC[0m
    fixtures/03-final-answer.txt       ESC[38;5;253m● ESC[39mSTEP 1
    fixtures/08-command-approval.txt   ESC[38;5;253m● ESC[1mESC[38;5;111mRunning a commandESC[0;2m · $ uname -a
    fixtures/09-false-moon-idle.txt     🌕ESC[38;5;244m · Tip: ctrl-s to add guidance …
"""

from __future__ import annotations

import enum
import re
from typing import List, Optional, Sequence, Set, Tuple

# ---------------------------------------------------------------------------
# SGR-only stripping. Mirrors kimi_cli.ANSI_CODE_PATTERN on purpose: consumers
# that already hold a clean line can pass it straight in, and the module keeps
# working if they only hold the raw line.
# ---------------------------------------------------------------------------
_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")

# The same sequence, but retaining its parameter list so a rule can ask *which*
# colour a row is drawn in rather than merely "is styled".
_SGR_PARAMS_RE = re.compile(r"\x1b\[([0-9;]*)m")

# Braille Patterns block — the Kimi Code live working indicator ("⠙ working…").
_BRAILLE_RE = re.compile(r"[\u2800-\u28ff]")

# Moon phases U+1F311..U+1F318. In Kimi Code these appear in the *idle* rotating
# tip row ("🌕 · Tip: …"), which is why they are NOT a processing signal here.
_MOON_RE = re.compile(r"[\U0001F311-\U0001F318]")

# The idle tip suffix. Kimi Code rotates tips; every observed variant ends the
# row with " · Tip: " in colour 244.
_TIP_RE = re.compile(r"·\s*Tip:")

# A line-start bullet plus at least one horizontal whitespace character.
#
# Anchored at line start and limited to [^\S\n] (horizontal whitespace only) so
# that a `●` embedded in chrome is never read as assistant output. This is the
# concrete defect A0 D2 records: the Kimi Code status footer renders
# `agent (Kimi-k2.6 ●)` and the older build's footer renders `… thinking`; a
# bare `●` search matches those. `[•●]` accepts both dialects' response glyphs.
BULLET_ANY_RE = re.compile(r"^[^\S\n]*[•●][^\S\n]+")
BULLET_LINE_RE = re.compile(r"^[^\S\n]*[•●]")

# Thinking styling. Both dialects draw reasoning in grey 38;5;244; Kimi Code
# additionally italicises it (`ESC[3m` right after the bullet). Kept as a set of
# alternatives rather than one expression so a future style addition is one
# entry, and so a test can assert which styles are recognised.
#
# The 24-bit case is deliberately NOT in this tuple: "any truecolor" is not
# evidence of reasoning, because a themed or emphasised *final answer* bullet is
# also truecolor. It is handled by `is_thinking_styled`, which additionally
# requires the triple to be grey/near-grey.
THINKING_STYLE_PATTERNS: Tuple[re.Pattern, ...] = (
    # grey + bullet (legacy `•`, current `●`), with optional whitespace between
    re.compile(r"\x1b\[38;5;244m[^\S\n]*[•●]"),
    # grey + italic + bullet (some legacy builds order the SGRs this way)
    re.compile(r"\x1b\[38;5;244m\x1b\[3m[^\S\n]*[•●]"),
    # bullet then italic (current Kimi Code: `ESC[38;5;244m● ESC[3m…`)
    re.compile(r"\x1b\[38;5;244m[^\S\n]*[•●][^\S\n]*\x1b\[3m"),
    # bullet then italic, with the colour carried on the text rather than the
    # bullet. Italic directly after a bullet is a measured thinking shape; the
    # final-answer colour is excluded separately in `is_thinking_styled`.
    re.compile(r"[•●][^\S\n]*\x1b\[3m"),
)

# The final-answer bullet colour (253). Decisive: a bullet drawn in it is an
# answer even when the text that follows is italic, which is exactly what a
# themed or emphasis-carrying answer looks like. Checked before every thinking
# rule so no styling heuristic can suppress a real answer.
FINAL_ANSWER_BULLET_STYLE_RE = re.compile(r"\x1b\[38;5;253m[^\S\n]*[•●]")

# 24-bit foreground before a bullet. The triple must be grey/near-grey to count
# as reasoning.
TRUECOLOR_BULLET_RE = re.compile(r"\x1b\[38;2;(\d{1,3});(\d{1,3});(\d{1,3})m[^\S\n]*[•●]")
# Max channel spread still called "grey". 0 is pure grey; a slightly warm or
# cool grey (128/130/126) is still reasoning. A saturated theme colour is not.
TRUECOLOR_GREY_TOLERANCE = 16

# ---------------------------------------------------------------------------
# Chrome / structure
#
# Every rule below is *structural*: a row is chrome because of where it sits in
# the row, what styles it, or how the whole row is shaped — never because a
# piece of UI vocabulary happens to appear somewhere inside it.
#
# That distinction is the whole point. A substring test for
# "connecting to mcp servers" classifies the assistant sentence
# "● connecting to mcp servers is not the problem" as BOOT_CHROME, and because
# `_locate_response_region` treats BOOT_CHROME as a response-end anchor the
# answer is then truncated at that line. The same holds for
# "The reported context: 50% is expected." and STATUS_FOOTER. Measured on the
# A0 captures, all of these are ordinary assistant prose.
# ---------------------------------------------------------------------------

# --- Welcome banner ------------------------------------------------------
#
# Kimi Code draws its startup banner inside a `│ … │` box whose frame is colour
# 111; the composer below it is the same shape in colour 240. The frame colour
# is therefore the discriminator between the two boxes, and it also keeps a
# Markdown table row (`│ a │ b │`, unstyled) out of this rule.
WELCOME_BOX_FRAME_RE = re.compile(r"\x1b\[38;5;111m[│╭╰]")
# The banner text as drawn: bold + colour 111, e.g.
# `ESC[1mESC[38;5;111mWelcome to Kimi Code!`.
WELCOME_BANNER_STYLED_RE = re.compile(r"\x1b\[1m\x1b\[38;5;111m\s*Welcome to Kimi Code(?: CLI)?!")
# The banner as drawn inside the welcome box: a `│`-prefixed row whose text is
# the banner. Covers both the 0.43.1 box (frame colour 111) and the older build
# that renders `Welcome to Kimi Code CLI!` in a colour-33 box. Structural: the
# box framing plus the banner text, so an answer line that merely says
# "Welcome to Kimi Code! is the literal banner" is not caught.
#
# Deliberately NOT end-anchored on a closing `│`. Measured on the 0.43.1
# capture, the banner row is
#   `ESC[38;5;111m│ESC[39m  ESC[38;5;111m▐█▛█▛█▌ESC[39m  ESC[1m…Welcome to Kimi Code!ESC[0m` + padding
# — the right edge is not drawn on this row, it is space-padded to the terminal
# width. Requiring a closing edge made this rule dead for the very banner it
# documents, which only stayed covered because the raw path also has the
# colour-111 frame marker. The screen path (`get_status_from_screen`) receives
# escape-free rows, so it had no coverage at all.
WELCOME_BOX_BANNER_RE = re.compile(r"^\s*│[^│]*Welcome to Kimi Code(?: CLI)?!")
# The banner as a whole row. Alternation, not replacement: the legacy banner
# must keep matching. Never used as the sole dialect detector (A1.1).
WELCOME_BANNER_RE = re.compile(r"^\s*Welcome to Kimi Code(?: CLI)?!\s*$")

# --- Boot messages -------------------------------------------------------
#
# Whole-row anchored. `Loading configuration…` is a boot row only when the row
# *is* that message; an answer that begins "● Loading configuration is the next
# step" is content.
#
# The optional leading braille glyph is measured, not decorative: Kimi draws
# these boot progress rows with the same indicator slot as the live spinner
# ("⠋ Loading configuration...", "⠏ Restoring conversation..."), so without it
# they read as a live turn-in-flight spinner and a freshly-booted terminal never
# reaches IDLE.
BOOT_MESSAGE_ROW_RE = re.compile(
    r"^\s*(?:[\u2800-\u28ff]\s*)?(?:"
    r"Loading configuration|Loading agent|Resolving dependencies|Restoring conversation"
    r"|Send /help for help information"
    r"|No session yet(?:\s*[—–-].*)?"
    r"|Run /web to continue your session in the browser"
    r"|✦\s*Try Kimi Code Web UI.*"
    r"|MCP server \"[^\"]*\" connected(?:\s*·.*)?"
    r"|tmux extended-keys is off.*"
    r")\s*[.…\s]*$",
    re.IGNORECASE,
)

# MCP boot progress rows. Braille glyphs appear here while the terminal is
# genuinely idle at the welcome screen, so they must not be read as a live
# turn-in-flight spinner. Measured shapes:
#
#   "⠧ MCP Servers: 0/1 connected, 0 tools"
#   "⠦ cao-mcp-server (connecting)"
#   "connecting to mcp servers..."
#
# Anchored to the whole row, and the trailing character class admits only
# punctuation / counts — so "... is only a phrase" stays content.
MCP_BOOT_ROW_RE = re.compile(
    r"^\s*(?:"
    r"[\u2800-\u28ff]\s*MCP Servers:\s*\d+/\d+.*"
    r"|[\u2800-\u28ff]\s*\S.*\(connecting\)\s*"
    r"|connecting to mcp servers[\s.…·()0-9/]*"
    r")\s*$",
    re.IGNORECASE,
)

# --- Status footer -------------------------------------------------------
#
# A footer row is recognised by *segments*, not by a substring search. Each
# segment below is a measured field of the 0.43.1 status line:
#
#   `A0 Gemini 2.5 Flash thinking  <dir>  master [±]      ctrl-o to hide …`
#   `yolo  agent (Kimi-k2.6 ●)  /tmp/x`
#   `                    context: 2% (14.8k/977k)`
#
# A row is footer chrome when it is *exactly* one segment (see
# `FOOTER_WHOLE_ROW_RES`) or when it carries **two or more** of them. One
# incidental mention is not enough, which is what keeps
# "The reported context: 50% is expected." and
# "Use ctrl-o to hide or reveal tool output if needed." in the answer.
FOOTER_SEGMENT_RES: Tuple[re.Pattern, ...] = (
    # context-usage indicator
    re.compile(r"context:\s*\d+(?:\.\d+)?%"),
    # agent/model segment: `agent (Kimi-k2.6 ●)`
    re.compile(r"agent\s*\([^)●]{0,80}●\)"),
    # git branch segment: `master [±]`
    re.compile(r"\[[±+\-]\]"),
    # rotating footer tips, drawn in colour 242 on the status row
    re.compile(r"ctrl-o to hide or reveal tool output"),
    re.compile(r"shift-tab to Plan mode"),
    re.compile(r"/goal for multi-step"),
    # approval-mode token at row start: `yolo  …` / `Ask When Needed  …`
    re.compile(r"^\s*(?:yolo|Ask When Needed|Never Ask)\b"),
)

# Segments that constitute a footer row on their own.
FOOTER_WHOLE_ROW_RES: Tuple[re.Pattern, ...] = (
    # Context indicator at the start of the row. Deliberately not end-anchored:
    # a narrow terminal wraps the tail, so the real row is
    # `                    context: 0.0% (0/262.1k` followed by `)` on the next
    # row. Row-start anchoring is what keeps prose that merely mentions the
    # phrase — "The reported context: 50% is expected." — out of this rule.
    re.compile(r"^\s*context:\s*\d+(?:\.\d+)?%"),
    re.compile(r"^\s*agent\s*\([^)●]{0,80}●\)\s*$"),
    re.compile(
        r"^\s*(?:ctrl-o to hide or reveal tool output"
        r"|shift-tab to Plan mode"
        r"|/goal for multi-step)\s*$"
    ),
    # Legacy status bar: `HH:MM  [yolo]  agent (model, thinking)  ctrl-x: …`.
    # Two measured structural tokens — the time column and the agent segment —
    # are required together, so a prose line that merely starts with a clock
    # time is not swept up.
    re.compile(r"^\s*\d+:\d+\s.*(?:agent|shell)\s*\("),
)

# Composer frames.
NEW_TUI_INPUT_RULE_RE = re.compile(r"^\s*─{2,}\s*input\s*─{2,}")
# The composer's prompt row ("│ > …"). A `│`-leading row that is *not* the
# prompt row and not bare frame is ordinary content — most importantly a
# Markdown table row (`│ a │ b │`), which a bare "starts with a box glyph"
# test would misread as ready chrome and which would then be filtered out of
# the extracted answer. See `is_composer_row` for the full rule.
COMPOSER_PROMPT_RE = re.compile(r"^\s*[│|]\s*>")
_COMPOSER_FRAME_CHARS = "─╭╮╰╯│| \t"

# User input echo. Kimi Code renders the submitted user message bold + colour
# 222 with a leading sparkle; the legacy TUI uses the same sparkle inline.
USER_INPUT_SPARKLE_RE = re.compile(r"[✨💫][^\S\n]+\S")
USER_INPUT_STYLE_RE = re.compile(r"\x1b\[1m\x1b\[38;5;222m|\x1b\[38;5;222m")

#: The measured foreground index Kimi Code draws submitted user input in. A
#: *wrapped* user message repeats this colour on its continuation rows without
#: repeating the sparkle, which is the only signal those rows carry.
USER_INPUT_COLOR_INDEX = 222

# Dimmed "… (N more lines, ctrl+o to expand)" tool-output collapse row.
COLLAPSED_TOOL_OUTPUT_RE = re.compile(r"…\s*\(\d+ more lines")

# Kimi Code's inline key hints that sit under a running tool call. Matched as
# the exact observed strings, not as a loose `^Press ` prefix: an assistant
# answer can legitimately begin "Press Ctrl+C to stop…", and silently deleting
# that from an extracted reply is worse than leaving one chrome row in.
TOOL_HINT_RE = re.compile(r"^\s*Press (?:Ctrl\+B to run in background|Esc to interrupt)\s*$")

# A bare full-width rule (`────…`), used by Kimi Code to bracket the approval
# dialog. Box-drawing glyphs only — a Markdown `---` rule is ASCII and stays
# content.
RULE_RE = re.compile(r"^\s*[─━═]{3,}\s*$")

# Kimi Code tool-execution header. Shares the colour-253 bullet with a final
# answer, so the escape-free form is separated by *shape*, and the styled form
# by the bold+colour-111 tool-name style, never by the bullet alone.
#
# Two measured families:
#
#   in-flight   `● Using find_profiles · MCP/cao-mcp-server`
#   completed   `● Used find_profiles · MCP/cao-mcp-server (kimi)`
#   built-in    `● Running a command · $ uname -a`
#               `● Used Read (ANSWER_SPEC.md) · 10 lines`
#
# The identifier is matched with **any** case. Capitalisation is deliberately
# not the discriminator: CAO's own MCP tools are snake_case (`find_profiles`,
# `send_message`, `memory_recall`, …), so a capitalised-identifier rule let
# every CAO MCP tool row through as answer text — the D6 production defect.
# What separates a tool row from prose is the measured *structural suffix* the
# renderer appends: a `·` detail separator (which for MCP tools is the
# `· MCP/<server>` target), a parenthesised argument list, or nothing else on
# the row. `● Used widely in production.` carries none of those and stays
# content.
_TOOL_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]*"
_TOOL_ROW_SUFFIX = r"(?=\s*[·(]|\s*$)"

# The tool name in the styled form, bold + colour 111. Both SGR orders are
# measured (`ESC[1mESC[38;5;111m` in the A0 captures, `ESC[38;5;111mESC[1m` in
# the 0.43.1 live capture), so both are accepted.
_TOOL_NAME_STYLE = r"(?:\x1b\[1m\x1b\[38;5;111m|\x1b\[38;5;111m\x1b\[1m)"

TOOL_CALL_RE = re.compile(
    r"^\s*(?:\x1b\[[0-9;]*m)*[•●]?[^\S\n]*"
    + _TOOL_NAME_STYLE
    + r"(?:Running a command|Calling|Using|Used|Read|Write|Edit|Search|Fetch)"
)
# The escape-free form of the same row. Every branch is anchored to a measured
# structural suffix for the identifier-carrying verbs.
TOOL_CALL_CLEAN_RE = re.compile(
    r"^\s*[•●]\s*(?:"
    r"Running a command"
    r"|Calling "
    r"|(?:Used|Using)\s+" + _TOOL_IDENTIFIER + _TOOL_ROW_SUFFIX + r")"
)

# ---------------------------------------------------------------------------
# Interactive dialogs
# ---------------------------------------------------------------------------

TRUST_TITLE_RE = re.compile(r"^\s*Trust this folder\?\s*$")
TRUST_HINT_RE = re.compile(r"↑↓\s*navigate\s*·\s*Enter\s*select\s*·\s*Esc\s*exit")
TRUST_OPTION_TRUST = "Trust this folder"
TRUST_OPTION_REJECT = "Don't trust"
TRUST_OPTIONS: Tuple[str, ...] = (TRUST_OPTION_TRUST, TRUST_OPTION_REJECT)
# The trust dialog's selection marker is U+276F. Deliberately distinct from the
# approval dialog's U+25B6 so one dialog's navigation can never be applied to
# the other.
TRUST_SELECT_MARKER = "❯"
# The trust dialog prints the workspace it is asking about on its own row, in
# colour 255. Matched structurally (a lone path-shaped token) rather than by an
# absolute-path prefix: the A0 scrubbed fixtures carry the placeholder
# `<A0DIR>/project`, and a real capture may equally be a container-translated
# guest path. The caller still compares the captured value against the actual
# pane working directory before acting, so a loose match here cannot cause a
# trust decision for the wrong folder.
TRUST_PATH_TOKEN_RE = re.compile(r"^\S*/\S*$")

APPROVAL_TITLE_RE = re.compile(r"▶\s*(?:Run this command\?|Approve\s|Allow\s)")
APPROVAL_HINT_RE = re.compile(r"↑/↓\s*select\s*·\s*1/2/3/4\s*choose")
APPROVAL_SELECT_MARKER = "▶"


class KimiLineKind(enum.Enum):
    """Semantic kind of a single transcript row.

    ``CONTENT`` is the catch-all for assistant prose that is not itself a
    bullet (continuation lines of a multi-line answer, code blocks, tables).
    """

    BLANK = "blank"
    USER_INPUT = "user_input"
    FINAL_BULLET = "final_bullet"
    THINKING_BULLET = "thinking_bullet"
    TOOL_CALL = "tool_call"
    TOOL_CHROME = "tool_chrome"
    RULE = "rule"
    LIVE_SPINNER = "live_spinner"
    IDLE_TIP = "idle_tip"
    READY_INPUT_FRAME = "ready_input_frame"
    STATUS_FOOTER = "status_footer"
    BOOT_CHROME = "boot_chrome"
    TRUST_DIALOG = "trust_dialog"
    APPROVAL_DIALOG = "approval_dialog"
    CONTENT = "content"


# Kinds that carry assistant-visible answer text. A row classified as anything
# else is chrome, reasoning, execution detail, or user echo and must never reach
# the caller as the agent's final message.
#
# TOOL_CALL and TOOL_CHROME are deliberately excluded. They share the final
# answer's `●` glyph (fixtures/08 renders `ESC[38;5;253m● Running a command ·
# $ uname -a`), so a glyph-only extractor folds tool-execution headers and
# "… (3 more lines, ctrl+o to expand)" collapse rows into the extracted answer.
# Handing that to a handoff/assign caller or to the memory layer presents
# execution plumbing as the agent's message.
ANSWER_KINDS = frozenset({KimiLineKind.FINAL_BULLET, KimiLineKind.CONTENT})

# Kinds that mean "the terminal is showing chrome, not a settled answer".
CHROME_KINDS = frozenset(
    {
        KimiLineKind.STATUS_FOOTER,
        KimiLineKind.READY_INPUT_FRAME,
        KimiLineKind.BOOT_CHROME,
        KimiLineKind.TRUST_DIALOG,
        KimiLineKind.APPROVAL_DIALOG,
        KimiLineKind.IDLE_TIP,
        KimiLineKind.TOOL_CALL,
        KimiLineKind.TOOL_CHROME,
        KimiLineKind.RULE,
    }
)

# Kinds that positively end a tool-output block (see `classify_rows`). Every
# entry is a row shape that cannot be tool payload: it either opens something
# new (a user echo, a fresh tool call is handled at the call site), or it is
# assistant output, or it is chrome that only renders once the tool has
# finished. A `CONTENT` or `BLANK` row is deliberately absent — those are
# exactly the shapes a payload takes.
TOOL_BLOCK_END_KINDS = frozenset(
    {
        KimiLineKind.FINAL_BULLET,
        KimiLineKind.THINKING_BULLET,
        KimiLineKind.USER_INPUT,
        KimiLineKind.READY_INPUT_FRAME,
        KimiLineKind.STATUS_FOOTER,
        KimiLineKind.LIVE_SPINNER,
        KimiLineKind.IDLE_TIP,
        KimiLineKind.TRUST_DIALOG,
        KimiLineKind.APPROVAL_DIALOG,
        KimiLineKind.BOOT_CHROME,
        KimiLineKind.RULE,
    }
)


def strip_sgr(line: str) -> str:
    """Remove SGR colour/style sequences, leaving structure intact."""

    return _SGR_RE.sub("", line)


def foreground_color_indices(raw_line: str) -> Set[int]:
    """The 256-colour foreground indices a row is drawn in.

    Parses each SGR parameter list rather than matching literal escape strings,
    because the renderer emits the same colour in more than one form: split
    (``ESC[1mESC[38;5;222m``) and combined (``ESC[1;38;5;222m``). A literal
    pattern for one form silently misses the other — the D6 production defect,
    where the wrapped user-message rows used the combined form and were read as
    answer content.

    Background (``48``) and 24-bit (``38;2;r;g;b``) parameters are skipped, so a
    background fill or a truecolor theme colour is never mistaken for a
    foreground index.
    """

    indices: Set[int] = set()
    for match in _SGR_PARAMS_RE.finditer(raw_line or ""):
        params = [part for part in match.group(1).split(";") if part != ""]
        index = 0
        while index < len(params):
            try:
                value = int(params[index])
            except ValueError:
                break
            if value in (38, 48) and index + 1 < len(params):
                mode = params[index + 1]
                if mode == "5" and index + 2 < len(params):
                    if value == 38:
                        try:
                            indices.add(int(params[index + 2]))
                        except ValueError:
                            pass
                    index += 3
                    continue
                if mode == "2" and index + 4 < len(params):
                    index += 5
                    continue
                index += 2
                continue
            index += 1
    return indices


class SpinnerSemantics(enum.Enum):
    """Which glyphs count as live work, per dialect.

    A0 measured the split: the legacy ``kimi-cli`` TUI animates a bare moon
    phase while working, while Kimi Code animates a braille indicator and only
    rotates moon phases through its *idle* tip row. Collapsing the two into one
    rule either reads a settled Kimi Code terminal as PROCESSING or drops the
    legacy signal entirely, so the dialect is an explicit argument.

    ``LEGACY`` is the default so any caller that has not resolved a dialect
    keeps the historical behaviour.
    """

    LEGACY = "legacy"
    CODE = "code"


def has_live_spinner_glyph(line: str) -> bool:
    """True when ``line`` carries a braille working indicator."""

    return bool(_BRAILLE_RE.search(line))


def is_boot_chrome_line(clean_line: str, raw_line: str = "") -> bool:
    """True when the row is Kimi's startup / MCP boot chrome.

    Structural only — see the block comment above the patterns. A row qualifies
    because it is *shaped* like boot chrome, not because it mentions boot
    vocabulary, so assistant prose that quotes the banner or the MCP progress
    line is not chrome and cannot truncate an answer.
    """

    if WELCOME_BOX_FRAME_RE.search(raw_line) or WELCOME_BANNER_STYLED_RE.search(raw_line):
        return True
    if WELCOME_BOX_BANNER_RE.match(clean_line) or WELCOME_BANNER_RE.match(clean_line):
        return True
    return bool(BOOT_MESSAGE_ROW_RE.match(clean_line) or MCP_BOOT_ROW_RE.match(clean_line))


def is_status_footer_line(clean_line: str) -> bool:
    """True when the row is TUI status/footer chrome rather than content.

    A footer row either *is* one measured segment, or carries two or more of
    them. One incidental mention inside a sentence is not a footer — that is
    what keeps "The reported context: 50% is expected." in the answer.
    """

    if any(pattern.match(clean_line) for pattern in FOOTER_WHOLE_ROW_RES):
        return True
    return sum(1 for pattern in FOOTER_SEGMENT_RES if pattern.search(clean_line)) >= 2


def is_idle_tip_line(clean_line: str, raw_line: str = "") -> bool:
    """True for Kimi Code's rotating idle tip row (``🌕 · Tip: …``).

    The tip row is drawn where the working indicator would be, so a glyph-only
    test reads a settled terminal as PROCESSING — A0's D1 defect, reproduced by
    fixtures 05 and 09. The row is positively identified by a moon-phase glyph
    *plus* the ``· Tip:`` suffix and the absence of any braille indicator.
    """

    if not _MOON_RE.search(clean_line):
        return False
    if _BRAILLE_RE.search(clean_line) or _BRAILLE_RE.search(raw_line):
        return False
    return bool(_TIP_RE.search(clean_line))


def is_live_spinner_line(
    clean_line: str,
    raw_line: str = "",
    semantics: SpinnerSemantics = SpinnerSemantics.LEGACY,
) -> bool:
    """True when ``clean_line`` is a live turn-in-flight working indicator.

    Positive evidence only:

    * a braille glyph (the measured 0.43.1 indicator: ``⠙ working…``), and
    * the row is not boot chrome (``⠧ MCP Servers: 0/1 connected`` is drawn while
      the terminal is idle at the welcome screen), and
    * the row is not the idle rotating tip.

    Moon phases are the dialect split (A3-4). A0 measured that a bare moon is
    the *legacy* processing glyph, while Kimi Code animates a braille indicator
    and only rotates moons through its idle tip row. Under
    :attr:`SpinnerSemantics.CODE` a moon is therefore not evidence of work — an
    answer that contains a standalone ``🌕`` line must not read as PROCESSING.
    Under :attr:`SpinnerSemantics.LEGACY` the historical bare-moon support is
    unchanged: that shape is ambiguous on the legacy TUI and treating it as work
    is the fail-safe direction, because a missed PROCESSING stalls a turn while
    a false one is corrected by the dispatch-grace and rendered-pane
    confirmation in ``get_status``.
    """

    if is_idle_tip_line(clean_line, raw_line):
        return False
    if is_boot_chrome_line(clean_line, raw_line):
        return False
    if _BRAILLE_RE.search(clean_line):
        return True
    if _MOON_RE.search(clean_line):
        return semantics is SpinnerSemantics.LEGACY
    return False


def is_thinking_styled(raw_line: str) -> bool:
    """True when the raw (ANSI-preserved) row is styled as reasoning.

    Evidence-based, not "any colour": a truecolor bullet counts as reasoning
    only when its triple is grey/near-grey, and the final-answer colour is
    decisive in the other direction. A themed or emphasised final answer must
    never be suppressed as reasoning merely because it is drawn in 24-bit
    colour.

    The asymmetry is deliberate. Both of Kimi's own styles are greyscale
    (reasoning 244, answer 253), so channel spread alone cannot separate them —
    that is what :data:`FINAL_ANSWER_BULLET_STYLE_RE` is for. For a truecolor
    row we therefore accept *any* near-grey as reasoning, including a bright
    one. That errs toward reasoning, which is the safe direction: a misread
    answer bullet raises :class:`OutputExtractionError` and the caller sees a
    failure, whereas a misread reasoning bullet would silently publish private
    reasoning as the agent's message.
    """

    if FINAL_ANSWER_BULLET_STYLE_RE.search(raw_line):
        return False
    if any(pattern.search(raw_line) for pattern in THINKING_STYLE_PATTERNS):
        return True
    for match in TRUECOLOR_BULLET_RE.finditer(raw_line):
        red, green, blue = (int(match.group(index)) for index in (1, 2, 3))
        if max(red, green, blue) - min(red, green, blue) <= TRUECOLOR_GREY_TOLERANCE:
            return True
    return False


def is_composer_row(clean_line: str) -> bool:
    """True when ``clean_line`` is part of an input composer, not content.

    Deliberately strict. The composer is recognisable by structure, and the
    structure has to be narrow because both dialects use box-drawing glyphs:

    * the ``── input ──`` rule (intermediate Kimi Code builds),
    * a bare ``╭``/``╰`` frame row,
    * a ``│`` row whose inner text is empty or begins with ``>`` (the prompt).

    A Markdown table row (``│ a │ b │``) has non-empty inner text that does not
    begin with ``>``, so it stays content. Getting this wrong silently deletes
    table rows from an extracted answer.
    """

    if NEW_TUI_INPUT_RULE_RE.match(clean_line):
        return True
    stripped = clean_line.strip()
    if not stripped:
        return False
    if stripped[0] in "╭╰":
        return True
    if stripped[0] in "│|":
        if COMPOSER_PROMPT_RE.match(clean_line):
            return True
        inner = stripped.strip("│|").strip()
        return inner == "" or inner.strip(_COMPOSER_FRAME_CHARS) == ""
    return False


def is_response_marker_line(clean_line: str) -> bool:
    """True when ``clean_line`` starts a response/thinking bullet *with a payload*.

    The trailing horizontal whitespace is what separates a response marker from
    a wrapped footer fragment. A narrow terminal wraps the status bar so a row
    can begin with a bare ``●`` or ``•`` followed by punctuation (``●)``). The
    PR #664 defect was matching those as assistant output, which latched
    "input received" on an idle terminal and reported it COMPLETED.

    Required behaviour, asserted by test:

    ===============  ================
    row              response marker?
    ===============  ================
    ``● answer``     yes
    ``• answer``     yes
    ``●)``           no
    ``•)``           no
    ===============  ================
    """

    return bool(BULLET_ANY_RE.match(clean_line))


def has_response_marker(text: str) -> bool:
    """True when any row of ``text`` starts a response/thinking bullet."""

    return any(is_response_marker_line(line) for line in (text or "").split("\n"))


def is_user_input_echo(raw_line: str, clean_line: Optional[str] = None) -> bool:
    """True when the row is the echo of a *submitted user message*.

    Two measured shapes, deliberately decided by one predicate so that the
    region locator and the extractor cannot disagree about where the user's
    message ends:

    * the first submitted row — sparkle-prefixed, drawn bold + colour 222;
    * a **wrapped continuation** row — no sparkle, carrying the same colour-222
      foreground (``ESC[1;38;5;222m``). A long submitted message wraps, and the
      continuation rows carry no glyph of their own. Treating only the
      sparkle row as the echo left the continuation inside the response region,
      where it was published as the agent's answer (D6 production defect).

    Prose is not swept up: a row must carry the colour-222 foreground or the
    sparkle, and a response bullet is rejected outright so an answer that
    quotes a sparkle cannot be read as a submission.
    """

    raw = raw_line or ""
    clean = strip_sgr(raw) if clean_line is None else clean_line

    # A response bullet is assistant output, never user echo — checked first so
    # a styled answer can never be reclassified as a submission.
    if is_response_marker_line(clean):
        return False

    if USER_INPUT_COLOR_INDEX in foreground_color_indices(raw):
        return True

    return bool(
        USER_INPUT_SPARKLE_RE.search(clean)
        and (USER_INPUT_STYLE_RE.search(raw) or clean.lstrip().startswith(("✨", "💫")))
    )


def is_tool_call_row(raw_line: str, clean_line: Optional[str] = None) -> bool:
    """True when the row is a tool-execution header (any identifier case).

    Structural only: the styled form is recognised by the tool-name style, the
    escape-free form by the measured suffix a tool row carries. See
    :data:`TOOL_CALL_RE` for why capitalisation is not the discriminator.
    """

    raw = raw_line or ""
    clean = strip_sgr(raw) if clean_line is None else clean_line
    return bool(TOOL_CALL_RE.search(raw) or TOOL_CALL_CLEAN_RE.search(clean))


def classify_line(
    raw_line: str,
    clean_line: Optional[str] = None,
    semantics: SpinnerSemantics = SpinnerSemantics.LEGACY,
) -> KimiLineKind:
    """Classify one transcript row.

    ``clean_line`` may be supplied by a caller that already stripped SGR;
    otherwise it is derived from ``raw_line``. ``semantics`` selects the
    dialect's spinner rules (see :class:`SpinnerSemantics`).
    """

    raw = raw_line or ""
    clean = strip_sgr(raw) if clean_line is None else clean_line
    stripped = clean.strip()

    if not stripped:
        return KimiLineKind.BLANK

    # --- dialogs first: their rows would otherwise be read as content ---
    if TRUST_TITLE_RE.match(clean) or TRUST_HINT_RE.search(clean):
        return KimiLineKind.TRUST_DIALOG
    if TRUST_SELECT_MARKER in clean:
        return KimiLineKind.TRUST_DIALOG
    if stripped in TRUST_OPTIONS or stripped.startswith("Project MCP targets:"):
        return KimiLineKind.TRUST_DIALOG
    if APPROVAL_TITLE_RE.search(clean) or APPROVAL_HINT_RE.search(clean):
        return KimiLineKind.APPROVAL_DIALOG
    if re.match(r"^\s*\d+\.\s*(?:Approve|Reject)\b", clean):
        return KimiLineKind.APPROVAL_DIALOG

    # --- live work indicators before generic chrome ---
    if is_live_spinner_line(clean, raw, semantics):
        return KimiLineKind.LIVE_SPINNER
    if is_idle_tip_line(clean, raw):
        return KimiLineKind.IDLE_TIP

    # --- chrome, structurally identified ---
    if is_boot_chrome_line(clean, raw):
        return KimiLineKind.BOOT_CHROME

    if is_status_footer_line(clean):
        return KimiLineKind.STATUS_FOOTER

    if is_composer_row(clean):
        return KimiLineKind.READY_INPUT_FRAME

    # Execution plumbing. Checked before the bullet branch: both rows below
    # can carry the final answer's `●`, and both must stay out of ANSWER_KINDS.
    if COLLAPSED_TOOL_OUTPUT_RE.search(clean):
        return KimiLineKind.TOOL_CHROME
    if TOOL_HINT_RE.match(clean):
        return KimiLineKind.TOOL_CHROME
    if RULE_RE.match(clean):
        return KimiLineKind.RULE
    if is_tool_call_row(raw, clean):
        return KimiLineKind.TOOL_CALL

    # --- assistant output ---
    if is_response_marker_line(clean):
        return (
            KimiLineKind.THINKING_BULLET if is_thinking_styled(raw) else KimiLineKind.FINAL_BULLET
        )

    # --- user echo (checked after bullets so a quoted sparkle inside an answer
    #     cannot be mistaken for a submission) ---
    if is_user_input_echo(raw, clean):
        return KimiLineKind.USER_INPUT

    return KimiLineKind.CONTENT


def classify_rows(
    raw_lines: Sequence[str],
    clean_lines: Optional[Sequence[str]] = None,
    semantics: SpinnerSemantics = SpinnerSemantics.LEGACY,
) -> List[KimiLineKind]:
    """Classify a whole transcript, in sequence.

    Row-by-row classification cannot answer the question the extractor actually
    asks — "is this JSON row the payload of the tool call above it, or the
    agent's answer?" — because neither a tool payload row nor a wrapped user
    message carries a marker of its own (Kimi Code draws the former dim and
    indented, exactly like an indented prose continuation, and the latter as
    plain text). This is the D6 production defect: the user message's second
    line, the tool header and the tool payload were all published as the answer.

    Two blocks are tracked, and both are asymmetric in the same way — each can
    only become active on a **positively identified** row, never on a heuristic
    about layout:

    **User echo (D6-F1).** Opens on a :data:`KimiLineKind.USER_INPUT` row. A
    submitted message that wraps renders its continuation rows as ordinary
    prose: the first row carries the sparkle and colour 222, and the wrapped
    rows either repeat the colour *or* carry no styling at all (the
    colour-free shape is what the escape-stripped consumers see). Immediately
    following ``CONTENT`` rows are therefore part of the same submission, until
    a blank row or any structured row ends it. A blank row always ends it, and
    an answer bullet is structured, so a real answer is never absorbed.

    **Tool output (D6-F3).** Opens on a :data:`KimiLineKind.TOOL_CALL` row.
    Inside it, rows that would otherwise be ``CONTENT`` become
    :data:`KimiLineKind.TOOL_CHROME`, and blank rows are kept as blanks without
    ending it (payloads are often blank-line separated). It ends at the first
    positive boundary — an answer bullet, reasoning, a new user echo, the
    composer, the footer, a spinner/tip, a dialog, boot chrome, or a rule.

    Because the two are mutually exclusive and both end on structured rows,
    exactly one semantic source of truth exists for each; the extractor
    consumes the result rather than re-deriving context per row.

    ``clean_lines`` may be supplied by a caller that already stripped SGR.
    """

    kinds: List[KimiLineKind] = []
    in_tool_block = False
    in_user_echo = False

    for index, raw in enumerate(raw_lines):
        if clean_lines is not None and index < len(clean_lines):
            clean = clean_lines[index]
        else:
            clean = strip_sgr(raw)

        kind = classify_line(raw, clean, semantics)

        # --- user echo: opens on a positive echo row, absorbs wrapped prose ---
        if kind is KimiLineKind.USER_INPUT:
            in_user_echo = True
            in_tool_block = False
            kinds.append(kind)
            continue
        if in_user_echo:
            if kind is KimiLineKind.CONTENT and clean.strip():
                kinds.append(KimiLineKind.USER_INPUT)
                continue
            in_user_echo = False

        # --- tool output: opens on a positive tool header ---
        if kind is KimiLineKind.TOOL_CALL:
            in_tool_block = True
            kinds.append(kind)
            continue
        if not in_tool_block:
            kinds.append(kind)
            continue
        if kind in TOOL_BLOCK_END_KINDS:
            in_tool_block = False
            kinds.append(kind)
            continue
        # Inside a block: blank rows continue it, everything else is payload.
        kinds.append(kind if kind is KimiLineKind.BLANK else KimiLineKind.TOOL_CHROME)

    return kinds


def classify_lines(
    script_output: str,
    semantics: SpinnerSemantics = SpinnerSemantics.LEGACY,
) -> List[Tuple[str, str, KimiLineKind]]:
    """Classify every row of ``script_output``, in sequence.

    Returns ``(raw_line, clean_line, kind)`` triples so callers can filter by
    kind and still emit the original text. Kinds are contextual — see
    :func:`classify_rows`, which this delegates to so there is exactly one
    tool-block state machine in the codebase.
    """

    raw_lines = (script_output or "").split("\n")
    clean_lines = [strip_sgr(raw) for raw in raw_lines]
    kinds = classify_rows(raw_lines, clean_lines, semantics)
    return [(raw, clean, kind) for raw, clean, kind in zip(raw_lines, clean_lines, kinds)]


# ---------------------------------------------------------------------------
# Dialog detection helpers
# ---------------------------------------------------------------------------


class TrustDialog:
    """A positively-identified workspace-trust dialog."""

    __slots__ = ("workspace", "options", "selected_index", "selected_option")

    def __init__(
        self,
        workspace: Optional[str],
        options: Sequence[str],
        selected_index: Optional[int],
    ) -> None:
        self.workspace = workspace
        self.options = list(options)
        self.selected_index = selected_index
        self.selected_option = (
            self.options[selected_index]
            if selected_index is not None and 0 <= selected_index < len(self.options)
            else None
        )


def detect_trust_dialog(rows: Sequence[str]) -> Optional[TrustDialog]:
    """Return the trust dialog described by ``rows``, or None.

    Requires the whole structure — title, navigation hint, and a recognised
    option set — so a transcript that merely quotes the dialog text is not
    mistaken for the live dialog. Returns a ``TrustDialog`` with
    ``selected_index=None`` when the dialog is present but no selection marker
    could be read; callers must treat that as "present but undecidable" and
    fail closed rather than guessing.
    """

    title_seen = False
    hint_seen = False
    options: List[str] = []
    selected_index: Optional[int] = None
    workspace: Optional[str] = None

    for raw in rows:
        clean = strip_sgr(raw)
        if TRUST_TITLE_RE.match(clean):
            title_seen = True
            continue
        if TRUST_HINT_RE.search(clean):
            hint_seen = True
            continue

        stripped = clean.strip()
        # Option rows: optional `❯ ` marker, then an exact known label.
        has_marker = stripped.startswith(TRUST_SELECT_MARKER)
        body = stripped[1:].strip() if has_marker else stripped
        if body in TRUST_OPTIONS:
            if body not in options:
                options.append(body)
            if has_marker and selected_index is None:
                selected_index = options.index(body)
            continue

        # Workspace row: a lone path-shaped token. The dialog draws it between
        # the navigation hint and the option list, so it is matched anywhere
        # rather than only before the title.
        if (
            workspace is None
            and stripped
            and " " not in stripped
            and TRUST_PATH_TOKEN_RE.match(stripped)
            and not stripped.startswith(
                (TRUST_SELECT_MARKER, APPROVAL_SELECT_MARKER, "↑", "↓", "·")
            )
        ):
            workspace = stripped

    if not (title_seen and hint_seen and options):
        return None
    return TrustDialog(workspace, options, selected_index)
