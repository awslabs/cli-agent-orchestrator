"""Tests for the workflow spec authoring service (issue #312, Bolt 2 / N2).

Covers load/validate, upsert+list, the byte-identical rebuild invariant
(FR-2.1 / C1a — drop the index, relist, assert identical), delete (+ 404 on
repeat), unknown name, unparseable-file skip, and name-validation rejection
(traversal).

NB-F1: test spec dirs must NOT live under /tmp — the shared validator BLOCKS it.
``tmp_path`` resolves to ``/private/var/folders/...`` on macOS (allowed) but to
``/tmp/pytest-...`` on Linux (blocked). To stay portable, the ``spec_dir``
fixture creates the directory under the user's home (always outside the blocked
frozenset) and verifies it passes the real shared validator.
"""

import sqlite3
import uuid
from pathlib import Path

import pytest

from cli_agent_orchestrator.clients.database import _migrate_workflow_index
from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.services import workflow_spec_service as svc

_GOOD_SPEC = """\
name: {name}
description: a {name} workflow
mode: sequential
steps:
  - id: only-step
    provider: claude_code
    agent: developer
    prompt: do the thing
"""


@pytest.fixture
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point DATABASE_FILE at a throwaway DB and create the workflow_index table.

    The service's ``_connect`` re-imports DATABASE_FILE from constants on each
    call, so patching the constant is sufficient.
    """
    db_path = tmp_path / "wf.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path, raising=True)
    _migrate_workflow_index()  # zero-arg, self-connecting, idempotent
    return db_path


@pytest.fixture
def spec_dir() -> Path:
    """An allowed (non-blocked) spec directory under the user's home.

    Verified against the real shared validator so the test exercises the same
    path policy production does.
    """
    base = Path.home() / ".cao-test-workflows" / uuid.uuid4().hex
    base.mkdir(parents=True, exist_ok=True)
    # Assert the dir is NOT rejected by the shared validator (NB-F1 guard).
    tmux_client._resolve_and_validate_working_directory(str(base))
    try:
        yield base
    finally:
        import shutil

        shutil.rmtree(base, ignore_errors=True)


def _write_spec(spec_dir: Path, name: str, body: str = None) -> Path:
    path = spec_dir / f"{name}.yaml"
    path.write_text(body if body is not None else _GOOD_SPEC.format(name=name))
    return path


class TestLoadAndValidate:
    def test_loads_valid_spec(self, spec_dir):
        path = _write_spec(spec_dir, "alpha")
        spec = svc.load_and_validate(str(path))
        assert spec.name == "alpha"
        assert spec.mode == "sequential"
        assert len(spec.steps) == 1

    def test_missing_file_raises_filenotfound(self, spec_dir):
        with pytest.raises(FileNotFoundError):
            svc.load_and_validate(str(spec_dir / "nope.yaml"))

    def test_invalid_spec_raises_valueerror(self, spec_dir):
        # Duplicate step id -> grammar fail -> ValueError (maps to 400).
        bad = (
            "name: bad\nmode: sequential\nsteps:\n"
            "  - id: dup\n    provider: claude_code\n    agent: developer\n    prompt: x\n"
            "  - id: dup\n    provider: claude_code\n    agent: developer\n    prompt: y\n"
        )
        path = _write_spec(spec_dir, "bad", bad)
        with pytest.raises(ValueError):
            svc.load_and_validate(str(path))

    def test_non_string_yaml_key_raises_valueerror_not_typeerror(self, spec_dir):
        """A parseable spec with a non-string mapping key (``1: foo``) must
        surface as the narrow ``ValueError`` the API maps to 400 — NOT leak a
        ``TypeError`` from ``WorkflowSpec(**data)`` (PR #320 never-raise class)."""
        path = _write_spec(spec_dir, "intkey", "1: foo\nname: intkey\nsteps: []\n")
        with pytest.raises(ValueError):
            svc.load_and_validate(str(path))


class TestValidateOnly:
    def test_pass(self, spec_dir):
        path = _write_spec(spec_dir, "ok")
        result = svc.validate_only(str(path))
        assert result.status == "pass"

    def test_pass_reserved_for_parallel(self, spec_dir):
        body = _GOOD_SPEC.format(name="par").replace("mode: sequential", "mode: parallel")
        path = _write_spec(spec_dir, "par", body)
        result = svc.validate_only(str(path))
        assert result.status == "pass_reserved"
        assert any("reserved" in n for n in result.reserved_notes)

    def test_fail_does_not_raise(self, spec_dir):
        path = _write_spec(spec_dir, "broken", "name: broken\nsteps: []\n")
        result = svc.validate_only(str(path))
        assert result.status == "fail"
        assert result.errors

    def test_non_string_yaml_key_does_not_raise(self, spec_dir):
        """A parseable spec with a non-string mapping key must come back as a
        clean ``fail`` ValidationResult — validate_only NEVER raises (FR-1.3),
        even when ``WorkflowSpec(**data)`` would raise ``TypeError`` (PR #320)."""
        path = _write_spec(spec_dir, "intkey", "1: foo\nname: intkey\nsteps: []\n")
        result = svc.validate_only(str(path))
        assert result.status == "fail"
        assert result.errors


class TestIndexUpsertAndList:
    def test_upsert_then_list(self, isolated_db, spec_dir):
        for nm in ("beta", "alpha", "gamma"):
            _write_spec(spec_dir, nm)
        rows = svc.list_workflows(scan_dir=str(spec_dir))
        names = [r.name for r in rows]
        # Ordered by name (B2-BR-3).
        assert names == ["alpha", "beta", "gamma"]
        assert all(r.step_count == 1 for r in rows)

    def test_upsert_is_idempotent_on_name(self, isolated_db, spec_dir):
        path = _write_spec(spec_dir, "dupe")
        spec = svc.load_and_validate(str(path))
        svc.upsert_index(spec, str(path))
        svc.upsert_index(spec, str(path))  # second upsert must not duplicate
        rows = svc.list_workflows(scan_dir=str(spec_dir))
        assert [r.name for r in rows] == ["dupe"]

    def test_byte_identical_rebuild_after_drop(self, isolated_db, spec_dir):
        for nm in ("zeta", "delta", "epsilon"):
            _write_spec(spec_dir, nm)
        before = [r.model_dump(exclude={"indexed_at"}) for r in svc.list_workflows(str(spec_dir))]

        # Drop the derived table entirely.
        with sqlite3.connect(str(isolated_db)) as conn:
            conn.execute("DROP TABLE workflow_index")
            conn.commit()
        _migrate_workflow_index()  # recreate empty

        after = [r.model_dump(exclude={"indexed_at"}) for r in svc.list_workflows(str(spec_dir))]
        assert before == after

    def test_unparseable_file_skipped(self, isolated_db, spec_dir):
        _write_spec(spec_dir, "good")
        # A malformed YAML file is skipped (logged), not fatal.
        (spec_dir / "garbage.yaml").write_text("name: garbage\nsteps: [\n")
        rows = svc.list_workflows(scan_dir=str(spec_dir))
        assert [r.name for r in rows] == ["good"]


class TestGetWorkflow:
    def test_get_by_name(self, isolated_db, spec_dir):
        _write_spec(spec_dir, "fetchme")
        svc.list_workflows(scan_dir=str(spec_dir))  # populate index
        spec = svc.get_workflow("fetchme", scan_dir=str(spec_dir))
        assert spec.name == "fetchme"

    def test_get_unknown_name_raises_keyerror(self, isolated_db, spec_dir):
        with pytest.raises(KeyError):
            svc.get_workflow("ghost", scan_dir=str(spec_dir))

    def test_get_rejects_traversal_name(self, isolated_db, spec_dir):
        with pytest.raises(ValueError):
            svc.get_workflow("..", scan_dir=str(spec_dir))

    def test_get_rejects_path_separator_name(self, isolated_db, spec_dir):
        with pytest.raises(ValueError):
            svc.get_workflow("../etc/passwd", scan_dir=str(spec_dir))


class TestDeleteWorkflow:
    def test_delete_removes_file_and_row(self, isolated_db, spec_dir):
        path = _write_spec(spec_dir, "removeme")
        svc.list_workflows(scan_dir=str(spec_dir))
        svc.delete_workflow("removeme", scan_dir=str(spec_dir))
        assert not path.exists()
        rows = svc.list_workflows(scan_dir=str(spec_dir))
        assert [r.name for r in rows] == []

    def test_delete_unknown_raises_keyerror(self, isolated_db, spec_dir):
        with pytest.raises(KeyError):
            svc.delete_workflow("never", scan_dir=str(spec_dir))

    def test_repeat_delete_is_404_not_silent(self, isolated_db, spec_dir):
        _write_spec(spec_dir, "twice")
        svc.list_workflows(scan_dir=str(spec_dir))
        svc.delete_workflow("twice", scan_dir=str(spec_dir))
        with pytest.raises(KeyError):
            svc.delete_workflow("twice", scan_dir=str(spec_dir))
