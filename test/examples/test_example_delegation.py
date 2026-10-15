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
