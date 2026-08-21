"""Tests for the memory archive backend seam (#345 D1).

Covers the registry (register/get, ValueError on unknown), the ABC
contract (abstract methods enforced), and the report dataclasses'
exact counter sets.
"""

import dataclasses

import pytest

from cli_agent_orchestrator.constants import MEMORY_ARCHIVE_DEFAULT_FORMAT
from cli_agent_orchestrator.services import memory_archive
from cli_agent_orchestrator.services.memory_archive import (
    ExportReport,
    ImportReport,
    MemoryArchiveBackend,
    get_backend,
    register_backend,
)


class _FakeBackend(MemoryArchiveBackend):
    format_name = "fake"

    def export_bundle(self, scope, scope_id, dest, include_history, redact, prune=False):
        return ExportReport(exported=1)

    def import_bundle(self, src, target_scope, conflict_policy, dry_run):
        return ImportReport(imported=1, target_scope=target_scope, dry_run=dry_run)


@pytest.fixture(autouse=True)
def _clean_registry():
    """Isolate registry mutations per test."""
    saved = dict(memory_archive._backends)
    yield
    memory_archive._backends.clear()
    memory_archive._backends.update(saved)


class TestRegistry:
    def test_register_and_get(self):
        register_backend("fake", _FakeBackend)
        assert get_backend("fake") is _FakeBackend

    def test_unknown_name_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown memory archive format 'nope'"):
            get_backend("nope")

    def test_registered_backend_round_trips_reports(self, tmp_path):
        register_backend("fake", _FakeBackend)
        backend = get_backend("fake")()
        export = backend.export_bundle("global", None, tmp_path, False, False)
        assert isinstance(export, ExportReport)
        assert export.exported == 1
        imported = backend.import_bundle(tmp_path, "global", "skip", True)
        assert isinstance(imported, ImportReport)
        assert imported.target_scope == "global"
        assert imported.dry_run is True

    def test_default_format_constant(self):
        assert MEMORY_ARCHIVE_DEFAULT_FORMAT == "okf"


class TestAbc:
    def test_cannot_instantiate_abstract(self):
        with pytest.raises(TypeError):
            MemoryArchiveBackend()

    def test_partial_implementation_rejected(self):
        class OnlyExport(MemoryArchiveBackend):
            format_name = "partial"

            def export_bundle(self, scope, scope_id, dest, include_history, redact):
                return ExportReport()

        with pytest.raises(TypeError):
            OnlyExport()


class TestReportShapes:
    """Lock the exact counter sets the design mandates."""

    def test_export_report_fields(self):
        names = {f.name for f in dataclasses.fields(ExportReport)}
        assert names == {
            "exported",
            "skipped_secret",
            "redacted",
            "pruned",
            "unchanged",
            "links_dropped",
            "skip_reasons",
        }
        r = ExportReport()
        assert (r.exported, r.skipped_secret, r.redacted) == (0, 0, 0)
        assert r.skip_reasons == {}

    def test_export_report_skip_reasons_carry_pattern_names_only(self):
        r = ExportReport(skipped_secret=1, skip_reasons={"my-topic": ["aws_access_key"]})
        assert r.skip_reasons["my-topic"] == ["aws_access_key"]

    def test_import_report_fields(self):
        names = {f.name for f in dataclasses.fields(ImportReport)}
        assert names == {
            "imported",
            "skipped_conflict",
            "replaced",
            "merged",
            "rejected",
            "see_also_dropped",
            "bodies_escaped",
            "timestamps_clamped",
            "errors",
            "target_scope",
            "target_scope_id",
            "dry_run",
        }
        r = ImportReport()
        assert r.errors == {}
        assert r.dry_run is False
        assert r.target_scope_id is None

    def test_report_default_factories_not_shared(self):
        a, b = ExportReport(), ExportReport()
        a.skip_reasons["k"] = ["x"]
        assert b.skip_reasons == {}
        c, d = ImportReport(), ImportReport()
        c.errors["f.md"] = "bad"
        assert d.errors == {}
