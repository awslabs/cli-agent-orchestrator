"""Pure helpers for rendering stamped CAO memory injection blocks."""

from __future__ import annotations

import re
from datetime import datetime, timezone

_OPENING_TAG_RE = re.compile(r"<cao-memory(?:\s[^>]*)?>")


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
