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

# The live working indicator is a braille glyph *at the indicator position*: the
# row's own prefix, where the renderer draws the spinner slot. Membership of the
# Braille block anywhere on a row is not evidence — an answer that mentions the
# character ("● The Braille letter A is ⠁.") or quotes it in a code block would
# otherwise be classified as a live turn, which both drops the row out of the
# answer and can pin the terminal at PROCESSING.
_SPINNER_PREFIX_RE = re.compile(r"^\s*[\u2800-\u28ff]")

# The reasoning block's continuation styling. Kimi draws reasoning in grey 244
# and italicises it; a wrapped reasoning line repeats that styling without
# repeating the bullet. This is the positive evidence that lets a reasoning
# block absorb its own continuation rows without also suppressing arbitrary
# unstyled prose that follows a thinking bullet.
_REASONING_CONTINUATION_RE = re.compile(r"\x1b\[3m|\x1b\[38;5;244m")

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
)

#: The reasoning foreground (grey 244). A bullet-then-italic row is reasoning
#: only when the row carries this colour somewhere: the measured shape is
#: `ESC[38;5;244m● ESC[3m…` (already covered above) or the colour carried on the
#: *text* instead of the bullet. Italic alone is not evidence — a legacy answer
#: that happens to be italic is the same shape, and refusing it would turn a good
#: answer into a terminal extraction failure.
REASONING_GREY_RE = re.compile(r"\x1b\[38;5;244m")
#: `•` immediately followed by italic, with no colour on the bullet itself.
BULLET_THEN_ITALIC_RE = re.compile(r"[•●][^\S\n]*\x1b\[3m")

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
#
# The rotating tips are *corroborating* evidence, not independent segments. A
# real status row carries one measured field (the git branch, the agent segment,
# the context indicator) plus a tip; an ordinary answer can mention the tips
# themselves, and "Use ctrl-o to hide or reveal tool output and shift-tab to Plan
# mode." matched two of them, classified as STATUS_FOOTER and truncated the rest
# of the answer. When the row is styled, the tip must carry the footer's own
# colour (242), which an answer does not; an escape-free row has no colour to
# lean on, so a measured field is required there instead.
FOOTER_SEGMENT_RES: Tuple[re.Pattern, ...] = (
    # context-usage indicator
    re.compile(r"context:\s*\d+(?:\.\d+)?%"),
    # agent/model segment: `agent (Kimi-k2.6 ●)`
    re.compile(r"agent\s*\([^)●]{0,80}●\)"),
    # git branch segment: `master [±]`
    re.compile(r"\[[±+\-]\]"),
    # approval-mode token at row start: `yolo  …` / `Ask When Needed  …`
    re.compile(r"^\s*(?:yolo|Ask When Needed|Never Ask)\b"),
)

#: The rotating tips, as the status row draws them.
FOOTER_TIP_RE = re.compile(
    r"ctrl-o to hide or reveal tool output|shift-tab to Plan mode|/goal for multi-step"
)
#: The same tips carrying the footer's own foreground colour.
FOOTER_TIP_STYLE_RE = re.compile(
    r"\x1b\[38;5;242m[^\x1b]*(?:ctrl-o to hide or reveal tool output"
    r"|shift-tab to Plan mode|/goal for multi-step)"
)

# Segments that constitute a footer row on their own.
FOOTER_WHOLE_ROW_RES: Tuple[re.Pattern, ...] = (
    # Context indicator at the start of the row, in the measured shape: the
    # percentage is followed by the parenthesised used/total counts. Requiring
    # that suffix is what keeps ordinary prose that merely *states* a metric —
    # "context: 50% means half the budget is used." — in the answer. The counts
    # are the renderer's own field, so a sentence does not carry them by accident.
    # Deliberately not end-anchored: a narrow terminal wraps the tail, so the
    # real row is `                    context: 0.0% (0/262.1k` followed by `)`
    # on the next row. Row-start anchoring plus the measured suffix is what keeps
    # a metric *mention* out of this rule.
    re.compile(r"^\s*context:\s*\d+(?:\.\d+)?%\s*\("),
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

# The legacy TUI's idle prompt: a sparkle on a row of its own (`✨` / `💫`),
# with nothing after it. The provider's `IDLE_PROMPT_PATTERN` has always treated
# the sparkle as the idle-prompt marker, but the classifier had no row kind for
# the bare form, so the terminal's own prompt row fell through to CONTENT and
# was published as the last line of the extracted answer. Anchored to the whole
# row, so an answer that merely mentions the glyph is untouched. Kimi Code
# replaced this composer with a boxed frame, and its moons carry different
# semantics, so the rule is scoped to the legacy dialect by
# `is_legacy_idle_prompt_line`.
LEGACY_IDLE_PROMPT_RE = re.compile(r"^\s*[✨💫]\s*$")

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

# Kimi Code tool-execution header. The measured shape is a bullet plus a
# renderer-drawn tool name, so the *styled* form is identified by the
# bold + colour-111 tool-name style and the *escape-free* form by the measured
# detail separator the renderer appends.
#
# Measured headers (A0 captures + the live 0.43.1 source-side turn):
#
#   in-flight   `● Using find_profiles · MCP/cao-mcp-server`
#   completed   `● Used find_profiles · MCP/cao-mcp-server (kimi)`
#   built-in    `● Running a command · $ uname -a`
#               `● Used Read (ANSWER_SPEC.md) · 10 lines`
#   live 0.43.1 `● Used <bold111>find_profiles</bold> · MCP/cao-mcp-server (kimi)`
#
# Every measured row carries the `·` detail separator. That is the positive
# renderer structure, and it is the only thing that separates a tool header from
# a sentence. An English verb plus an identifier plus an opening parenthesis is
# NOT evidence: `● Calling retry() twice is safe.`,
# `● Calling connect (with TLS) encrypts the connection.` and
# `● Using Python (3.12) is recommended.` are ordinary answer prose, and a
# misread header opens a tool block whose continuation rows are then suppressed
# as TOOL_CHROME — destroying the answer, and at the public `mode=LAST` boundary
# degrading to the raw-transcript fallback.
#
# The identifier is matched with **any** case, and the tool-name character set
# includes `-` and `.` because real MCP tool names use them (`search-docs`,
# `docs.search`). Capitalisation is deliberately not the discriminator: CAO's own
# MCP tools are snake_case (`find_profiles`, `send_message`, `memory_recall`, …),
# so a capitalised-identifier rule let every CAO MCP tool row through as answer
# text — the D6 production defect.
_TOOL_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_.-]*"
#: The measured detail separator: a `·` with horizontal whitespace on both sides.
_TOOL_DETAIL_SEP = r"[^\S\n]·[^\S\n]"
#: The measured detail *body* after that separator: a line count (`· 10 lines`)
#: or the MCP server that served the call (`· MCP/cao-mcp-server (kimi)`). The
#: separator alone is not evidence — prose writes middots too, and
#: `• Used Python · no external dependencies.` was reproduced as an answer this
#: rule refused.
_TOOL_DETAIL_BODY = r"(?:\d+ lines?(?![^\S\n]*\w)|MCP/)"
#: Optional parenthesised argument list, as in `Used Read (ANSWER_SPEC.md)`.
_TOOL_ARG_LIST = r"[^\S\n]*\([^)]*\)"

# The tool name in the styled form, bold + colour 111. Both SGR orders are
# measured (`ESC[1mESC[38;5;111m` in the A0 captures, `ESC[38;5;111mESC[1m` in
# the 0.43.1 live capture), so both are accepted.
_TOOL_NAME_STYLE = r"(?:\x1b\[1m\x1b\[38;5;111m|\x1b\[38;5;111m\x1b\[1m)"
_TOOL_VERBS = r"(?:Running a command|Calling|Using|Used|Read|Write|Edit|Search|Fetch)"

# The styled form: the renderer draws either the verb or the identifier in the
# tool-name style. Both measured orders are accepted.
TOOL_CALL_RE = re.compile(
    r"^\s*(?:\x1b\[[0-9;]*m)*[•●]?[^\S\n]*(?:"
    + _TOOL_NAME_STYLE
    + _TOOL_VERBS
    + r"|"
    + _TOOL_VERBS
    + r"[^\S\n]+"
    + _TOOL_NAME_STYLE
    + r")"
)
# The escape-free form. A sentence that merely *begins* with a tool verb is
# answer content, so a row is a tool row only when it carries the renderer's own
# structural evidence: the measured `·` detail separator.
#
# Neither the bare `<verb> <word>` shape nor a parenthesised argument list is
# enough on its own, because both are also how ordinary prose looks —
# `• Used Python` and `• Using Python (3.12)` were each reproduced as answers
# that this rule refused. Every measured completed tool row carries the `·`
# detail separator (`● Used <Tool> (<arg>) · <N> lines`, `● Used find_profiles ·
# MCP/cao-mcp-server`), so requiring it loses no measured shape while keeping
# prose out. A row still in flight is a braille spinner, which
# :func:`is_live_spinner_line` handles separately.
TOOL_CALL_CLEAN_RE = re.compile(
    r"^\s*[•●]\s*(?:"
    + r"Running a command"
    + _TOOL_DETAIL_SEP
    + r"|(?:Used|Using|Calling)[^\S\n]+"
    + _TOOL_IDENTIFIER
    + r"(?:"
    + _TOOL_ARG_LIST
    + r")?"
    + _TOOL_DETAIL_SEP
    + _TOOL_DETAIL_BODY
    + r")"
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
# colour 255 (measured on both A0 trust captures). That row is identified from
# the dialog's structure — its position between the navigation hint and the
# option list, its measured colour, and the fact that it is not one of the
# dialog's own chrome rows — rather than by "a whitespace-free token containing
# a slash". That earlier heuristic rejected every workspace path with a space in
# it (`/tmp/my project`), which made the dialog unanswerable, while
# simultaneously accepting any slash-bearing string elsewhere in the dialog
# body. The caller still compares the captured value against the actual pane
# working directory with an exact equality test before acting, so identifying
# the wrong row here cannot cause a trust decision for the wrong folder — it can
# only fail closed.
TRUST_WORKSPACE_COLOR_INDEX = 255
# The gated-MCP section header, drawn between the workspace row and the option
# list. It is dialog chrome, never the workspace.
TRUST_MCP_TARGETS_PREFIX = "Project MCP targets:"


def _trust_option_body(stripped: str) -> str:
    """The option label on a row, with any selection marker removed."""

    if stripped.startswith(TRUST_SELECT_MARKER):
        return stripped[len(TRUST_SELECT_MARKER) :].strip()
    return stripped


def is_trust_option_row(clean_line: str) -> bool:
    """True when the row is one of the trust dialog's option rows.

    Positive identification: an optional selection marker followed by one of
    the exact measured option labels. A row that merely *contains* the marker is
    not a dialog row — matching on containment classified a quoted marker in
    assistant prose as TRUST_DIALOG, and because TRUST_DIALOG ends a response
    region the remainder of the answer was silently dropped.
    """

    return _trust_option_body(clean_line.strip()) in TRUST_OPTIONS


def is_trust_workspace_candidate(clean_line: str) -> bool:
    """True when the row can be the workspace the dialog is asking about.

    Structurally: a non-empty row that names a path and is not one of the
    dialog's own chrome rows (title, navigation hint, option, gated-MCP header,
    or a bare rule). :func:`detect_trust_dialog` then uses the row's position
    and its measured colour to choose between candidates.

    Deliberately not restricted to whitespace-free tokens: a real workspace path
    may contain spaces, and rejecting those made the dialog unanswerable.
    """

    stripped = clean_line.strip()
    if not stripped or "/" not in stripped:
        return False
    if TRUST_TITLE_RE.match(clean_line) or TRUST_HINT_RE.search(clean_line):
        return False
    if is_trust_option_row(clean_line):
        return False
    if stripped.startswith(TRUST_MCP_TARGETS_PREFIX):
        return False
    if RULE_RE.match(clean_line):
        return False
    return True


# The approval dialog's own rows. The title is anchored to the row start so a
# sentence that merely *mentions* it ("The dialog says ▶ Run this command?
# before execution.") is not even a candidate — a substring test classified the
# mentioning sentence as the dialog and, because the dialog ends the response
# region, truncated the answer at that row.
APPROVAL_TITLE_RE = re.compile(r"^\s*▶\s*(?:Run this command\?|Approve\s|Allow\s)")
APPROVAL_HINT_RE = re.compile(r"↑/↓\s*select\s*·\s*1/2/3/4\s*choose")
# A numbered option row, with the optional selection marker the renderer draws.
APPROVAL_OPTION_RE = re.compile(r"^\s*(?:▶\s*)?\d+\.\s*(?:Approve|Reject)\b")
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

# Kinds that positively end a tool-output block (see `classify_rows`).
#
# Tool output is arbitrary content, so a payload row must not be able to certify
# that it has stopped being payload by happening to resemble TUI chrome. Only
# rows whose *structure* cannot be payload end the block, and the ones a payload
# can trivially forge are deliberately absent:
#
# * ``RULE`` — a payload can contain a `────` line (reproduced: it let private
#   payload into the answer);
# * ``BOOT_CHROME`` — payload can contain "connecting to mcp servers…";
# * ``FINAL_BULLET`` — a bare bullet is exactly what payload that starts with a
#   bullet looks like, so only the renderer's *answer colour* ends the block
#   (see `_ends_tool_block`);
# * ``CONTENT``/``BLANK`` — those are the shapes a payload takes.
TOOL_BLOCK_END_KINDS = frozenset(
    {
        KimiLineKind.USER_INPUT,
        KimiLineKind.READY_INPUT_FRAME,
        KimiLineKind.STATUS_FOOTER,
        KimiLineKind.LIVE_SPINNER,
        KimiLineKind.IDLE_TIP,
        KimiLineKind.TRUST_DIALOG,
        KimiLineKind.APPROVAL_DIALOG,
    }
)


#: Tool-block end kinds whose *plain text* a payload can trivially reproduce.
#: They end a block only when the row carries renderer styling, because an
#: escape-free capture cannot prove the row is chrome rather than payload.
#: Dialogs are deliberately absent: they are already confirmed against the whole
#: capture by :func:`_confirm_context_kinds`, which is stronger evidence than
#: styling.
_STYLE_REQUIRED_END_KINDS = frozenset(
    {
        KimiLineKind.STATUS_FOOTER,
        KimiLineKind.READY_INPUT_FRAME,
        KimiLineKind.LIVE_SPINNER,
        KimiLineKind.IDLE_TIP,
    }
)


def _ends_tool_block(raw_line: str, kind: KimiLineKind) -> bool:
    """True when a row inside a tool block is positively *not* tool payload.

    Tool output is arbitrary content, so a payload row must not be able to
    certify that it has stopped being payload by happening to resemble TUI
    chrome. Only renderer evidence that belongs outside payload ends the block:

    * a final-answer bullet drawn in the answer colour (colour 253);
    * a sequence-confirmed dialog;
    * a composer frame, status footer, spinner or idle tip **drawn with
      styling** — their plain-text shapes are exactly what a payload can
      contain (reproduced: an escape-free ``context: 99% (1/2)`` line and a
      ``────`` rule both let private payload into the answer).

    An *unstyled* response bullet is not evidence either: it is
    indistinguishable from payload that starts with a bullet, so an escape-free
    capture keeps the block open and fails closed rather than publishing payload
    as the answer.

    Reasoning and user submissions are handled by the caller, which has to
    update their own block state as well.
    """

    if kind is KimiLineKind.FINAL_BULLET:
        return bool(FINAL_ANSWER_BULLET_STYLE_RE.search(raw_line or ""))
    if kind not in TOOL_BLOCK_END_KINDS:
        return False
    if kind in _STYLE_REQUIRED_END_KINDS and not _SGR_RE.search(raw_line or ""):
        return False
    return True


def _confirm_context_kinds(
    raw_lines: Sequence[str],
    clean_lines: Sequence[str],
    kinds: Sequence[KimiLineKind],
) -> List[KimiLineKind]:
    """Downgrade context-sensitive UI kinds the surrounding rows do not confirm.

    A row whose *text* looks like part of a dialog or an approval prompt is not
    UI state; only the whole structure is. The trust dialog needs its title, its
    navigation hint and its option set (:func:`detect_trust_dialog`), and the
    approval dialog needs its navigation hint *and* a numbered option row.
    Without that context the row is ordinary answer content — and treating it as
    chrome silently ends the response region, dropping the rest of the answer.

    The approval confirmation deliberately does not accept the title alone: the
    same substring that classified the row would then confirm it, so a sentence
    that merely quotes the prompt would confirm itself. Reproduced collisions
    this closes: a quoted menu example (``● Menu example:`` /
    ``❯ Trust this folder`` / ``Continue…``), a fenced ``❯ Don't trust``, a
    numbered procedure (``1. Approve the plan.``), prose quoting
    ``▶ Run this command?``, and prose that merely starts with
    ``Project MCP targets:``.
    """

    confirmed = list(kinds)

    if detect_trust_dialog(raw_lines) is None:
        confirmed = [
            KimiLineKind.CONTENT if kind is KimiLineKind.TRUST_DIALOG else kind
            for kind in confirmed
        ]

    approval_confirmed = any(APPROVAL_HINT_RE.search(clean) for clean in clean_lines) and any(
        APPROVAL_OPTION_RE.match(clean) for clean in clean_lines
    )
    if not approval_confirmed:
        confirmed = [
            KimiLineKind.CONTENT if kind is KimiLineKind.APPROVAL_DIALOG else kind
            for kind in confirmed
        ]

    return confirmed


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


def is_status_footer_line(clean_line: str, raw_line: str = "") -> bool:
    """True when the row is TUI status/footer chrome rather than content.

    A footer row either *is* one measured field (``FOOTER_WHOLE_ROW_RES``) or
    carries two or more of them. One incidental mention inside a sentence is not
    a footer — that is what keeps "The reported context: 50% is expected." in the
    answer. A rotating tip is corroborating evidence only: it counts alongside a
    measured field, never on its own, because an answer can name the tips.
    """

    if any(pattern.match(clean_line) for pattern in FOOTER_WHOLE_ROW_RES):
        return True
    segments = sum(1 for pattern in FOOTER_SEGMENT_RES if pattern.search(clean_line))
    if segments >= 2:
        return True
    if not FOOTER_TIP_RE.search(clean_line):
        return False
    if _SGR_RE.search(raw_line or ""):
        return bool(FOOTER_TIP_STYLE_RE.search(raw_line))
    return segments >= 1


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


def is_legacy_idle_prompt_line(
    clean_line: str,
    semantics: SpinnerSemantics = SpinnerSemantics.LEGACY,
) -> bool:
    """True when the row is the legacy TUI's bare ``✨`` / ``💫`` idle prompt.

    Anchored to the whole row, so an answer that merely mentions the glyph stays
    content, and scoped to the legacy dialect, whose composer this is: Kimi Code
    draws a boxed composer instead, and its moon glyphs have separate semantics.
    """

    if semantics is SpinnerSemantics.CODE:
        return False
    return bool(LEGACY_IDLE_PROMPT_RE.match(clean_line))


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
    # The indicator is a braille glyph in the spinner slot, not braille anywhere
    # on the row: an answer that mentions the character is still an answer.
    if _SPINNER_PREFIX_RE.match(clean_line):
        return True
    if _MOON_RE.search(clean_line):
        return semantics is SpinnerSemantics.LEGACY
    return False


def is_reasoning_continuation(raw_line: str) -> bool:
    """True when a row carries the reasoning block's own continuation styling.

    Used only *inside* an established reasoning block: it is the positive
    renderer evidence that the row belongs to the same private reasoning, so a
    wrapped reasoning line is not published as the agent's answer. It is
    deliberately not a licence to suppress arbitrary unstyled prose after a
    thinking bullet.

    The final-answer colour is decisive in the other direction, exactly as it is
    in :func:`is_thinking_styled`. Kimi draws the answer in colour 253 and
    italicises emphasis *within* an answer, so an emphasised answer bullet that
    immediately follows reasoning also carries italic — without this check the
    reasoning block would swallow the very answer it precedes.
    """

    raw = raw_line or ""
    if FINAL_ANSWER_BULLET_STYLE_RE.search(raw):
        return False
    return bool(_REASONING_CONTINUATION_RE.search(raw))


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

    Italic, though, is not reasoning evidence on its own. Both measured forms
    carry the grey as well — on the bullet (the patterns above) or on the text
    (:data:`BULLET_THEN_ITALIC_RE` plus :data:`REASONING_GREY_RE`) — while an
    emphasised *answer* renders as a bullet followed by italic with no grey at
    all. Treating that as reasoning would refuse a perfectly good legacy answer.
    """

    if FINAL_ANSWER_BULLET_STYLE_RE.search(raw_line):
        return False
    if any(pattern.search(raw_line) for pattern in THINKING_STYLE_PATTERNS):
        return True
    # The "colour carried on the text rather than the bullet" shape needs the
    # colour as well: italic alone is also how an emphasised *answer* renders.
    if BULLET_THEN_ITALIC_RE.search(raw_line) and REASONING_GREY_RE.search(raw_line):
        return True
    for match in TRUECOLOR_BULLET_RE.finditer(raw_line):
        red, green, blue = (int(match.group(index)) for index in (1, 2, 3))
        if max(red, green, blue) - min(red, green, blue) <= TRUECOLOR_GREY_TOLERANCE:
            return True
    return False


def is_composer_row(clean_line: str) -> bool:
    """True when ``clean_line`` is shaped like part of an input composer.

    Row-level *candidate* only. Two of these shapes are ambiguous on their own —
    a Markdown table row can be `│ a │ b │`, and `| > | Redirect stdout |` matches
    the prompt rule exactly — so :func:`classify_rows` confirms them against the
    frame the renderer draws around the prompt before any of them is allowed to
    end a response region (see :func:`_confirm_ready_frames`).

    Shapes:

    * the self-identifying ``── input ──`` rule (intermediate Kimi Code builds),
    * a bare ``╭``/``╰`` frame edge,
    * a ``│`` row whose inner text is empty, or that begins with ``>``.
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


#: A box-drawing frame edge, which is what the renderer draws around the prompt
#: row. A `│`-only shape is not enough: a Markdown table uses the same glyph in
#: the same column position.
_FRAME_EDGE_RE = re.compile(r"^\s*[╭╰]")
_FRAME_EDGE_OPEN_RE = re.compile(r"^\s*╭")
_FRAME_EDGE_CLOSE_RE = re.compile(r"^\s*╰")
#: A row drawn as part of a box (the composer's interior / a table's interior).
_FRAME_BOX_ROW_RE = re.compile(r"^\s*│")
#: How far either side of a prompt row its frame edge may sit. The measured
#: composer is three rows (`╭──╮` / `│ > │` / `╰──╯`), so a small window is enough
#: and a wide one would start letting unrelated box art qualify.
_FRAME_EDGE_WINDOW = 2


def _confirm_ready_frames(
    cleans: Sequence[str], kinds: Sequence[KimiLineKind]
) -> List[KimiLineKind]:
    """Downgrade composer rows that no input frame backs.

    A ``│ > … │`` row is the composer **only inside the box the renderer draws
    around it**. Taken alone the same shape is a Markdown table row —
    ``| > | Redirect stdout |`` matches the prompt rule character for character —
    and because ``READY_INPUT_FRAME`` ends the response region, one such table row
    truncated the answer. The self-identifying ``── input ──`` rule and a bare
    ``╭``/``╰`` edge stand on their own; everything else needs a frame edge in the
    window to be chrome.

    Only rows that are *composer-shaped* are considered. ``READY_INPUT_FRAME`` is
    also the kind of the legacy bare ``✨``/``💫`` idle prompt, which is not a
    composer and has no frame to be backed by — narrowing on
    :func:`is_composer_row` keeps that prompt a boundary.
    """

    self_identifying = {
        index
        for index, clean in enumerate(cleans)
        if NEW_TUI_INPUT_RULE_RE.match(clean) or _FRAME_EDGE_RE.match(clean)
    }
    frame_backed = set(self_identifying)
    # The `── input ──` rule names the input box itself, so its immediate
    # neighbourhood is composer even when the renderer draws no edges around it.
    for index, clean in enumerate(cleans):
        if NEW_TUI_INPUT_RULE_RE.match(clean):
            for offset in range(1, _FRAME_EDGE_WINDOW + 1):
                frame_backed.add(index - offset)
                frame_backed.add(index + offset)

    # A prompt row is otherwise composer only when it sits *inside* a frame: an
    # opening edge within the window, a closing edge within the window, and box
    # rows in between. Distance alone is not enough — a legacy input box four
    # rows above a Markdown table put `| > | Redirect stdout |` within the window
    # and deleted the row from the answer.
    opens = {index for index, clean in enumerate(cleans) if _FRAME_EDGE_OPEN_RE.match(clean)}
    closes = {index for index, clean in enumerate(cleans) if _FRAME_EDGE_CLOSE_RE.match(clean)}

    def _box_row(clean: str) -> bool:
        return bool(_FRAME_BOX_ROW_RE.match(clean or ""))

    def _inside_a_frame(index: int) -> bool:
        for above in range(1, _FRAME_EDGE_WINDOW + 1):
            top = index - above
            if top not in opens or not all(_box_row(cleans[k]) for k in range(top + 1, index)):
                continue
            for below in range(1, _FRAME_EDGE_WINDOW + 1):
                bottom = index + below
                if bottom in closes and all(
                    _box_row(cleans[k]) for k in range(index + 1, bottom)
                ):
                    return True
        return False

    for index in range(len(cleans)):
        if _inside_a_frame(index):
            frame_backed.add(index)

    return [
        (
            KimiLineKind.CONTENT
            if kind is KimiLineKind.READY_INPUT_FRAME
            and index not in frame_backed
            and is_composer_row(cleans[index])
            else kind
        )
        for index, kind in enumerate(kinds)
    ]


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


def is_user_input_continuation(raw_line: str, clean_line: Optional[str] = None) -> bool:
    """True when the row continues an *established* submitted-message block.

    Kimi Code draws submitted input in colour 222. Only the first row carries
    the sparkle, so a wrapped continuation row carries the colour without the
    glyph. That makes colour 222 *continuation* evidence only: on its own it must
    not start a block, because an answer or a code block can contain a colour-222
    row, and treating one as a fresh submission moves the extraction start past
    the answer content that preceded it.
    """

    raw = raw_line or ""
    clean = strip_sgr(raw) if clean_line is None else clean_line

    # The colour *is* the evidence, and it is stronger than the bullet exclusion:
    # a submitted message may contain a pasted list, so a colour-222 row that
    # begins with a bullet continues the submission rather than becoming an
    # answer bullet. The two are still distinguished — the renderer draws an
    # answer in colour 253, which is not this colour — so an answer bullet can
    # never be read as a continuation.
    return USER_INPUT_COLOR_INDEX in foreground_color_indices(raw)


def is_user_input_start(raw_line: str, clean_line: Optional[str] = None) -> bool:
    """True when the row is a positively identified *submitted* user message.

    The measured first row is sparkle-prefixed and drawn bold + colour 222.
    Requiring the sparkle is what makes this a block start: it is the renderer's
    own "this is a submission" marker, whereas a bare colour-222 row is
    ambiguous and is accepted only as continuation.
    """

    raw = raw_line or ""
    clean = strip_sgr(raw) if clean_line is None else clean_line

    if is_response_marker_line(clean):
        return False

    return bool(
        USER_INPUT_SPARKLE_RE.search(clean)
        and (USER_INPUT_STYLE_RE.search(raw) or clean.lstrip().startswith(("✨", "💫")))
    )


def is_user_input_echo(raw_line: str, clean_line: Optional[str] = None) -> bool:
    """Row-level predicate: the row is *styled* like submitted user input.

    Context-free, so it answers "is this row drawn like the user's message"
    rather than "does this row start a submission". :func:`classify_rows` uses
    the narrower :func:`is_user_input_start` / :func:`is_user_input_continuation`
    split, because only sequence context can tell the two apart.

    Prose is not swept up: a row must carry the colour-222 foreground or the
    sparkle, and a response bullet is rejected outright so an answer that quotes
    a sparkle cannot be read as a submission.
    """

    return is_user_input_start(raw_line, clean_line) or is_user_input_continuation(
        raw_line, clean_line
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
    if is_trust_option_row(clean):
        return KimiLineKind.TRUST_DIALOG
    if stripped.startswith(TRUST_MCP_TARGETS_PREFIX):
        return KimiLineKind.TRUST_DIALOG
    if APPROVAL_TITLE_RE.search(clean) or APPROVAL_HINT_RE.search(clean):
        return KimiLineKind.APPROVAL_DIALOG
    if APPROVAL_OPTION_RE.match(clean):
        return KimiLineKind.APPROVAL_DIALOG

    # --- live work indicators before generic chrome ---
    if is_live_spinner_line(clean, raw, semantics):
        return KimiLineKind.LIVE_SPINNER
    if is_idle_tip_line(clean, raw):
        return KimiLineKind.IDLE_TIP
    # The legacy composer's bare prompt is ready chrome: it is the row the TUI
    # shows once the turn has settled, so it must end the response region rather
    # than ride along as the answer's last line.
    if is_legacy_idle_prompt_line(clean, semantics):
        return KimiLineKind.READY_INPUT_FRAME

    # --- chrome, structurally identified ---
    if is_boot_chrome_line(clean, raw):
        return KimiLineKind.BOOT_CHROME

    if is_status_footer_line(clean, raw):
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

    Four things are tracked, and all of them are asymmetric in the same way —
    each can only become active on a **positively identified** row, and each can
    only be left on positive evidence, never on a heuristic about layout:

    **User submission (D6-F1).** Opens only on a positively identified submitted
    row — the sparkle-prefixed, colour-222 shape the renderer draws. A wrapped
    message's continuation rows repeat the colour 222 without the sparkle, so a
    colour-222 row *continues* an established block but can never start one:
    otherwise a colour-222 row inside an answer or a code block would look like a
    fresh submission and move the extraction start past earlier answer content.
    Unstyled prose after a submission is *not* absorbed (the legacy table case).

    **Reasoning.** Opens on a :data:`KimiLineKind.THINKING_BULLET`. A wrapped
    reasoning line repeats the reasoning styling (grey 244 / italic) without the
    bullet, so those rows are absorbed as reasoning rather than published.
    Arbitrary *unstyled* prose after a thinking bullet is not absorbed.

    **Tool output (D6-F3).** Opens on a :data:`KimiLineKind.TOOL_CALL` row.
    Inside it, rows that would otherwise be ``CONTENT`` become
    :data:`KimiLineKind.TOOL_CHROME`, and blank rows are kept as blanks without
    ending it (payloads are often blank-line separated). It ends only on
    positive renderer evidence that the row is not payload — see
    :func:`_ends_tool_block`. Payload cannot certify its own end by resembling
    chrome, and an escape-free capture fails closed rather than publishing
    payload as the answer.

    **Context-sensitive UI kinds.** ``TRUST_DIALOG`` and ``APPROVAL_DIALOG`` are
    confirmed against the whole capture before any of the above runs, so a row
    that merely *quotes* a dialog is ordinary content — see
    :func:`_confirm_context_kinds`.

    ``clean_lines`` may be supplied by a caller that already stripped SGR.
    """

    raws = list(raw_lines)
    if clean_lines is None:
        cleans = [strip_sgr(raw) for raw in raws]
    else:
        provided = list(clean_lines)
        cleans = [
            (
                provided[index]
                if index < len(provided) and provided[index] is not None
                else strip_sgr(raw)
            )
            for index, raw in enumerate(raws)
        ]

    kinds = [classify_line(raw, clean, semantics) for raw, clean in zip(raws, cleans)]
    kinds = _confirm_context_kinds(raws, cleans, kinds)
    kinds = _confirm_ready_frames(cleans, kinds)

    result: List[KimiLineKind] = []
    in_tool_block = False
    in_user_echo = False
    echo_absorbing_prose = False
    in_reasoning = False

    for raw, clean, kind in zip(raws, cleans, kinds):
        stripped = clean.strip()

        # --- submitted user message: a positive submission starts the block,
        #     and only the echo's own continuation styling extends it ---
        if is_user_input_start(raw, clean):
            in_user_echo = True
            echo_absorbing_prose = True
            in_tool_block = False
            in_reasoning = False
            result.append(KimiLineKind.USER_INPUT)
            continue
        if in_user_echo:
            # Positive evidence: the submitted message's own styling. It survives
            # a blank line, because a submission may have several paragraphs and
            # a pasted list — a blank row is not by itself "the submission ended".
            if is_user_input_continuation(raw, clean):
                echo_absorbing_prose = True
                result.append(KimiLineKind.USER_INPUT)
                continue
            if not stripped:
                # A blank line does not end the block, but it does end the weaker
                # "the next plain row is still part of it" inference, so a blank
                # row cannot pull ordinary answer prose into the submission.
                echo_absorbing_prose = False
                result.append(KimiLineKind.BLANK)
                continue
            if echo_absorbing_prose and semantics is SpinnerSemantics.CODE:
                if kind is KimiLineKind.CONTENT:
                    result.append(KimiLineKind.USER_INPUT)
                    continue
            in_user_echo = False
            echo_absorbing_prose = False

        # A row-level USER_INPUT that neither started nor continued a submission
        # is not a submission at all: the colour is *continuation* evidence, so a
        # colour-222 row sitting inside an answer must not become a fresh echo —
        # the region locator anchors on the last echo, and would drop every
        # answer row before this one.
        if kind is KimiLineKind.USER_INPUT:
            kind = KimiLineKind.CONTENT

        # --- reasoning: the bullet opens the block, its own styling extends it ---
        if kind is KimiLineKind.THINKING_BULLET:
            in_reasoning = True
            in_tool_block = False
            result.append(kind)
            continue
        if in_reasoning:
            if not stripped:
                # A blank line may separate reasoning paragraphs. Styling, not
                # layout, is what continues the block.
                result.append(KimiLineKind.BLANK)
                continue
            # A row the classifier reads as an answer bullet is a *new* response,
            # not a continuation, however it is styled. Italic emphasis is how an
            # answer renders too, so absorbing on styling alone would swallow a
            # legitimate italic answer that follows reasoning.
            if kind is not KimiLineKind.FINAL_BULLET and is_reasoning_continuation(raw):
                result.append(KimiLineKind.THINKING_BULLET)
                continue
            in_reasoning = False

        # --- tool output: opens on a positive tool header ---
        if kind is KimiLineKind.TOOL_CALL:
            in_tool_block = True
            in_reasoning = False
            result.append(kind)
            continue
        if not in_tool_block:
            result.append(kind)
            continue
        if _ends_tool_block(raw, kind):
            in_tool_block = False
            result.append(kind)
            continue
        # Inside a block: blank rows continue it, everything else is payload.
        result.append(kind if kind is KimiLineKind.BLANK else KimiLineKind.TOOL_CHROME)

    return result


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
    workspace_is_styled = False

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
        body = _trust_option_body(stripped)
        if body in TRUST_OPTIONS:
            if body not in options:
                options.append(body)
            if stripped.startswith(TRUST_SELECT_MARKER) and selected_index is None:
                selected_index = options.index(body)
            continue

        # Workspace row: a path-naming row in the dialog body — after the
        # navigation hint and before the option list. The measured renderer
        # draws it in colour 255, which is preferred over position alone when
        # both signals are available.
        if hint_seen and not options and is_trust_workspace_candidate(clean):
            styled = TRUST_WORKSPACE_COLOR_INDEX in foreground_color_indices(raw)
            if workspace is None or (styled and not workspace_is_styled):
                workspace = stripped
                workspace_is_styled = styled

    if not (title_seen and hint_seen and options):
        return None
    return TrustDialog(workspace, options, selected_index)
