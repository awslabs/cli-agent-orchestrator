"""Normalise raw CAO Dojo captures into the committed trace format (#1116).

Pure functions only: no I/O, no clock, no randomness. The recorder
(``record_trace.py``) collects raw frames from a live server and hands them
here; the tests call the same functions on synthetic captures.

The trace is NDJSON. Line 1 is ``{"_meta": {...}}``; every later line is one
record::

    {"capture": "approve", "seq": 3, "plane": "run", "run": "approve-run-1",
     "dir": "out", "frame": {...}}

Normalisation, in order:

1. **Drop** ``RAW`` frames and the run plane's re-wrapped ``CUSTOM`` frames
   ``cao.raw`` and ``cao.message_delivery``, and a top-level ``rawEvent``
   (upstream's Dojo normaliser drops it too, ``event-trace-events.ts:296``).
2. **Reduce requests** to ``{"resume": [...]}``, keeping only ``interruptId``,
   ``status`` and ``payload.approved`` per entry.
3. **Remap identities** first-seen, across the whole file, in record order with
   keys walked in sorted order. Generated ids (event ids, interrupt ids,
   ``traceparent``, and AG-UI 1.0's ``eventId``, ``subagentRunId`` and
   ``parentSubagentRunId``) become ``id-N``; CAO's named identities get kind-prefixed
   tokens (``terminal-N``, ``session-N``, ``profile-N``, ``window-N``,
   ``runner-N``) so the
   leak rule can type them. One table per kind for the whole file, so a card's
   ``props.interrupt_id`` and run 1's ``interrupts[0].id`` map to the same token.
4. **Synthesise timestamps** from a monotone clock starting at
   ``2026-01-01T00:00:00Z``, one second per timestamp field in walk order. An
   ISO string stays an ISO string and an integer stays an integer, so order and
   type survive and timing jitter does not.
5. **Number** each capture's records from 1 (``seq``).

The output is canonical JSON (``sort_keys``, compact separators, ``\\n``), so
the same capture always gives the same bytes, and ``normalise`` is idempotent.

``shape()`` projects a normalised trace onto what tier 2 compares: the record
sequence, each record's key-path set, and a closed set of values. Everything
that legitimately differs between a ``--live`` (Claude Code) and a ``--mock``
(mock_cli) recording is outside the projection, so both modes must produce
identical shape bytes.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# --- Fixed recorder constants --------------------------------------------------
# The leak rule's positive controls import these, so a rule written too wide
# (for example a `cao-` check that catches THREAD_ID) fails its own test.

SCHEMA = "cao-dojo-trace/1"
SHAPE_SCHEMA = "cao-dojo-trace-shape/1"
THREAD_ID = "cao-dojo-trace"
CAPTURES: Tuple[str, ...] = ("none", "approve", "reject")
CLOCK_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)
CAO_GENERATIVE_UI = "cao.generative_ui"

# Frames the run plane re-wraps from the fleet bus. Never recorded.
DROPPED_CUSTOM_NAMES = frozenset({"cao.raw", "cao.message_delivery"})

# Every key a `_meta` line may carry. Closed: L13 fails on anything else.
META_KEYS = frozenset(
    {
        "schema",
        "cao_version",
        "cao_build",
        "cao_build_dirty",
        "ag_ui_protocol",
        "protocol_version",
        "mode",
        "provider",
        "provider_version",
        "captures",
        "shape_sha256",
    }
)


def run_id(capture: str, n: int) -> str:
    """The fixed run id the recorder sends for run ``n`` of ``capture``."""
    return f"{capture}-run-{n}"


RUN_IDS: Tuple[str, ...] = tuple(run_id(c, n) for c in CAPTURES for n in (1, 2))

# --- Identity classes ----------------------------------------------------------

# ``eventId``, ``subagentRunId`` and ``parentSubagentRunId`` are AG-UI 1.0's
# per-run identities (event metadata and subagent attribution).
ID_KEYS = frozenset(
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
TERMINAL_KEYS = frozenset({"terminal_id", "terminalId", "step_id"})
SESSION_KEYS = frozenset({"session_name", "sessionName"})
PROFILE_KEYS = frozenset({"agent_profile", "step_name"})
WINDOW_KEYS = frozenset({"window", "tmux_window"})
# A runner id is a label an operator chose and can look like a host name. ``null`` (a
# local terminal, the only kind on main) stays ``null``; a string becomes ``runner-N``.
RUNNER_KEYS = frozenset({"runner_id", "runnerId"})
TIMESTAMP_KEYS = frozenset({"timestamp", "last_active", "expiresAt"})
# The two ends of a message: a terminal id wherever a frame carries it (``metadata.cao`` on
# an attributed frame, the value of ``cao.message_dispatched``). ``null`` stays ``null``.
PARTY_KEYS = frozenset(
    (
        "sender",
        "receiver",
    )
)
# Free-form per-run identities a custom driver generates (the recorder pins its own). They
# go through the ``id`` table, except the values the recorder itself sends, which are part of
# the trace's shape and stay as they are.
FREE_ID_KEYS = frozenset(
    (
        "threadId",
        "runId",
        "toolCallId",
    )
)

TOKEN_PREFIX = {
    "id": "id",
    "terminal": "terminal",
    "session": "session",
    "profile": "profile",
    "window": "window",
    "runner": "runner",
}
TOKEN_RE = re.compile(r"^(id|terminal|session|profile|window|runner)-\d+$")


def classify(path: Tuple[Any, ...]) -> Optional[str]:
    """Return the identity class of the value at ``path`` (key names only).

    ``path`` is the tuple of keys and list indexes from the record root. The
    class decides which token table the value is remapped through.
    """
    if not path:
        return None
    key = path[-1]
    parent = path[-2] if len(path) >= 2 else None
    grandparent = path[-3] if len(path) >= 3 else None
    if isinstance(key, int):
        return None
    if key in ID_KEYS:
        return "id"
    if key in TERMINAL_KEYS:
        return "terminal"
    if key in SESSION_KEYS:
        return "session"
    if key in PROFILE_KEYS:
        return "profile"
    if key in WINDOW_KEYS:
        return "window"
    if key in RUNNER_KEYS:
        return "runner"
    if key in PARTY_KEYS and path[0] == "frame":
        return "terminal"
    # Positional identities: snapshot rows and interrupts.
    if isinstance(parent, int) and grandparent == "terminals" and key == "id":
        return "terminal"
    if isinstance(parent, int) and grandparent == "sessions" and key in ("id", "name"):
        return "session"
    if isinstance(parent, int) and grandparent == "interrupts" and key == "id":
        return "id"
    return None


def snapshot_path(record: Mapping[str, Any], path: Tuple[Any, ...]) -> Tuple[Any, ...]:
    """Map a STATE_DELTA op's value onto the snapshot path it patches.

    ``frame.delta[i].value`` under an op with ``path: "/terminals"`` is held to the same rules
    as ``frame.snapshot.terminals``, so a patch cannot carry what a snapshot may not. The
    scrubber classifies through this and the leak rule reads through it too (one mapping, so
    the two cannot drift).
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


class _Tables:
    """First-seen remap tables, one per identity class, plus the clock."""

    def __init__(self) -> None:
        self.tables: Dict[str, Dict[str, str]] = {k: {} for k in TOKEN_PREFIX}
        self.ticks = 0
        # The record being walked, so a STATE_DELTA value is classified by the path it patches.
        self.record: Mapping[str, Any] = {}

    def token(self, kind: str, value: str) -> str:
        table = self.tables[kind]
        if value not in table:
            table[value] = f"{TOKEN_PREFIX[kind]}-{len(table) + 1}"
        return table[value]

    def tick(self, value: Any) -> Any:
        moment = CLOCK_EPOCH + timedelta(seconds=self.ticks)
        self.ticks += 1
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return int(moment.timestamp() * 1000)
        if isinstance(value, float):
            return float(int(moment.timestamp() * 1000))
        return moment.isoformat(timespec="microseconds")


def _walk(value: Any, path: Tuple[Any, ...], tables: _Tables) -> Any:
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for key in sorted(value):
            out[key] = _walk(value[key], path + (key,), tables)
        return out
    if isinstance(value, list):
        return [_walk(item, path + (i,), tables) for i, item in enumerate(value)]
    if value is None:
        return None
    key = path[-1] if path else None
    if isinstance(key, str) and key in TIMESTAMP_KEYS and isinstance(value, (str, int, float)):
        return tables.tick(value)
    if isinstance(key, str) and key in FREE_ID_KEYS and isinstance(value, str):
        if value == THREAD_ID or value in RUN_IDS:
            return value
        return tables.token("id", value)
    kind = classify(snapshot_path(tables.record, path))
    if kind is not None and isinstance(value, str):
        return tables.token(kind, value)
    return value


def _keep_frame(frame: Mapping[str, Any]) -> bool:
    ftype = frame.get("type")
    if ftype == "RAW":
        return False
    if ftype == "CUSTOM" and frame.get("name") in DROPPED_CUSTOM_NAMES:
        return False
    return True


def reduce_request(request: Mapping[str, Any]) -> Dict[str, Any]:
    """Keep only what a resume needs. The recorder never sends anything typed."""
    entries = []
    for entry in request.get("resume") or []:
        reduced: Dict[str, Any] = {
            "interruptId": entry.get("interruptId"),
            "status": entry.get("status"),
        }
        payload = entry.get("payload")
        if isinstance(payload, dict) and "approved" in payload:
            reduced["payload"] = {"approved": payload["approved"]}
        entries.append(reduced)
    return {"resume": entries}


def normalise_records(raw: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Normalise raw records (no `_meta`) into trace records, in order."""
    tables = _Tables()
    out: List[Dict[str, Any]] = []
    seq: Dict[str, int] = {}
    for record in raw:
        record = copy.deepcopy(dict(record))
        record.pop("seq", None)
        if "frame" in record:
            frame = dict(record["frame"])
            if not _keep_frame(frame):
                continue
            frame.pop("rawEvent", None)
            record["frame"] = frame
        if "request" in record:
            record["request"] = reduce_request(record["request"])
        capture = record["capture"]
        seq[capture] = seq.get(capture, 0) + 1
        record["seq"] = seq[capture]
        tables.record = record
        out.append(_walk(record, (), tables))
    return out


def dumps(obj: Any) -> str:
    """Canonical one-line JSON, the trace's only serialisation."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def normalise(meta: Mapping[str, Any], raw: Iterable[Mapping[str, Any]]) -> bytes:
    """Return the trace bytes for ``meta`` plus the normalised ``raw`` records."""
    lines = [dumps({"_meta": dict(meta)})]
    lines.extend(dumps(r) for r in normalise_records(raw))
    return ("\n".join(lines) + "\n").encode("utf-8")


def parse(data: bytes | str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Split trace bytes into ``(meta, records)``."""
    text = data.decode("utf-8") if isinstance(data, bytes) else data
    lines = [line for line in text.split("\n") if line.strip()]
    if not lines:
        raise ValueError("empty trace")
    first = json.loads(lines[0])
    if set(first) != {"_meta"}:
        raise ValueError('line 1 must be {"_meta": {...}}')
    return first["_meta"], [json.loads(line) for line in lines[1:]]


def renormalise(data: bytes) -> bytes:
    """``normalise`` applied to an already-normalised trace (idempotence check)."""
    meta, records = parse(data)
    return normalise(meta, records)


# --- Shape projection (the tier 2 baseline) ------------------------------------

# Values compared by tier 2, as (record-relative) key paths with list indexes
# collapsed to `[]`. Everything else is compared by key path only.
CLOSED_VALUE_PATHS = frozenset(
    {
        "capture",
        "plane",
        "dir",
        "run",
        "frame.type",
        "frame.name",
        "frame.protocolVersion",
        "frame.threadId",
        "frame.runId",
        "frame.outcome.type",
        "frame.outcome.interrupts[].responseSchema",
        "frame.outcome.interrupts[].metadata.options",
        "frame.value.component",
        "frame.value.props.options",
        "request.resume[].status",
        "request.resume[].payload.approved",
    }
)

# `_meta` keys whose values are part of the shape. The rest differ between
# modes or between releases (`cao_version`), which is not drift.
META_SHAPE_VALUES = ("schema", "protocol_version", "captures")
# `_meta` keys never part of the shape: mode-dependent, or the shape's own hash.
META_SHAPE_EXCLUDED = frozenset(
    {"mode", "provider", "provider_version", "cao_build", "cao_build_dirty", "shape_sha256"}
)

# The allowed live/mock differences. Closed. They are never closed
# values above, so the projection drops their values; this tuple exists so a
# test can assert exactly that, and so the list is written down once.
ALLOWED_DIFFERENCES = (
    "reason",
    "provider",
    "metadata.provider",
    "props.provider",
    "props.reason",
    "_meta.mode",
    "_meta.provider",
    "_meta.provider_version",
    "_meta.cao_build",
    "_meta.cao_build_dirty",
)


def _paths(value: Any, prefix: str, out: Dict[str, Any]) -> None:
    if isinstance(value, dict):
        if not value:
            out[prefix] = {}
        for key in sorted(value):
            _paths(value[key], f"{prefix}.{key}" if prefix else key, out)
        return
    if isinstance(value, list):
        if not value:
            out[prefix] = []
        for i, item in enumerate(value):
            _paths(item, f"{prefix}[{i}]", out)
        return
    out[prefix] = value


_INDEX_RE = re.compile(r"\[\d+\]")


def _collapse(path: str) -> str:
    return _INDEX_RE.sub("[]", path)


def _closed_prefix(path: str) -> Optional[str]:
    collapsed = _collapse(path)
    for closed in CLOSED_VALUE_PATHS:
        if (
            collapsed == closed
            or collapsed.startswith(closed + ".")
            or (collapsed.startswith(closed + "["))
        ):
            return closed
    return None


def record_shape(record: Mapping[str, Any]) -> Dict[str, Any]:
    flat: Dict[str, Any] = {}
    _paths(record, "", flat)
    values = {p: v for p, v in flat.items() if _closed_prefix(p) is not None}
    return {"paths": sorted(flat), "values": values}


def shape(data: bytes) -> Dict[str, Any]:
    """Project a normalised trace onto what tier 2 compares."""
    meta, records = parse(data)
    meta_keys = sorted(k for k in meta if k not in META_SHAPE_EXCLUDED)
    return {
        "schema": SHAPE_SCHEMA,
        "meta": {
            "keys": meta_keys,
            "values": {k: meta[k] for k in META_SHAPE_VALUES if k in meta},
        },
        "records": [record_shape(r) for r in records],
    }


def shape_bytes(data: bytes) -> bytes:
    """The shape file's bytes: indented for review diffs, otherwise canonical."""
    text = json.dumps(shape(data), sort_keys=True, indent=1, ensure_ascii=False)
    return (text + "\n").encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def first_shape_difference(committed: Mapping[str, Any], live: Mapping[str, Any]) -> Optional[str]:
    """Describe the first difference between two shapes, or ``None`` if equal."""
    if committed.get("schema") != live.get("schema"):
        return f"shape schema {committed.get('schema')!r} != {live.get('schema')!r}"
    if committed.get("meta") != live.get("meta"):
        return f"_meta: committed {committed.get('meta')} live {live.get('meta')}"
    crec: Sequence[Mapping[str, Any]] = committed.get("records") or []
    lrec: Sequence[Mapping[str, Any]] = live.get("records") or []
    for i in range(max(len(crec), len(lrec))):
        if i >= len(crec):
            return f"record {i + 1}: live-only record {_label(lrec[i])}"
        if i >= len(lrec):
            return f"record {i + 1}: committed-only record {_label(crec[i])}"
        c, l_ = crec[i], lrec[i]
        if c == l_:
            continue
        label = _label(l_)
        live_only = sorted(set(l_["paths"]) - set(c["paths"]))
        committed_only = sorted(set(c["paths"]) - set(l_["paths"]))
        if live_only:
            return f"{label}: key {live_only[0]} live-only"
        if committed_only:
            return f"{label}: key {committed_only[0]} committed-only"
        for key in sorted(set(c["values"]) | set(l_["values"])):
            if c["values"].get(key) != l_["values"].get(key):
                return (
                    f"{label}: value at {key}: committed {c['values'].get(key)!r} "
                    f"live {l_['values'].get(key)!r}"
                )
    return None


def _label(rec_shape: Mapping[str, Any]) -> str:
    v = rec_shape.get("values", {})
    parts = [str(v.get("capture", "?"))]
    if "run" in v:
        parts.append(str(v["run"]))
    else:
        parts.append(str(v.get("plane", "?")))
    parts.append(str(v.get("frame.type", v.get("dir", ""))))
    return " ".join(parts)
