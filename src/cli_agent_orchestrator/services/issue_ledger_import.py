"""Import a markdown issue ledger into the tracker.

Written for the cao-conductor self-heal ledger (`OPEN_ISSUES.md` /
`CLOSED_ISSUES.md`, ~208 entries across ~4,900 lines), whose entries look like:

    ## cond-0039 — [P2] event-mirror lock contention logs full traceback

    - **filed:** 2026-07-21T17:01:14Z
    - **reporter:** 13e6fe47
    - **status:** open
    - **failing command:** `...`
    - **evidence:** /path/to/report.md

    Prose body...

    ---

Two rules govern the import, and both exist because a migration that quietly
reinterprets its source is worse than one that refuses:

*Nothing is dropped.* Fields this parser does not recognise are preserved
verbatim in a trailer appended to the body rather than discarded — there are
twenty-odd one-off field names in that ledger (`affected heads:`,
`preserved run:`, `promoted:`) and each was written because it mattered once.

*The file decides terminality, the prose decides detail.* A status line in
that ledger is free text — "fixed, merged, deployed, and live-accepted on
2026-07-29" — so it cannot be parsed into a state machine. Which FILE an entry
sits in is unambiguous, so that carries the open/closed decision and the prose
is preserved as the resolution. The one exception is a status line that starts
with `open` or `deferred`, which states the answer outright.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from cli_agent_orchestrator.services import issue_tracker as tracker
from cli_agent_orchestrator.services.issue_tracker import TrackerError

# `## cond-0039 — [P2] title`, `## cond-0200 [P2] — title`, or a plain hyphen
# instead of the em dash. The severity sits on either side of the dash in the
# real ledger — exactly one of 208 entries uses the second form, which is the
# kind of detail a migration validated only against its own fixtures misses.
_HEADING = re.compile(
    r"^##\s+([a-z][a-z0-9-]{0,15}-\d{1,9})\s*(?:\[(P[0-4])\]\s*)?[—–-]\s*(.+?)\s*$",
    re.IGNORECASE,
)
# The ledger spells severity four ways: `[P2] title`, `P2: title`, `P1 title`,
# and a `- **severity:**` field. All four are in live use, and `P0` appears
# alongside P1-P4 for the most severe class.
_SEVERITY_PREFIX = re.compile(r"^(?:\[(P[0-4])\]|(P[0-4])[:\s])\s*(.+)$")
_FIELD = re.compile(r"^-\s+\*\*(.+?):\*\*\s*(.*)$")

# A leading ISO-ish date/time, with whatever prose the author appended after
# it. Seven entries write `2026-07-21 (external adjudicator)` — parsing that
# as a whole timestamp fails, and a failed parse that silently falls back to
# "now" would restamp a year-old defect as filed today.
_STAMP_HEAD = re.compile(
    r"^\s*(\d{4}-\d{2}-\d{2}"
    r"(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?)\s*(.*)$"
)

# Field names this parser understands. Everything else is preserved verbatim.
_KNOWN = {
    "filed": "filed",
    "reporter": "reporter",
    "status": "status",
    "failing command": "failing_command",
    "evidence": "evidence",
    "component": "component",
    "severity": "severity",
    "assignee": "assignee",
}

# Values that mean "this field was deliberately left empty".
_EMPTY_MARKERS = {"(none given)", "(none)", "none", "n/a", "-", ""}


def _clean(value: str) -> Optional[str]:
    text = value.strip().strip("`").strip()
    if text.lower() in _EMPTY_MARKERS:
        return None
    return text or None


def _parse_stamp(value: str) -> Tuple[Optional[datetime], Optional[str]]:
    """Parse a ledger `filed:` stamp into ``(timestamp, trailing prose)``.

    The ledger writes explicit-UTC `Z` stamps, sometimes with an author note
    appended (`2026-07-21 (external adjudicator)`). A stamp without a zone is
    read as UTC rather than local: these entries are written by
    `conduct issue file` from `now_utc()`, so UTC is what they meant.

    The trailing prose is returned rather than discarded — in every case it
    names who filed the entry, and those are exactly the entries with no
    `reporter:` field.
    """
    text = value.strip()
    if not text:
        return None, None
    head = _STAMP_HEAD.match(text)
    if not head:
        return None, text
    stamp, remainder = head.group(1), head.group(2).strip()
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None, text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed, (remainder or None)


def parse_ledger(text: str) -> List[Dict[str, Any]]:
    """Parse a ledger into raw entry dicts. Pure; touches no database."""
    entries: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    body_lines: List[str] = []
    # A `- **x:**` line counts as metadata only inside the leading block. The
    # same shape appears mid-paragraph in real bodies, and hoisting one of
    # those out would delete the sentence it belonged to. The block ends at the
    # first line that is neither blank nor a field.
    in_metadata = False

    for line in text.splitlines():
        heading = _HEADING.match(line)
        if heading:
            if current is not None:
                current["body_lines"] = body_lines
                entries.append(current)
            key, title = heading.group(1).lower(), heading.group(3).strip()
            severity = (heading.group(2) or "").upper() or None
            severity_match = _SEVERITY_PREFIX.match(title)
            if severity_match:
                severity = severity_match.group(1) or severity_match.group(2)
                title = severity_match.group(3).strip()
            current = {"key": key, "title": title, "severity": severity, "fields": {}, "extra": []}
            body_lines = []
            in_metadata = True
            continue

        if current is None:
            continue  # preamble before the first heading

        if in_metadata:
            if not line.strip():
                continue  # blank lines around the metadata block
            field = _FIELD.match(line)
            if field:
                name = field.group(1).strip().lower()
                value = field.group(2).strip()
                if name in _KNOWN:
                    current["fields"][_KNOWN[name]] = value
                else:
                    current["extra"].append((field.group(1).strip(), value))
                continue
            in_metadata = False

        if line.strip() == "---":
            continue

        body_lines.append(line)

    if current is not None:
        current["body_lines"] = body_lines
        entries.append(current)

    return entries


def _resolve_status(
    prose: Optional[str], default_status: str
) -> Tuple[str, List[str], Optional[str]]:
    """Map a free-text ledger status onto (status, labels, resolution)."""
    if not prose:
        return default_status, [], None
    lowered = prose.strip().lower()
    if lowered == "open":
        return "open", [], None
    if lowered.startswith("open"):
        # "open; deferred from the pre-chess P0/P1 gate" — still open, and the
        # qualifier is the part somebody will want to read.
        return "open", [], prose.strip()
    if lowered.startswith("deferred"):
        # Deferring is a scheduling decision, not a resolution. Calling it
        # `wontfix` would retire three live defects by transcription error.
        return "open", ["deferred"], prose.strip()
    return default_status, [], prose.strip()


def build_issue(
    entry: Dict[str, Any],
    *,
    project_id: str,
    default_status: str,
    component: Optional[str] = None,
) -> Dict[str, Any]:
    """Turn one parsed entry into `create_issue` keyword arguments."""
    fields = entry["fields"]
    status, labels, resolution = _resolve_status(fields.get("status"), default_status)
    filed_at, filed_note = _parse_stamp(fields.get("filed", "") or "")

    preserved_fields = list(entry["extra"])
    if filed_note:
        preserved_fields.append(("filed note", filed_note))
    if fields.get("filed") and filed_at is None:
        # Never let an unreadable stamp fall through to "filed today": say so.
        preserved_fields.append(("filed (unparsed)", fields["filed"]))
    elif not fields.get("filed"):
        # One real entry has no `filed:` line at all. It will be stamped with
        # the import time, so record that the date is the migration's, not the
        # author's.
        preserved_fields.append(("filed", "absent in the markdown ledger"))

    body = "\n".join(entry["body_lines"]).strip()
    if preserved_fields:
        preserved = "\n".join(f"- **{name}:** {value}" for name, value in preserved_fields)
        body = f"{body}\n\n<!-- preserved from the markdown ledger -->\n{preserved}".strip()

    severity = entry["severity"] or _clean(fields.get("severity", "") or "") or "unset"
    if severity not in tracker.SEVERITIES:
        severity = "unset"

    return {
        "project_id": project_id,
        "key": entry["key"],
        "title": entry["title"][: tracker.MAX_TITLE],
        "body": body,
        "status": status,
        "severity": severity,
        "component": component or _clean(fields.get("component", "") or ""),
        "reporter": _clean(fields.get("reporter", "") or ""),
        "assignee": _clean(fields.get("assignee", "") or ""),
        "labels": labels,
        "failing_command": _clean(fields.get("failing_command", "") or ""),
        "evidence": _clean(fields.get("evidence", "") or ""),
        "created_at": filed_at,
        "origin": "migration",
        "_resolution": resolution,
    }


def import_ledger(
    path: str,
    *,
    project_id: str,
    default_status: str = "open",
    component: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Import one ledger file, skipping entries whose key already exists.

    Re-running is safe: an existing key is reported as skipped rather than
    overwritten, so a partially-completed import can simply be repeated.
    """
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()

    entries = parse_ledger(text)
    imported = 0
    skipped = 0
    notes: List[str] = []

    for entry in entries:
        payload = build_issue(
            entry, project_id=project_id, default_status=default_status, component=component
        )
        resolution = payload.pop("_resolution")
        if dry_run:
            imported += 1
            continue
        try:
            created = tracker.create_issue(**payload)
        except TrackerError as exc:
            if exc.code == "conflict":
                skipped += 1
                notes.append(f"{payload['key']}: already present, left untouched")
                continue
            raise
        if resolution:
            tracker.update_issue(created["key"], actor="ledger-import", resolution=resolution)
        imported += 1

    return {
        "path": path,
        "project_id": project_id,
        "parsed": len(entries),
        "imported": imported,
        "skipped": skipped,
        "dry_run": dry_run,
        "notes": notes,
    }
