"""Operator-macro notation: the §5.3 editing-surface grammar.

**Provisional server authority (Lane B).**  §9 assigns the server notation
parser to Lane A, co-located with the control-input contract, consumed by
Lane B's §5.4 routes.  Lane A has not merged on this base, so this module is
Lane B's provisional authority.  It implements the frozen §5.3 grammar
*exactly* — nothing here is a frontend invention — and the shared golden
vectors (``web/src/test/fixtures/macroNotationVectors.json``) pin
byte-identical behaviour between this parser and the TypeScript live-preview
parser, mirroring the digest golden-vector precedent.  When Lane A lands,
integration swaps this implementation for the contract-co-located one; the
§5.4 routes keep calling the same two functions and the same vectors must
keep passing.

Grammar (pinned, §5.3)::

    sequence := event (WS+ event)*
    event    := text | named | chord | repeat
    text     := '"' JSON-string '"'      # JSON escaping exactly
    named    := [a-z][a-z0-9-]*          # the fourteen names in NAMED_KEYS
    chord    := 'ctrl+' [a-z]            # ctrl+c ctrl+s … (D7 mapping)
    repeat   := (named|chord) '*' [1-9][0-9]*   # up*3; expansion counts
                                                # toward the 32-event cap

Notation names map to wire names (``enter`` → ``Enter``, ``page-up`` →
``PageUp``, …); ``ctrl+c`` → ``key C-c`` (provider-agnostic interrupt);
every other ``ctrl+x`` → ``chord C-x`` (D7).  Parse errors carry an offset
and a message; unparseable or unrepresentable notation cannot be saved or
sent (the client disables the action; the server 422s).

Notation never touches disk (§5.1): the stored/transmitted correctness
boundary is the v3 event array, and every parse result is validated through
the contract's ``normalize_sequence_events``.
"""

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from cli_agent_orchestrator.services.control_input_contract import (
    MAX_SEQUENCE_EVENTS,
    MAX_SEQUENCE_TEXT_BYTES,
    normalize_sequence_events,
)

# The fourteen named keys of the §5.3 grammar, notation name → wire name.
NAMED_KEYS: Dict[str, str] = {
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

# Wire name → notation name, for the canonical renderer.
WIRE_TO_NOTATION: Dict[str, str] = {wire: name for name, wire in NAMED_KEYS.items()}

# A symbol token is a maximal run of these characters; quoting is handled
# separately (a text event may itself contain whitespace).
_SYMBOL_RE = re.compile(r"[a-z0-9+\-*]+")

# WS between events is pinned to the ASCII whitespace set.  The grammar's
# ``WS+`` is deliberately *not* Python's ``str.isspace()`` (nor JavaScript's
# ``\s``): the two parsers — this authority and the TS live preview — must
# agree byte-for-byte, and the platform whitespace classes diverge on edge
# characters (U+0085, U+FEFF, the C0 separators).
_WHITESPACE = " \t\n\r\v\f"

_REPEAT_COUNT_RE = re.compile(r"[1-9][0-9]*")

_CHORD_LETTER_RE = re.compile(r"C-([a-z])")


class NotationError(ValueError):
    """One parse/render failure carrying the §5.3 (offset, message) pair."""

    def __init__(self, offset: int, message: str) -> None:
        super().__init__(message)
        self.offset = offset
        self.message = message

    def as_dict(self) -> Dict[str, Any]:
        return {"offset": self.offset, "message": self.message}


def _utf8_len(text: str) -> int:
    try:
        return len(text.encode("utf-8"))
    except UnicodeEncodeError as exc:
        # A lone surrogate parses as valid JSON but is not UTF-8-encodable,
        # so it can never become a wire event's text.
        raise ValueError("lone-surrogate") from exc


def _scan_json_string(notation: str, start: int) -> Tuple[str, int]:
    """Parse the JSON string opening at ``start``; return (value, end)."""
    n = len(notation)
    k = start + 1
    closed = False
    while k < n:
        ch = notation[k]
        if ch == "\\":
            k += 2
            continue
        if ch == '"':
            closed = True
            break
        k += 1
    if not closed:
        raise NotationError(start, "unterminated text event")
    fragment = notation[start : k + 1]
    try:
        value = json.loads(fragment)
    except json.JSONDecodeError:
        raise NotationError(start, "invalid JSON string in text event") from None
    if not isinstance(value, str):  # pragma: no cover - '"' forces a string
        raise NotationError(start, "invalid JSON string in text event")
    return value, k + 1


def _split_repeat(token: str, start: int) -> Tuple[str, Optional[int]]:
    """Split ``base[*N]``; the repeat count obeys [1-9][0-9]* exactly."""
    if "*" not in token:
        return token, None
    base, _, count_text = token.partition("*")
    if _REPEAT_COUNT_RE.fullmatch(count_text) is None:
        raise NotationError(
            start + len(base),
            f"invalid repeat count '*{count_text}': expected '*' followed by a " "positive integer",
        )
    return base, int(count_text)


def _map_symbol(base: str, start: int) -> Dict[str, Any]:
    """Map one named key or ctrl chord to its wire event (D7)."""
    if base.startswith("ctrl+"):
        rest = base[len("ctrl+") :]
        if len(rest) == 1 and "a" <= rest <= "z":
            if rest == "c":
                return {"type": "key", "key": "C-c"}
            return {"type": "chord", "chord": f"C-{rest}"}
        raise NotationError(
            start,
            f"unrepresentable chord '{base}': only ctrl+<letter> has a pinned "
            "terminal byte encoding",
        )
    if base in NAMED_KEYS:
        return {"type": "key", "key": NAMED_KEYS[base]}
    if "+" in base:
        raise NotationError(
            start,
            f"unrepresentable event '{base}': terminal byte streams cannot "
            "express modifier combinations other than ctrl+<letter>",
        )
    raise NotationError(start, f"unknown key name '{base}'")


def parse_notation(notation: str) -> List[Dict[str, Any]]:
    """Parse §5.3 notation into a normalized v3 event array.

    Raises :class:`NotationError` with an offset and message on any failure:
    unparseable or unrepresentable notation never becomes events.  Caps are
    enforced as the events accumulate — a repeat expansion counts toward the
    32-event cap — so the failing token's offset is always known.
    """
    if not isinstance(notation, str):
        raise TypeError(f"notation must be a string, got {type(notation).__name__}")
    events: List[Dict[str, Any]] = []
    text_bytes = 0
    i = 0
    n = len(notation)
    while True:
        while i < n and notation[i] in _WHITESPACE:
            i += 1
        if i >= n:
            break
        start = i
        ch = notation[i]
        if ch == '"':
            value, i = _scan_json_string(notation, i)
            if i < n and notation[i] not in _WHITESPACE:
                raise NotationError(i, "expected whitespace between events")
            try:
                text_bytes += _utf8_len(value)
            except ValueError:
                raise NotationError(
                    start,
                    "text event is not UTF-8-encodable (lone surrogate); it can "
                    "never become a wire event",
                ) from None
            if text_bytes > MAX_SEQUENCE_TEXT_BYTES:
                raise NotationError(
                    start,
                    f"text event pushes the sequence past the "
                    f"{MAX_SEQUENCE_TEXT_BYTES}-byte aggregate cap",
                )
            events.append({"type": "text", "text": value})
        elif "a" <= ch <= "z":
            match = _SYMBOL_RE.match(notation, i)
            assert match is not None  # ch already matched the class
            token = match.group(0)
            i = match.end()
            if i < n and notation[i] not in _WHITESPACE:
                raise NotationError(i, "expected whitespace between events")
            base, count = _split_repeat(token, start)
            event = _map_symbol(base, start)
            if count is None:
                if len(events) + 1 > MAX_SEQUENCE_EVENTS:
                    raise NotationError(
                        start,
                        f"sequence holds at most {MAX_SEQUENCE_EVENTS} events",
                    )
                events.append(event)
            else:
                if len(events) + count > MAX_SEQUENCE_EVENTS:
                    raise NotationError(
                        start,
                        f"repeat '{token}' expands past the " f"{MAX_SEQUENCE_EVENTS}-event cap",
                    )
                events.extend(dict(event) for _ in range(count))
        else:
            raise NotationError(
                start,
                'expected an event (a "quoted" text, a key name, or ' "ctrl+<letter>)",
            )
    if not events:
        raise NotationError(0, "empty notation: name at least one event")
    # The contract is the correctness boundary: what parsed must also be a
    # valid wire sequence (shape and caps), never a notation-only dialect.
    return normalize_sequence_events(events)


def _event_notation(event: Dict[str, Any]) -> str:
    """One event's canonical notation token (no repeat folding)."""
    event_type = event.get("type")
    if event_type == "text":
        # Canonical text form: JSON escaping exactly, non-ASCII literal.
        return json.dumps(event["text"], ensure_ascii=False)
    if event_type == "key":
        key = event.get("key")
        if key == "C-c":
            return "ctrl+c"
        name = WIRE_TO_NOTATION.get(key if isinstance(key, str) else "")
        if name is None:
            raise ValueError(f"key {key!r} has no notation name")
        return name
    if event_type == "chord":
        chord = event.get("chord")
        match = _CHORD_LETTER_RE.fullmatch(chord if isinstance(chord, str) else "")
        # ``ctrl+c`` parses to ``key C-c`` (D7), so a chord C-c has no
        # faithful notation form — rendering one would round-trip to a
        # different event.
        if match is None or match.group(1) == "c":
            raise ValueError(f"chord {chord!r} has no notation form")
        return f"ctrl+{match.group(1)}"
    raise ValueError(f"event type {event_type!r} has no notation form")


def render_notation(events: List[Dict[str, Any]]) -> str:
    """Render the canonical notation for a validated v3 event array.

    Runs of two or more identical non-text events fold to ``name*N``; text
    events never fold.  ``parse_notation(render_notation(events)) == events``
    for every representable array — the round-trip the §7.4 editor relies on.
    """
    normalized = normalize_sequence_events(events)
    tokens: List[str] = []
    i = 0
    while i < len(normalized):
        event = normalized[i]
        token = _event_notation(event)
        if event["type"] == "text":
            tokens.append(token)
            i += 1
            continue
        run_end = i
        while (
            run_end < len(normalized)
            and normalized[run_end]["type"] != "text"
            and normalized[run_end] == event
        ):
            run_end += 1
        run = run_end - i
        tokens.append(f"{token}*{run}" if run >= 2 else token)
        i = run_end
    return " ".join(tokens)


def _preview_token(event: Dict[str, Any]) -> str:
    """One event's preview token: ``"text"``, ``[Enter]``, ``[Ctrl+S]``."""
    if event["type"] == "text":
        return json.dumps(event["text"], ensure_ascii=False)
    if event["type"] == "chord":
        chord = event.get("chord", "")
        letter = chord[2:] if chord.startswith("C-") else chord
        return f"[Ctrl+{letter.upper()}]"
    key = event.get("key", "")
    if key == "C-c":
        return "[Ctrl+C]"
    return f"[{key}]"


def render_preview(events: List[Dict[str, Any]]) -> str:
    """The §5.3 normalized preview: ``"text" [Enter] [Up]×3 [Ctrl+S]``."""
    normalized = normalize_sequence_events(events)
    tokens: List[str] = []
    i = 0
    while i < len(normalized):
        event = normalized[i]
        token = _preview_token(event)
        if event["type"] == "text":
            tokens.append(token)
            i += 1
            continue
        run_end = i
        while (
            run_end < len(normalized)
            and normalized[run_end]["type"] != "text"
            and normalized[run_end] == event
        ):
            run_end += 1
        run = run_end - i
        tokens.append(f"{token}×{run}" if run >= 2 else token)
        i = run_end
    return " ".join(tokens)


def parse_with_preview(notation: str) -> Tuple[List[Dict[str, Any]], str]:
    """The §5.3 authority endpoint's success shape: (events, preview)."""
    events = parse_notation(notation)
    return events, render_preview(events)
