"""The typed leak rule for the CAO Dojo trace (#1116).

``check(data) -> [Finding]`` reads a normalised trace and reports anything that
identifies the machine, the person, or the fleet that recorded it. It never
uses the scrubber's tables: it is a second, independent reading of the output,
so a scrubber bug shows up here instead of being trusted.

Each rule is either **path-scoped** (it applies only at the listed keys) or
**global** (it applies to every string value outside ``_meta``). The rule ids
(L1-L14) are stable: ``test_cao_dojo_trace_leaks.py`` injects one leak per
rule and asserts that exact id fires at that exact path, so deleting a rule
fails its own test. A near miss per rule pins the other edge, so widening a
rule fails too.

The rule is a heuristic for free text. A string outside the typed keys is held
only to the global patterns (paths, home, host, user, UUIDs, and runs of 8 or
more hex digits not adjacent to another hex digit), so a new free-text field
that can carry anything else needs a rule of its own.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import socket
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Tuple

from trace_scrub import CAO_GENERATIVE_UI, META_KEYS, parse

# --- Findings ------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    rule_id: str
    line: int
    json_path: str
    excerpt: str

    def __str__(self) -> str:
        return f"{self.rule_id} at line {self.line} {self.json_path}: {self.excerpt}"


def _excerpt(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return text if len(text) <= 80 else text[:77] + "..."


# --- Patterns ------------------------------------------------------------------

L1_ABS_PATH = re.compile(
    r"(^|[\s\"'=:])(/(Users|home|private|tmp|var|Volumes|opt|root|etc|mnt|srv|run|github)/"
    r"|[A-Za-z]:\\)"
)
L4_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# Any run of 8 or more hex digits, either case, not adjacent to another hex digit:
# an 8-hex terminal id, a dashless uuid4().hex, a 16-hex span or attribution hash,
# a 40-hex git sha. Hex-bounded rather than word-bounded, so a run glued to a
# word character (`session_e327d8c9`, `x0123456789abcdef`) is still found.
L5_HEX = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{8,}(?![0-9a-fA-F])")
# _meta keys that legitimately hold a hex digest.
META_HEX_KEYS = frozenset({"cao_build", "shape_sha256"})

ID_TOKEN = re.compile(r"^id-\d+$")
TERMINAL_TOKEN = re.compile(r"^terminal-\d+$")
SESSION_TOKEN = re.compile(r"^session-\d+$")
PROFILE_TOKEN = re.compile(r"^profile-\d+$")
WINDOW_TOKEN = re.compile(r"^window-\d+$")
RUNNER_TOKEN = re.compile(r"^runner-\d+$")

L5_KEYS = frozenset({"terminal_id", "terminalId", "step_id"})
L6_KEYS = frozenset({"session_name", "sessionName"})
L7_KEYS = frozenset({"agent_profile", "step_name"})
L8_KEYS = frozenset({"window", "tmux_window"})
# L15: a runner id. Kept apart from the scrubber's RUNNER_KEYS on purpose, like L14.
L15_KEYS = frozenset({"runner_id", "runnerId"})
# The name 3.0 replaced. It is never tokenised, so any value under it is a raw label.
L15_OLD_KEYS = frozenset({"runtime_id", "runtimeId"})
# L14: generated ids. Kept apart from the scrubber's ID_KEYS on purpose, so a key
# dropped there still fails here. A raw AG-UI 1.0 session attribution
# (``session:<16 hex>``) under one of these keys fails here by key, whatever its form.
L14_KEYS = frozenset(
    {
        "event_id",
        "eventId",
        "interrupt_id",
        "interruptId",
        "traceparent",
        "subagentRunId",
        "parentSubagentRunId",
    }
)

# L3: words that occur in the trace itself. A login equal to one of these, or
# shorter than four characters, would match the trace's own vocabulary, so the
# username check skips it (the hostname check uses the same floor).
L3_MIN_LENGTH = 4
L3_STOPLIST = frozenset(
    {
        "none",
        "approve",
        "deny",
        "reject",
        "session",
        "success",
        "claude",
        "mock",
        "terminal",
        "profile",
        "window",
        "interrupt",
        "run",
        "step",
        "cao",
    }
)

# L12: what a reduced request may hold.
REQUEST_KEYS = frozenset({"resume"})
RESUME_ENTRY_KEYS = frozenset({"interruptId", "status", "payload"})
RESUME_PAYLOAD_KEYS = frozenset({"approved"})

# L13: the closed record shapes.
RECORD_KEYS = {
    ("stream", "out"): frozenset({"capture", "seq", "plane", "dir", "frame"}),
    ("run", "out"): frozenset({"capture", "seq", "plane", "run", "dir", "frame"}),
    ("run", "request"): frozenset({"capture", "seq", "plane", "run", "dir", "request"}),
}


# --- Runner identity (read at call time so tests can monkeypatch) ------------


def _home() -> Optional[str]:
    home = os.path.expanduser("~")
    return home if home and home not in ("/", "~") else None


def _hostnames() -> List[str]:
    try:
        long_name = socket.gethostname()
    except OSError:  # pragma: no cover - platform without a hostname
        return []
    names = {long_name, long_name.split(".")[0]}
    return sorted(n for n in names if len(n) >= L3_MIN_LENGTH and n.lower() not in L3_STOPLIST)


def _username() -> Optional[str]:
    try:
        name = getpass.getuser()
    except Exception:  # pragma: no cover - no login name available
        return None
    if len(name) < L3_MIN_LENGTH or name.lower() in L3_STOPLIST:
        return None
    return name


# --- Walk ----------------------------------------------------------------------


def _whole_word(word: str) -> "re.Pattern[str]":
    return re.compile(rf"(?<![\w-]){re.escape(word)}(?![\w-])", re.I)


def _fmt(path: Tuple[Any, ...]) -> str:
    out = "$"
    for part in path:
        out += f"[{part}]" if isinstance(part, int) else f".{part}"
    return out


def _iter_values(value: Any, path: Tuple[Any, ...]) -> Iterable[Tuple[Tuple[Any, ...], Any]]:
    yield path, value
    if isinstance(value, dict):
        for key in sorted(value):
            yield from _iter_values(value[key], path + (key,))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from _iter_values(item, path + (i,))


def _key(path: Tuple[Any, ...], depth: int = 1) -> Any:
    return path[-depth] if len(path) >= depth else None


def _logical(record: dict, path: Tuple[Any, ...]) -> Tuple[Any, ...]:
    """Map a STATE_DELTA op's value onto the snapshot path it patches.

    ``frame.delta[i].value`` under an op with ``path: "/terminals"`` is held to
    the same rules as ``frame.snapshot.terminals``, so a patch cannot carry what
    a snapshot may not.
    """
    frame = record.get("frame")
    if (
        len(path) >= 4
        and path[0] == "frame"
        and path[1] == "delta"
        and isinstance(path[2], int)
        and path[3] == "value"
        and isinstance(frame, dict)
        and frame.get("type") == "STATE_DELTA"
    ):
        ops = frame.get("delta") or []
        op = ops[path[2]] if path[2] < len(ops) else None
        pointer = op.get("path") if isinstance(op, dict) else None
        if isinstance(pointer, str) and pointer.startswith("/"):
            segments: List[Any] = []
            for raw in pointer[1:].split("/"):
                part = raw.replace("~1", "/").replace("~0", "~")
                segments.append(int(part) if part.isdigit() else part)
            return ("frame", "snapshot", *segments, *path[4:])
    return path


class _Checker:
    def __init__(self) -> None:
        self.findings: List[Finding] = []
        self.home = _home()
        # Whole-word matches only, for the hostname as for the username, so a
        # host named like part of a trace word (`card` in `approval_card`) is
        # not a finding.
        self.host_res = [_whole_word(host) for host in _hostnames()]
        user = _username()
        self.user_re = _whole_word(user) if user else None

    def add(self, rule: str, line: int, path: Tuple[Any, ...], value: Any) -> None:
        self.findings.append(Finding(rule, line, _fmt(path), _excerpt(value)))

    # Global string rules (L1-L5 global halves).
    def strings(self, line: int, path: Tuple[Any, ...], value: str, in_meta: bool) -> None:
        if in_meta:
            if _key(path) in META_HEX_KEYS:
                return
            if L4_UUID.search(value):
                self.add("L4", line, path, value)
            elif L5_HEX.search(value):
                self.add("L5", line, path, value)
            return
        if L1_ABS_PATH.search(value) or value.startswith("~/"):
            self.add("L1", line, path, value)
        if self.home and self.home in value:
            self.add("L2", line, path, value)
        for host_re in self.host_res:
            if host_re.search(value):
                self.add("L3", line, path, value)
                break
        else:
            if self.user_re is not None and self.user_re.search(value):
                self.add("L3", line, path, value)
        if L4_UUID.search(value):
            self.add("L4", line, path, value)
        elif L5_HEX.search(value):
            self.add("L5", line, path, value)

    def scoped(self, line: int, path: Tuple[Any, ...], value: Any, report: Tuple[Any, ...]) -> None:
        """Path-scoped rules. ``path`` is logical (see ``_logical``); ``report`` is real."""
        key = _key(path)
        parent = _key(path, 2)
        grandparent = _key(path, 3)
        if not isinstance(key, str):
            return
        in_terminals = isinstance(parent, int) and grandparent == "terminals"
        in_sessions = isinstance(parent, int) and grandparent == "sessions"

        if key in L5_KEYS or (in_terminals and key == "id"):
            if value is not None and not (isinstance(value, str) and TERMINAL_TOKEN.match(value)):
                self.add("L5", line, report, value)
        if key in L6_KEYS or (in_sessions and key in ("id", "name")):
            if value is not None and not (isinstance(value, str) and SESSION_TOKEN.match(value)):
                self.add("L6", line, report, value)
        if key in L7_KEYS:
            if value is not None and not (isinstance(value, str) and PROFILE_TOKEN.match(value)):
                self.add("L7", line, report, value)
        if key in L8_KEYS and value is not None:
            # A scrubber token (`window-100`) is never a finding; anything else is.
            if not (isinstance(value, str) and WINDOW_TOKEN.match(value)):
                self.add("L8", line, report, value)
        if key in L15_OLD_KEYS:
            self.add("L15", line, report, value)
        if key in L15_KEYS and value is not None:
            if not (isinstance(value, str) and RUNNER_TOKEN.match(value)):
                self.add("L15", line, report, value)
        if key in L14_KEYS or (
            isinstance(parent, int) and grandparent == "interrupts" and key == "id"
        ):
            if value is not None and not (isinstance(value, str) and ID_TOKEN.match(value)):
                self.add("L14", line, report, value)
        # L11: the prompt body. CAO drops it
        # (AgentHandoffWithApproval.on_provider_waiting); this asserts it stays dropped.
        if key == "message" and (
            (isinstance(parent, int) and grandparent == "interrupts") or parent == "props"
        ):
            if value not in (None, ""):
                self.add("L11", line, report, value)

    def frame(self, line: int, frame: Any) -> None:
        if not isinstance(frame, dict):
            return
        base = ("frame",)
        ftype = frame.get("type")
        if ftype == "RAW":
            self.add("L9", line, base + ("type",), ftype)
        if ftype == "CUSTOM" and frame.get("name") != CAO_GENERATIVE_UI:
            self.add("L9", line, base + ("name",), frame.get("name"))
        if ftype == "STATE_DELTA":
            if "metadata" in frame:
                self.add("L10", line, base + ("metadata",), frame["metadata"])
            for i, op in enumerate(frame.get("delta") or []):
                if isinstance(op, dict) and op.get("path") == "/last_file_mod":
                    self.add("L10", line, base + ("delta", i, "path"), op.get("path"))

    def request(self, line: int, request: Any) -> None:
        base = ("request",)
        if not isinstance(request, dict):
            self.add("L12", line, base, request)
            return
        for key in sorted(set(request) - REQUEST_KEYS):
            self.add("L12", line, base + (key,), request[key])
        for i, entry in enumerate(request.get("resume") or []):
            if not isinstance(entry, dict):
                self.add("L12", line, base + ("resume", i), entry)
                continue
            for key in sorted(set(entry) - RESUME_ENTRY_KEYS):
                self.add("L12", line, base + ("resume", i, key), entry[key])
            payload = entry.get("payload")
            if isinstance(payload, dict):
                for key in sorted(set(payload) - RESUME_PAYLOAD_KEYS):
                    self.add("L12", line, base + ("resume", i, "payload", key), payload[key])

    def record_keys(self, line: int, record: dict) -> None:
        allowed = RECORD_KEYS.get((record.get("plane"), record.get("dir")))
        if allowed is None:
            self.add("L13", line, ("plane",), f"{record.get('plane')}/{record.get('dir')}")
            return
        for key in sorted(set(record) - allowed):
            self.add("L13", line, (key,), record[key])


def check(data: bytes | str) -> List[Finding]:
    """Return every leak finding in a normalised trace. Empty means clean."""
    meta, records = parse(data)
    checker = _Checker()

    for key in sorted(set(meta) - META_KEYS):
        checker.add("L13", 1, ("_meta", key), meta[key])
    for path, value in _iter_values(meta, ("_meta",)):
        if isinstance(value, str):
            checker.strings(1, path, value, in_meta=True)

    for index, record in enumerate(records, start=2):
        checker.record_keys(index, record)
        if "frame" in record:
            checker.frame(index, record["frame"])
        if "request" in record:
            checker.request(index, record["request"])
        for path, value in _iter_values(record, ()):
            checker.scoped(index, _logical(record, path), value, path)
            if isinstance(value, str):
                checker.strings(index, path, value, in_meta=False)

    return checker.findings


def format_findings(findings: Iterable[Finding]) -> str:
    lines = [str(f) for f in findings]
    lines.append(
        "the scrubber missed a field; fix examples/ag-ui/cao-dojo/tools/trace_scrub.py, "
        "then re-record"
    )
    return "\n".join(lines)
