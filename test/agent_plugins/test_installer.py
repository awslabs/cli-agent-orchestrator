"""Installer tests — correctness properties P4 (isolation), P5 (idempotence), P6 (skills-only)."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cli_agent_orchestrator.agent_plugins.installer import (
    PluginInstallError,
    affected_sessions,
    install,
    uninstall,
    validate_source,
)
from cli_agent_orchestrator.agent_plugins.models import PluginSource, Severity
from cli_agent_orchestrator.agent_plugins.store import InstalledPluginStore

from .conftest import CANONICAL_EXAMPLE_DIR, build_plugin


def path_source(root: Path, **kwargs) -> PluginSource:
    return PluginSource(kind="path", location=str(root), **kwargs)


def snapshot(*roots: Path) -> dict:
    """A content-addressed snapshot of every tree, for exact-equality assertions."""
    state: dict = {}
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            key = str(path.relative_to(root))
            if path.is_symlink():
                state[f"{root.name}:{key}"] = f"link->{os.readlink(path)}"
            elif path.is_dir():
                state[f"{root.name}:{key}"] = "dir"
            else:
                state[f"{root.name}:{key}"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return state


def do_install(source: Path, store, skills_dir, **kwargs):
    return install(
        path_source(source), store=store, skills_dir=skills_dir, refresh_agents=False, **kwargs
    )


class TestInstallHappyPath:
    def test_install_publishes_projects_and_records(self, store, skills_dir, make_plugin):
        source = make_plugin("demo", skills=["alpha"])
        outcome = do_install(source, store, skills_dir)

        assert outcome.installed
        assert outcome.record is not None
        assert outcome.record.projected_skill_names == ("alpha",)
        assert (skills_dir / "alpha" / "SKILL.md").is_file()
        assert (store.plugin_root("demo") / "plugin.json").is_file()

    def test_install_records_the_source_for_later_display(self, store, skills_dir, make_plugin):
        source = make_plugin("demo")
        outcome = do_install(source, store, skills_dir)

        assert outcome.record.source.kind == "path"
        assert outcome.record.source.location == str(source)
        assert outcome.record.version == "1.0.0"

    def test_install_creates_the_plugin_data_directory(self, store, skills_dir, make_plugin):
        """§9.1's persistent directory exists from install, not from first use."""
        do_install(make_plugin("demo"), store, skills_dir)
        assert store.plugin_data_dir("demo").is_dir()

    def test_canonical_example_installs_and_delivers_its_skill(self, store, skills_dir):
        """AC4: ``cao plugin add`` installs the canonical example plugin."""
        outcome = do_install(CANONICAL_EXAMPLE_DIR, store, skills_dir)

        assert outcome.installed
        assert "migrate-agent-plugin" in outcome.record.projected_skill_names
        assert (skills_dir / "migrate-agent-plugin" / "SKILL.md").is_file()

    def test_canonical_example_with_an_invalid_sibling_skill(self, store, skills_dir, tmp_path):
        """AC4: an intentionally invalid sibling is skipped, the fixture still installs."""
        import shutil

        source = tmp_path / "example-plus"
        shutil.copytree(CANONICAL_EXAMPLE_DIR, source)
        broken = source / "skills" / "intentionally-broken"
        broken.mkdir()
        (broken / "SKILL.md").write_text(
            "---\nname: a-different-name\ndescription: d\n---\n\nx\n", encoding="utf-8"
        )

        outcome = do_install(source, store, skills_dir)

        assert outcome.installed
        assert "migrate-agent-plugin" in outcome.record.projected_skill_names
        assert any(f.code == "skill.invalid" for f in outcome.report.findings)


class TestNameCollision:
    def test_reinstalling_without_force_is_refused_and_suggests_force(
        self, store, skills_dir, make_plugin
    ):
        source = make_plugin("demo")
        do_install(source, store, skills_dir)

        with pytest.raises(PluginInstallError, match="--force"):
            do_install(source, store, skills_dir)

    def test_force_replaces_and_keeps_the_projection(self, store, skills_dir, tmp_path):
        first = build_plugin(tmp_path / "v1" / "demo", "demo", skills=["alpha"])
        do_install(first, store, skills_dir)

        second = build_plugin(tmp_path / "v2" / "demo", "demo", skills=["alpha", "beta"])
        outcome = do_install(second, store, skills_dir, force=True)

        assert outcome.record.projected_skill_names == ("alpha", "beta")
        assert (skills_dir / "beta" / "SKILL.md").is_file()


class TestDryRun:
    def test_dry_run_publishes_nothing(self, store, skills_dir, make_plugin):
        source = make_plugin("demo", skills=["alpha"])
        before = snapshot(store.plugins_dir, skills_dir)

        outcome = do_install(source, store, skills_dir, dry_run=True)

        assert outcome.dry_run and not outcome.installed
        assert outcome.report.loadable
        assert snapshot(store.plugins_dir, skills_dir) == before

    def test_validate_source_is_the_dry_run(self, store, make_plugin):
        source = make_plugin("demo", skills=["alpha"])
        report = validate_source(path_source(source), store=store)

        assert report.loadable
        assert report.skill_names == ("alpha",)


class TestUninstall:
    def test_uninstall_removes_the_root_the_record_and_the_projection(
        self, store, skills_dir, make_plugin
    ):
        do_install(make_plugin("demo", skills=["alpha"]), store, skills_dir)

        outcome = uninstall("demo", store=store, skills_dir=skills_dir, refresh_agents=False)

        assert outcome.removed
        assert not store.plugin_root("demo").exists()
        assert store.get("demo") is None
        assert not (skills_dir / "alpha").exists()

    def test_uninstalling_an_absent_plugin_is_an_error(self, store, skills_dir):
        with pytest.raises(PluginInstallError, match="not installed"):
            uninstall("ghost", store=store, skills_dir=skills_dir, refresh_agents=False)

    def test_purge_data_is_opt_in(self, store, skills_dir, make_plugin):
        do_install(make_plugin("demo", skills=["alpha"]), store, skills_dir)
        (store.plugin_data_dir("demo", create=True) / "state").write_text("x", encoding="utf-8")

        uninstall("demo", store=store, skills_dir=skills_dir, refresh_agents=False)
        assert store.plugin_data_dir("demo").exists()


class TestAffectedSessions:
    def test_no_live_sessions_means_nothing_affected(self, store, skills_dir, make_plugin):
        do_install(make_plugin("demo", skills=["alpha"]), store, skills_dir)
        assert affected_sessions("demo", store=store) == []

    def test_a_session_whose_profile_matches_is_reported(
        self, store, skills_dir, make_plugin, monkeypatch
    ):
        do_install(make_plugin("demo", skills=["alpha"]), store, skills_dir)

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.session_service.list_sessions",
            lambda: [{"id": "cao-work"}],
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.clients.database.list_terminals_by_session",
            lambda name: [{"id": "abcd1234", "agent_profile": "worker"}],
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.load_agent_profile",
            lambda name: type("P", (), {"skills": ["alpha"]})(),
        )

        affected = affected_sessions("demo", store=store)
        assert len(affected) == 1
        assert affected[0].terminal_id == "abcd1234"
        assert affected[0].skill_names == ("alpha",)

    def test_a_profile_with_no_skill_filter_receives_the_full_catalog(
        self, store, skills_dir, make_plugin, monkeypatch
    ):
        """``skills: None`` means the whole catalog, so it *does* reference them."""
        do_install(make_plugin("demo", skills=["alpha"]), store, skills_dir)

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.session_service.list_sessions",
            lambda: [{"id": "cao-work"}],
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.clients.database.list_terminals_by_session",
            lambda name: [{"id": "abcd1234", "agent_profile": "worker"}],
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.load_agent_profile",
            lambda name: type("P", (), {"skills": None})(),
        )

        assert affected_sessions("demo", store=store)[0].skill_names == ("alpha",)

    def test_a_non_matching_filter_is_not_affected(
        self, store, skills_dir, make_plugin, monkeypatch
    ):
        do_install(make_plugin("demo", skills=["alpha"]), store, skills_dir)

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.session_service.list_sessions",
            lambda: [{"id": "cao-work"}],
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.clients.database.list_terminals_by_session",
            lambda name: [{"id": "abcd1234", "agent_profile": "worker"}],
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.load_agent_profile",
            lambda name: type("P", (), {"skills": ["something-else"]})(),
        )

        assert affected_sessions("demo", store=store) == []

    def test_a_broken_session_backend_never_blocks_removal(
        self, store, skills_dir, make_plugin, monkeypatch
    ):
        do_install(make_plugin("demo", skills=["alpha"]), store, skills_dir)

        def boom():
            raise RuntimeError("tmux is gone")

        monkeypatch.setattr("cli_agent_orchestrator.services.session_service.list_sessions", boom)
        assert affected_sessions("demo", store=store) == []


# --- Property 4: Isolation --------------------------------------------------
# Validates: Requirements 9.2, 9.3, 9.4

_INVALID_PLUGIN_SHAPES = st.sampled_from(
    [
        {"manifest_text": "{ broken"},
        {"manifest_text": "[]"},
        {"manifest_text": ""},
        {"schema_id": None},
        {"schema_id": "https://example.invalid/x.schema.json"},
        {"name_override": "Bad Name"},
        {"name_override": "trailing-"},
        {"extra_manifest": {"name": 42}},
    ]
)


@given(shape=_INVALID_PLUGIN_SHAPES, preexisting=st.integers(min_value=0, max_value=2))
@settings(
    max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_property_invalid_install_changes_nothing(tmp_path_factory, shape, preexisting):
    """For any invalid plugin and any pre-existing installed set, nothing changes."""
    base = tmp_path_factory.mktemp("isolation")
    store = InstalledPluginStore(base / "agent-plugins", base / "agent-plugin-data")
    skills_dir = base / "skills"
    skills_dir.mkdir()

    for index in range(preexisting):
        good = build_plugin(base / "src" / f"good-{index}", f"good-{index}", skills=[f"s{index}"])
        install(path_source(good), store=store, skills_dir=skills_dir, refresh_agents=False)

    before = snapshot(store.plugins_dir, skills_dir)

    options = dict(shape)
    name = options.pop("name_override", "bad-plugin")
    bad = build_plugin(base / "src" / "bad", name, skills=["dangerous"], **options)

    try:
        outcome = install(
            path_source(bad), store=store, skills_dir=skills_dir, refresh_agents=False
        )
        assert not outcome.installed
    except PluginInstallError:
        pass

    assert snapshot(store.plugins_dir, skills_dir) == before


# --- Property 5: Idempotence ------------------------------------------------
# Validates: Requirements 10.1, 10.2, 10.3, 10.4


@given(skill_count=st.integers(min_value=0, max_value=3))
@settings(
    max_examples=20, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_property_force_reinstall_equals_a_single_install(tmp_path_factory, skill_count):
    """``add(X)`` then ``add(X, force=True)`` == a single ``add(X)``."""
    base = tmp_path_factory.mktemp("idem")
    skills = [f"skill-{i}" for i in range(skill_count)]
    source = build_plugin(base / "src", "demo", skills=skills)

    def fresh(label: str):
        home = base / label
        store = InstalledPluginStore(home / "agent-plugins", home / "agent-plugin-data")
        sdir = home / "skills"
        sdir.mkdir(parents=True)
        return store, sdir

    store_a, skills_a = fresh("once")
    install(path_source(source), store=store_a, skills_dir=skills_a, refresh_agents=False)

    store_b, skills_b = fresh("twice")
    install(path_source(source), store=store_b, skills_dir=skills_b, refresh_agents=False)
    install(
        path_source(source), store=store_b, skills_dir=skills_b, force=True, refresh_agents=False
    )

    def normalize(state: dict) -> dict:
        # The install record carries a timestamp and the projected root path,
        # neither of which is part of "the same store state".
        return {k: v for k, v in state.items() if ".state" not in k and "link->" not in str(v)}

    assert normalize(snapshot(store_a.plugins_dir, skills_a)) == normalize(
        snapshot(store_b.plugins_dir, skills_b)
    )
    assert store_a.get("demo").skill_names == store_b.get("demo").skill_names


@given(skill_count=st.integers(min_value=1, max_value=3), purge=st.booleans())
@settings(
    max_examples=20, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_property_install_then_remove_restores_the_prior_state(
    tmp_path_factory, skill_count, purge
):
    """``add(X)`` then ``remove(X)`` restores the pre-add state; data persists unless purged."""
    base = tmp_path_factory.mktemp("roundtrip")
    store = InstalledPluginStore(base / "agent-plugins", base / "agent-plugin-data")
    skills_dir = base / "skills"
    skills_dir.mkdir()

    resident = build_plugin(base / "src" / "resident", "resident", skills=["kept"])
    install(path_source(resident), store=store, skills_dir=skills_dir, refresh_agents=False)

    before = snapshot(store.plugins_dir, skills_dir)

    source = build_plugin(
        base / "src" / "demo", "demo", skills=[f"tmp-{i}" for i in range(skill_count)]
    )
    install(path_source(source), store=store, skills_dir=skills_dir, refresh_agents=False)
    (store.plugin_data_dir("demo", create=True) / "state").write_text("s", encoding="utf-8")

    uninstall("demo", purge_data=purge, store=store, skills_dir=skills_dir, refresh_agents=False)

    assert snapshot(store.plugins_dir, skills_dir) == before
    assert store.plugin_data_dir("demo").exists() is (not purge)


# --- Property 6: Skills-only conformance ------------------------------------
# Validates: Requirements 11.1, 11.5


@given(skill_count=st.integers(min_value=1, max_value=4))
@settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_property_skills_only_plugins_fully_conform(tmp_path_factory, skill_count):
    """Valid manifest + ≥1 valid skill + no ``mcp.json`` → everything projects, zero fatals."""
    base = tmp_path_factory.mktemp("skillsonly")
    store = InstalledPluginStore(base / "agent-plugins", base / "agent-plugin-data")
    skills_dir = base / "skills"
    skills_dir.mkdir()

    names = [f"skill-{i}" for i in range(skill_count)]
    source = build_plugin(base / "src", "demo", skills=names)

    outcome = install(path_source(source), store=store, skills_dir=skills_dir, refresh_agents=False)

    assert outcome.report.loadable
    assert outcome.report.mcp_present is False
    assert not any(f.severity is Severity.FATAL for f in outcome.findings)
    assert set(outcome.record.projected_skill_names) == set(names)
    for name in names:
        assert (skills_dir / name / "SKILL.md").is_file()
