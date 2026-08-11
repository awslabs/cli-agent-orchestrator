"""Property tests for cross-provider delivery (W6, W11).

**Validates: Requirements 13.2, 13.6, 13.7, 18.9, 18.10; Property P10**

Covers skill-delivery properties across every provider, plus the MCP-delivery
properties that only become meaningful once plugin MCP servers are delivered
into agent profiles (Increment 2).

Deliberately **not** duplicated: ``test_reachable_set_equals_the_union_for_every_provider``,
``test_every_provider_agrees_with_every_other`` and
``test_projected_plugin_skills_reach_every_provider_regardless`` are all the same
property as ``test_delivery_equivalence.py``'s
``test_property_delivery_equivalence_across_providers``, which already exists here
and reads each provider's real artifact. ``test_invalid_plugin_skills_reach_no_provider``,
``test_catalog_based_providers_do_see_extra_dir_skills`` and
``test_filesystem_direct_providers_do_not_see_extra_dir_skills`` exist here as
concrete parametrized tests in that same file (the last two as Criteria 13.6/13.7).

The one genuinely irreplaceable test in this file is
``TestTheRejectedAlternativeWouldNotHaveWorked``. It is not a test of CAO's
behaviour at all — it is a test of the *counterfactual* the design rests on. The
design's single most consequential choice is projecting into ``SKILLS_DIR``
instead of registering plugin roots as extra skill directories, and the stated
reason is that the alternative cannot reach Kiro or OpenCode. That claim is
falsifiable, so it should be asserted rather than believed.
"""

from __future__ import annotations

import glob
import itertools
import json
from pathlib import Path
from typing import List, Set

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cli_agent_orchestrator.agent_plugins.installer import install
from cli_agent_orchestrator.agent_plugins.mcp_delivery import (
    collect_plugin_mcp_servers,
    merge_plugin_mcp_servers,
)
from cli_agent_orchestrator.agent_plugins.models import PluginSource
from cli_agent_orchestrator.agent_plugins.store import InstalledPluginStore
from cli_agent_orchestrator.models.provider import ProviderType

from .conftest import MCP_SCHEMA_ID, build_plugin, write_skill

FS_SETTINGS = settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)

_counter = itertools.count()

PLUGIN_NAMES = st.lists(
    st.sampled_from(["alpha", "beta", "gamma", "zulu"]), min_size=1, max_size=3, unique=True
)


def _world(base: Path) -> dict:
    home = base / f"world-{next(_counter)}"
    skills = home / "skills"
    skills.mkdir(parents=True)
    return {
        "home": home,
        "skills_dir": skills,
        "store": InstalledPluginStore(home / "agent-plugins", home / "agent-plugin-data"),
    }


def _install(world: dict, source: Path):
    return install(
        PluginSource(kind="path", location=str(source)),
        store=world["store"],
        skills_dir=world["skills_dir"],
        refresh_agents=False,
    )


def _mcp(**servers) -> str:
    return json.dumps({"$schema": MCP_SCHEMA_ID, "mcpServers": servers}, indent=2)


STDIO = {"type": "stdio", "command": "demo-server"}


def _kiro_reachable(skills_dir: Path) -> Set[str]:
    """Skill names Kiro's two ``skill://`` globs expand to.

    The union of both patterns, because the union is what Kiro receives.
    """
    matches = glob.glob(f"{skills_dir}/**/SKILL.md", recursive=True)
    matches += glob.glob(f"{skills_dir}/*/SKILL.md")
    return {Path(match).parent.name for match in matches}


class TestMcpFailureNeverAffectsSkills:
    """§7.2.2.2 and §10.1 — the two component types are independent.

    Stated as a property because the interesting input is *which* way the
    ``mcp.json`` is broken, and there are many ways. A validator that treated an
    MCP problem as a plugin problem would pass a test using one malformed
    document and fail on another.
    """

    @FS_SETTINGS
    @given(
        broken=st.sampled_from(
            [
                "{ not json",
                "",
                "[]",
                "null",
                '{"mcpServers": []}',
                '{"$schema": "https://example.test/wrong.json", "mcpServers": {}}',
                json.dumps({"$schema": MCP_SCHEMA_ID, "mcpServers": {"s": {"type": "carrier"}}}),
                json.dumps({"$schema": MCP_SCHEMA_ID, "mcpServers": {"s": {"type": "stdio"}}}),
            ]
        ),
        skills=st.lists(st.sampled_from(["alpha", "beta", "gamma"]), min_size=1, unique=True),
    )
    def test_however_broken_the_mcp_json_the_skills_still_deliver(
        self, tmp_path: Path, broken: str, skills: List[str]
    ) -> None:
        world = _world(tmp_path)
        source = build_plugin(world["home"] / "src", "demo", skills=list(skills), mcp_text=broken)

        outcome = _install(world, source)

        assert outcome.installed, [f.message for f in outcome.report.findings]
        assert set(outcome.record.projected_skill_names) == set(skills)
        assert _kiro_reachable(world["skills_dir"]) >= set(skills)

    @FS_SETTINGS
    @given(skills=st.lists(st.sampled_from(["alpha", "beta"]), min_size=1, unique=True))
    def test_a_valid_mcp_json_does_not_change_skill_delivery_either(
        self, tmp_path: Path, skills: List[str]
    ) -> None:
        """The symmetric half: adding MCP must not perturb the skill half.

        Without this, "MCP failures don't affect skills" could be true only
        because MCP never affects anything, and a future coupling would go
        unnoticed in the working direction.
        """
        world = _world(tmp_path)
        source = build_plugin(
            world["home"] / "src", "demo", skills=list(skills), mcp_text=_mcp(tools=STDIO)
        )

        outcome = _install(world, source)

        assert set(outcome.record.projected_skill_names) == set(skills)
        assert _kiro_reachable(world["skills_dir"]) >= set(skills)


class TestMcpDeliveryAlgebra:
    """Requirements 18.9 and 18.10 — the merge is a well-behaved function."""

    @FS_SETTINGS
    @given(names=PLUGIN_NAMES)
    def test_the_delivered_set_is_exactly_the_union_of_distinct_server_names(
        self, tmp_path: Path, names: List[str]
    ) -> None:
        """Each plugin contributes its own server; nothing appears twice."""
        world = _world(tmp_path)
        for name in names:
            _install(
                world,
                build_plugin(
                    world["home"] / "src" / name,
                    name,
                    skills=[f"s-{name}"],
                    mcp_text=_mcp(**{f"tools-{name}": STDIO}),
                ),
            )

        delivery = collect_plugin_mcp_servers(store=world["store"])

        assert set(delivery.servers) == {f"tools-{name}" for name in names}
        assert delivery.owners == {f"tools-{name}": name for name in names}

    @FS_SETTINGS
    @given(names=PLUGIN_NAMES)
    def test_a_profile_server_is_never_replaced_whatever_the_plugins_declare(
        self, tmp_path: Path, names: List[str]
    ) -> None:
        """Requirement 18.10 — the operator's own declaration always wins.

        Property rather than example because the number of claimants is the
        interesting variable: a merge that iterated and overwrote would keep the
        profile's entry for one plugin and lose it for three.
        """
        world = _world(tmp_path)
        for name in names:
            _install(
                world,
                build_plugin(
                    world["home"] / "src" / name,
                    name,
                    skills=[f"s-{name}"],
                    mcp_text=_mcp(**{"contested": STDIO}),
                ),
            )

        merged, delivery = merge_plugin_mcp_servers(
            {"contested": {"type": "stdio", "command": "mine"}}, store=world["store"]
        )

        assert merged["contested"]["command"] == "mine"
        assert any(f.code == "mcp_delivery.profile_collision" for f in delivery.findings)

    @FS_SETTINGS
    @given(names=PLUGIN_NAMES, repeats=st.integers(min_value=2, max_value=4))
    def test_collecting_repeatedly_returns_the_same_answer(
        self, tmp_path: Path, names: List[str], repeats: int
    ) -> None:
        """Delivery is re-derived on every profile build, so it must be stable.

        This is the property that makes re-mapping (rather than persisting) safe:
        if two collections of an unchanged store could differ, two agents installed
        minutes apart would get different servers.
        """
        world = _world(tmp_path)
        for name in names:
            _install(
                world,
                build_plugin(
                    world["home"] / "src" / name,
                    name,
                    skills=[f"s-{name}"],
                    mcp_text=_mcp(**{f"tools-{name}": STDIO}),
                ),
            )

        results = [collect_plugin_mcp_servers(store=world["store"]) for _ in range(repeats)]

        for result in results[1:]:
            assert result.servers == results[0].servers
            assert result.owners == results[0].owners


class TestTheRejectedAlternativeWouldNotHaveWorked:
    """The counterfactual the design rests on, asserted rather than believed.

    design.md rejects "append plugin roots to ``_skill_search_dirs()``" on the
    grounds that it cannot reach Kiro CLI or OpenCode, both of which are handed a
    path rooted at ``SKILLS_DIR``. If that were wrong, the whole projection
    mechanism would be unnecessary complexity — so the claim is worth a test.

    These tests assert a property of the *alternative*, not of CAO. They pass by
    demonstrating that a skill placed outside ``SKILLS_DIR`` is invisible to a
    ``SKILLS_DIR``-rooted glob no matter how it was registered.
    """

    @FS_SETTINGS
    @given(names=st.lists(st.sampled_from(["alpha", "beta"]), min_size=1, unique=True))
    def test_a_skill_outside_the_store_is_invisible_to_kiros_globs(
        self, tmp_path: Path, names: List[str]
    ) -> None:
        """Whatever an extra-directory registration did, the glob cannot see it.

        Kiro receives patterns rooted at ``SKILLS_DIR``; expansion happens inside
        Kiro. So registering a directory elsewhere in CAO's settings changes
        nothing about what those patterns match — which is precisely why extra-dirs
        registration was rejected and Requirement 13.7 records the limitation.
        """
        world = _world(tmp_path)
        elsewhere = world["home"] / "plugin-roots"
        elsewhere.mkdir(parents=True)
        for name in names:
            write_skill(elsewhere / name, name)

        reachable = _kiro_reachable(world["skills_dir"])

        assert reachable.isdisjoint(set(names))

    @FS_SETTINGS
    @given(names=st.lists(st.sampled_from(["alpha", "beta"]), min_size=1, unique=True))
    def test_the_same_skills_become_visible_once_projected(
        self, tmp_path: Path, names: List[str]
    ) -> None:
        """The other half, which is what makes the first half an argument.

        Identical skills, identical provider mechanism, one difference: they are
        inside the store. If this failed the conclusion would be "Kiro cannot see
        plugin skills at all", not "projection is the right mechanism".
        """
        world = _world(tmp_path)
        source = build_plugin(world["home"] / "src", "demo", skills=list(names))

        _install(world, source)

        assert _kiro_reachable(world["skills_dir"]) >= set(names)

    @FS_SETTINGS
    @given(names=st.lists(st.sampled_from(["alpha", "beta"]), min_size=1, unique=True))
    def test_opencodes_single_symlink_has_the_same_limitation(
        self, tmp_path: Path, names: List[str]
    ) -> None:
        """The second filesystem-direct provider, for the same reason.

        OpenCode reads one symlink at ``OPENCODE_CONFIG_DIR/skills`` pointing at
        ``SKILLS_DIR``. One link cannot reach two directories, so a registered
        extra directory is unreachable by construction rather than by oversight.
        """
        from unittest import mock

        world = _world(tmp_path)
        opencode_dir = world["home"] / "opencode"
        opencode_dir.mkdir()
        elsewhere = world["home"] / "plugin-roots"
        elsewhere.mkdir()
        for name in names:
            write_skill(elsewhere / name, name)

        with (
            mock.patch(
                "cli_agent_orchestrator.utils.opencode_config.OPENCODE_CONFIG_DIR", opencode_dir
            ),
            mock.patch(
                "cli_agent_orchestrator.utils.opencode_config.SKILLS_DIR", world["skills_dir"]
            ),
        ):
            from cli_agent_orchestrator.utils.opencode_config import ensure_skills_symlink

            ensure_skills_symlink()

        through_link = opencode_dir / "skills"
        visible = {path.name for path in through_link.iterdir() if (path / "SKILL.md").is_file()}

        assert visible.isdisjoint(set(names))


class TestEveryProviderSeesTheSameSkillsUnderFuzzing:
    """Property P10, with the *number* of plugins as the generated variable.

    ``test_delivery_equivalence.py``'s property generates counts of builtin,
    plugin and invalid skills. This one generates the plugin *set* and asserts the
    two filesystem-direct providers agree with the store's own view — the pair the
    design says are hardest to satisfy.
    """

    @FS_SETTINGS
    @given(names=PLUGIN_NAMES)
    def test_the_store_view_and_kiros_glob_view_agree(
        self, tmp_path: Path, names: List[str]
    ) -> None:
        world = _world(tmp_path)
        expected = set()
        for name in names:
            _install(world, build_plugin(world["home"] / "src" / name, name, skills=[f"s-{name}"]))
            expected.add(f"s-{name}")

        assert _kiro_reachable(world["skills_dir"]) == expected

    @FS_SETTINGS
    @given(names=PLUGIN_NAMES)
    def test_a_plugin_that_fails_to_load_contributes_nothing_to_any_view(
        self, tmp_path: Path, names: List[str]
    ) -> None:
        """An unloadable plugin beside valid ones changes neither view."""
        world = _world(tmp_path)
        expected = set()
        for name in names:
            _install(world, build_plugin(world["home"] / "src" / name, name, skills=[f"s-{name}"]))
            expected.add(f"s-{name}")

        # No `$schema` is fatal per §5.3, so nothing of this plugin publishes.
        broken = build_plugin(
            world["home"] / "src" / "broken", "broken", skills=["s-broken"], schema_id=None
        )
        outcome = _install(world, broken)

        assert not outcome.installed
        assert _kiro_reachable(world["skills_dir"]) == expected


ALL_PROVIDERS = [provider.value for provider in ProviderType]
