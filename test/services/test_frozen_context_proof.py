"""FR-9's Pass criterion, as one scenario (issue #583 Bolt 2, unit ``frozen-context-proof``).

FR-9 says: *altering CAO memory after a failure does not change what a resumed run sees; the resolved
memory content, source and hash used by the run are recorded.* That is a claim about a SEQUENCE — a run
resolves memory, fails, the memory is edited, the run resumes — and every other test in this Bolt
verifies one link of it. Each of them can pass while the sequence is broken: a resume that reads the
record correctly but a later terminal that re-resolves satisfies "the resume reads the record" and
still violates the requirement.

``unit-of-work.md`` made this its own unit for a reason drawn from the previous Bolt: the upgrade-window
test had to be bolted onto Bolt 1B's Definition of Done at ``delivery-planning`` because
``units-generation`` had assigned it to no unit, "despite it being the top-ranked risk". An end-to-end
scenario owned by nobody does not get written.

WHAT THIS PROVES, AND WHAT IT DOES NOT. Every link in the chain here is the real shipped code: the real
journal on a temporary database, the real manifest envelope, the real ``frozen_run_memory``, the real
``inject_memory_context``. Two boundaries are stubbed — the provider process, and what memory resolution
returns.

So this does NOT prove that tmux delivers the bytes, that a real provider starts, or that a real process
death preserves the record. It proves the FROZEN-CONTEXT guarantee. That limit is written here rather
than left implicit, because a module named as an end-to-end proof invites a reader to believe it covers
more than it does — and this issue's Bolt 1C spent a whole unit removing five surfaces that claimed
more than they delivered.

IT LIVES UNDER ``test/services/`` DELIBERATELY. CI runs
``pytest test/ --ignore=test/providers/test_kiro_cli_integration.py --ignore=test/e2e -m "not e2e"``, so
a maximally faithful version of this scenario placed under ``test/e2e`` would never execute, and FR-9's
Pass criterion would be verified only when someone remembered to run it by hand. A proof that does not
run is not a proof — the "owned by nobody" failure wearing a different hat.
"""

import hashlib
import json

import pytest

from cli_agent_orchestrator.services import (
    approval_gate,
    approval_store,
    execution_manifest,
    frozen_run_memory,
    settings_service,
    terminal_service,
    workflow_journal,
)

RUN_ID = "run-frozen-proof"

#: The block the ORIGINAL run resolves and the manifest records.
BLOCK_ORIGINAL = "<cao-memory>ORIGINAL-BLOCK-FROZEN-AT-FIRST-TERMINAL</cao-memory>"

#: What resolution would return AFTER the edit. The resumed run must NEVER see this.
#: Deliberately unmistakable in a failure message: the whole diagnosis of this test is "which block
#: arrived?", so the two must not look alike.
BLOCK_AFTER_EDIT = "<cao-memory>EDITED-AFTER-THE-FAILURE-MUST-NOT-APPEAR</cao-memory>"

#: The CANONICAL published AWS example key, not an invented secret-shaped string. An invented one
#: matches none of ``secret_gate._SECRET_PATTERNS``, so a redaction assertion built on it passes while
#: demonstrating nothing — a mistake made earlier in this same Bolt and caught only because a different
#: assertion in the same test disagreed.
EXAMPLE_SECRET = "AKIAIOSFODNN7EXAMPLE"


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """A temp database, and the injection guard cleared.

    Both are explicit rather than assumed. ``_memory_injected_terminals`` is MODULE-LEVEL state and this
    scenario injects more than once; ``DATABASE_FILE`` is repointed on every module that reads it,
    because the journal's own ``_MIGRATED_PATHS`` comment records that five test modules repoint it
    mid-process. The suite also runs with random ordering, so nothing here may depend on another test
    having left a clean process behind.
    """
    from cli_agent_orchestrator import constants
    from cli_agent_orchestrator.clients import database as database_client

    path = tmp_path / "cao.db"
    monkeypatch.setattr(constants, "DATABASE_FILE", path)
    monkeypatch.setattr(database_client, "DATABASE_FILE", path, raising=False)
    monkeypatch.setattr(workflow_journal, "DATABASE_FILE", path, raising=False)
    monkeypatch.setattr(approval_store, "DATABASE_FILE", path, raising=False)
    database_client.init_db()

    with terminal_service._memory_injected_lock:
        terminal_service._memory_injected_terminals.clear()
    yield
    with terminal_service._memory_injected_lock:
        terminal_service._memory_injected_terminals.clear()


def _freeze_a_run(run_id: str = RUN_ID) -> None:
    """Create a script run whose manifest is frozen with an EMPTY memory record.

    Built with the REAL ``execution_manifest.build`` and ``workflow_journal.insert_run`` rather than a
    hand-written JSON literal: a literal would let this test survive a change to the envelope's shape,
    and a test that survives a change to the thing it proves is worse than no test, because it reports
    success about a structure that no longer exists.
    """
    envelope = execution_manifest.build(
        plan_id="plan-v1:frozen-proof",
        source_hash="sha256:abc123",
        inputs={"target": "docs"},
        repo_baseline={"commit": "deadbeef", "dirty": False},
    )
    workflow_journal.insert_run(
        run_id,
        "frozen-proof-workflow",
        json.dumps({"source": "print(1)", "path": None, "content_hash": "sha256:abc123"}),
        json.dumps({"target": "docs"}),
        "RUNNING",
        "2026-08-19T00:00:00+00:00",
        "script",
        "1",
        execution_manifest.serialise(envelope),
    )


# ===========================================================================
# THE SCENARIO — one test, because FR-9's criterion is the SEQUENCE
# ===========================================================================


def test_editing_memory_after_a_failure_does_not_change_what_the_resumed_run_sees(monkeypatch):
    """FR-9's Pass criterion, start to finish.

    Split into per-phase tests, each phase could pass while the whole broke. The phases below are steps
    inside one test, each carrying its own assertions so a failure names the step rather than the final
    comparison.
    """
    _freeze_a_run()

    resolutions = []

    def _resolve(terminal_id, task_description):
        resolutions.append(terminal_id)
        return BLOCK_ORIGINAL

    monkeypatch.setattr(frozen_run_memory, "_resolve_live", _resolve)

    # ---- PHASE 1: the original run resolves, records, and injects -------------------------------
    first_block = frozen_run_memory.frozen_memory_for(RUN_ID, "term-original", "do the task")
    assert first_block == BLOCK_ORIGINAL
    assert len(resolutions) == 1, "the first terminal must resolve exactly once"

    recorded = execution_manifest.parse(workflow_journal.get_run(RUN_ID).manifest_json)
    assert recorded is not None
    assert recorded.memory.content == BLOCK_ORIGINAL
    assert recorded.memory.source == frozen_run_memory.MEMORY_SOURCE_CURATED
    original_hash = recorded.memory.content_hash
    assert original_hash == hashlib.sha256(BLOCK_ORIGINAL.encode("utf-8")).hexdigest()

    injected_first = terminal_service.inject_memory_context(
        "do the task", "term-original", frozen_memory=first_block
    )
    assert BLOCK_ORIGINAL in injected_first, "the original run's agent must see the frozen block"

    # ---- PHASE 2: the run fails ----------------------------------------------------------------
    # Nothing to do, and that is the point of persist-before-use: the record was durable BEFORE the
    # block was ever handed over, so a failure at any moment after phase 1 leaves the record intact.

    # ---- PHASE 3: CAO memory is edited ---------------------------------------------------------
    def _resolve_after_edit(terminal_id, task_description):
        resolutions.append(terminal_id)
        return BLOCK_AFTER_EDIT

    monkeypatch.setattr(frozen_run_memory, "_resolve_live", _resolve_after_edit)

    # THE ANTI-VACUITY GUARD. Without this the entire test passes whenever the simulated edit fails to
    # take effect — a stub that silently kept returning the original block would produce a green test
    # that proves nothing at all. This is the single most likely way this test could rot.
    assert (
        frozen_run_memory._resolve_live("probe", "probe") == BLOCK_AFTER_EDIT
    ), "the edit must be real: a LIVE resolution has to return the new content"
    resolutions.pop()  # the probe is not a run resolution

    # ---- PHASE 4: the run resumes --------------------------------------------------------------
    resumed_block = frozen_run_memory.frozen_memory_for(RUN_ID, "term-resumed", "do the task")

    assert resumed_block == BLOCK_ORIGINAL, (
        "the resumed run must see the block frozen at the ORIGINAL run — this is FR-9's Pass "
        "criterion, and receiving BLOCK_AFTER_EDIT here is its Fail criterion"
    )
    assert BLOCK_AFTER_EDIT not in (resumed_block or "")
    assert len(resolutions) == 1, (
        "a resume must resolve NOTHING. A second entry here means the run re-read the live store, "
        "which is exactly what a post-failure memory edit changes"
    )

    injected_resumed = terminal_service.inject_memory_context(
        "do the task", "term-resumed", frozen_memory=resumed_block
    )
    assert BLOCK_ORIGINAL in injected_resumed
    assert BLOCK_AFTER_EDIT not in injected_resumed

    # ---- PHASE 5: the record is unchanged ------------------------------------------------------
    after = execution_manifest.parse(workflow_journal.get_run(RUN_ID).manifest_json)
    assert after is not None
    assert after.memory.content_hash == original_hash, (
        "FR-9's second half: the RECORDED hash must be unchanged. Content equality alone would miss "
        "a resume that re-recorded the same bytes with a fresh hash"
    )
    assert after.memory.content == BLOCK_ORIGINAL
    assert after.memory.source == frozen_run_memory.MEMORY_SOURCE_CURATED


# ===========================================================================
# The two consequences of "the run sees what was STORED"
# ===========================================================================


def test_the_agent_sees_the_redacted_copy_not_the_raw_resolution(monkeypatch):
    """The decision that the run sees the STORED copy lives in ONE line of ``frozen_run_memory``.

    Returning ``resolved`` instead of ``filled.memory.content`` is a one-word change that passes every
    unit test in this Bolt, and its consequence is that the original run and its replays see different
    bytes whenever redaction bit. NO other test spans resolve -> store -> inject, so nothing else can
    notice that word changing.
    """
    _freeze_a_run()
    block_with_secret = f"<cao-memory>token={EXAMPLE_SECRET}</cao-memory>"
    monkeypatch.setattr(frozen_run_memory, "_resolve_live", lambda tid, task: block_with_secret)

    frozen = frozen_run_memory.frozen_memory_for(RUN_ID, "term-1", "task")
    injected = terminal_service.inject_memory_context("task", "term-1", frozen_memory=frozen)

    assert EXAMPLE_SECRET not in injected, (
        "the agent must receive the REDACTED stored copy. Seeing the raw key here means the raw "
        "resolution was injected instead of what the manifest recorded"
    )
    assert "<cao-memory>" in injected, "the block itself must still arrive"

    recorded = execution_manifest.parse(workflow_journal.get_run(RUN_ID).manifest_json)
    assert recorded is not None and EXAMPLE_SECRET not in recorded.memory.content
    assert (
        recorded.memory.content_hash
        == hashlib.sha256(block_with_secret.encode("utf-8")).hexdigest()
    ), "the hash covers the FULL resolved content, taken before redaction"


def test_an_oversized_block_is_injected_truncated_and_says_so(monkeypatch):
    """The other half of "what was stored": when the bound bites, the agent sees less."""
    from cli_agent_orchestrator.constants import WORKFLOW_MANIFEST_MAX_BYTES

    _freeze_a_run()
    huge = "<cao-memory>" + ("m" * WORKFLOW_MANIFEST_MAX_BYTES) + "</cao-memory>"
    monkeypatch.setattr(frozen_run_memory, "_resolve_live", lambda tid, task: huge)

    frozen = frozen_run_memory.frozen_memory_for(RUN_ID, "term-1", "task")

    assert frozen is not None
    assert len(frozen) < len(huge), "the injected block is the truncated stored copy"
    recorded = execution_manifest.parse(workflow_journal.get_run(RUN_ID).manifest_json)
    assert recorded is not None
    assert recorded.memory.truncated is True, "truncation must be RECORDED, not silent"
    assert recorded.memory.content_hash == hashlib.sha256(huge.encode("utf-8")).hexdigest()

    stored_size = len(workflow_journal.get_run(RUN_ID).manifest_json.encode("utf-8"))
    assert stored_size <= WORKFLOW_MANIFEST_MAX_BYTES


# ===========================================================================
# The recorded dependency on approval-gate, proven rather than assumed
# ===========================================================================


def test_the_gate_dependency_is_real_when_enforcement_is_on(tmp_path, monkeypatch):
    """``unit-of-work.md`` made this unit depend on ``approval-gate``.

    Enforcement defaults OFF, so that dependency is satisfied by construction and would be invisible.
    This turns it on — through the REAL setting and the REAL store, not a stubbed predicate, because a
    stubbed setting only proves the stub.
    """
    _freeze_a_run()
    manifest_json = workflow_journal.get_run(RUN_ID).manifest_json

    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"workflow": {"require_approval": True}}))
    monkeypatch.setattr(settings_service, "SETTINGS_FILE", settings_file)
    monkeypatch.delenv("CAO_WORKFLOW_REQUIRE_APPROVAL", raising=False)

    with pytest.raises(approval_gate.PlanApprovalRequiredError) as excinfo:
        approval_gate.ensure_plan_approved(tier="script", manifest_json=manifest_json)
    assert excinfo.value.plan_id == "plan-v1:frozen-proof"

    approval_store.grant("plan-v1:frozen-proof", "test-account")
    approval_gate.ensure_plan_approved(tier="script", manifest_json=manifest_json)  # must not raise


def test_enforcement_off_is_the_default_so_the_scenario_above_needs_no_approval(
    monkeypatch, tmp_path
):
    """Why the main scenario does not grant an approval: with the default, nothing is gated."""
    monkeypatch.delenv("CAO_WORKFLOW_REQUIRE_APPROVAL", raising=False)
    monkeypatch.setattr(settings_service, "SETTINGS_FILE", tmp_path / "absent.json")
    _freeze_a_run()

    approval_gate.ensure_plan_approved(
        tier="script", manifest_json=workflow_journal.get_run(RUN_ID).manifest_json
    )
