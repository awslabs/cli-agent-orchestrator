"""Every shipped example profile set still resolves and delegates under the ceiling rule.

For each example directory, every profile's allowlist and ceiling resolve the
way ``create_terminal`` resolves them, and every profile that is meant to
delegate (``role: supervisor``, or an explicit ``delegateTools``) can launch
every other profile in its set: the child's resolved allowlist fits inside the
parent's ceiling. The sets the review named as at risk under a plain
reject-on-widen rule (tool-restrictions, cross-provider, assign, aws,
fxmacrodata-macro, serply-research) are asserted by name so a missing
directory fails rather than silently passing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Tuple

import frontmatter
import pytest

from cli_agent_orchestrator.agent_plugins.mcp_delivery import grantable_server_names
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.utils.delegation import (
    intersect_ceiling,
    resolve_delegate_tools,
    tools_exceeding,
)
from cli_agent_orchestrator.utils.tool_mapping import resolve_allowed_tools

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples"
NAMED_SETS = (
    "tool-restrictions",
    "cross-provider",
    "assign",
    "aws",
    "fxmacrodata-macro",
    "serply-research",
    "aidlc-portfolio",
    "robotics",
)


def _profiles_in(example: str) -> Dict[str, AgentProfile]:
    found: Dict[str, AgentProfile] = {}
    for path in sorted((EXAMPLES_DIR / example).rglob("*.md")):
        if path.name in ("README.md", "SKILL.md"):
            continue
        post = frontmatter.load(path)
        meta = post.metadata
        if not meta or "name" not in meta or "description" not in meta:
            continue
        if "steps" in meta:  # workflow specs, not agent profiles
            continue
        found[str(path.relative_to(EXAMPLES_DIR))] = AgentProfile(**meta)
    return found


def _resolve(profile: AgentProfile) -> Tuple[List[str], List[str]]:
    allowed = resolve_allowed_tools(
        profile.allowedTools, profile.role, grantable_server_names(profile)
    )
    ceiling = resolve_delegate_tools(profile.delegateTools, profile.role, allowed)
    return allowed, ceiling


_SETS = {name: _profiles_in(name) for name in NAMED_SETS}


@pytest.mark.parametrize("example", NAMED_SETS)
def test_named_set_has_profiles(example):
    assert _SETS[example], f"no agent profiles found under examples/{example}"


@pytest.mark.parametrize(
    "rel_path,profile",
    [(p, prof) for profiles in _SETS.values() for p, prof in profiles.items()],
    ids=[p for profiles in _SETS.values() for p in profiles],
)
def test_profile_resolves_allowlist_and_ceiling(rel_path, profile):
    allowed, ceiling = _resolve(profile)
    assert allowed, rel_path
    # A ceiling always covers the agent's own tools.
    assert tools_exceeding(allowed, ceiling) == [], rel_path


def _delegating_pairs():
    for example, profiles in _SETS.items():
        for parent_path, parent in profiles.items():
            if parent.role != "supervisor" and parent.delegateTools is None:
                continue
            for child_path, child in profiles.items():
                if child_path != parent_path:
                    yield example, parent_path, parent, child_path, child


@pytest.mark.parametrize(
    "example,parent_path,parent,child_path,child",
    list(_delegating_pairs()),
    ids=[f"{p}->{c}" for _, p, _, c, _ in _delegating_pairs()],
)
def test_supervisor_can_assign_every_profile_in_its_set(
    example, parent_path, parent, child_path, child
):
    _, parent_ceiling = _resolve(parent)
    child_allowed, child_ceiling = _resolve(child)
    assert (
        tools_exceeding(child_allowed, parent_ceiling) == []
    ), f"{parent_path} may not launch {child_path} under the delegation rule"
    # The child's recorded ceiling never widens past the parent's.
    assert tools_exceeding(intersect_ceiling(child_ceiling, parent_ceiling), parent_ceiling) == []


def test_the_review_pairs_are_covered():
    """The supervisor->developer pattern the docs teach, in the sets that teach it."""
    pairs = {(e, p.split("/")[-1], c.split("/")[-1]) for e, p, _, c, _ in _delegating_pairs()}
    assert (
        "tool-restrictions",
        "tool_restrictions_supervisor.md",
        "tool_restrictions_developer.md",
    ) in pairs
    assert ("cross-provider", "cross_provider_supervisor.md", "data_analyst_codex.md") in pairs
    assert ("assign", "analysis_supervisor.md", "data_analyst.md") in pairs


def test_reviewer_profiles_cannot_launch_developers():
    """The hole the rule closes, stated against the shipped reviewer examples."""
    reviewer = _SETS["tool-restrictions"]["tool-restrictions/tool_restrictions_reviewer.md"]
    developer = _SETS["tool-restrictions"]["tool-restrictions/tool_restrictions_developer.md"]
    _, reviewer_ceiling = _resolve(reviewer)
    developer_allowed, _ = _resolve(developer)
    assert tools_exceeding(developer_allowed, reviewer_ceiling) == [
        "fs_*",
        "execute_bash",
        "web_fetch",
    ]


# ---------------------------------------------------------------------------
# Review A, T2: drive the REAL create_terminal for the example pairs, so the
# "no shipped example breaks" claim is exercised where the enforcement lives.
# ---------------------------------------------------------------------------
import asyncio  # noqa: E402
from unittest.mock import AsyncMock, MagicMock  # noqa: E402

from cli_agent_orchestrator.services.terminal_service import create_terminal  # noqa: E402
from cli_agent_orchestrator.utils.delegation import DelegationCeilingError  # noqa: E402

_TS = "cli_agent_orchestrator.services.terminal_service"


@pytest.fixture
def example_launch(monkeypatch):
    """Real create_terminal + real SQLite rows; tmux, provider and FIFOs mocked.
    Profiles resolve by NAME across every example set, as the server would."""
    by_name = {prof.name: prof for profiles in _SETS.values() for prof in profiles.values()}
    ids = iter(f"{n:08x}" for n in range(0xB0000000, 0xB0000FFF))
    monkeypatch.setattr(f"{_TS}.generate_terminal_id", lambda: next(ids))
    monkeypatch.setattr(f"{_TS}.generate_window_name", lambda p: f"{p}-win")
    monkeypatch.setattr(f"{_TS}.generate_session_name", lambda: "cao-ex")
    monkeypatch.setattr(f"{_TS}.agent_profiles.load_agent_profile", lambda n: by_name[n])
    monkeypatch.setattr(f"{_TS}.status_monitor", MagicMock())
    monkeypatch.setattr(f"{_TS}.fifo_manager", MagicMock())
    fifo_dir = MagicMock()
    fifo_dir.__truediv__ = MagicMock(return_value="fake.fifo")
    monkeypatch.setattr(f"{_TS}.FIFO_DIR", fifo_dir)
    provider = AsyncMock()
    provider.initialize.return_value = True
    pm = MagicMock()
    pm.create_provider.return_value = provider
    monkeypatch.setattr(f"{_TS}.provider_manager", pm)
    backend = MagicMock()
    backend.session_exists.return_value = True
    backend.create_window.return_value = "win"
    backend.supports_event_inbox.return_value = True
    monkeypatch.setattr("cli_agent_orchestrator.backends.registry._backend", backend)

    def _launch(profile_name, **kw):
        return asyncio.run(
            create_terminal(
                "claude_code", profile_name, session_name="cao-ex", new_session=False, **kw
            )
        )

    return _launch


@pytest.mark.usefixtures("isolated_memory_db")
@pytest.mark.parametrize(
    "example,parent_path,parent,child_path,child",
    list(_delegating_pairs()),
    ids=[f"{p}->{c}" for _, p, _, c, _ in _delegating_pairs()],
)
def test_supervisor_assigns_every_profile_through_create_terminal(
    example_launch, example, parent_path, parent, child_path, child
):
    """Operator launches the parent (its ceiling is recorded on the row), then the
    parent creates the child with caller_id, exactly as assign does server-side."""
    parent_terminal = example_launch(parent.name)
    child_terminal = example_launch(child.name, caller_id=parent_terminal.id)
    assert child_terminal.caller_id == parent_terminal.id
    assert tools_exceeding(child_terminal.allowed_tools, parent_terminal.delegate_tools) == []
    assert tools_exceeding(child_terminal.delegate_tools, parent_terminal.delegate_tools) == []


@pytest.mark.usefixtures("isolated_memory_db")
def test_shipped_reviewer_cannot_assign_the_shipped_developer(example_launch):
    """The closed hole, through the real path: tool_restrictions_reviewer ->
    tool_restrictions_developer is refused with the tools named."""
    reviewer = example_launch("tool_restrictions_reviewer")
    with pytest.raises(DelegationCeilingError) as excinfo:
        example_launch("tool_restrictions_developer", caller_id=reviewer.id)
    assert excinfo.value.excess == ["fs_*", "execute_bash", "web_fetch"]
    assert reviewer.id in str(excinfo.value)
    # ...while the reviewer may still launch another reviewer.
    peer = example_launch("tool_restrictions_reviewer", caller_id=reviewer.id)
    assert peer.delegate_tools == reviewer.delegate_tools
