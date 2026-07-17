"""Shared parsing rules for canonical memory topics and index entries."""

from __future__ import annotations

import re
from typing import Match, Optional

TOPIC_HEADER_RE = re.compile(
    r"^<!-- id: (?P<id>[a-f0-9-]+) \| scope: (?P<scope>[^|]+) "
    r"\| type: (?P<type>[^|]+) \| tags: (?P<tags>.*?) -->$"
)

INDEX_ENTRY_RE = re.compile(
    r"^- \[(?P<key>[^\]]+)\]\((?P<path>[^)]+)\) — "
    r"type:(?P<type>\S+) tags:(?P<tags>.*?) ~(?P<tokens>\d+)tok "
    r"updated:(?P<updated>\S+)$"
)


def normalize_memory_tags(tags: str) -> str:
    """Return the canonical comma-separated representation without losing tag text."""
    return ",".join(part.strip() for part in tags.split(",") if part.strip())


def parse_index_entry(line: str) -> Optional[Match[str]]:
    """Parse one complete index entry using the canonical field delimiters."""
    return INDEX_ENTRY_RE.fullmatch(line)
