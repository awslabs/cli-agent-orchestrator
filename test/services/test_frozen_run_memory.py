"""Tests for resolve-once-and-freeze (issue #583 Bolt 2, unit ``memory-resolve-once``).

Four carry the unit's load:

* ``test_the_record_is_persisted_before_the_block_is_returned`` — the ordering IS the correctness
  argument, and the happy path looks identical either way, so only an ordering assertion can see it.
* ``test_a_failed_persist_returns_no_block`` — using memory the manifest does not record is the one
  outcome FR-9 forbids, and it fails silently: an empty record is indistinguishable from a run that
  never created a terminal.
* ``test_a_run_that_resolved_no_memory_does_not_re_resolve`` — the once-per-run flag must be the
  record's PRESENCE, not its content's truthiness. Under a truthiness flag this run re-resolves at
  every terminal and again on every resume, each time reading the LIVE store.
* ``test_filling_the_record_does_not_change_the_plan_id`` — memory is not one of ``PlanFields``' nine
  fields. If it ever becomes one, every approval on the machine silently stops matching.
"""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from cli_agent_orchestrator.services import (
    execution_manifest,
    frozen_run_memory,
    plan_identifier,
    workflow_journal,
)


@pytest.fixture()
def journalled_run(tmp_path, monkeypatch):
    """A script run with a frozen manifest whose memory record is EMPTY, as the freeze leaves it."""
    from cli_agent_orchestrator import constants
    from cli_agent_orchestrator.clients import database as database_client

    path = tmp_path / "cao.db"
    monkeypatch.setattr(constants, "DATABASE_FILE", path)
    monkeypatch.setattr(database_client, "DATABASE_FILE", path, raising=False)
    monkeypatch.setattr(workflow_journal, "DATABASE_FILE", path, raising=False)
    database_client.init_db()

    envelope = execution_manifest.build(
        plan_id="plan-v1:abc",
        source_hash="sha256:deadbeef",
        inputs={"k": "v"},
        repo_baseline={"commit": "abc123"},
    )
    workflow_journal.insert_run(
        "run-1",
        "wf",
        "{}",
        "{}",
        "RUNNING",
        "2026-08-19T00:00:00+00:00",
        "script",
        "1",
        execution_manifest.serialise(envelope),
    )
    return "run-1"


@pytest.fixture()
def resolves(monkeypatch):
    """Stub live resolution and count how many times it is consulted."""
    calls = []

    def _resolve(terminal_id, task_description):
        calls.append((terminal_id, task_description))
        return "<cao-memory>LIVE BLOCK</cao-memory>"

    monkeypatch.setattr(frozen_run_memory, "_resolve_live", _resolve)
    return calls


# ---------------------------------------------------------------------------
# The four load-bearing properties
# ---------------------------------------------------------------------------


def test_the_record_is_persisted_before_the_block_is_returned(
    journalled_run, resolves, monkeypatch
):
    """ORDERING. A crash after using and before persisting loses the FR-9 evidence, silently."""
    events = []

    real_compare_and_set = workflow_journal.compare_and_set_run_manifest

    def _tracked(run_id, expected_manifest_json, manifest_json):
        events.append("persisted")
        return real_compare_and_set(run_id, expected_manifest_json, manifest_json)

    monkeypatch.setattr(workflow_journal, "compare_and_set_run_manifest", _tracked)

    block = frozen_run_memory.frozen_memory_for(journalled_run, "term-1", "do the task")
    events.append("returned")

    assert events == ["persisted", "returned"], (
        "the record must be written BEFORE the block is handed back — a crash in the other order "
        "leaves a run that saw memory nothing recorded, which is FR-9's Fail criterion"
    )
    assert block is not None


def test_a_failed_persist_returns_an_explicit_empty_block(journalled_run, resolves, monkeypatch):
    """No block rather than an unrecorded one. The run proceeds with NO memory, visibly."""

    def _boom(run_id, expected_manifest_json, manifest_json):
        raise RuntimeError("disk full")

    monkeypatch.setattr(workflow_journal, "compare_and_set_run_manifest", _boom)

    assert frozen_run_memory.frozen_memory_for(journalled_run, "term-1", "task") == ""


def test_a_run_that_resolved_no_memory_does_not_re_resolve(journalled_run, monkeypatch):
    """`""` IS a resolved result. The flag is the record's presence, never the content's truthiness."""
    calls = []
    monkeypatch.setattr(
        frozen_run_memory,
        "_resolve_live",
        lambda tid, task: calls.append(tid) or "",
    )

    first = frozen_run_memory.frozen_memory_for(journalled_run, "term-1", "task")
    second = frozen_run_memory.frozen_memory_for(journalled_run, "term-2", "task")

    assert first == ""
    assert second == "", "an empty resolved block must be honoured, not re-resolved"
    assert len(calls) == 1, (
        "under a truthiness flag this run would re-resolve at every terminal and on every resume, "
        "each time reading the LIVE store — the drift FR-9 exists to prevent"
    )


def test_filling_the_record_does_not_change_the_plan_id(journalled_run, resolves):
    """Memory is absent from PlanFields. If it ever isn't, every approval silently stops matching."""
    before = execution_manifest.parse(workflow_journal.get_run(journalled_run).manifest_json)
    frozen_run_memory.frozen_memory_for(journalled_run, "term-1", "task")
    after = execution_manifest.parse(workflow_journal.get_run(journalled_run).manifest_json)

    assert before is not None and after is not None
    assert after.plan_id == before.plan_id

    fields = plan_identifier.PlanFields(
        source_hash=after.source_hash,
        inputs=after.inputs,
        repo_baseline=after.repo_baseline,
        provider=None,
        model=None,
        profile=None,
        permissions=None,
        limits=None,
        retry_policy=None,
    )
    assert "memory" not in plan_identifier.PlanFields.__dataclass_fields__
    assert plan_identifier.compute(fields).startswith("plan-v1:")


# ---------------------------------------------------------------------------
# Resolve once
# ---------------------------------------------------------------------------


def test_the_first_terminal_resolves_and_records(journalled_run, resolves):
    block = frozen_run_memory.frozen_memory_for(journalled_run, "term-1", "do the task")

    assert block == "<cao-memory>LIVE BLOCK</cao-memory>"
    stored = execution_manifest.parse(workflow_journal.get_run(journalled_run).manifest_json)
    assert stored is not None
    assert stored.memory.content == block
    assert stored.memory.source == frozen_run_memory.MEMORY_SOURCE_CURATED
    assert stored.memory.content_hash


def test_a_second_terminal_reuses_the_record(journalled_run, resolves):
    first = frozen_run_memory.frozen_memory_for(journalled_run, "term-1", "task")
    second = frozen_run_memory.frozen_memory_for(journalled_run, "term-2", "different task")

    assert first == second
    assert len(resolves) == 1, "resolution happens once per RUN, not once per terminal"


def test_concurrent_first_fills_return_the_persisted_winner(journalled_run, monkeypatch):
    """A CAS loss reloads the winner instead of overwriting it with this terminal's block."""
    both_resolvers_started = threading.Barrier(2)

    def _resolve(terminal_id, task_description):
        both_resolvers_started.wait(timeout=2)
        return f"<cao-memory>{terminal_id}</cao-memory>"

    monkeypatch.setattr(frozen_run_memory, "_resolve_live", _resolve)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(
            frozen_run_memory.frozen_memory_for, journalled_run, "term-first", "task"
        )
        second_future = executor.submit(
            frozen_run_memory.frozen_memory_for, journalled_run, "term-second", "task"
        )
        first = first_future.result(timeout=5)
        second = second_future.result(timeout=5)

    stored = execution_manifest.parse(workflow_journal.get_run(journalled_run).manifest_json)
    assert stored is not None
    assert first == second == stored.memory.content
    assert stored.memory.source == frozen_run_memory.MEMORY_SOURCE_CURATED


def test_an_unreadable_manifest_after_cas_loss_suppresses_live_memory(
    journalled_run, resolves, monkeypatch
):
    real_parse = execution_manifest.parse
    parse_calls = 0

    def _parse_then_fail(manifest_json):
        nonlocal parse_calls
        parse_calls += 1
        return real_parse(manifest_json) if parse_calls == 1 else None

    monkeypatch.setattr(workflow_journal, "compare_and_set_run_manifest", lambda *_args: False)
    monkeypatch.setattr(execution_manifest, "parse", _parse_then_fail)

    assert frozen_run_memory.frozen_memory_for(journalled_run, "term-1", "task") == ""


def test_a_resume_reads_the_record_and_never_resolves(journalled_run, resolves):
    """The resume path in miniature: the record exists, so nothing is resolved."""
    frozen_run_memory.frozen_memory_for(journalled_run, "term-1", "task")
    resolves.clear()

    replayed = frozen_run_memory.frozen_memory_for(journalled_run, "term-fresh", "task")

    assert replayed == "<cao-memory>LIVE BLOCK</cao-memory>"
    assert resolves == [], "a resume must never re-resolve — that is FR-9's whole point"


# ---------------------------------------------------------------------------
# When there is nothing frozen to honour
# ---------------------------------------------------------------------------


def test_no_run_id_resolves_nothing(resolves):
    """A non-workflow terminal. C-1: today's live path, untouched."""
    assert frozen_run_memory.frozen_memory_for(None, "term-1", "task") is None
    assert frozen_run_memory.frozen_memory_for("", "term-1", "task") is None
    assert resolves == []


def test_a_run_with_no_manifest_resolves_nothing(journalled_run, resolves):
    """A YAML run never freezes a manifest; "nothing frozen" is not "no memory"."""
    workflow_journal.insert_run(
        "run-yaml", "wf", "{}", "{}", "RUNNING", "2026-08-19T00:00:00+00:00"
    )
    assert frozen_run_memory.frozen_memory_for("run-yaml", "term-1", "task") is None
    assert resolves == []


def test_an_unknown_run_resolves_nothing(journalled_run, resolves):
    assert frozen_run_memory.frozen_memory_for("no-such-run", "term-1", "task") is None
    assert resolves == []


def test_an_unreadable_manifest_resolves_nothing(journalled_run, resolves):
    row = workflow_journal.get_run(journalled_run)
    assert row is not None and row.manifest_json is not None
    assert workflow_journal.compare_and_set_run_manifest(
        journalled_run, row.manifest_json, "{not json"
    )
    assert not hasattr(workflow_journal, "update_run_manifest")
    assert frozen_run_memory.frozen_memory_for(journalled_run, "term-1", "task") is None
    assert resolves == []


# ---------------------------------------------------------------------------
# with_memory — the fill that must not disturb plan_id's inputs
# ---------------------------------------------------------------------------


def test_with_memory_leaves_every_other_field_byte_identical():
    """The other fields are what plan_id was hashed from."""
    envelope = execution_manifest.build(
        plan_id="plan-v1:xyz",
        source_hash="sha256:cafe",
        inputs={"deep": {"nested": [1, 2, 3]}},
        repo_baseline={"commit": "abc", "dirty": True},
        notes="a note",
    )
    filled = execution_manifest.with_memory(envelope, content="block", source="src")

    before = json.loads(execution_manifest.serialise(envelope))
    after = json.loads(execution_manifest.serialise(filled))
    before.pop("memory")
    after.pop("memory")
    assert after == before, "filling memory must not move a single other field"


def test_with_memory_hashes_the_full_content_before_redacting():
    """``content_hash`` identifies what was RESOLVED, not what survived the ruleset of the day."""
    import hashlib

    secret = "AKIAIOSFODNN7EXAMPLE"
    envelope = execution_manifest.build(
        plan_id="plan-v1:x", source_hash="s", inputs={}, repo_baseline={}
    )
    filled = execution_manifest.with_memory(envelope, content=secret, source="src")

    assert filled.memory.content_hash == hashlib.sha256(secret.encode("utf-8")).hexdigest()
    assert filled.memory.content != secret, "the stored copy must be redacted"
    assert filled.redacted is True


def test_with_memory_does_not_clear_a_redaction_flag_the_freeze_set():
    """``redacted`` records that something was redacted at some point; a clean fill must not reset it."""
    envelope = execution_manifest.build(
        plan_id="plan-v1:x",
        source_hash="s",
        inputs={"token": "AKIAIOSFODNN7EXAMPLE"},
        repo_baseline={},
    )
    assert envelope.redacted is True
    filled = execution_manifest.with_memory(envelope, content="nothing secret", source="src")
    assert filled.redacted is True


def test_with_memory_bounds_an_oversized_block_and_flags_it():
    """The freeze measured a document with NO memory, so filling it can exceed a bound that passed."""
    from cli_agent_orchestrator.constants import WORKFLOW_MANIFEST_MAX_BYTES

    envelope = execution_manifest.build(
        plan_id="plan-v1:x", source_hash="s", inputs={}, repo_baseline={}
    )
    huge = "m" * (WORKFLOW_MANIFEST_MAX_BYTES * 2)
    filled = execution_manifest.with_memory(envelope, content=huge, source="src")

    assert filled.memory.truncated is True
    assert filled.truncated is True
    assert len(execution_manifest.serialise(filled).encode("utf-8")) <= WORKFLOW_MANIFEST_MAX_BYTES
    assert len(filled.memory.content) < len(huge)


# ---------------------------------------------------------------------------
# NFR-1
# ---------------------------------------------------------------------------


def test_the_block_content_is_never_logged(journalled_run, resolves, caplog):
    import logging

    with caplog.at_level(logging.DEBUG):
        frozen_run_memory.frozen_memory_for(journalled_run, "term-1", "task")

    assert "LIVE BLOCK" not in caplog.text, "memory text must not reach a second sink"
