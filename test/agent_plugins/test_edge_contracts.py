"""Validation, projection, store and CLI edge contracts.

Three themes, all stated guarantees rather than incidental branches:

* **The pinned schemas are a hard dependency.** A missing, unreadable or
  malformed vendored schema must surface as a *finding* naming the problem, never
  as a traceback and never as a silent pass — the offline-validation claim (§5.2)
  depends on it.
* **Projection never raises into terminal creation.** An unwritable skill store or
  an unsafe recorded name degrades to a finding, because the alternative is a
  launch that fails.
* **The CLI turns library errors into `ClickException`.** A raw traceback out of
  `cao plugin ...` is a bug regardless of the cause.
"""

from __future__ import annotations

import json
import shutil

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.agent_plugins import projection as projection_mod
from cli_agent_orchestrator.agent_plugins import validation as validation_mod
from cli_agent_orchestrator.agent_plugins.projection import (
    ProjectionClaimError,
    rebuild_projection,
    release_projection_claim,
)
from cli_agent_orchestrator.agent_plugins.store import PluginStoreError, _validate_plugin_dirname
from cli_agent_orchestrator.agent_plugins.validation import (
    SchemaUnavailableError,
    load_pinned_schema,
    supported_schema_id,
    validate_plugin,
)

from .conftest import build_plugin
from .test_store import make_record


class TestPinnedSchemaLoading:
    def test_an_unknown_schema_filename_is_rejected(self):
        with pytest.raises(SchemaUnavailableError, match="Unknown pinned schema"):
            load_pinned_schema("not-a-schema.json")

    def test_an_unreadable_schema_is_reported(self, monkeypatch):
        load_pinned_schema.cache_clear()
        monkeypatch.setattr(
            validation_mod.resources,
            "files",
            lambda _pkg: (_ for _ in ()).throw(FileNotFoundError("gone")),
        )
        try:
            with pytest.raises(SchemaUnavailableError, match="not readable"):
                load_pinned_schema(validation_mod.PLUGIN_SCHEMA_FILENAME)
        finally:
            monkeypatch.undo()
            load_pinned_schema.cache_clear()

    def test_a_malformed_schema_is_reported(self, monkeypatch):
        load_pinned_schema.cache_clear()

        class FakeAnchor:
            def joinpath(self, *_a):
                return self

            def read_text(self, *_a, **_k):
                return "{not valid json"

        monkeypatch.setattr(validation_mod.resources, "files", lambda _pkg: FakeAnchor())
        try:
            with pytest.raises(SchemaUnavailableError, match="not valid JSON"):
                load_pinned_schema(validation_mod.PLUGIN_SCHEMA_FILENAME)
        finally:
            monkeypatch.undo()
            load_pinned_schema.cache_clear()

    def test_a_schema_without_an_id_is_reported(self, monkeypatch):
        # `supported_schema_id` is itself lru_cached, so an earlier test in the
        # session can have already memoised the real answer. Clearing both caches
        # is what makes this assertion order-independent.
        load_pinned_schema.cache_clear()
        supported_schema_id.cache_clear()
        monkeypatch.setattr(validation_mod, "load_pinned_schema", lambda _f: {"type": "object"})
        try:
            with pytest.raises(SchemaUnavailableError, match="declares no \\$id"):
                supported_schema_id(validation_mod.PLUGIN_SCHEMA_FILENAME)
        finally:
            monkeypatch.undo()
            load_pinned_schema.cache_clear()
            supported_schema_id.cache_clear()

    def test_validation_reports_a_finding_when_the_schema_is_unavailable(
        self, tmp_path, monkeypatch
    ):
        """A broken install must produce a finding, not a pass and not a crash."""
        plugin = build_plugin(tmp_path / "p", "demo", skills=["alpha"])
        monkeypatch.setattr(
            validation_mod,
            "supported_schema_id",
            lambda _f: (_ for _ in ()).throw(SchemaUnavailableError("vendored schema missing")),
        )
        report = validate_plugin(plugin)

        assert report.loadable is False
        assert any("schema" in (f.message or "").lower() for f in report.findings)


class TestSkillsDirContainment:
    def test_a_skills_dir_that_escapes_the_root_is_reported(self, tmp_path, monkeypatch):
        """§4.1: a symlinked `skills/` pointing outside must not be traversed."""
        plugin = build_plugin(tmp_path / "p", "demo", skills=["alpha"])
        outside = tmp_path / "outside"
        outside.mkdir()
        shutil.rmtree(plugin / "skills")
        (plugin / "skills").symlink_to(outside, target_is_directory=True)

        report = validate_plugin(plugin)

        assert report.skills == () or report.skill_names == ()
        assert report.findings, "an escaping skills/ must be reported"

    def test_dot_prefixed_entries_are_not_skills(self, tmp_path):
        plugin = build_plugin(tmp_path / "p2", "demo", skills=["alpha"])
        hidden = plugin / "skills" / ".hidden"
        hidden.mkdir()
        (hidden / "SKILL.md").write_text("---\nname: .hidden\ndescription: d\n---\n", "utf-8")

        report = validate_plugin(plugin)

        assert ".hidden" not in report.skill_names
        assert "alpha" in report.skill_names


class TestProjectionDegradesRatherThanRaising:
    def test_an_unwritable_skill_store_is_a_finding_not_an_exception(
        self, store, tmp_path, monkeypatch
    ):
        target = tmp_path / "skills-ro"
        monkeypatch.setattr(
            projection_mod.Path,
            "mkdir",
            lambda self, *a, **k: (_ for _ in ()).throw(OSError("read-only filesystem")),
        )
        result = rebuild_projection(store, skills_dir=target)

        assert result.findings, "an unwritable store must be reported"

    def test_an_unsafe_recorded_plugin_name_is_a_finding(self, store, skills_dir, monkeypatch):
        """A hand-edited record must not turn into a path traversal."""
        store.write_record(make_record("demo", projected_skill_names=("alpha",)))
        monkeypatch.setattr(
            type(store),
            "plugin_root",
            lambda self, name: (_ for _ in ()).throw(ValueError(f"unsafe name {name!r}")),
        )
        result = rebuild_projection(store, skills_dir=skills_dir)

        assert "alpha" not in result.projected
        assert result.findings

    def test_a_failed_materialization_is_reported(self, store, skills_dir, tmp_path, monkeypatch):
        source = build_plugin(tmp_path / "src", "demo", skills=["alpha"])
        store.publish(source, make_record("demo", skill_names=("alpha",)))
        from cli_agent_orchestrator.agent_plugins.models import Finding, Severity

        monkeypatch.setattr(
            projection_mod,
            "_materialize",
            lambda store, winners, target_dir, mode, previous=None: (
                {},
                mode,
                [
                    Finding(
                        severity=Severity.SKIPPED,
                        code="projection.failed",
                        spec_ref="§6.1",
                        message="could not link alpha",
                        path="skills/alpha",
                    )
                ],
            ),
        )
        result = rebuild_projection(store, skills_dir=skills_dir)

        assert "alpha" not in result.projected or result.findings

    def test_release_reports_rather_than_swallows_an_unreadable_state_dir(self, store, monkeypatch):
        """Inverted deliberately (review finding F2).

        This test used to assert ``is None`` — the same value returned when no
        plugin claimed the name. That indistinguishability is the defect: the only
        caller, ``cao skills add --force``, read it as "nothing to release" and
        went on to replace the projection with the user's own directory while the
        record still claimed the name, which the next sweep then deleted. An
        unreadable state directory must surface as the failure state.
        """
        monkeypatch.setattr(
            type(store),
            "list_installed",
            lambda self: (_ for _ in ()).throw(OSError("state dir unreadable")),
        )
        with pytest.raises(ProjectionClaimError):
            release_projection_claim("anything", store=store)


class TestStoreNameValidation:
    @pytest.mark.parametrize("name", ["a/b", "a\\b", "..", "a/../b", "/abs"])
    def test_an_unsafe_plugin_name_is_rejected(self, name):
        with pytest.raises(ValueError):
            _validate_plugin_dirname(name)

    def test_a_safe_name_is_returned_unchanged(self):
        assert _validate_plugin_dirname("cao-contributor") == "cao-contributor"

    def test_is_installed_is_false_for_an_unsafe_name(self, store):
        """The read API must answer, not raise, for a name it would reject."""
        assert store.is_installed("../escape") is False

    def test_a_concurrent_occupant_without_force_fails_loudly(self, store, tmp_path, monkeypatch):
        """Deterministic version of the publish race: name free at entry, taken at swap."""
        source = build_plugin(tmp_path / "src", "raced2", skills=["alpha"])
        destination = store.plugin_root("raced2")
        real_copytree = shutil.copytree

        def occupy_then_copy(src, dst, *a, **k):
            result = real_copytree(src, dst, *a, **k)
            destination.mkdir(parents=True, exist_ok=True)  # a rival lands mid-flight
            (destination / "WHO.txt").write_text("rival", encoding="utf-8")
            return result

        monkeypatch.setattr(shutil, "copytree", occupy_then_copy)

        with pytest.raises(PluginStoreError, match="published concurrently"):
            store.publish(source, make_record("raced2"), force=False)

        monkeypatch.undo()
        assert (destination / "WHO.txt").read_text(encoding="utf-8") == "rival"


class TestCliTurnsLibraryErrorsIntoClickExceptions:
    def _invoke(self, args, monkeypatch, **patches):
        from cli_agent_orchestrator.cli.commands import agent_plugin as mod

        for name, value in patches.items():
            monkeypatch.setattr(mod, name, value, raising=False)
        return CliRunner().invoke(mod.agent_plugin, args)

    def test_add_reports_an_install_error_cleanly(self, monkeypatch, tmp_path):
        from cli_agent_orchestrator.agent_plugins.installer import PluginInstallError

        result = self._invoke(
            ["add", str(tmp_path)],
            monkeypatch,
            install=lambda *a, **k: (_ for _ in ()).throw(PluginInstallError("bad plugin")),
        )
        assert result.exit_code != 0
        assert "bad plugin" in result.output
        assert not isinstance(result.exception, PluginInstallError)

    def test_add_reports_an_unexpected_error_cleanly(self, monkeypatch, tmp_path):
        result = self._invoke(
            ["add", str(tmp_path)],
            monkeypatch,
            install=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert result.exit_code != 0
        assert "boom" in result.output

    def test_list_reports_a_store_error_cleanly(self, monkeypatch):
        result = self._invoke(
            ["list"],
            monkeypatch,
            sweep_dangling_projections=lambda *a, **k: None,
            InstalledPluginStore=lambda *a, **k: (_ for _ in ()).throw(OSError("unreadable")),
        )
        assert result.exit_code != 0
        assert "unreadable" in result.output

    def test_remove_reports_an_install_error_cleanly(self, monkeypatch):
        from cli_agent_orchestrator.agent_plugins.installer import PluginInstallError

        result = self._invoke(
            ["remove", "demo", "--yes"],
            monkeypatch,
            uninstall=lambda *a, **k: (_ for _ in ()).throw(PluginInstallError("not installed")),
        )
        assert result.exit_code != 0
        assert "not installed" in result.output
