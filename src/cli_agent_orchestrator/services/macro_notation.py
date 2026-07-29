"""The macro notation: one pinned grammar, and this is the authority.

Native-TUI-console §5.3.  Two parsers exist — this Python authority
(server-side, behind ``POST /macros/parse-notation``) and Lane B's
TypeScript live preview — and they are tested against the same golden
vectors (``test/fixtures/notation_vectors.json``), mirroring the digest
golden-vector precedent: the two sides cannot drift into spelling the
same macro two ways.

The grammar, exactly:

.. code-block:: text

    sequence := event (WS+ event)*
    event    := text | named | chord | repeat
    text     := '"' JSON-string '"'        # JSON escaping exactly; , + / \\ literal inside quotes
    named    := [a-z][a-z0-9-]*            # enter escape up down left right home end
                                           # page-up page-down delete insert tab backspace
    chord    := 'ctrl+' [a-z]              # ctrl+c ctrl+s … (D7 mapping)
    repeat   := (named|chord) '*' [1-9][0-9]*   # up*3; expansion counts toward the 32-event cap

Notation never touches disk (§5.1): the stored and transmitted
correctness boundary is the v3 event array, and this module's only
product is that array plus its readable preview.  Parse errors carry a
0-based character offset and a message; an unparseable or
unrepresentable macro cannot be saved or sent.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from cli_agent_orchestrator.services.control_input_contract import (
    MAX_SEQUENCE_EVENTS,
    MAX_SEQUENCE_TEXT_BYTES,
)

# Notation names to wire key names (§5.3).  The set is the §3.2 key set
# spelled in notation form; a name outside it is a parse error, never a
# guess (``BTab``, modified arrows, and F-keys have no notation spelling
# because the wire refuses them).
NOTATION_KEY_NAMES = {
    "enter": "Enter",
    "escape": "Escape",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "home": "Home",
    "end": "End",
    "page-up": "PageUp",
    "page-down": "PageDown",
    "delete": "Delete",
    "insert": "Insert",
    "tab": "Tab",
    "backspace": "Backspace",
}

_NAMED_PATTERN = re.compile(r"[a-z][a-z0-9-]*")
_CHORD_PREFIX = "ctrl+"
_REPEAT_PATTERN = re.compile(r"[1-9][0-9]*")

# Modifier words a combination may be built from.  A combination of them
# (``ctrl+shift+x``, ``alt+x``, …) has no standard-mode byte encoding —
# tmux would inject the base key or a wrong encoding (§3.3) — so it is
# named and refused rather than misread as a single-modifier chord.
_MODIFIER_WORDS = frozenset({"ctrl", "alt", "meta", "cmd", "shift", "super"})
_COMBINATION_PATTERN = re.compile(r"[a-z0-9][a-z0-9+\-]*")


def _multi_modifier_failure(notation: str, pos: int) -> NotationResult:
    """The §3.3 refusal for a combination the byte stream cannot carry."""
    match = _COMBINATION_PATTERN.match(notation, pos)
    combination = match.group(0) if match is not None else notation[pos:]
    return _fail(
        pos,
        f"multi-modifier combination {combination!r} cannot be represented: no "
        "standard-mode terminal byte encoding exists for it (tmux would inject the "
        "base key or a wrong encoding), so it is refused, never approximated",
    )


# Text bytes a pane can never be sent honestly through the control path
# (the deployed literal screen): ESC and its C1 spelling synthesise
# escape sequences; CR/LF submit at a point the caller did not choose.
# The send path screens them again; the parse authority refuses them here
# so an unrepresentable macro cannot be saved.
_ILLEGAL_TEXT_CHARS = ("\x1b", "\x9b", "\r", "\n")

# A lone surrogate (a high surrogate not followed by a low one, or a low
# one not preceded by a high one) is valid JSON but not UTF-8-encodable.
_LONE_SURROGATE_RE = re.compile(
    r"[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]"
)


@dataclass(frozen=True)
class NotationError:
    """One parse failure: where, and what is wrong there."""

    offset: int
    message: str


@dataclass(frozen=True)
class NotationResult:
    """The parse answer: the events and preview, or the errors.

    ``errors`` is empty exactly when the parse succeeded; ``events`` and
    ``preview`` are then both set.  At most one error is reported — the
    parse fails fast at the first malformed token, because a parser that
    continues past an error would be guessing at intent from then on.
    """

    events: Optional[List[Dict[str, Any]]]
    preview: Optional[str]
    errors: List[NotationError]


def _fail(offset: int, message: str) -> NotationResult:
    return NotationResult(events=None, preview=None, errors=[NotationError(offset, message)])


def _chord_event(letter: str) -> Dict[str, Any]:
    """The wire event for one ``ctrl+<letter>`` token (D7).

    ``ctrl+c`` maps to the provider-agnostic ``key`` event ``C-c``; every
    other letter maps to a ``chord`` event (which the server admits only
    when the provider+build pins it — a send-time, per-terminal fact this
    parser cannot know and therefore does not check).
    """
    if letter == "c":
        return {"type": "key", "key": "C-c"}
    return {"type": "chord", "chord": f"C-{letter}"}


def parse_notation(notation: str) -> NotationResult:
    """Parse one notation string into its v3 events and readable preview.

    The single authority for the grammar above.  Never approximates: a
    token that is not exactly one of the four event forms is an error
    with an offset, and an unrepresentable one (an over-cap expansion, an
    over-budget text, a control character the pane cannot be sent) is
    refused rather than approximated into something else.
    """
    if not isinstance(notation, str):
        return _fail(0, f"notation must be a string, got {type(notation).__name__}")
    length = len(notation)
    pos = 0
    while pos < length and notation[pos].isspace():
        pos += 1
    if pos == length:
        return _fail(pos, "a macro names at least one event")

    events: List[Dict[str, Any]] = []
    text_bytes = 0

    while pos < length:
        event_pos = pos
        base: Optional[Dict[str, Any]] = None
        repeatable = False
        char = notation[pos]

        if char == '"':
            # A JSON string, scanned to its closing quote (backslash
            # escapes honoured) and decoded by JSON's own rules, exactly.
            index = pos + 1
            closed = False
            while index < length:
                c = notation[index]
                if c == "\\":
                    index += 2
                    continue
                if c == '"':
                    closed = True
                    break
                index += 1
            if not closed:
                return _fail(event_pos, "unterminated string: a text token is a JSON string")
            token = notation[event_pos : index + 1]
            try:
                decoded = json.loads(token)
            except json.JSONDecodeError as exc:
                return _fail(
                    event_pos + exc.pos,
                    f"invalid JSON string: {exc.msg}; text uses JSON escaping exactly "
                    "(comma, plus, slash and backslash are literal inside quotes)",
                )
            if not isinstance(decoded, str) or decoded == "":
                return _fail(event_pos, "a text event must be a non-empty string")
            for illegal in _ILLEGAL_TEXT_CHARS:
                at = decoded.find(illegal)
                if at != -1:
                    return _fail(
                        event_pos,
                        "the text contains a control character (ESC, C1 CSI, CR, or LF) "
                        "the control path can never send honestly; an unrepresentable "
                        "macro is refused, never approximated",
                    )
            if _LONE_SURROGATE_RE.search(decoded) is not None:
                # A lone surrogate parses as valid JSON but is not
                # UTF-8-encodable, so it can never become a wire event's
                # text; screening here keeps the failure in the ordinary
                # offset-bearing shape (the endpoint answers 422, never
                # 500) — the same defect class as the r11 repeat guard.
                return _fail(
                    event_pos,
                    "the text contains a lone surrogate that is not UTF-8-encodable; "
                    "an unrepresentable macro is refused, never approximated",
                )
            text_bytes += len(decoded.encode("utf-8"))
            if text_bytes > MAX_SEQUENCE_TEXT_BYTES:
                return _fail(
                    event_pos,
                    f"this text pushes the sequence past the {MAX_SEQUENCE_TEXT_BYTES}-byte "
                    "aggregate text cap; a macro is a short control burst, not a document",
                )
            base = {"type": "text", "text": decoded}
            pos = index + 1
        elif notation.startswith(_CHORD_PREFIX, pos):
            letter_at = pos + len(_CHORD_PREFIX)
            if letter_at < length:
                word = _NAMED_PATTERN.match(notation, letter_at)
                if (
                    word is not None
                    and word.group(0) in _MODIFIER_WORDS
                    and (word.end() >= length or notation[word.end()] in "+ \t\n")
                ):
                    # ``ctrl+shift+x`` and friends: a modifier where the
                    # chord's single letter must be.
                    return _multi_modifier_failure(notation, pos)
            if letter_at >= length or not ("a" <= notation[letter_at] <= "z"):
                return _fail(
                    letter_at,
                    "a chord is 'ctrl+' followed by one letter a-z; multi-modifier and "
                    "non-letter chords are unrepresentable and refused, never approximated",
                )
            base = _chord_event(notation[letter_at])
            repeatable = True
            pos = letter_at + 1
        else:
            match = _NAMED_PATTERN.match(notation, pos)
            if match is None:
                return _fail(
                    pos,
                    f"expected a quoted text, a named key, or a ctrl+<letter> chord, "
                    f"not {char!r}; known key names are {sorted(NOTATION_KEY_NAMES)}",
                )
            name = match.group(0)
            if name in _MODIFIER_WORDS and match.end() < length and notation[match.end()] == "+":
                # ``alt+x``, ``meta+x`` and friends at event position.
                return _multi_modifier_failure(notation, pos)
            if name not in NOTATION_KEY_NAMES:
                return _fail(
                    pos,
                    f"unknown named key {name!r}; known key names are "
                    f"{sorted(NOTATION_KEY_NAMES)} — unlisted keys (BTab, modified "
                    "arrows, F-keys) are refused, never approximated",
                )
            base = {"type": "key", "key": NOTATION_KEY_NAMES[name]}
            repeatable = True
            pos = match.end()

        count = 1
        if repeatable and pos < length and notation[pos] == "*":
            repeat_pos = pos
            digits = _REPEAT_PATTERN.match(notation, pos + 1)
            if digits is None:
                return _fail(
                    repeat_pos + 1,
                    "a repeat count is a positive integer written [1-9][0-9]* "
                    "(zero and empty counts are malformed, not no-ops)",
                )
            count_text = digits.group(0)
            if len(count_text) > 2:
                # A count of 100+ can never fit the 32-event budget, even
                # in an empty sequence — fail BEFORE the integer
                # conversion (CPython refuses over-long digit strings with
                # a bare ValueError from its int-max-str-digits guard).
                # r11/Sol: the failure keeps the ordinary offset-bearing
                # shape, and the endpoint answers 422, never 500; the
                # message embeds no token, so it is bounded by
                # construction.
                return _fail(
                    event_pos,
                    f"this event brings the sequence past the {MAX_SEQUENCE_EVENTS}-event "
                    "cap; a repeat expansion counts every event it stands for",
                )
            count = int(count_text)
            pos = digits.end()

        if len(events) + count > MAX_SEQUENCE_EVENTS:
            return _fail(
                event_pos,
                f"this event brings the sequence past the {MAX_SEQUENCE_EVENTS}-event "
                "cap; a repeat expansion counts every event it stands for",
            )
        events.extend(base for _ in range(count))

        if pos < length:
            if not notation[pos].isspace():
                return _fail(pos, f"expected whitespace between events, not {notation[pos]!r}")
            while pos < length and notation[pos].isspace():
                pos += 1

    return NotationResult(events=events, preview=preview_sequence(events), errors=[])


def _preview_token(event: Dict[str, Any]) -> str:
    """One event's canonical preview token, extending the deployed recorder.

    Text renders as its JSON string (the notation's own spelling, so the
    preview round-trips); a chord renders ``[Ctrl+X]``; the provider-
    agnostic interrupt key renders ``[Ctrl+C]``; every other key renders
    its wire name in brackets.
    """
    if event["type"] == "text":
        return json.dumps(event["text"], ensure_ascii=False)
    if event["type"] == "chord":
        return f"[Ctrl+{event['chord'][2:].upper()}]"
    if event["key"] == "C-c":
        return "[Ctrl+C]"
    return f"[{event['key']}]"


def preview_sequence(events: List[Dict[str, Any]]) -> str:
    """The canonical one-line preview: ``"text" [Enter] [Up]×3 [Ctrl+S]``.

    A run of two or more identical key/chord events collapses to
    ``[Name]×N`` — the normalized form, whether the run came from a
    repeat token or from spelled-out repetition.  Text events are never
    collapsed: two adjacent text events are different content, not a
    repeat.
    """
    tokens: List[str] = []
    index = 0
    while index < len(events):
        event = events[index]
        run_end = index + 1
        if event["type"] != "text":
            while run_end < len(events) and events[run_end] == event:
                run_end += 1
        token = _preview_token(event)
        run = run_end - index
        tokens.append(f"{token}×{run}" if run > 1 else token)
        index = run_end
    return " ".join(tokens)
