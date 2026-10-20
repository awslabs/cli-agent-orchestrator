"""Shared append-only section rendering for native and vault memories."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

_SECTION_TIMESTAMP_RE = re.compile(r"## (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)")
_SECTION_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class MemoryAppendEntry:
    """One caller-supplied entry plus the fixed clock used to render it."""

    content: str
    occurred_at: Optional[datetime]
    now: datetime


@dataclass(frozen=True)
class MemoryAppendResult:
    """Rendered append and the timestamp decisions used to produce it."""

    content: str
    entry_body: str
    section_at: datetime
    first_section_at: datetime
    timestamp_clamped: bool


def normalize_utc(value: datetime) -> datetime:
    """Return an aware UTC datetime without changing its instant."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def occurred_at_would_clamp(
    occurred_at: Optional[datetime],
    latest_section_at: Optional[datetime],
    now: datetime,
) -> bool:
    """Apply the established D5 future/out-of-order clamp rule."""
    if occurred_at is None:
        return False
    normalized_occurred_at = normalize_utc(occurred_at)
    normalized_now = normalize_utc(now)
    normalized_latest = normalize_utc(latest_section_at) if latest_section_at is not None else None
    return normalized_occurred_at > normalized_now or (
        normalized_latest is not None and normalized_occurred_at < normalized_latest
    )


def append_section(existing_body: str, entry: MemoryAppendEntry) -> MemoryAppendResult:
    """Append one ISO section without composing from a stale caller-side body."""
    now = normalize_utc(entry.now)
    occurred_at = normalize_utc(entry.occurred_at) if entry.occurred_at is not None else None
    existing_timestamps = _SECTION_TIMESTAMP_RE.findall(existing_body)
    first_existing = (
        datetime.strptime(existing_timestamps[0], _SECTION_TIMESTAMP_FORMAT).replace(
            tzinfo=timezone.utc
        )
        if existing_timestamps
        else None
    )
    latest_existing = (
        datetime.strptime(existing_timestamps[-1], _SECTION_TIMESTAMP_FORMAT).replace(
            tzinfo=timezone.utc
        )
        if existing_timestamps
        else None
    )
    timestamp_clamped = occurred_at_would_clamp(occurred_at, latest_existing, now)
    section_at = occurred_at if occurred_at is not None and not timestamp_clamped else now
    entry_body = entry.content
    if timestamp_clamped:
        assert occurred_at is not None
        original_iso = occurred_at.strftime(_SECTION_TIMESTAMP_FORMAT)
        entry_body = f"_Originally recorded: {original_iso}_\n{entry.content}"

    section = f"## {section_at.strftime(_SECTION_TIMESTAMP_FORMAT)}\n{entry_body}\n"
    retained = existing_body.rstrip("\n")
    rendered = f"{retained}\n\n{section}" if retained else section
    return MemoryAppendResult(
        content=rendered,
        entry_body=entry_body,
        section_at=section_at,
        first_section_at=first_existing or section_at,
        timestamp_clamped=timestamp_clamped,
    )
