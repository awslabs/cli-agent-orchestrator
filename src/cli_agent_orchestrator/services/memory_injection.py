"""Pure helpers for rendering stamped CAO memory injection blocks."""

from __future__ import annotations

import re
from datetime import datetime, timezone

_OPENING_TAG_RE = re.compile(r"<cao-memory(?:\s[^>]*)?>")
_BLOCK_RE = re.compile(r"<cao-memory(?:\s[^>]*)?>(.*?)</cao-memory>", re.DOTALL)


def _generated_at() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def render_memory_block(body: str, *, generated_at: str | None = None) -> str:
    """Wrap ``body`` in a memory block stamped with its UTC build time."""
    timestamp = generated_at or _generated_at()
    return f'<cao-memory generated="{timestamp}">\n{body}\n</cao-memory>'


def empty_memory_block(*, generated_at: str | None = None) -> str:
    """Return the explicit tombstone used to replace stale provider context."""
    return render_memory_block("<!-- no memories selected -->", generated_at=generated_at)


def stamp_memory_block(block: str, *, generated_at: str | None = None) -> str:
    """Add or refresh ``generated`` on the first memory opening tag.

    Provider plugins may receive a block from a remote owner running an older
    CAO version, so the final writer enforces the stamp as well as the local
    deterministic builder.
    """
    timestamp = generated_at or _generated_at()
    return _OPENING_TAG_RE.sub(f'<cao-memory generated="{timestamp}">', block, count=1)


def memory_blocks_have_same_body(existing: str, proposed: str) -> bool:
    """Compare memory bodies while ignoring opening-tag metadata."""
    existing_match = _BLOCK_RE.search(existing)
    proposed_match = _BLOCK_RE.search(proposed)
    return (
        existing_match is not None
        and proposed_match is not None
        and existing_match.group(1) == proposed_match.group(1)
    )


def uniquely_delimited_block(content: str, begin_marker: str, end_marker: str) -> str | None:
    """Return one well-formed managed section, rejecting ambiguous files."""
    if content.count(begin_marker) != 1 or content.count(end_marker) != 1:
        return None
    begin = content.find(begin_marker) + len(begin_marker)
    end = content.find(end_marker, begin)
    return content[begin:end] if end >= begin else None
