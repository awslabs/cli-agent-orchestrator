"""Tests for freezing a script-tier run's execution manifest (issue #583 Bolt 2, ``manifest-freeze``).

Two of these carry the unit's load:

* ``test_a_secret_in_inputs_does_not_reach_the_manifest`` is the concrete reason
  ``execution_manifest.build`` is the only door. ``inputs`` is arbitrary user JSON and the most likely
  place a credential appears in a workflow run.
* ``test_changing_the_source_hash_changes_the_plan_id`` converts this unit's transitive-coverage
  ARGUMENT into a PROPERTY. Six of FR-8's nine manifest fields are omitted because they have no
  run-level source in this tier; the omission is only safe because the script's own source chooses
  them, so a source change must move the identifier. If this test fails, the omission is unsafe and a
  changed provider could execute under a stale approval.
"""

import json

import pytest

from cli_agent_orchestrator.services import execution_manifest, manifest_freeze

SECRET = "AKIAIOSFODNN7EXAMPLE"  # matches secret_gate's ``aws_access_key`` pattern

OMITTED = ("provider", "model", "profile", "permissions", "limits", "retry_policy")


def _frozen(**overrides) -> dict:
    kwargs = {"source_hash": "abc123", "inputs": {"k": "v"}}
    kwargs.update(overrides)
    value = manifest_freeze.build_manifest_json(**kwargs)
    assert value is not None
    return json.loads(value)


# ---------------------------------------------------------------------------
# The two load-bearing properties
# ---------------------------------------------------------------------------


def test_a_secret_in_inputs_does_not_reach_the_manifest():
    """Everything reaching the column passes through ``build``'s tree redaction."""
    value = manifest_freeze.build_manifest_json(source_hash="h", inputs={"tok": SECRET})
    assert value is not None
    assert SECRET not in value
    assert json.loads(value)["redacted"] is True


def test_changing_the_source_hash_changes_the_plan_id():
    """The transitive-coverage property. See the module docstring.

    The six omitted fields are chosen by the script's own source, so a source change must move the
    identifier. This is what makes omitting them safe rather than merely convenient.
    """
    a = _frozen(source_hash="hash-of-script-v1")["plan_id"]
    b = _frozen(source_hash="hash-of-script-v2")["plan_id"]
    assert a != b


# ---------------------------------------------------------------------------
# What is recorded, and what is not
# ---------------------------------------------------------------------------


def test_the_six_absent_fields_are_omitted_not_null():
    """A null would assert "exists but empty"; these fields have no run-level existence in this tier."""
    document = _frozen()
    for name in OMITTED:
        assert name not in document, f"{name} should be omitted, not present as null"


def test_the_note_explaining_the_omission_travels_in_the_envelope():
    """The envelope is what an agent reads when diagnosing a failed run, so the reason lives there."""
    document = _frozen()
    assert "notes" in document
    for name in OMITTED:
        assert name in document["notes"]
    assert "source_hash" in document["notes"]


def test_the_three_recorded_fields_are_present():
    document = _frozen()
    assert document["source_hash"] == "abc123"
    assert document["inputs"] == {"k": "v"}
    assert "repo_baseline" in document
    assert document["plan_id"].startswith("plan-v1:")


def test_the_memory_record_is_empty_at_freeze_time():
    """There is no resolved memory at run start; ``memory-resolve-once`` (pass 2B) fills it."""
    memory = _frozen()["memory"]
    assert memory["content"] == ""
    assert memory["truncated"] is False


def test_the_frozen_value_round_trips_through_parse():
    value = manifest_freeze.build_manifest_json(source_hash="h", inputs={"a": [1, 2]})
    assert value is not None
    assert execution_manifest.parse(value) is not None


# ---------------------------------------------------------------------------
# Totality and identity stability
# ---------------------------------------------------------------------------


def test_returns_none_and_logs_rather_than_raising(monkeypatch, caplog):
    """Best-effort: the run must not fail because its manifest could not be assembled.

    A ``None`` writes NULL, which the approval gate reads as "never approved" and REFUSES — so the
    failure direction is fail-closed, never an unapproved run that executes.
    """

    def _boom(*_args, **_kwargs):
        raise RuntimeError("assembly exploded")

    monkeypatch.setattr(manifest_freeze.plan_identifier, "compute", _boom)
    with caplog.at_level("WARNING"):
        assert manifest_freeze.build_manifest_json(source_hash="h", inputs={}) is None
    assert any("manifest freeze failed" in r.message for r in caplog.records)


def test_the_plan_id_does_not_depend_on_the_working_directory(monkeypatch, tmp_path):
    """Path-independence: two machines running an identical plan must agree.

    The baseline deliberately excludes the worktree path for exactly this reason, so a differing cwd
    with the same repository state cannot move the identifier.
    """
    monkeypatch.setattr(
        manifest_freeze,
        "derive_baseline",
        lambda _cwd: {"available": True, "commit": "c", "dirty": False},
    )
    a = _frozen(cwd=str(tmp_path))["plan_id"]
    b = _frozen(cwd="/some/other/path")["plan_id"]
    assert a == b


def test_a_missing_baseline_still_freezes():
    """Outside a git repository the manifest is still written; absence is representable."""
    document = _frozen(cwd="/nonexistent/path/for/test")
    assert document["repo_baseline"] == {"available": False}
    assert document["plan_id"].startswith("plan-v1:")


def test_the_baseline_is_part_of_the_identity():
    """Two runs of an identical script against different commits are different plans."""
    a = _frozen(cwd="/nonexistent/path/a")["plan_id"]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            manifest_freeze,
            "derive_baseline",
            lambda _cwd: {"available": True, "commit": "deadbeef", "dirty": False},
        )
        b = _frozen()["plan_id"]
    assert a != b
