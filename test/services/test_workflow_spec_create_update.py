"""Create/update operations for Python workflow specs (issue #583, Bolt 3, unit 3).

Guards ``create_workflow`` and ``update_workflow``. Containment and atomicity belong to
unit 2 and are tested in ``test_workflow_spec_write_containment.py``; what is tested here
is the ordered preconditions, the lint gate, and that a created spec is actually
REACHABLE through the real read path.

Two properties are asserted that a weaker suite would skip:

- **Every rejection leaves the directory byte-identical** (BR-3A3-13), checked with
  ``os.listdir`` so a leftover temp dotfile from unit 2's path would show up. A
  rejection that has already written is still a rejection from the caller's view.
- **A successful create round-trips through ``get_workflow`` and ``list_workflows``**
  (BR-3A3-14). Stopping at "the file exists" would miss both failures this unit exists
  to prevent: a collision making the spec ungettable, and an index row that never landed.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cli_agent_orchestrator.models.workflow import TierCollisionError
from cli_agent_orchestrator.services import workflow_spec_service as svc

# A minimal spec that passes lint. ``run_step`` is the legacy surface, which carries no
# recovery-policy requirement — so this is clean without declaring one.
GOOD = "INPUTS = {'topic': {'type': 'string', 'required': True}}\n"


def _entries(base: Path) -> list[str]:
    return sorted(os.listdir(base))


# ---------------------------------------------------------------------------
# Happy path, and the round trip through the REAL read path
# ---------------------------------------------------------------------------
def test_create_writes_the_spec_and_returns_its_parsed_form(tmp_path: Path) -> None:
    spec = svc.create_workflow("my-wf", GOOD, scan_dir=str(tmp_path))

    assert (tmp_path / "my-wf.py").read_text() == GOOD
    assert spec.name == "my-wf"
    assert spec.source == GOOD
    assert "topic" in spec.inputs, "INPUTS must be extracted, AST-only"
    assert _entries(tmp_path) == ["my-wf.py"], "no temp file left behind"


def test_the_returned_content_hash_describes_exactly_what_landed(tmp_path: Path) -> None:
    """The contract unit 4's stale-update check depends on (SR-3A3-7).

    If the validated text and the written text ever diverged, this hash would describe
    neither, and the stale-update comparison would be meaningless.
    """
    import hashlib

    spec = svc.create_workflow("hashed", GOOD, scan_dir=str(tmp_path))
    on_disk = (tmp_path / "hashed.py").read_bytes()
    assert spec.content_hash == hashlib.sha256(on_disk).hexdigest()


def test_a_created_spec_is_gettable_and_listable(tmp_path: Path) -> None:
    """BR-3A3-14 — through the real read path, not just a file-exists check."""
    svc.create_workflow("round-trip", GOOD, scan_dir=str(tmp_path))

    got = svc.get_workflow("round-trip", scan_dir=str(tmp_path))
    assert got.source == GOOD

    names = [row.name for row in svc.list_workflows(scan_dir=str(tmp_path))]
    assert "round-trip" in names


def test_update_replaces_the_content_verbatim(tmp_path: Path) -> None:
    # ``expected_hash`` is required (unit 4, BR-3A4-1) — the create's returned hash is
    # exactly what a caller presents on the following update.
    created = svc.create_workflow("edit-me", GOOD, scan_dir=str(tmp_path))
    new_source = "# rewritten, no trailing newline and a © char\nINPUTS = {}"

    spec = svc.update_workflow("edit-me", new_source, created.content_hash, scan_dir=str(tmp_path))

    assert (tmp_path / "edit-me.py").read_text() == new_source
    assert spec.source == new_source
    assert svc.get_workflow("edit-me", scan_dir=str(tmp_path)).source == new_source


# ---------------------------------------------------------------------------
# Preconditions — each must leave the filesystem untouched (BR-3A3-13)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "bad_name",
    ["with/slash", "..", ".", "has space", "way-too-long" * 10, "unicodeé"],
)
def test_rejects_an_invalid_name_without_writing(tmp_path: Path, bad_name: str) -> None:
    with pytest.raises(ValueError):
        svc.create_workflow(bad_name, GOOD, scan_dir=str(tmp_path))
    assert _entries(tmp_path) == []


@pytest.mark.parametrize("yaml_name", ["thing.yaml", "thing.yml", "THING.YAML"])
def test_refuses_yaml_with_a_message_naming_the_restriction(tmp_path: Path, yaml_name: str) -> None:
    """BR-3A3-2 — an agent that reasonably tried YAML should learn the rule."""
    with pytest.raises(ValueError, match="YAML specs cannot be created or updated"):
        svc.create_workflow(yaml_name, GOOD, scan_dir=str(tmp_path))
    assert _entries(tmp_path) == []


def test_refuses_a_name_that_already_carries_the_py_extension(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="bare workflow name"):
        svc.create_workflow("thing.py", GOOD, scan_dir=str(tmp_path))
    assert _entries(tmp_path) == []


def test_create_refuses_an_existing_spec_and_leaves_it_byte_identical(
    tmp_path: Path,
) -> None:
    """BR-3A3-3 — an agent meaning 'make a new workflow' must not clobber one."""
    svc.create_workflow("exists", GOOD, scan_dir=str(tmp_path))
    original = (tmp_path / "exists.py").read_bytes()

    with pytest.raises(FileExistsError, match="already exists"):
        svc.create_workflow("exists", "# different\n", scan_dir=str(tmp_path))

    assert (tmp_path / "exists.py").read_bytes() == original
    assert _entries(tmp_path) == ["exists.py"]


def test_update_refuses_a_missing_spec_and_creates_nothing(tmp_path: Path) -> None:
    # Any hash will do: the existence check precedes the stale-hash comparison
    # (BR-3A4-6), so a missing spec is a FileNotFoundError and never a StaleSpecError.
    with pytest.raises(FileNotFoundError, match="does not exist"):
        svc.update_workflow("absent", GOOD, "0" * 64, scan_dir=str(tmp_path))
    assert _entries(tmp_path) == []


def test_refuses_a_cross_tier_collision_before_writing(tmp_path: Path) -> None:
    """BR-3A3-4 / SR-3A3-4 — otherwise the created spec is unreachable on arrival.

    Without this check the write succeeds and every later ``get_workflow`` raises
    ``TierCollisionError`` — a file that cannot be read the moment it lands.
    """
    (tmp_path / "clash.yaml").write_text("name: clash\nmode: sequential\nsteps: []\n")

    with pytest.raises(TierCollisionError):
        svc.create_workflow("clash", GOOD, scan_dir=str(tmp_path))

    assert _entries(tmp_path) == ["clash.yaml"], "no .py written, no temp file"


# ---------------------------------------------------------------------------
# The lint gate (BR-3A3-5, SR-3A3-3)
# ---------------------------------------------------------------------------
def test_refuses_a_spec_with_a_lint_error_and_says_which_rule(tmp_path: Path) -> None:
    """CAO must not write a spec it would refuse to run.

    A syntax error is an ERROR finding, so the gate refuses before
    ``_extract_inputs`` is ever reached — which is why this unit does not need the
    read path's skip-on-syntax-finding branch.
    """
    with pytest.raises(ValueError, match="lint errors and would not be runnable"):
        svc.create_workflow("broken", "def oops(\n", scan_dir=str(tmp_path))
    assert _entries(tmp_path) == []


def test_refuses_a_malformed_inputs_literal(tmp_path: Path) -> None:
    """BR-3A3-7 — validation is AST-only and never executes the module."""
    with pytest.raises(ValueError):
        svc.create_workflow(
            "bad-inputs", "INPUTS = {'a': {'type': 'nonsense'}}\n", scan_dir=str(tmp_path)
        )
    assert _entries(tmp_path) == []


def test_a_warning_level_finding_does_not_block_the_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BR-3A3-6 — only ERROR refuses; the gate must not become a style enforcer.

    Injects a warning-only lint result rather than crafting source that happens to trip
    a warning rule, so the test does not break when the rule catalogue changes.

    ``rule_id`` must be a REAL id: it is a closed ``Literal`` in
    ``models/workflow.py``:335, so an invented value fails Pydantic validation. That is
    the trap the project rule learned at ``nfr-requirements:c1`` records — a field
    constrained in another module, invisible from this file alone.
    """
    from cli_agent_orchestrator.models.workflow import (
        LintFinding,
        ScriptValidationResult,
    )

    def _warn_only(source: str, path: str) -> ScriptValidationResult:
        return ScriptValidationResult(
            status="pass",
            findings=[
                LintFinding(rule_id="dynamic-import", severity="warning", line=1, message="careful")
            ],
            errors=[],
        )

    monkeypatch.setattr(svc, "lint_script", _warn_only)

    spec = svc.create_workflow("warned", GOOD, scan_dir=str(tmp_path))

    assert (tmp_path / "warned.py").exists()
    assert [f.rule_id for f in spec.findings] == ["dynamic-import"]


# ---------------------------------------------------------------------------
# Index failure is self-healing (BR-3A3-12, SR-3A3-9)
# ---------------------------------------------------------------------------
def test_a_failed_index_upsert_logs_and_still_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The file is canonical; the index rebuilds from disk on the next list.

    Asserts the SELF-HEALING claim, not merely the tolerance claim: after the failed
    upsert, ``list_workflows`` still finds the spec because it rebuilds from files.
    """

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected index failure")

    monkeypatch.setattr(svc, "upsert_index", _boom)

    with caplog.at_level("WARNING"):
        spec = svc.create_workflow("resilient", GOOD, scan_dir=str(tmp_path))

    assert spec.source == GOOD, "the operation must report success"
    assert (tmp_path / "resilient.py").read_text() == GOOD
    assert any(
        "index row could not be updated" in r.getMessage() for r in caplog.records
    ), "a silently swallowed index failure is invisible"

    # Self-healing: monkeypatch is undone, so the rebuild finds it on disk.
    monkeypatch.undo()
    names = [row.name for row in svc.list_workflows(scan_dir=str(tmp_path))]
    assert "resilient" in names
