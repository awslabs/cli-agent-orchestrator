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

    def test_oversized_spec_read_is_capped_not_full_file(self, spec_dir, monkeypatch):
        """The byte-cap check must not read an oversized file fully into memory
        first — ``fh.read()`` is called with a bounded size argument (MAX+1),
        so a multi-gigabyte spec is rejected after reading only MAX+1 bytes,
        not after buffering the whole file."""
        from cli_agent_orchestrator.constants import WORKFLOW_MAX_SPEC_BYTES

        path = _write_spec(spec_dir, "huge", "x" * (WORKFLOW_MAX_SPEC_BYTES * 4))
        real_open = open
        captured = {}

        class _WrappedFile:
            def __init__(self, fh):
                self._fh = fh

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return self._fh.__exit__(*exc)

            def read(self, size=-1):
                captured["size"] = size
                return self._fh.read(size)

        def wrapped_open(file, *a, **kw):
            fh = real_open(file, *a, **kw)
            if str(file) == os.path.realpath(str(path)):
                return _WrappedFile(fh)
            return fh

        monkeypatch.setattr("builtins.open", wrapped_open)
        with pytest.raises(ValueError, match="bytes"):
            svc.load_and_validate(str(path), base_dir=str(spec_dir))
        assert captured["size"] == WORKFLOW_MAX_SPEC_BYTES + 1


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


def _write_script(spec_dir: Path, name: str, body: str = "def main():\n    pass\n") -> Path:
    path = spec_dir / f"{name}.py"
    path.write_text(body)
    return path


class TestScriptTierGetWorkflow:
    """Tier detection + ScriptSpec resolution (issue #312, Bolt 4 / U5, A1)."""

    def test_py_extension_resolves_to_scriptspec(self, spec_dir):
        from cli_agent_orchestrator.models.workflow import ScriptSpec

        path = _write_script(spec_dir, "scr")
        spec = svc.get_workflow(str(path), scan_dir=str(spec_dir))
        assert isinstance(spec, ScriptSpec)
        assert spec.name == "scr"
        assert spec.findings == []

    def test_py_non_utf8_spec_raises_valueerror_not_unicodedecodeerror(self, spec_dir):
        """A ``.py`` spec containing invalid UTF-8 bytes must surface as the
        narrow ``ValueError`` the API maps to 400 — a bare ``UnicodeDecodeError``
        would leak past the boundary's ``except ValueError`` (it IS a
        ``ValueError`` subclass, but the read path re-raises it explicitly here
        for an unambiguous error message rather than relying on subclassing)."""
        path = spec_dir / "badenc.py"
        path.write_bytes(b"def main():\n    x = '\xff\xfe'\n    pass\n")
        with pytest.raises(ValueError):
            svc.get_workflow(str(path), scan_dir=str(spec_dir))

    def test_py_load_time_lint_carries_findings(self, spec_dir):
        path = _write_script(spec_dir, "badscr", "def main(:\n")
        spec = svc.get_workflow(str(path), scan_dir=str(spec_dir))
        assert any(f.rule_id == "syntax" for f in spec.findings)

    def test_py_valid_syntax_but_malformed_inputs_still_raises(self, spec_dir):
        """Graceful degradation is scoped to UNPARSEABLE source only. A
        syntactically VALID script whose ``INPUTS`` literal is malformed is a
        real author error the LOAD path must still surface as ``ValueError`` —
        skipping ``_extract_inputs`` only when a ``syntax`` finding exists must
        NOT swallow this case (guards against over-softening the fix)."""
        path = _write_script(
            spec_dir, "badinputs", "INPUTS = {'x': {'type': 'nonsense'}}\ndef main():\n    pass\n"
        )
        with pytest.raises(ValueError):
            svc.get_workflow(str(path), scan_dir=str(spec_dir))

    def test_cross_tier_collision_raises_at_access_time(self, spec_dir):
        from cli_agent_orchestrator.models.workflow import TierCollisionError

        _write_spec(spec_dir, "dup")
        _write_script(spec_dir, "dup")
        with pytest.raises(TierCollisionError):
            svc.get_workflow(str(spec_dir / "dup.py"), scan_dir=str(spec_dir))

    def test_unrecognized_extension_raises_valueerror(self, spec_dir):
        path = spec_dir / "weird.txt"
        path.write_text("hello")
        with pytest.raises(ValueError):
            svc.get_workflow(str(path), scan_dir=str(spec_dir))

    def test_yaml_arm_stays_byte_identical(self, spec_dir):
        # A .yaml file alongside a UNRELATED .py file (different stem) must
        # resolve exactly as before — no collision, no new dispatch executed.
        path = _write_spec(spec_dir, "onlyyaml")
        _write_script(spec_dir, "unrelated")
        spec = svc.get_workflow(str(path), scan_dir=str(spec_dir))
        assert spec.name == "onlyyaml"
        assert spec.mode == "sequential"

    def test_py_traversal_escaping_scan_dir_rejected(self, spec_dir, tmp_path):
        """A ``.py`` path reaching outside ``scan_dir`` via ``..`` traversal is
        rejected by ``_safe_spec_path`` before the file is ever opened
        (CodeQL py/path-injection sink at ``_read_script_spec``'s ``open()``)."""
        outside = tmp_path / "outside.py"
        outside.write_text("def main():\n    pass\n")
        with pytest.raises(ValueError, match="escapes its validated directory"):
            svc.get_workflow(str(spec_dir / ".." / "outside.py"), scan_dir=str(spec_dir))

    def test_py_symlink_escaping_scan_dir_rejected(self, spec_dir):
        """A ``.py`` symlink inside ``scan_dir`` whose target resolves OUTSIDE it
        is rejected — the resolved (not the literal) path is what's checked."""
        target = "/etc/hosts"
        link = spec_dir / "sneaky.py"
        if not os.path.exists(target):
            pytest.skip("no stable escape target on this platform")
        os.symlink(target, link)
        with pytest.raises(ValueError, match="escapes its validated directory"):
            svc.get_workflow(str(link), scan_dir=str(spec_dir))

    def test_py_bare_name_lookup_with_tampered_index_row_still_rejected(
        self, isolated_db, spec_dir, tmp_path
    ):
        """The bare-name arm of ``get_workflow`` resolves through
        ``_resolve_source_path`` -> a raw string pulled out of SQLite, then
        ``_load_by_extension`` -> ``_read_script_spec``. If the index row's
        ``source_path`` were ever tampered/stale/out-of-policy (e.g. a scan_dir
        reconfigured after indexing, or direct DB manipulation), the bare-name
        lookup must NOT trust that string — ``_read_script_spec`` re-validates
        it via ``_safe_spec_path`` before ever calling ``open()``, so the
        escape is still caught even though the name-based path never touches
        ``_safe_spec_path`` directly on the way in."""
        import sqlite3

        outside = tmp_path / "outside.py"
        outside.write_text("def main():\n    pass\n")

        with sqlite3.connect(str(isolated_db)) as conn:
            conn.execute(
                "INSERT INTO workflow_index "
                "(name, source_path, mode, step_count, description, indexed_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("tampered", str(outside), "script", None, "", "2026-01-01T00:00:00Z"),
            )
            conn.commit()

        # A name-based lookup that skips rebuild would read the tampered row
        # straight from the index; rebuild itself would also drop this row
        # since the file lives outside scan_dir. Either way, calling the
        # lower-level read primitive directly with the tampered string proves
        # _read_script_spec's OWN re-validation is what's protecting it.
        with pytest.raises(ValueError, match="escapes its validated directory"):
            svc._read_script_spec(str(outside), "tampered", base_dir=str(spec_dir))


class TestScriptTierRebuildIndex:
    """``rebuild_index_from_files`` .py glob widening (A2, BR-4)."""

    def test_script_row_has_none_step_count(self, isolated_db, spec_dir):
        _write_script(spec_dir, "scr")
        rows = svc.list_workflows(scan_dir=str(spec_dir))
        assert len(rows) == 1
        assert rows[0].mode == "script"
        assert rows[0].step_count is None

    def test_yaml_row_step_count_unchanged(self, isolated_db, spec_dir):
        _write_spec(spec_dir, "yamlwf")
        rows = svc.list_workflows(scan_dir=str(spec_dir))
        assert rows[0].step_count == 1

    def test_colliding_stem_skipped_from_index_other_names_still_index(self, isolated_db, spec_dir):
        # The .py arm skips a colliding stem (BR-2); the pre-existing YAML arm
        # is unaffected — its own scan loop indexes "dup.yaml" unchanged
        # (FR-5.1) even though the SAME name also has a colliding .py sibling.
        # Only the .py arm's OWN row is suppressed; other .py names still index.
        _write_spec(spec_dir, "dup")
        _write_script(spec_dir, "dup")
        _write_script(spec_dir, "solo")
        rows = svc.list_workflows(scan_dir=str(spec_dir))
        by_name = {r.name: r for r in rows}
        assert sorted(by_name) == ["dup", "solo"]
        assert by_name["dup"].mode == "sequential"  # the YAML row, not a script row
        assert by_name["solo"].mode == "script"


class TestRenderFindings:
    def test_renders_lint_findings_as_dicts(self):
        from cli_agent_orchestrator.models.workflow import LintFinding

        findings = [LintFinding(rule_id="syntax", severity="error", line=1, message="bad")]
        rendered = svc.render_findings(findings)
        assert rendered == [{"rule_id": "syntax", "severity": "error", "line": 1, "message": "bad"}]


# ---------------------------------------------------------------------------
# Unit A — _extract_inputs (AST-only INPUTS extraction, FR-A1 / BR-A1 / BR-A2)
# ---------------------------------------------------------------------------
class TestExtractInputs:
    """AST extraction of a script's ``INPUTS`` declaration — never executed."""

    def test_valid_inputs_parsed(self):
        src = (
            "INPUTS = {\n"
            '    "topic": {"type": "string", "required": True},\n'
            '    "count": {"type": "int", "default": 3},\n'
            '    "dry": {"type": "bool", "default": False},\n'
            '    "root": {"type": "path"},\n'
            "}\n"
        )
        inputs = svc._extract_inputs(src)
        assert set(inputs) == {"topic", "count", "dry", "root"}
        assert inputs["topic"].type == "string" and inputs["topic"].required is True
        assert inputs["count"].type == "int" and inputs["count"].default == 3
        assert inputs["dry"].type == "bool" and inputs["dry"].default is False
        assert inputs["root"].type == "path" and inputs["root"].required is False

    def test_absent_inputs_returns_empty(self):
        assert svc._extract_inputs("x = 1\n\ndef main():\n    return x\n") == {}

    def test_annotated_assignment_supported(self):
        src = 'INPUTS: dict = {"a": {"type": "string"}}\n'
        assert svc._extract_inputs(src)["a"].type == "string"

    def test_first_assignment_wins(self):
        src = 'INPUTS = {"a": {"type": "int"}}\nINPUTS = {"b": {"type": "bool"}}\n'
        inputs = svc._extract_inputs(src)
        assert set(inputs) == {"a"} and inputs["a"].type == "int"

    def test_syntax_error_raises_valueerror(self):
        # SyntaxError is NOT a ValueError subclass — it must be mapped explicitly.
        with pytest.raises(ValueError, match="malformed workflow script"):
            svc._extract_inputs("def main(:\n    pass\n")

    def test_non_dict_literal_raises_valueerror(self):
        with pytest.raises(ValueError, match="must be a dict literal"):
            svc._extract_inputs("INPUTS = [1, 2, 3]\n")

    def test_bad_type_raises_valueerror(self):
        with pytest.raises(ValueError, match="is invalid"):
            svc._extract_inputs('INPUTS = {"a": {"type": "float"}}\n')

    def test_default_type_mismatch_raises_valueerror(self):
        with pytest.raises(ValueError, match="does not match declared"):
            svc._extract_inputs('INPUTS = {"a": {"type": "int", "default": "not-int"}}\n')

    def test_unexpected_entry_key_raises_valueerror(self):
        with pytest.raises(ValueError, match="unexpected key"):
            svc._extract_inputs('INPUTS = {"a": {"type": "int", "bogus": 1}}\n')

    def test_no_execution_of_module_side_effects(self):
        """The extractor NEVER runs the module — an import-time side effect that
        would raise if executed must not fire (M2 no-execution guarantee)."""
        src = (
            "raise RuntimeError('this script must never be executed')\n"
            'INPUTS = {"a": {"type": "string"}}\n'
        )
        # A pure AST walk reaches INPUTS without ever evaluating the raise.
        inputs = svc._extract_inputs(src)
        assert inputs["a"].type == "string"

    def test_read_script_spec_populates_inputs(self, spec_dir):
        path = _write_script(spec_dir, "withinputs", 'INPUTS = {"topic": {"type": "string"}}\n')
        spec = svc.get_workflow(str(path), scan_dir=str(spec_dir))
        assert spec.inputs["topic"].type == "string"

    def test_read_script_spec_malformed_inputs_raises(self, spec_dir):
        path = _write_script(spec_dir, "badinputs", "INPUTS = [1, 2]\n")
        with pytest.raises(ValueError, match="must be a dict literal"):
            svc.get_workflow(str(path), scan_dir=str(spec_dir))


class TestColocatedPathGuards:
    """The containment SafeAccessCheck is colocated with each filesystem sink.

    CodeQL's ``py/path-injection`` ``startswith`` barrier is flow-sensitive and
    function-local, so a helper that validates a path then RETURNS it leaves the
    caller's ``open``/``isfile`` sink seeing an unchecked value (alerts
    166/167/168). These tests pin the behavior of the two helpers that now own
    every taint-reachable sink: ``_read_contained_spec_bytes`` (open) and
    ``_contained_spec_file`` (isfile).
    """

    def test_read_contained_returns_realpath_and_bytes(self, spec_dir):
        path = _write_spec(spec_dir, "reader")
        real_path, raw = svc._read_contained_spec_bytes(str(path), base_dir=str(spec_dir))
        assert real_path == os.path.realpath(str(path))
        assert isinstance(raw, bytes) and b"reader" in raw

    def test_read_contained_missing_file_raises_filenotfound(self, spec_dir):
        with pytest.raises(FileNotFoundError):
            svc._read_contained_spec_bytes(str(spec_dir / "absent.yaml"), base_dir=str(spec_dir))

    def test_read_contained_escaping_path_raises_valueerror(self, spec_dir):
        # An absolute path outside the bound base escapes containment.
        with pytest.raises(ValueError, match="escapes its validated directory"):
            svc._read_contained_spec_bytes("/etc/passwd", base_dir=str(spec_dir))

    def test_read_contained_symlink_escape_rejected(self, spec_dir):
        target = "/etc/hosts"
        if not os.path.exists(target):
            pytest.skip("no stable escape target on this platform")
        link = spec_dir / "escape.yaml"
        os.symlink(target, link)
        with pytest.raises(ValueError, match="escapes its validated directory"):
            svc._read_contained_spec_bytes(str(link), base_dir=str(spec_dir))

    def test_read_contained_opens_exactly_once(self, spec_dir, monkeypatch):
        """The single open() sink lives behind the colocated guard — one call."""
        path = _write_spec(spec_dir, "single")
        real_open = open
        opens = {"count": 0}

        def counting_open(file, *a, **kw):
            if str(file) == os.path.realpath(str(path)):
                opens["count"] += 1
            return real_open(file, *a, **kw)

        monkeypatch.setattr("builtins.open", counting_open)
        svc._read_contained_spec_bytes(str(path), base_dir=str(spec_dir))
        assert opens["count"] == 1

    def test_contained_spec_file_returns_realpath_for_file(self, spec_dir):
        path = _write_spec(spec_dir, "probe")
        assert svc._contained_spec_file(str(path), base_dir=str(spec_dir)) == os.path.realpath(
            str(path)
        )

    def test_contained_spec_file_returns_none_for_missing(self, spec_dir):
        # In-base but not an existing file -> None (caller falls through to the
        # index lookup), never an exception.
        assert (
            svc._contained_spec_file(str(spec_dir / "ghost.yaml"), base_dir=str(spec_dir)) is None
        )

    def test_contained_spec_file_escaping_path_raises(self, spec_dir):
        with pytest.raises(ValueError, match="escapes its validated directory"):
            svc._contained_spec_file("/etc/passwd", base_dir=str(spec_dir))

    def test_validate_only_escaping_path_raises_not_fails(self, spec_dir):
        """An escaping path is a hard ValueError (-> 400), NOT a soft fail
        result — the guard fires before the read degrades to a fail."""
        with pytest.raises(ValueError, match="escapes its validated directory"):
            svc.validate_only("/etc/passwd", base_dir=str(spec_dir))

    def test_get_workflow_by_absolute_path_loads(self, isolated_db, spec_dir):
        """The path arm of get_workflow loads a contained spec file directly
        (via the colocated isfile probe), no index lookup required."""
        path = _write_spec(spec_dir, "direct")
        spec = svc.get_workflow(str(path), scan_dir=str(spec_dir))
        assert spec.name == "direct"

    def test_get_workflow_escaping_path_raises(self, isolated_db, spec_dir):
        with pytest.raises(ValueError, match="escapes its validated directory"):
            svc.get_workflow("/etc/passwd.yaml", scan_dir=str(spec_dir))
