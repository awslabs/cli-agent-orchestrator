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
from unittest.mock import Mock

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
    assert plan_identifier.compute(fields).startswith("plan-v2:")


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


@pytest.mark.parametrize(
    "run_id, expected",
    [("missing-script", ""), ("missing-yaml", None), ("unknown", None), (None, None)],
)
def test_missing_row_never_selects_live_memory_for_known_script(
    monkeypatch, resolves, run_id, expected, tmp_path, caplog
):
    import logging
    from types import SimpleNamespace

    from cli_agent_orchestrator.services import settings_service, workflow_service

    monkeypatch.setattr(workflow_journal, "get_run", lambda key: None)
    monkeypatch.setattr(
        workflow_service,
        "run_registry",
        {
            "missing-script": SimpleNamespace(tier="script"),
            "missing-yaml": SimpleNamespace(tier="yaml"),
        },
    )
    settings = tmp_path / "settings.json"
    monkeypatch.setattr(settings_service, "SETTINGS_FILE", settings)
    monkeypatch.delenv("CAO_WORKFLOW_REQUIRE_APPROVAL", raising=False)
    for enforced in (False, True):
        settings.write_text(json.dumps({"workflow": {"require_approval": enforced}}))
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=frozen_run_memory.logger.name):
            assert frozen_run_memory.frozen_memory_for(run_id, "term", "work") == expected
        if run_id == "missing-script":
            assert caplog.record_tuples == [
                (
                    frozen_run_memory.logger.name,
                    logging.WARNING,
                    "frozen memory: run 'missing-script': run row missing; memory withheld",
                )
            ]
            assert str(tmp_path) not in caplog.text
        else:
            assert caplog.records == []
    assert resolves == []


def test_remote_memory_resolution_uses_gateway_not_local_service(
    journalled_run, monkeypatch, mocker
):
    """F5: the frozen path must select the same remote owner as live terminals."""
    from cli_agent_orchestrator.services import memory_gateway, memory_service

    monkeypatch.setenv("CAO_MEMORY_API_URL", "http://memory-owner.invalid")
    gateway = mocker.patch.object(
        memory_gateway, "memory_context_for_terminal", return_value="REMOTE BLOCK"
    )
    local_service = mocker.patch.object(memory_service, "MemoryService")
    task = "x" * 500

    assert (
        frozen_run_memory.frozen_memory_for(journalled_run, "term-remote", task) == "REMOTE BLOCK"
    )
    gateway.assert_called_once_with("term-remote", task_description=task[:200])
    local_service.assert_not_called()
    stored = execution_manifest.parse(workflow_journal.get_run(journalled_run).manifest_json)
    assert stored.memory.content == "REMOTE BLOCK"
    assert (
        frozen_run_memory.frozen_memory_for(journalled_run, "term-replay", "later")
        == "REMOTE BLOCK"
    )
    gateway.assert_called_once()


def test_remote_gateway_freezes_owner_context_without_local_curator(
    journalled_run, monkeypatch, mocker
):
    """Exercise the real gateway branch, stubbing only context lookup and HTTP."""
    from cli_agent_orchestrator.services import memory_gateway

    monkeypatch.setenv("CAO_MEMORY_API_URL", "http://memory-owner.invalid")
    context = {"terminal_id": "term-remote", "cwd": "/project"}
    mocker.patch.object(memory_gateway.MemoryService, "_get_terminal_context", return_value=context)
    local_curator = mocker.patch.object(
        memory_gateway.MemoryService, "get_curated_memory_context", return_value="STALE LOCAL"
    )
    post = mocker.patch.object(memory_gateway, "_post", return_value={"context": "OWNER BLOCK"})
    gateway = mocker.spy(memory_gateway, "memory_context_for_terminal")

    assert (
        frozen_run_memory.frozen_memory_for(journalled_run, "term-remote", "task") == "OWNER BLOCK"
    )
    gateway.assert_called_once_with("term-remote", task_description="task")
    local_curator.assert_not_called()
    post.assert_called_once_with(
        "/internal/memory/context", {"terminal_context": context, "budget_chars": 3000}
    )


@pytest.mark.parametrize("tier, expected", [("script", ""), ("yaml", None), (None, None)])
def test_journal_read_failure_withholds_only_known_script_memory(
    monkeypatch, resolves, caplog, tier, expected
):
    """F2 prime: a journal read fault must not select live memory for a known script."""
    import logging
    from types import SimpleNamespace

    from cli_agent_orchestrator.services import workflow_service

    def fail_read(_run_id):
        raise RuntimeError("journal unavailable")

    monkeypatch.setattr(workflow_journal, "get_run", fail_read)
    monkeypatch.setattr(
        workflow_service, "run_registry", {"read-fault": SimpleNamespace(tier=tier)} if tier else {}
    )
    with caplog.at_level(logging.WARNING, logger=frozen_run_memory.logger.name):
        block = frozen_run_memory.frozen_memory_for("read-fault", "term", "task")
    assert block == expected
    assert resolves == []
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING
    assert "read-fault" in caplog.text
    assert "journal unavailable" in caplog.text
    if tier == "script":
        assert "memory withheld" in caplog.text
        assert "resolving live" not in caplog.text
    else:
        assert "resolving live" in caplog.text


# ---------------------------------------------------------------------------
# A remote owner's failure must not escape `frozen_memory_for` (#828)
# ---------------------------------------------------------------------------


@pytest.fixture()
def remote_gateway(monkeypatch, mocker):
    """Route resolution through the real gateway, with only HTTP and terminal-context lookup stubbed."""
    from cli_agent_orchestrator.services import memory_gateway, memory_service

    monkeypatch.setenv("CAO_MEMORY_API_URL", "http://memory-owner.invalid")
    context = {"terminal_id": "term-remote", "session_name": "s", "cwd": "/project"}
    mocker.patch.object(memory_service.MemoryService, "_get_terminal_context", return_value=context)
    curator = mocker.patch.object(
        memory_service.MemoryService, "get_curated_memory_context", return_value="STALE LOCAL"
    )
    return memory_gateway, curator


def _http_error_response(status_code):
    import requests

    response = Mock(status_code=status_code)
    response.json.return_value = {"detail": "error"}
    response.raise_for_status.side_effect = requests.HTTPError(f"{status_code} error")
    return response


@pytest.mark.parametrize(
    "failure",
    ["owner_404", "connection_error"],
)
def test_remote_owner_failure_withholds_memory_without_raising(
    journalled_run, remote_gateway, mocker, caplog, failure
):
    """A 404 (memory disabled on the owner) or a connection failure must not escape, and must
    not fall back to the local curator — the owner must not be called a second time (#828)."""
    import logging

    import requests

    memory_gateway, curator = remote_gateway
    if failure == "owner_404":
        mocker.patch.object(memory_gateway.requests, "post", return_value=_http_error_response(404))
    else:
        mocker.patch.object(
            memory_gateway.requests, "post", side_effect=requests.ConnectionError("refused")
        )

    with caplog.at_level(logging.WARNING, logger=frozen_run_memory.logger.name):
        block = frozen_run_memory.frozen_memory_for(journalled_run, "term-remote", "task")

    assert block == ""
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING
    assert journalled_run in caplog.text
    curator.assert_not_called()
    stored = execution_manifest.parse(workflow_journal.get_run(journalled_run).manifest_json)
    assert stored.memory.source == ""
    assert stored.memory.content == ""


# ---------------------------------------------------------------------------
# The local kill switch must bind the frozen remote path too (#828)
# ---------------------------------------------------------------------------


def test_local_switch_off_withholds_remote_memory_without_calling_owner(
    journalled_run, monkeypatch, mocker
):
    from cli_agent_orchestrator.services import memory_gateway, memory_service, settings_service

    monkeypatch.setenv("CAO_MEMORY_API_URL", "http://memory-owner.invalid")
    monkeypatch.setattr(settings_service, "is_memory_enabled", lambda: False)
    post = mocker.patch.object(memory_gateway, "_post")
    context_lookup = mocker.patch.object(memory_service.MemoryService, "_get_terminal_context")

    block = frozen_run_memory.frozen_memory_for(journalled_run, "term-off", "task")

    assert block == ""
    post.assert_not_called()
    context_lookup.assert_not_called()
    stored = execution_manifest.parse(workflow_journal.get_run(journalled_run).manifest_json)
    assert stored.memory.content == ""
    assert stored.memory.source == frozen_run_memory.MEMORY_SOURCE_CURATED


# ---------------------------------------------------------------------------
# End to end: a remote-owner failure must not fail the step or leak its terminal
# ---------------------------------------------------------------------------


def test_e2e_run_agent_step_completes_on_remote_owner_404(journalled_run, remote_gateway, mocker):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from cli_agent_orchestrator.models.terminal import TerminalStatus
    from cli_agent_orchestrator.services import agent_step

    memory_gateway, _ = remote_gateway
    mocker.patch.object(memory_gateway.requests, "post", return_value=_http_error_response(404))
    term = MagicMock()
    term.id = "abc12345"
    mocker.patch.object(
        agent_step.terminal_service, "create_terminal", new=AsyncMock(return_value=term)
    )
    send = mocker.patch.object(agent_step.terminal_service, "send_input", return_value=True)
    delete = mocker.patch.object(agent_step.terminal_service, "delete_terminal", return_value=True)
    mocker.patch.object(agent_step.terminal_service, "get_output", return_value="the answer")
    mocker.patch.object(agent_step.terminal_service, "exit_terminal_cli", return_value=None)
    mocker.patch.object(agent_step.terminal_service, "get_working_directory", return_value=None)
    mocker.patch.object(agent_step, "wait_until_status", new=AsyncMock(return_value=True))
    mocker.patch.object(
        agent_step.status_monitor, "get_status", return_value=TerminalStatus.COMPLETED
    )
    mocker.patch.object(agent_step.launch_guard, "check", return_value=None)

    result = asyncio.run(
        agent_step.run_agent_step(
            "kiro_cli",
            "dev",
            "x",
            env_vars={"CAO_WORKFLOW_RUN_ID": journalled_run, "CAO_WORKFLOW_GENERATION": "1"},
        )
    )

    assert result.last_message == "the answer"
    send.assert_called_once()
    assert send.call_args.kwargs.get("frozen_memory") == ""
    delete.assert_called_once()
    stored = execution_manifest.parse(workflow_journal.get_run(journalled_run).manifest_json)
    assert stored.memory.source == ""
