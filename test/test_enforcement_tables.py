"""SECURITY.md and docs/tool-restrictions.md must agree with utils/enforcement.py.

The two tables are what an operator reads before trusting a Blocked list; the
module is what the launch gate and the server act on. This test is the only
thing that keeps them the same.
"""

import re
from pathlib import Path

import pytest

from cli_agent_orchestrator.constants import PROVIDERS
from cli_agent_orchestrator.utils.enforcement import NATIVE, NONE, PROMPT, PROVIDER_ENFORCEMENT

REPO = Path(__file__).resolve().parents[1]

# Display name as written in the docs tables -> provider id.
DISPLAY_TO_PROVIDER = {
    "Claude Code": "claude_code",
    "Kiro CLI": "kiro_cli",
    "Copilot CLI": "copilot_cli",
    "OpenCode CLI": "opencode_cli",
    "Grok Build CLI": "grok_cli",
    "Kimi CLI": "kimi_cli",
    "Codex": "codex",
    "Antigravity CLI": "antigravity_cli",
    "OMP": "omp",
    "MiniMax Code": "mcode",
    "Hermes": "hermes",
    "Cursor CLI": "cursor_cli",
}


def _level_from_label(label: str) -> str:
    head = label.strip().lower()
    if head.startswith(("hard", "native")):
        return NATIVE
    if head.startswith("soft"):
        return PROMPT
    if head.startswith(("none", "not enforced", "profile-defined")):
        return NONE
    raise AssertionError(f"unrecognised enforcement label {label!r}")


def _table_rows(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    start = text.index("| Provider | Enforcement |")
    rows = {}
    for line in text[start:].splitlines()[2:]:
        if not line.startswith("|"):
            break
        cells = [c.strip().strip("*") for c in line.strip("|").split("|")]
        rows[cells[0]] = cells[1]
    return rows


@pytest.mark.parametrize("doc", ["SECURITY.md", "docs/tool-restrictions.md"])
def test_docs_table_matches_the_code(doc):
    rows = _table_rows(REPO / doc)
    seen = set()
    for display, label in rows.items():
        provider = DISPLAY_TO_PROVIDER[display]
        seen.add(provider)
        assert _level_from_label(label) == PROVIDER_ENFORCEMENT[provider], (doc, display, label)
    expected = set(PROVIDERS) - {"mock_cli"}
    assert seen == expected, (doc, sorted(expected - seen), sorted(seen - expected))


def test_every_registered_provider_has_an_enforcement_level():
    assert set(PROVIDER_ENFORCEMENT) == set(PROVIDERS)


def test_no_enforcement_providers_are_the_documented_ones():
    assert {p for p, lvl in PROVIDER_ENFORCEMENT.items() if lvl == NONE} == {
        "hermes",
        "cursor_cli",
        "mock_cli",
    }


def test_install_time_providers_are_native():
    from cli_agent_orchestrator.utils.enforcement import INSTALL_TIME_PROVIDERS

    assert INSTALL_TIME_PROVIDERS == {"opencode_cli", "kiro_cli"}
    for provider in INSTALL_TIME_PROVIDERS:
        assert PROVIDER_ENFORCEMENT[provider] == NATIVE, provider
