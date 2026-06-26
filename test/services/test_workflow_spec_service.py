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

import os
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
        spec = svc.load_and_validate(str(path), base_dir=str(spec_dir))
        assert spec.name == "alpha"
        assert spec.mode == "sequential"
        assert len(spec.steps) == 1

    def test_missing_file_raises_filenotfound(self, spec_dir):
        with pytest.raises(FileNotFoundError):
            svc.load_and_validate(str(spec_dir / "nope.yaml"), base_dir=str(spec_dir))

    def test_invalid_spec_raises_valueerror(self, spec_dir):
        # Duplicate step id -> grammar fail -> ValueError (maps to 400).
        bad = (
            "name: bad\nmode: sequential\nsteps:\n"
            "  - id: dup\n    provider: claude_code\n    agent: developer\n    prompt: x\n"
            "  - id: dup\n    provider: claude_code\n    agent: developer\n    prompt: y\n"
        )
        path = _write_spec(spec_dir, "bad", bad)
        with pytest.raises(ValueError):
            svc.load_and_validate(str(path), base_dir=str(spec_dir))

    def test_non_string_yaml_key_raises_valueerror_not_typeerror(self, spec_dir):
        """A parseable spec with a non-string mapping key (``1: foo``) must
        surface as the narrow ``ValueError`` the API maps to 400 — NOT leak a
        ``TypeError`` from ``WorkflowSpec(**data)`` (PR #320 never-raise class)."""
        path = _write_spec(spec_dir, "intkey", "1: foo\nname: intkey\nsteps: []\n")
        with pytest.raises(ValueError):
            svc.load_and_validate(str(path), base_dir=str(spec_dir))

    def test_blocked_directory_rejected(self, spec_dir):
        """A spec path in a blocked system directory is rejected before any
        stat/open (CodeQL py/path-injection guard, PR #326 sec-bot finding)."""
        with pytest.raises(ValueError):
            svc.load_and_validate("/etc/passwd.yaml")

    def test_path_escaping_validated_dir_rejected(self, spec_dir, tmp_path):
        """A spec path whose realpath escapes its configured base directory via a
        symlink is rejected, not silently followed (the now load-bearing
        containment SafeAccessCheck — PR #326 dead-assertion fix)."""
        import os

        # A symlink inside the (allowed) base dir pointing OUT of it: realpath
        # resolves outside spec_dir, so the containment guard trips even though
        # spec_dir itself is the bound base.
        target = "/etc/hosts"
        link = spec_dir / "sneaky.yaml"
        if not os.path.exists(target):
            pytest.skip("no stable escape target on this platform")
        os.symlink(target, link)
        with pytest.raises(ValueError):
            svc.load_and_validate(str(link), base_dir=str(spec_dir))

    def test_valid_spec_outside_base_dir_rejected(self, spec_dir, tmp_path):
        """A perfectly valid spec that resolves OUTSIDE the configured base is
        rejected — the containment check now constrains something (Option A,
        PR #326 dead-assertion fix). Previously this passed because the base was
        derived from the file's own parent (tautological)."""
        # Write a valid spec in one allowed dir, but bind the base to a DIFFERENT
        # allowed dir. The spec resolves outside that base -> ValueError.
        other_base = Path.home() / ".cao-test-workflows" / uuid.uuid4().hex
        other_base.mkdir(parents=True, exist_ok=True)
        try:
            tmux_client._resolve_and_validate_working_directory(str(other_base))
            path = _write_spec(spec_dir, "stray")
            with pytest.raises(ValueError, match="escapes its validated directory"):
                svc.load_and_validate(str(path), base_dir=str(other_base))
        finally:
            import shutil

            shutil.rmtree(other_base, ignore_errors=True)

    def test_file_read_once_no_toctou(self, spec_dir, monkeypatch):
        """The spec file is opened EXACTLY ONCE (PR #326 TOCTOU fix): the same
        decoded text feeds grammar validation and model construction. A second
        read could pick up a mutated revision that never cleared validation."""
        path = _write_spec(spec_dir, "once")
        real_open = open
        opens = {"count": 0}

        def counting_open(file, *a, **kw):
            if str(file) == os.path.realpath(str(path)):
                opens["count"] += 1
            return real_open(file, *a, **kw)

        monkeypatch.setattr("builtins.open", counting_open)
        spec = svc.load_and_validate(str(path), base_dir=str(spec_dir))
        assert spec.name == "once"
        assert opens["count"] == 1


class TestValidateOnly:
    def test_pass(self, spec_dir):
        path = _write_spec(spec_dir, "ok")
        result = svc.validate_only(str(path), base_dir=str(spec_dir))
        assert result.status == "pass"

    def test_pass_reserved_for_parallel(self, spec_dir):
        body = _GOOD_SPEC.format(name="par").replace("mode: sequential", "mode: parallel")
        path = _write_spec(spec_dir, "par", body)
        result = svc.validate_only(str(path), base_dir=str(spec_dir))
        assert result.status == "pass_reserved"
        assert any("reserved" in n for n in result.reserved_notes)

    def test_fail_does_not_raise(self, spec_dir):
        path = _write_spec(spec_dir, "broken", "name: broken\nsteps: []\n")
        result = svc.validate_only(str(path), base_dir=str(spec_dir))
        assert result.status == "fail"
        assert result.errors

    def test_non_string_yaml_key_does_not_raise(self, spec_dir):
        """A parseable spec with a non-string mapping key must come back as a
        clean ``fail`` ValidationResult — validate_only NEVER raises (FR-1.3),
        even when ``WorkflowSpec(**data)`` would raise ``TypeError`` (PR #320)."""
        path = _write_spec(spec_dir, "intkey", "1: foo\nname: intkey\nsteps: []\n")
        result = svc.validate_only(str(path), base_dir=str(spec_dir))
        assert result.status == "fail"
        assert result.errors

    def test_missing_file_returns_fail_not_raises(self, spec_dir):
        """A nonexistent (but in-policy) spec path is a ``fail`` result, NOT an
        exception — the service reads the file behind the guard and degrades to
        the model's never-raise contract (PR #326 CodeQL text-only refactor)."""
        result = svc.validate_only(str(spec_dir / "ghost.yaml"), base_dir=str(spec_dir))
        assert result.status == "fail"
        assert result.errors

    def test_model_validate_only_never_opens_a_path(self, spec_dir, monkeypatch):
        """The model-level ``validate_only`` is text-only: it must NEVER call
        ``open`` even when handed a string that happens to be a real file path
        (PR #326 — removes the path-injection sink at the source)."""
        from cli_agent_orchestrator.models import workflow as model

        path = _write_spec(spec_dir, "decoy")

        def _boom(*a, **kw):
            raise AssertionError("model.validate_only must not open the filesystem")

        monkeypatch.setattr("builtins.open", _boom)
        # Passing a real path string -> treated as raw YAML text, no open() call.
        result = model.validate_only(str(path))
        assert result.status == "fail"  # the path string is not valid spec YAML


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
        spec = svc.load_and_validate(str(path), base_dir=str(spec_dir))
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
