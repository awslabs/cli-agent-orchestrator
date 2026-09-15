"""Text utilities for cleaning raw terminal output."""

import re

# Cursor-to-column-1 sequences that semantically start a new logical line.
# Must be replaced with \n BEFORE the general CSI strip, otherwise the text
# that follows gets glued to the previous content and ^ anchors fail.
# - \x1b[1G / \x1b[G   — CHA (Cursor Horizontal Absolute) to column 1
# - \x1b[nA            — CUU (Cursor Up), used with CHA for spinner redraws
# - \x1b[E / \x1b[nE   — CNL (Cursor Next Line)
# - \x1b[<row>;1H      — CUP (Cursor Position) to column 1 of any row.
#   Codex's TUI lays out its bottom prompt + status bar via CUP rather than CHA
#   (e.g. ``\x1b[46;1H›``), so without normalising CUP-to-col-1 the ``›`` idle
#   prompt stays glued mid-stream and the per-line idle check at
#   codex.py:get_status never matches.
_LINE_START_CSI = re.compile(r"\x1b\[(?:1?G|\d*A|\d*E|\d+;1H)")

# Forward horizontal positioning on the SAME line: CHA to a column > 1
# (\x1b[<n>G with n != 1) or cursor-forward (\x1b[<n>C). The TUI lays out spaced
# words/columns with these instead of emitting literal spaces, e.g. the
# completion summary "✻\x1b[3GWorked\x1b[10Gfor\x1b[14G3s" or a spinner
# "✢\x1b[3GCultivating…". Stripping them to "" glues the words together
# ("Workedfor3s", "✢Cultivating…") and breaks status patterns that rely on the
# spacing ("<Verb>ed for Ns", "<glyph> <gerund>…"). Replace with a single space
# so words stay separated. Runs AFTER _LINE_START_CSI, which has already turned
# column-1 CHA into newlines, so only column>1 moves remain here.
_FORWARD_CURSOR_CSI = re.compile(r"\x1b\[\d*[GC]")

# CSI (Control Sequence Introducer) — covers SGR, cursor, erase, scroll, etc.
# Per ECMA-48: ESC [ <params> <intermediates> <final byte>
_CSI_PATTERN = re.compile(r"(\x9B|\x1B\[)[0-?]*[ -/]*[@-~]")

# OSC (Operating System Command) — terminal title, hyperlinks, etc.
# ESC ] ... (BEL | ST)
#
# Deliberately NOT a raw string: the negated class must exclude the real ESC/BEL
# bytes, and static analysers that read the pattern out of an r"..." literal see
# `\x1b` as the four characters `\`,`x`,`1`,`b` rather than one control byte. A
# class that only excludes those four characters looks unbounded to them, so
# `\x1b]\x1b]\x1b]…` reads as a quadratic-backtracking input (CWE-1333) that
# CPython never actually walks. Letting Python resolve the escapes here keeps
# the compiled pattern identical and the class unambiguous. `\\]` / `\\\\` are
# the regex escapes for a literal `]` and the `ESC \` (ST) terminator.
_OSC_PATTERN = re.compile("\x1b\\][^\x07\x1b]*(?:\x07|\x1b\\\\)")

# Non-printable control characters (except \t and \n which are meaningful)
# Includes C1 control range (\x80-\x9f) minus \x9B which is handled as CSI above
_CONTROL_CHARS_PATTERN = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f\x80-\x9a\x9c-\x9f]")


def strip_terminal_escapes(text: str) -> str:
    """Strip terminal escape sequences and control characters from text.

    Handles CSI sequences (colors, cursor movement, erase), OSC sequences
    (terminal title), non-printable control characters, and normalizes
    carriage returns to newlines so regex anchors (^/$) work correctly.

    WARNING: This function does NOT render carriage returns — it normalizes
    bare \\r to \\n. In a real terminal, \\r moves the cursor to column 0 so the
    next write overwrites the current line (used by spinners, progress bars).
    Here, each \\r becomes a new line, so spinner frames like "⠋ Thinking..."
    will appear as separate lines rather than collapsing into one. This is
    acceptable for status detection (pattern matching still works) but NOT
    suitable for extracting user-visible output.

    Used for status detection on raw FIFO buffer output.
    For message extraction, use tmux capture-pane which renders the terminal.
    """
    # Replace cursor-to-column-1 sequences with \n BEFORE stripping other CSI.
    # These sequences mean "start writing from column 1" (e.g. spinner redraws,
    # prompt redraws) — semantically a new logical line for pattern matching.
    text = _LINE_START_CSI.sub("\n", text)
    # Preserve word separation for column-positioned text (see _FORWARD_CURSOR_CSI).
    text = _FORWARD_CURSOR_CSI.sub(" ", text)
    text = _CSI_PATTERN.sub("", text)
    text = _OSC_PATTERN.sub("", text)
    text = _CONTROL_CHARS_PATTERN.sub("", text)
    # Normalize \r\n and bare \r to \n so ^ anchors work after carriage returns.
    # FIFO output uses \r for in-place redraws (spinners, prompts) — for status
    # detection, each redraw is a new logical line of output.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text
