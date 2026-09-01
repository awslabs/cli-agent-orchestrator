"""Projection tests — correctness property P8, copy-mode fallback, dangling sweep.

**P8: Projection non-shadowing and deterministic collision winner**
**Validates: Requirements 14.1, 14.2, 14.3, 14.4, 14.5**
"""

from __future__ import annotations

import os
from itertools import permutations
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cli_agent_orchestrator.agent_plugins import provenance
from cli_agent_orchestrator.agent_plugins.installer import install, uninstall
from cli_agent_orchestrator.agent_plugins.models import PluginSource, Severity
from cli_agent_orchestrator.agent_plugins.projection import (
    PROJECTION_MODE_COPY,
    rebuild_projection,
    sweep_dangling_projections,
)
from cli_agent_orchestrator.agent_plugins.store import InstalledPluginStore

from .conftest import build_plugin, write_skill


def path_source(root: Path) -> PluginSource:
    return PluginSource(kind="path", location=str(root))


def do_install(source: Path, store, skills_dir, **kwargs):
    return install(
        path_source(source), store=store, skills_dir=skills_dir, refresh_agents=False, **kwargs
    )


def codes(findings) -> list:
    return [f.code for f in findings]


class TestBasicProjection:
    def test_projected_skill_is_a_symlink_into_the_plugin_root(
        self, store, skills_dir, make_plugin
    ):
        do_install(make_plugin("demo", skills=["alpha"]), store, skills_dir)

        link = skills_dir / "alpha"
        assert link.is_symlink()
        assert Path(os.path.realpath(link)) == Path(
            os.path.realpath(store.plugin_root("demo") / "skills" / "alpha")
        )

    def test_the_link_reads_like_an_ordinary_skill_folder(self, store, skills_dir, make_plugin):
        """``list_skills()``'s ``is_dir()``/``SKILL.md is_file()`` gates follow links."""
        do_install(make_plugin("demo", skills=["alpha"]), store, skills_dir)

        link = skills_dir / "alpha"
        assert link.is_dir()
        assert (link / "SKILL.md").is_file()

    def test_the_link_is_named_with_the_unprefixed_skill_name(self, store, skills_dir, make_plugin):
        """No namespacing is possible: the folder name must equal the frontmatter name."""
        do_install(make_plugin("my-plugin", skills=["alpha"]), store, skills_dir)
        assert (skills_dir / "alpha").exists()
        assert not (skills_dir / "my-plugin-alpha").exists()

    def test_rebuild_is_idempotent(self, store, skills_dir, make_plugin):
        do_install(make_plugin("demo", skills=["alpha"]), store, skills_dir)

        first = rebuild_projection(store, skills_dir=skills_dir)
        second = rebuild_projection(store, skills_dir=skills_dir)

        assert dict(first.projected) == dict(second.projected)
        assert (skills_dir / "alpha").is_symlink()


class TestNonShadowing:
    """Requirement 14.1 — a pre-existing skill always wins."""

    def test_builtin_or_user_added_skill_is_never_shadowed(self, store, skills_dir, make_plugin):
        write_skill(skills_dir / "alpha", "alpha", "the pre-existing one")

        outcome = do_install(make_plugin("demo", skills=["alpha"]), store, skills_dir)

        assert outcome.installed  # the plugin still installs
        assert not (skills_dir / "alpha").is_symlink()
        assert "the pre-existing one" in (skills_dir / "alpha" / "SKILL.md").read_text()
        assert "projection.preexisting_collision" in codes(outcome.projection_findings)
        assert outcome.record.projected_skill_names == ()

    def test_a_skill_from_extra_dirs_is_also_pre_existing(
        self, store, skills_dir, make_plugin, monkeypatch, tmp_path
    ):
        """``_skill_search_dirs()`` searches SKILLS_DIR first, so projecting would shadow.

        An ``extra_dirs`` skill is user-added by any reading of Requirement 14.1,
        and shadowing one would also break Requirement 13.2's reachable-set
        union.
        """
        extra = tmp_path / "extra"
        write_skill(extra / "alpha", "alpha", "from an extra dir")
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_extra_skill_dirs",
            lambda: [str(extra)],
        )

        outcome = do_install(make_plugin("demo", skills=["alpha"]), store, skills_dir)

        assert not (skills_dir / "alpha").exists()
        assert "projection.preexisting_collision" in codes(outcome.projection_findings)

    def test_the_skipped_finding_names_the_colliding_skill(self, store, skills_dir, make_plugin):
        write_skill(skills_dir / "alpha", "alpha")
        outcome = do_install(make_plugin("demo", skills=["alpha"]), store, skills_dir)

        finding = next(
            f for f in outcome.projection_findings if f.code == "projection.preexisting_collision"
        )
        assert finding.severity is Severity.SKIPPED
        assert "alpha" in finding.message and "demo" in finding.message


class TestPluginVersusPluginCollision:
    """Requirement 14.2 — lexicographically smallest plugin name wins."""

    def test_lexicographically_smallest_plugin_wins(self, store, skills_dir, tmp_path):
        for name in ("zeta", "alpha", "mike"):
            build_plugin(tmp_path / "src" / name, name, skills=["shared"])

        for name in ("zeta", "mike", "alpha"):
            do_install(tmp_path / "src" / name, store, skills_dir)

        assert provenance.owning_plugin("shared", store) == "alpha"
        assert Path(os.path.realpath(skills_dir / "shared")).parts[-3] == "alpha"

    def test_every_loser_gets_a_finding_naming_the_winner(self, store, skills_dir, tmp_path):
        for name in ("alpha", "zeta"):
            build_plugin(tmp_path / "src" / name, name, skills=["shared"])

        do_install(tmp_path / "src" / "alpha", store, skills_dir)
        outcome = do_install(tmp_path / "src" / "zeta", store, skills_dir)

        finding = next(
            f for f in outcome.projection_findings if f.code == "projection.plugin_collision"
        )
        assert finding.severity is Severity.SKIPPED
        assert "zeta" in finding.message and "alpha" in finding.message

    def test_installing_an_earlier_claimant_reassigns_and_warns(self, store, skills_dir, tmp_path):
        """Requirement 14 + design's transition rule: the change itself is reported."""
        build_plugin(tmp_path / "src" / "zeta", "zeta", skills=["shared"])
        build_plugin(tmp_path / "src" / "alpha", "alpha", skills=["shared"])

        do_install(tmp_path / "src" / "zeta", store, skills_dir)
        assert provenance.owning_plugin("shared", store) == "zeta"

        outcome = do_install(tmp_path / "src" / "alpha", store, skills_dir)

        assert provenance.owning_plugin("shared", store) == "alpha"
        warning = next(
            f for f in outcome.projection_findings if f.code == "projection.winner_changed"
        )
        assert warning.severity is Severity.WARNING
        assert "zeta" in warning.message and "alpha" in warning.message
        # The loser's SKIPPED finding is emitted *in addition to* the warning.
        assert "projection.plugin_collision" in codes(outcome.projection_findings)

    def test_removing_the_winner_promotes_the_next_claimant(self, store, skills_dir, tmp_path):
        build_plugin(tmp_path / "src" / "alpha", "alpha", skills=["shared"])
        build_plugin(tmp_path / "src" / "zeta", "zeta", skills=["shared"])
        do_install(tmp_path / "src" / "alpha", store, skills_dir)
        do_install(tmp_path / "src" / "zeta", store, skills_dir)

        uninstall("alpha", store=store, skills_dir=skills_dir, refresh_agents=False)

        assert provenance.owning_plugin("shared", store) == "zeta"
        assert (skills_dir / "shared" / "SKILL.md").is_file()

    def test_install_timestamp_is_not_the_tiebreaker(self, store, skills_dir, tmp_path):
        """Requirement 14.5 — the rule reads only the persisted manifest name."""
        build_plugin(tmp_path / "src" / "zzz", "zzz", skills=["shared"])
        build_plugin(tmp_path / "src" / "aaa", "aaa", skills=["shared"])

        # Install the later-sorting plugin first; if `installed_at` were the key
        # it would win.
        do_install(tmp_path / "src" / "zzz", store, skills_dir)
        do_install(tmp_path / "src" / "aaa", store, skills_dir)

        assert provenance.owning_plugin("shared", store) == "aaa"


class TestCopyModeFallback:
    def test_copy_mode_materializes_content_rather_than_a_link(
        self, store, skills_dir, make_plugin, monkeypatch
    ):
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_skill_projection_mode",
            lambda: PROJECTION_MODE_COPY,
        )
        do_install(make_plugin("demo", skills=["alpha"]), store, skills_dir)

        projected = skills_dir / "alpha"
        assert not projected.is_symlink()
        assert (projected / "SKILL.md").is_file()

    def test_copy_mode_entries_are_still_recognized_as_managed(
        self, store, skills_dir, make_plugin, monkeypatch
    ):
        """A copied projection has no structural marker, so the records must carry it.

        Without this, the next rebuild would see a plain directory in the skill
        store and mistake CAO's own copy for a pre-existing user skill.
        """
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_skill_projection_mode",
            lambda: PROJECTION_MODE_COPY,
        )
        do_install(make_plugin("demo", skills=["alpha"]), store, skills_dir)

        result = rebuild_projection(store, skills_dir=skills_dir)

        assert dict(result.projected) == {"alpha": "demo"}
        assert "projection.preexisting_collision" not in codes(result.findings)

    def test_symlink_failure_falls_back_to_copy_with_a_report(
        self, store, skills_dir, make_plugin, monkeypatch
    ):
        """Requirement 13.4 — the Windows-without-Developer-Mode case."""
        do_install(make_plugin("demo", skills=["alpha"]), store, skills_dir)
        (skills_dir / "alpha").unlink()

        def no_symlinks(self, *args, **kwargs):
            raise OSError("symlink creation is unsupported")

        monkeypatch.setattr(Path, "symlink_to", no_symlinks)
        result = rebuild_projection(store, skills_dir=skills_dir)

        assert result.mode == PROJECTION_MODE_COPY
        assert "projection.copy_fallback" in codes(result.findings)
        assert (skills_dir / "alpha" / "SKILL.md").is_file()
        assert not (skills_dir / "alpha").is_symlink()


class TestSweepAndDanglingLinks:
    def test_stale_projection_is_swept_on_rebuild(self, store, skills_dir, make_plugin):
        do_install(make_plugin("demo", skills=["alpha"]), store, skills_dir)
        store.unpublish("demo")

        result = rebuild_projection(store, skills_dir=skills_dir)

        assert not (skills_dir / "alpha").exists()
        assert "alpha" in result.swept

    def test_dangling_link_is_swept_without_a_rebuild(self, store, skills_dir, make_plugin):
        do_install(make_plugin("demo", skills=["alpha"]), store, skills_dir)
        # Simulate a store mutated out of band: the target vanishes, the link stays.
        import shutil

        shutil.rmtree(store.plugin_root("demo") / "skills" / "alpha")

        swept = sweep_dangling_projections(store, skills_dir=skills_dir)

        assert "alpha" in swept
        assert not (skills_dir / "alpha").is_symlink()

    def test_the_sweep_never_touches_a_user_owned_skill(self, store, skills_dir):
        write_skill(skills_dir / "mine", "mine")
        sweep_dangling_projections(store, skills_dir=skills_dir)
        assert (skills_dir / "mine" / "SKILL.md").is_file()

    def test_a_dangling_link_is_simply_not_enumerated(self, store, skills_dir, make_plugin):
        """The read paths already tolerate a broken link — both gates return False."""
        do_install(make_plugin("demo", skills=["alpha"]), store, skills_dir)
        import shutil

        shutil.rmtree(store.plugin_root("demo") / "skills" / "alpha")

        link = skills_dir / "alpha"
        assert link.is_symlink()
        assert link.is_dir() is False  # not an exception
        assert (link / "SKILL.md").is_file() is False

    def test_sweep_that_cannot_unlink_logs_and_continues(
        self, store, skills_dir, make_plugin, monkeypatch, caplog
    ):
        """Requirement 15.5 — an unremovable link must not halt the sweep."""
        do_install(make_plugin("demo", skills=["alpha", "beta"]), store, skills_dir)
        store.unpublish("demo")

        real_unlink = Path.unlink

        def selective_unlink(self, *args, **kwargs):
            if self.name == "alpha":
                raise PermissionError("read-only filesystem")
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", selective_unlink)
        result = rebuild_projection(store, skills_dir=skills_dir)

        assert "beta" in result.swept  # the sweep kept going
        assert not (skills_dir / "beta").exists()


class TestSourceIntegrity:
    def test_a_record_pointing_at_a_missing_skill_reports_rather_than_raises(
        self, store, skills_dir, make_plugin
    ):
        do_install(make_plugin("demo", skills=["alpha"]), store, skills_dir)
        import shutil

        shutil.rmtree(store.plugin_root("demo") / "skills" / "alpha")

        result = rebuild_projection(store, skills_dir=skills_dir)

        assert "projection.source_missing" in codes(result.findings)
        assert dict(result.projected) == {}


# --- Property 8: non-shadowing and deterministic collision winner -----------
# Validates: Requirements 14.1, 14.2, 14.3, 14.4, 14.5

# Adversarial for iteration order: names differing only in the final character,
# and a creation order that is the reverse of their sort order — so a rule
# accidentally keyed on `installed_at` or `scandir` fails rather than passing by
# luck.
_ADVERSARIAL_NAME_SETS = st.sampled_from(
    [
        ("plug-a", "plug-b", "plug-c"),
        ("plug-c", "plug-b", "plug-a"),
        ("aaa", "aab", "aac"),
        ("zzz", "zzy", "zzx"),
        ("p1", "p2"),
        ("alpha.one", "alpha.two"),
    ]
)


@given(names=_ADVERSARIAL_NAME_SETS)
@settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_property_winner_is_invariant_under_install_order(tmp_path_factory, names):
    """Any permutation of install order elects the same winner."""
    base = tmp_path_factory.mktemp("perm")
    for name in names:
        build_plugin(base / "src" / name, name, skills=["shared"])

    expected = min(names)

    for index, order in enumerate(permutations(names)):
        home = base / f"home-{index}"
        store = InstalledPluginStore(home / "agent-plugins", home / "agent-plugin-data")
        sdir = home / "skills"
        sdir.mkdir(parents=True)

        for name in order:
            install(
                path_source(base / "src" / name),
                store=store,
                skills_dir=sdir,
                refresh_agents=False,
            )

        assert provenance.owning_plugin("shared", store) == expected, order


@given(names=_ADVERSARIAL_NAME_SETS, rebuilds=st.integers(min_value=2, max_value=4))
@settings(
    max_examples=15, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_property_repeated_rebuilds_never_flip_the_winner(tmp_path_factory, names, rebuilds):
    """Requirement 14.3 — an unchanged installed set rebuilds identically."""
    base = tmp_path_factory.mktemp("stable")
    home = base / "home"
    store = InstalledPluginStore(home / "agent-plugins", home / "agent-plugin-data")
    sdir = home / "skills"
    sdir.mkdir(parents=True)

    for name in names:
        source = build_plugin(base / "src" / name, name, skills=["shared"])
        install(path_source(source), store=store, skills_dir=sdir, refresh_agents=False)

    results = [dict(rebuild_projection(store, skills_dir=sdir).projected) for _ in range(rebuilds)]

    assert all(result == results[0] for result in results)
    assert results[0] == {"shared": min(names)}


@given(names=_ADVERSARIAL_NAME_SETS)
@settings(
    max_examples=20, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_property_loser_count_is_exactly_k_minus_one(tmp_path_factory, names):
    """k claimants → exactly k−1 ``projection.plugin_collision`` findings."""
    base = tmp_path_factory.mktemp("losers")
    home = base / "home"
    store = InstalledPluginStore(home / "agent-plugins", home / "agent-plugin-data")
    sdir = home / "skills"
    sdir.mkdir(parents=True)

    for name in names:
        source = build_plugin(base / "src" / name, name, skills=["shared"])
        install(path_source(source), store=store, skills_dir=sdir, refresh_agents=False)

    result = rebuild_projection(store, skills_dir=sdir)
    collisions = [f for f in result.findings if f.code == "projection.plugin_collision"]

    assert len(collisions) == len(names) - 1
    assert all(min(names) in f.message for f in collisions)


@given(preexisting=st.booleans(), names=_ADVERSARIAL_NAME_SETS)
@settings(
    max_examples=20, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_property_preexisting_always_beats_every_plugin(tmp_path_factory, preexisting, names):
    base = tmp_path_factory.mktemp("nonshadow")
    home = base / "home"
    store = InstalledPluginStore(home / "agent-plugins", home / "agent-plugin-data")
    sdir = home / "skills"
    sdir.mkdir(parents=True)

    if preexisting:
        write_skill(sdir / "shared", "shared", "pre-existing")

    for name in names:
        source = build_plugin(base / "src" / name, name, skills=["shared"])
        install(path_source(source), store=store, skills_dir=sdir, refresh_agents=False)

    if preexisting:
        assert provenance.owning_plugin("shared", store) is None
        assert "pre-existing" in (sdir / "shared" / "SKILL.md").read_text(encoding="utf-8")
    else:
        assert provenance.owning_plugin("shared", store) == min(names)
