"""Every copy of the relay's tool wording names Kiro Crew's ``wait`` tool beside the four CAO tools.

The relay chat is offered five tools: the four ``cao-ops-mcp`` tools and Kiro Crew's ``wait``, which
only pauses the chat. The texts that say what the chat is offered, or what never asks, live in
several files held together only by tests, so this tripwire reads all of them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "ui/src/chat.ts",
    "ui/src/components/RelayControls.tsx",
    "backend/cao_shelf/relays.py",
    "api-contract.md",
    "README.md",
    "ui/bundle/index.mjs",
)
# a phrase naming the four tools must be followed by "wait" within this many characters, after an
# optional parenthetical that lists the four names
WINDOW = 40


def _text(rel: str) -> str:
    raw = (ROOT / rel).read_text("utf-8")
    # Python and TS split long strings across lines; join adjacent literals so a phrase is whole
    joined = re.sub(r"\"\s*\n\s*\"|'\s*\+\s*\n\s*'", "", raw)
    return re.sub(r"\s+", " ", joined)  # Markdown wraps prose at any space


def _misses(text: str) -> list[str]:
    misses = []
    for m in re.finditer(r"four (?:CAO|ops) tools", text):
        rest = re.sub(r"^\s*\([^)]*\)", "", text[m.end() :])
        if "wait" not in rest[:WINDOW]:
            misses.append(text[max(0, m.start() - 30) : m.end() + WINDOW])
    return misses


@pytest.mark.parametrize("rel", FILES)
def test_four_tools_is_always_followed_by_wait(rel: str) -> None:
    assert _misses(_text(rel)) == []


def test_the_tripwire_catches_a_copy_without_wait() -> None:
    assert _misses("the relay's four CAO tools never ask on kas") != []
    assert _misses("its four CAO tools (a, b, c and d) and Kiro Crew's wait tool") == []
    assert _misses("the four ops tools and `wait` never ask") == []


@pytest.mark.parametrize("rel", ("README.md", "api-contract.md"))
def test_the_pending_request_advice_counts_five_tools(rel: str) -> None:
    text = _text(rel)
    assert "one of the four." not in text
    assert "one of those five" in text
