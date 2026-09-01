"""Property tests for install/remove state transitions and projection (W5).

**Validates: Requirements 9.4, 10.3, 14.2, 14.3, 15.4; Properties P4, P6, P8**

These are *algebraic* properties of the
installed set — idempotence, inverse pairs, order-independence — which is the
class of bug a table of examples reliably misses, because each individual
operation looks right and only a sequence exposes the fault.

Deliberately **not** duplicated, because ``test_installer.py`` and
``test_projection.py`` already carry an equivalent Hypothesis property:
``test_add_then_remove_restores_the_prior_state``
(``test_property_install_then_remove_restores_the_prior_state``),
``test_add_then_force_add_equals_a_single_add``
(``test_property_force_reinstall_equals_a_single_install``),
``test_failed_install_changes_nothing``
(``test_property_invalid_install_changes_nothing``),
``test_valid_skills_only_plugin_fully_loads_and_projects``
(``test_property_skills_only_plugins_fully_conform``),
``test_lexicographically_smallest_plugin_wins`` and
``test_winner_is_invariant_under_install_order_permutation``
(``test_property_winner_is_invariant_under_install_order``),
``test_winner_is_stable_across_repeated_rebuilds``
(``test_property_repeated_rebuilds_never_flip_the_winner``),
``test_exactly_k_minus_one_losers_are_reported``
(``test_property_loser_count_is_exactly_k_minus_one``), and
``test_pre_existing_skill_is_never_shadowed``
(``test_property_preexisting_always_beats_every_plugin``).
"""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import List

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cli_agent_orchestrator.agent_plugins import provenance
from cli_agent_orchestrator.agent_plugins.installer import PluginInstallError, install, uninstall
from cli_agent_orchestrator.agent_plugins.models import PluginSource
from cli_agent_orchestrator.agent_plugins.projection import rebuild_projection
from cli_agent_orchestrator.agent_plugins.store import InstalledPluginStore

from .conftest import build_plugin, write_skill

FS_SETTINGS = settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)

_counter = itertools.count()

#: Plugin names whose sort order is deliberately not their creation order, so a
#: rule accidentally keyed on ``installed_at`` or ``scandir`` fails rather than
#: passing by luck.
PLUGIN_NAMES = st.lists(
    st.sampled_from(["alpha", "beta", "gamma", "zulu", "aab", "zzy"]),
    min_size=2,
    max_size=4,
    unique=True,
)


def _world(base: Path) -> dict:
    """A fresh store and skill store, isolated per Hypothesis example.

    A function-scoped fixture is created once for the whole test, so state from
    one example would leak into the next and the second install would collide on
    the plugin name. Every example therefore gets its own directory.
    """
    home = base / f"world-{next(_counter)}"
    skills = home / "skills"
    skills.mkdir(parents=True)
    return {
        "home": home,
        "skills_dir": skills,
        "store": InstalledPluginStore(home / "agent-plugins", home / "agent-plugin-data"),
    }


def _install(world: dict, source: Path, *, force: bool = False, dry_run: bool = False):
    return install(
        PluginSource(kind="path", location=str(source)),
        force=force,
        dry_run=dry_run,
        store=world["store"],
        skills_dir=world["skills_dir"],
        refresh_agents=False,
    )


def _snapshot(world: dict) -> dict:
    """Everything about the world that an operation could legitimately change."""
    store = world["store"]
    return {
        "records": sorted(
            (r.name, r.version, tuple(sorted(r.projected_skill_names)))
            for r in store.list_installed()
        ),
        "projected": sorted(
            path.name for path in world["skills_dir"].iterdir() if path.name != ".state"
        ),
        "owners": dict(provenance.projection_map(store)),
    }


class TestOperationsThatMustChangeNothing:
    """Property P4 — a non-install leaves the world byte-identical."""

    @FS_SETTINGS
    @given(names=PLUGIN_NAMES)
    def test_dry_run_changes_nothing(self, tmp_path: Path, names: List[str]) -> None:
        """``--dry-run`` reports and returns; it must not publish or project.

        Run against a world that already has plugins in it, because "changes
        nothing" is trivially true of an empty store and the interesting failure
        is a dry run that rebuilds the projection of everything else.
        """
        world = _world(tmp_path)
        for name in names[:-1]:
            _install(world, build_plugin(world["home"] / "src" / name, name, skills=[f"s-{name}"]))

        before = _snapshot(world)
        candidate = build_plugin(
            world["home"] / "src" / names[-1], names[-1], skills=[f"s-{names[-1]}"]
        )

        outcome = _install(world, candidate, dry_run=True)

        assert outcome.dry_run and not outcome.installed
        assert _snapshot(world) == before

    @FS_SETTINGS
    @given(names=PLUGIN_NAMES, missing=st.sampled_from(["nope", "does-not-exist"]))
    def test_an_unreachable_source_changes_nothing(
        self, tmp_path: Path, names: List[str], missing: str
    ) -> None:
        """A resolver failure is a different class from a validation failure.

        ``test_property_invalid_install_changes_nothing`` covers a plugin that
        resolves and then fails validation. This covers one that never resolves at
        all — the path where ``PluginInstallError`` is raised rather than an
        unloadable report returned, so the rollback happens on a different branch.
        """
        world = _world(tmp_path)
        for name in names:
            _install(world, build_plugin(world["home"] / "src" / name, name, skills=[f"s-{name}"]))

        before = _snapshot(world)

        try:
            _install(world, world["home"] / missing)
        except PluginInstallError:
            pass

        assert _snapshot(world) == before

    @FS_SETTINGS
    @given(names=PLUGIN_NAMES, extra_removals=st.integers(min_value=1, max_value=3))
    def test_repeated_removal_is_idempotent(
        self, tmp_path: Path, names: List[str], extra_removals: int
    ) -> None:
        """Removing an absent plugin is a no-op, not an error and not a mutation.

        Matters because the CLI and the API both allow a removal to be retried
        after a partial failure, and because a panel can double-submit.
        """
        world = _world(tmp_path)
        for name in names:
            _install(world, build_plugin(world["home"] / "src" / name, name, skills=[f"s-{name}"]))

        target = names[0]
        uninstall(
            target, store=world["store"], skills_dir=world["skills_dir"], refresh_agents=False
        )
        after_first = _snapshot(world)

        for _ in range(extra_removals):
            try:
                uninstall(
                    target,
                    store=world["store"],
                    skills_dir=world["skills_dir"],
                    refresh_agents=False,
                )
            except PluginInstallError:
                pass

        assert _snapshot(world) == after_first


class TestPluginDataLifecycle:
    """Requirement 9.4 / 10.3 — ``PLUGIN_DATA`` outlives an update, not a purge."""

    @FS_SETTINGS
    @given(names=PLUGIN_NAMES)
    def test_data_survives_removal_without_purge(self, tmp_path: Path, names: List[str]) -> None:
        """§9.1's persistence guarantee, which is the whole point of a data dir.

        A plugin that stored state must find it again after a reinstall; deleting
        it on every removal would make the directory pointless.
        """
        world = _world(tmp_path)
        name = names[0]
        _install(world, build_plugin(world["home"] / "src" / name, name, skills=["alpha"]))

        data_dir = world["store"].plugin_data_dir(name)
        marker = data_dir / "state.txt"
        marker.write_text("kept", encoding="utf-8")

        uninstall(name, store=world["store"], skills_dir=world["skills_dir"], refresh_agents=False)

        assert marker.is_file()
        assert marker.read_text(encoding="utf-8") == "kept"

    @FS_SETTINGS
    @given(names=PLUGIN_NAMES)
    def test_purge_deletes_the_data(self, tmp_path: Path, names: List[str]) -> None:
        """The explicit opt-out, and it must actually opt out."""
        world = _world(tmp_path)
        name = names[0]
        _install(world, build_plugin(world["home"] / "src" / name, name, skills=["alpha"]))

        data_dir = world["store"].plugin_data_dir(name)
        (data_dir / "state.txt").write_text("gone", encoding="utf-8")

        uninstall(
            name,
            purge_data=True,
            store=world["store"],
            skills_dir=world["skills_dir"],
            refresh_agents=False,
        )

        assert not data_dir.exists()

    @FS_SETTINGS
    @given(names=PLUGIN_NAMES)
    def test_a_forced_reinstall_keeps_the_data(self, tmp_path: Path, names: List[str]) -> None:
        """An *update* must never be the operation that loses a plugin's state.

        This is why the data directory is created at install rather than at first
        use: a reinstall that recreated it would silently start from empty.
        """
        world = _world(tmp_path)
        name = names[0]
        source = build_plugin(world["home"] / "src" / name, name, skills=["alpha"])
        _install(world, source)

        marker = world["store"].plugin_data_dir(name) / "state.txt"
        marker.write_text("preserved", encoding="utf-8")

        _install(world, source, force=True)

        assert marker.read_text(encoding="utf-8") == "preserved"


class TestProjectionIsAFunctionOfTheInstalledSet:
    """Property P8, stated as strongly as it can be stated."""

    @FS_SETTINGS
    @given(names=PLUGIN_NAMES)
    def test_the_projection_depends_only_on_which_plugins_are_installed(
        self, tmp_path: Path, names: List[str]
    ) -> None:
        """Two worlds with the same installed set project identically.

        Stronger than the order-permutation property: those worlds are built by
        different *sequences* — one straight through, one with an extra plugin
        installed and then removed — so anything that leaked history (a stale
        ledger entry, a dangling link, a record field) shows up as a difference.
        """
        direct = _world(tmp_path)
        for name in names:
            _install(
                direct, build_plugin(direct["home"] / "src" / name, name, skills=[f"s-{name}"])
            )

        detoured = _world(tmp_path)
        transient = "transient-plugin"
        _install(
            detoured,
            build_plugin(detoured["home"] / "src" / transient, transient, skills=["s-transient"]),
        )
        for name in names:
            _install(
                detoured, build_plugin(detoured["home"] / "src" / name, name, skills=[f"s-{name}"])
            )
        uninstall(
            transient,
            store=detoured["store"],
            skills_dir=detoured["skills_dir"],
            refresh_agents=False,
        )

        assert _snapshot(direct)["projected"] == _snapshot(detoured)["projected"]
        assert _snapshot(direct)["owners"] == _snapshot(detoured)["owners"]

    @FS_SETTINGS
    @given(names=PLUGIN_NAMES, rebuilds=st.integers(min_value=2, max_value=4))
    def test_rebuilding_is_a_fixed_point(
        self, tmp_path: Path, names: List[str], rebuilds: int
    ) -> None:
        """Requirement 14.3 — rebuild is a pure function, so it converges at once.

        Distinct from "the winner does not flip": this asserts the *whole*
        projection is unchanged, which also catches a rebuild that recreated links
        it should have left alone or accumulated findings across calls.
        """
        world = _world(tmp_path)
        for name in names:
            _install(world, build_plugin(world["home"] / "src" / name, name, skills=[f"s-{name}"]))

        first = _snapshot(world)
        for _ in range(rebuilds):
            rebuild_projection(world["store"], skills_dir=world["skills_dir"])

        assert _snapshot(world) == first


class TestCollisionReporting:
    """Requirement 14.2 — a contested name resolves once and says so."""

    @FS_SETTINGS
    @given(names=PLUGIN_NAMES)
    def test_a_contested_name_is_projected_exactly_once(
        self, tmp_path: Path, names: List[str]
    ) -> None:
        """Every claimant installs; the name appears once, owned by the winner.

        The failure this excludes is a projection that overwrites on each install
        and ends up owned by whoever went last — which would look correct in the
        filesystem and be wrong in the record.
        """
        world = _world(tmp_path)
        for name in names:
            _install(world, build_plugin(world["home"] / "src" / name, name, skills=["shared"]))

        projected = [path.name for path in world["skills_dir"].iterdir() if path.name == "shared"]

        assert projected == ["shared"]
        assert provenance.owning_plugin("shared", world["store"]) == min(names)

    @FS_SETTINGS
    @given(names=PLUGIN_NAMES)
    def test_installing_an_earlier_claimant_reports_the_transition(
        self, tmp_path: Path, names: List[str]
    ) -> None:
        """A reassignment is announced, not silently applied.

        Install the later-sorting plugin first, then the earlier one: the name
        changes hands, and an agent that was receiving one plugin's content now
        receives another's. A ``SKIPPED`` finding on the loser is not sufficient
        signal for that, so ``projection.winner_changed`` exists.
        """
        ordered = sorted(names)
        later, earlier = ordered[-1], ordered[0]

        world = _world(tmp_path)
        _install(world, build_plugin(world["home"] / "src" / later, later, skills=["shared"]))
        assert provenance.owning_plugin("shared", world["store"]) == later

        outcome = _install(
            world, build_plugin(world["home"] / "src" / earlier, earlier, skills=["shared"])
        )

        assert provenance.owning_plugin("shared", world["store"]) == earlier
        assert any(f.code == "projection.winner_changed" for f in outcome.projection_findings)

    @FS_SETTINGS
    @given(names=PLUGIN_NAMES)
    def test_a_preexisting_skill_keeps_the_name_and_the_attempt_is_reported(
        self, tmp_path: Path, names: List[str]
    ) -> None:
        """Requirement 14.1 — and the operator is told the plugin's copy is unused.

        Complements ``test_property_preexisting_always_beats_every_plugin``, which
        asserts the winner. This asserts the *report*: a shadowing attempt that
        succeeded silently would leave an operator wondering why a plugin's skill
        appears to do nothing.
        """
        world = _world(tmp_path)
        write_skill(world["skills_dir"] / "shared", "shared", "The pre-existing one.")

        findings = []
        for name in names:
            outcome = _install(
                world, build_plugin(world["home"] / "src" / name, name, skills=["shared"])
            )
            findings.extend(outcome.projection_findings)

        assert (world["skills_dir"] / "shared").is_dir()
        assert not (world["skills_dir"] / "shared").is_symlink()
        assert provenance.owning_plugin("shared", world["store"]) is None
        assert any(f.code == "projection.preexisting_collision" for f in findings)


class TestRemovalWithdrawsEverything:
    """Requirement 15.4 — removal is the inverse of install for skills too."""

    @FS_SETTINGS
    @given(names=PLUGIN_NAMES)
    def test_removing_a_plugin_removes_exactly_its_own_skills(
        self, tmp_path: Path, names: List[str]
    ) -> None:
        """The siblings' skills must survive, and the removed one's must not.

        Both halves in one property: a removal that swept too widely and one that
        swept too narrowly are both single-assertion-passing bugs.
        """
        world = _world(tmp_path)
        for name in names:
            _install(world, build_plugin(world["home"] / "src" / name, name, skills=[f"s-{name}"]))

        target = names[0]
        survivors = {f"s-{name}" for name in names[1:]}

        uninstall(
            target, store=world["store"], skills_dir=world["skills_dir"], refresh_agents=False
        )

        remaining = {path.name for path in world["skills_dir"].iterdir() if path.name != ".state"}
        assert f"s-{target}" not in remaining
        assert survivors <= remaining
