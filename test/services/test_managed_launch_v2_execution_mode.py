"""Tests for the execution-mode / native-attachment seam of managed-launch v2.

These cover the wire contract the orchestration lane consumes: the mode
is resolved once at reserve (before any provider I/O), persisted on the
reservation row, projected on every public read, carried into both bind
receipts, and enforced at bind against the exclusive attachment store.

The negative cases matter more than the positive ones here.  Every way a
generation could silently change mode, or two owners could end up
attached to one provider session, is a case where nothing visibly fails
and the damage shows up later as interleaved turns in a transcript that
looks like one run.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import uuid

import pytest
from pydantic import ValidationError

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.managed_launch_v2 import (
    PROTOCOL_VERSION_V2,
    ManagedLaunchV2BindRequest,
    ManagedLaunchV2ReserveRequest,
)
from cli_agent_orchestrator.services import execution_mode as em
from cli_agent_orchestrator.services import managed_launch_v2 as v2
from cli_agent_orchestrator.services import native_attachment, native_tui_launch, run_manifest
from cli_agent_orchestrator.services import vintage_migration as vm
from cli_agent_orchestrator.services.destructive_endpoint import binding_record_path
from cli_agent_orchestrator.services.managed_launch import (
    ManagedLaunchConflict,
    ManagedLaunchUnavailable,
)
from cli_agent_orchestrator.services.managed_provider_bridge import BRIDGE_VERSION

DELIVERY_ID = "33333333-3333-4333-8333-333333333333"
SESSION_ID = "thr_0192a7b4"


@pytest.fixture(autouse=True)
def _companion(tmp_path, monkeypatch):
    monkeypatch.setattr(v2, "COMPANION_DIR", tmp_path / "companion")


@pytest.fixture
def worktree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def _reserve_request(worktree, tmp_path, **changes):
    executable = tmp_path / "fake-provider"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    payload = {
        "protocol_version": PROTOCOL_VERSION_V2,
        "reservation_id": str(uuid.uuid4()),
        "session_name": "cao-test",
        "provider": "codex",
        "agent_profile": "reviewer-sol-max",
        "caller_id": "deadbeef",
        "working_directory": str(worktree),
        "trusted_project_root": str(worktree),
        "expected_model": "gpt-5.6-sol",
        "expected_effort": "xhigh",
        "provider_executable": str(executable),
        "provider_executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "obligation_generation": "obgen-7c2e4a1b",
        "task_id": "self-heal-demo-task",
        "run_id": "run-0001",
        "delivery_id": DELIVERY_ID,
        "launch_nonce": "n" * 40,
    }
    payload.update(changes)
    return ManagedLaunchV2ReserveRequest(**payload)


#: A native reservation names a provider that actually has a native
#: branch.  Pairing ``native_tui`` with a provider that has none would be
#: testing a combination the launch path refuses outright.
#: ``trusted_project_root`` is a codex-only field, so a kimi reservation
#: must leave it unset rather than inherit the codex default.
_NATIVE_PROVIDER = {"provider": "kimi_cli", "trusted_project_root": None}
_NATIVE = {"execution_mode": "native_tui", **_NATIVE_PROVIDER}

#: Per-provider readiness evidence.  The native kind is deliberately
#: distinct from the ACP kind, so a receipt from one mode cannot satisfy
#: a bind in the other.
_PROVIDER_FIXTURES = {
    "codex": {
        "version": "0.146.0",
        "acp_kind": "codex-thread-start",
        "native_kind": "codex-native-thread-start",
    },
    "kimi_cli": {
        "version": "0.29.0",
        "acp_kind": "kimi-acp-session-new",
        "native_kind": "kimi-native-tui-attached",
    },
}


def _ready_bridge_state(record, monkeypatch):
    fixture = _PROVIDER_FIXTURES[record["provider"]]
    native = record["execution_mode"] == "native_tui"
    receipt = {
        "bridge_version": BRIDGE_VERSION,
        "receipt_id": SESSION_ID,
        "provider_session_id": SESSION_ID,
        "provider_receipt_kind": fixture["native_kind"] if native else fixture["acp_kind"],
        "provider_version": fixture["version"],
        "model_input_ready": True,
        "reservation_id": record["reservation_id"],
        "terminal_id": record["terminal_id"],
        "generation": record["generation"],
        "provider": record["provider"],
        "agent_profile": record["agent_profile"],
        "model": record["request"]["expected_model"],
        "effort": record["request"]["expected_effort"],
        "working_directory": record["working_directory"],
    }
    # A native generation has no provider transcript to digest; the argv
    # that started its pane is the evidence that stands in its place.
    if native:
        receipt["launch_argv_sha256"] = "b" * 64
    else:
        receipt["provider_transcript_sha256"] = "a" * 64
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.managed_provider_bridge.read_state",
        lambda rid: {"state": "ready", "readiness": receipt},
        raising=False,
    )
    return receipt


def _bind_request(record, **changes):
    payload = {
        "protocol_version": PROTOCOL_VERSION_V2,
        "terminal_id": record["terminal_id"],
        "generation": record["generation"],
        "attempt_id": str(uuid.uuid4()),
    }
    payload.update(changes)
    return ManagedLaunchV2BindRequest(**payload)


def _launched(worktree, tmp_path, monkeypatch, **changes):
    """Reserve + claim_launch + a ready bridge, ready to bind."""
    request = _reserve_request(worktree, tmp_path, **changes)
    record, _ = v2.reserve(request)
    record, _ = v2.claim_launch(record["reservation_id"])
    _ready_bridge_state(record, monkeypatch)
    return request, record


def _intent():
    return native_attachment.acquire_intent(
        acquisition_method=native_attachment.ACQUISITION_RESUME,
        acquisition_receipt={"session_id": SESSION_ID},
        admits_only_new_instructions=True,
        replays_task_bytes=False,
    )


def _set_columns(reservation_id, **columns):
    """Write reservation columns directly, simulating durable state.

    Used to manufacture rows the current write path cannot produce — a
    pre-contract legacy row, or a row whose persisted mode has drifted
    from its request echo.  Both are states a real database can be in
    after an upgrade, and neither can be reached through ``reserve``.
    """
    with database.SessionLocal() as db:
        row = (
            db.query(database.ManagedLaunchV2ReservationModel)
            .filter(database.ManagedLaunchV2ReservationModel.reservation_id == reservation_id)
            .one()
        )
        for key, value in columns.items():
            setattr(row, key, value)
        db.commit()


# --------------------------------------------------------------------
# Reserve-time resolution
# --------------------------------------------------------------------


def test_reserve_without_mode_or_class_stays_acp(isolated_memory_db, worktree, tmp_path):
    """The historical branch is what a caller that names nothing gets."""
    record, created = v2.reserve(_reserve_request(worktree, tmp_path))
    assert created
    assert record["execution_mode"] == em.ACP
    assert record["execution_mode_source"] == em.SOURCE_CLASS_DEFAULT
    # A row this binary wrote is never legacy, even when it resolved to
    # the same mode a legacy row would report.
    assert record["is_legacy_execution_mode"] is False


def test_glm_route_with_implicit_mode_is_refused_before_acp_resolution(worktree, tmp_path):
    request = _reserve_request(worktree, tmp_path, provider_route="glm", worker_class="hands_off")

    with pytest.raises(
        ManagedLaunchConflict,
        match="provider_route='glm' requires execution_mode='native_tui'",
    ):
        v2._resolve_reserve_mode(request)


def test_reserve_explicit_native_mode_is_recorded_at_launch_precedence(
    isolated_memory_db, worktree, tmp_path
):
    record, _ = v2.reserve(_reserve_request(worktree, tmp_path, execution_mode="native_tui"))
    assert record["execution_mode"] == em.NATIVE_TUI
    assert record["execution_mode_source"] == em.SOURCE_LAUNCH


@pytest.mark.parametrize(
    ("worker_class", "expected"),
    [
        ("persistent", em.NATIVE_TUI),
        ("long_running", em.NATIVE_TUI),
        ("human_monitored", em.NATIVE_TUI),
        ("one_shot", em.ACP),
        ("hands_off", em.ACP),
        ("unspecified", em.ACP),
    ],
)
def test_reserve_worker_class_supplies_the_default(
    isolated_memory_db, worktree, tmp_path, worker_class, expected
):
    record, _ = v2.reserve(_reserve_request(worktree, tmp_path, worker_class=worker_class))
    assert record["execution_mode"] == expected
    assert record["execution_mode_source"] == em.SOURCE_CLASS_DEFAULT


def test_reserve_explicit_mode_outranks_the_class_default(isolated_memory_db, worktree, tmp_path):
    """A class default is a default; an explicit input overrides it."""
    record, _ = v2.reserve(
        _reserve_request(worktree, tmp_path, worker_class="persistent", execution_mode="acp")
    )
    assert record["execution_mode"] == em.ACP
    assert record["execution_mode_source"] == em.SOURCE_LAUNCH


def test_reserve_rejects_a_mode_outside_the_closed_enum(worktree, tmp_path):
    """Rejected at the wire model, so a bad mode never reaches the store."""
    with pytest.raises(ValidationError):
        _reserve_request(worktree, tmp_path, execution_mode="Native_TUI")
    with pytest.raises(ValidationError):
        _reserve_request(worktree, tmp_path, worker_class="daemon")


def test_reserve_persists_both_mode_columns(isolated_memory_db, worktree, tmp_path):
    request = _reserve_request(worktree, tmp_path, worker_class="persistent")
    v2.reserve(request)
    with database.SessionLocal() as db:
        row = (
            db.query(database.ManagedLaunchV2ReservationModel)
            .filter(
                database.ManagedLaunchV2ReservationModel.reservation_id == request.reservation_id
            )
            .one()
        )
        assert row.execution_mode == em.NATIVE_TUI
        assert row.execution_mode_source == em.SOURCE_CLASS_DEFAULT


def test_reserve_replay_is_idempotent_and_keeps_the_mode(isolated_memory_db, worktree, tmp_path):
    request = _reserve_request(worktree, tmp_path, execution_mode="native_tui")
    first, created = v2.reserve(request)
    assert created
    again, created_again = v2.reserve(request)
    assert created_again is False
    assert again["execution_mode"] == em.NATIVE_TUI
    assert again["terminal_id"] == first["terminal_id"]


def test_reserve_replay_with_a_different_mode_conflicts(isolated_memory_db, worktree, tmp_path):
    """The reserved mode is immutable; a retry cannot switch branch."""
    request = _reserve_request(worktree, tmp_path, execution_mode="acp")
    v2.reserve(request)
    switched = _reserve_request(
        worktree,
        tmp_path,
        reservation_id=request.reservation_id,
        execution_mode="native_tui",
    )
    with pytest.raises(ManagedLaunchConflict):
        v2.reserve(switched)


def test_reserve_replay_across_the_upgrade_boundary_stays_idempotent(
    isolated_memory_db, worktree, tmp_path
):
    """An in-flight pre-contract reservation must not conflict on replay.

    The stored request predates the mode keys entirely.  A caller that
    still names no mode is presenting the same request it always did,
    so the replay is idempotent rather than a hard conflict.
    """
    request = _reserve_request(worktree, tmp_path)
    v2.reserve(request)
    stored = json.loads(request.model_dump_json())
    stored.pop("launch_nonce")
    stored.pop("execution_mode")
    stored.pop("worker_class")
    _set_columns(
        request.reservation_id,
        request_json=v2._canonical_json(stored),
        execution_mode=None,
        execution_mode_source=None,
    )

    replayed, created = v2.reserve(request)
    assert created is False
    # It reads back as legacy ACP: the row genuinely predates the contract.
    assert replayed["execution_mode"] == em.ACP
    assert replayed["is_legacy_execution_mode"] is True


def test_reserve_replay_that_newly_names_a_mode_still_conflicts(
    isolated_memory_db, worktree, tmp_path
):
    """The upgrade accommodation covers silence only, never a new demand."""
    request = _reserve_request(worktree, tmp_path)
    v2.reserve(request)
    stored = json.loads(request.model_dump_json())
    stored.pop("launch_nonce")
    stored.pop("execution_mode")
    stored.pop("worker_class")
    _set_columns(
        request.reservation_id,
        request_json=v2._canonical_json(stored),
        execution_mode=None,
        execution_mode_source=None,
    )

    demanding = _reserve_request(
        worktree,
        tmp_path,
        reservation_id=request.reservation_id,
        execution_mode="native_tui",
    )
    with pytest.raises(ManagedLaunchConflict, match="different request"):
        v2.reserve(demanding)


def test_reserve_replay_checks_the_mode_column_not_the_request_echo(
    isolated_memory_db, worktree, tmp_path
):
    """The persisted column is the mode of record on a replay.

    Manufactures durable skew — request echo says native, column says
    acp — which ``reserve`` cannot produce but an upgrade or an external
    write can.  The replay must be judged against the column, because
    that is what every downstream guard reads.
    """
    request = _reserve_request(worktree, tmp_path, execution_mode="native_tui")
    v2.reserve(request)
    _set_columns(
        request.reservation_id,
        execution_mode=em.ACP,
        execution_mode_source=em.SOURCE_LAUNCH,
    )
    with pytest.raises(ManagedLaunchConflict, match="immutable"):
        v2.reserve(request)


# --------------------------------------------------------------------
# Legacy projection
# --------------------------------------------------------------------


def test_legacy_row_projects_as_acp_on_every_public_read(isolated_memory_db, worktree, tmp_path):
    """A NULL mode is ACP with source 'legacy' — never null, never native."""
    request = _reserve_request(worktree, tmp_path)
    v2.reserve(request)
    _set_columns(request.reservation_id, execution_mode=None, execution_mode_source=None)

    record = v2.get(request.reservation_id)
    assert record["execution_mode"] == em.ACP
    assert record["execution_mode_source"] == em.SOURCE_LEGACY
    assert record["is_legacy_execution_mode"] is True


def test_a_stored_mode_outside_the_enum_fails_closed(isolated_memory_db, worktree, tmp_path):
    """A corrupt stored mode is refused, never coerced to a valid one.

    NULL means legacy ACP, but a *present* value outside the closed enum
    means the column cannot be trusted — durable corruption, or a mode
    written by a newer binary and then rolled back.  Reading it as ACP
    would silently downgrade a generation that may be running native, so
    every reader refuses instead.
    """
    request = _reserve_request(worktree, tmp_path)
    v2.reserve(request)
    _set_columns(request.reservation_id, execution_mode="native")

    with pytest.raises(em.ExecutionModeInvalid):
        run_manifest.build("run-0001")
    with pytest.raises(ManagedLaunchUnavailable):
        v2.get(request.reservation_id)


def test_public_projection_never_omits_the_mode_keys(isolated_memory_db, worktree, tmp_path):
    """A consumer can rely on the keys existing on every v2 record."""
    request = _reserve_request(worktree, tmp_path)
    reserved, _ = v2.reserve(request)
    fetched = v2.get(request.reservation_id)
    launching, _ = v2.claim_launch(request.reservation_id)
    for surface in (reserved, fetched, launching):
        for key in ("execution_mode", "execution_mode_source", "is_legacy_execution_mode"):
            assert key in surface


# --------------------------------------------------------------------
# Vintage migration
# --------------------------------------------------------------------


def _v2_columns(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(managed_launch_v2_reservations)")}
    finally:
        conn.close()


def test_migration_adds_the_mode_columns_to_an_existing_table(tmp_path):
    """The additive path, not just fresh creation, must yield the columns."""
    db_path = tmp_path / "metadata.db"
    conn = sqlite3.connect(str(db_path))
    try:
        # The v2 reservation table exactly as an earlier binary created it:
        # real rows, and none of the columns added since.
        conn.execute(
            "CREATE TABLE managed_launch_v2_reservations ("
            "reservation_id TEXT PRIMARY KEY, "
            "protocol_vintage TEXT NOT NULL, "
            "state TEXT NOT NULL, "
            "created_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL"
            ")"
        )
        conn.execute(
            "INSERT INTO managed_launch_v2_reservations VALUES ('r-1','v2','reserved','t','t')"
        )
        conn.commit()
    finally:
        conn.close()

    receipt = vm.migrate_v2(db_path)
    assert "execution_mode" in receipt["added_columns"]
    assert "execution_mode_source" in receipt["added_columns"]
    columns = _v2_columns(db_path)
    assert {"execution_mode", "execution_mode_source"} <= columns

    # The pre-existing row survives and reads back as legacy.
    conn = sqlite3.connect(str(db_path))
    try:
        value = conn.execute(
            "SELECT execution_mode FROM managed_launch_v2_reservations WHERE reservation_id='r-1'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert value is None
    assert em.mode_of_record({"execution_mode": value}) == em.ACP


def test_migration_reports_no_added_columns_on_a_second_run(tmp_path):
    """``added_columns`` is the receipt of an actual schema change."""
    db_path = tmp_path / "metadata.db"
    first = vm.migrate_v2(db_path)
    # Fresh creation already includes the columns in the DDL.
    assert first["added_columns"] == []
    assert {"execution_mode", "execution_mode_source"} <= _v2_columns(db_path)
    again = vm.migrate_v2(db_path)
    assert again["already_present"] is True
    assert again["added_columns"] == []


def test_migration_journals_the_added_columns(tmp_path):
    db_path = tmp_path / "metadata.db"
    conn = sqlite3.connect(str(db_path))
    try:
        # Both v2 tables already exist, in their pre-contract shape. This
        # is the case the journal field was added for: the tables are
        # present, so ``already_present`` is true, yet the schema really
        # did change.
        conn.execute(
            "CREATE TABLE managed_launch_v2_reservations ("
            "reservation_id TEXT PRIMARY KEY, "
            "protocol_vintage TEXT NOT NULL, "
            "state TEXT NOT NULL, "
            "created_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL"
            ")"
        )
        conn.execute("CREATE TABLE managed_launch_v2_terminals (id TEXT PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()
    receipt = vm.migrate_v2(db_path)
    assert receipt["already_present"] is True

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(f"SELECT detail FROM {vm.JOURNAL_TABLE}").fetchall()
    finally:
        conn.close()
    details = [json.loads(row[0]) for row in rows]
    added = [d for d in details if "execution_mode" in d.get("added_columns", [])]
    # Without this the receipt for a real schema change would read as a
    # no-op, because ``already_present`` was already true.
    assert added, details
    assert added[0]["already_present"] is True


# --------------------------------------------------------------------
# Bind: mode-tagged receipts
# --------------------------------------------------------------------


def test_bind_tags_both_receipts_with_the_execution_mode(
    isolated_memory_db, worktree, tmp_path, monkeypatch
):
    _request, record = _launched(worktree, tmp_path, monkeypatch, **_NATIVE)
    bound = v2.bind_native(record["reservation_id"], _bind_request(record))

    assert bound["execution_mode"] == em.NATIVE_TUI
    assert bound["binding"]["schema"] == "cao-managed-v2-native-binding-v1"
    assert bound["binding"]["execution_mode"] == em.NATIVE_TUI

    path = binding_record_path(v2.COMPANION_DIR, record["terminal_id"], record["generation"])
    published = json.loads(path.read_text())
    assert published["schema"] == "cao-generation-binding-v1"
    assert published["execution_mode"] == em.NATIVE_TUI


def test_bind_tags_an_acp_generation_as_acp(isolated_memory_db, worktree, tmp_path, monkeypatch):
    _request, record = _launched(worktree, tmp_path, monkeypatch)
    bound = v2.bind_native(record["reservation_id"], _bind_request(record))
    assert bound["binding"]["execution_mode"] == em.ACP
    path = binding_record_path(v2.COMPANION_DIR, record["terminal_id"], record["generation"])
    assert json.loads(path.read_text())["execution_mode"] == em.ACP


def test_native_binding_digest_covers_the_execution_mode(isolated_memory_db):
    """Mode-tagged evidence: an ACP binding cannot satisfy native admission.

    Compares two bindings identical in every other field, which is the
    only way to attribute the difference to the mode alone — two real
    binds also differ by attempt id and fencing token, so a digest
    difference between them would prove nothing.
    """
    binding = {
        "schema": "cao-managed-v2-native-binding-v1",
        "attempt_id": "11111111-1111-4111-8111-111111111111",
        "execution_mode": em.ACP,
        "native_session_id": SESSION_ID,
    }
    acp_digest = v2.native_binding_digest({"binding": dict(binding)})
    native_digest = v2.native_binding_digest(
        {"binding": {**binding, "execution_mode": em.NATIVE_TUI}}
    )
    assert acp_digest and native_digest
    assert acp_digest != native_digest


def test_bind_refuses_a_restated_mode_that_disagrees(
    isolated_memory_db, worktree, tmp_path, monkeypatch
):
    _request, record = _launched(worktree, tmp_path, monkeypatch, execution_mode="acp")
    with pytest.raises(ManagedLaunchConflict, match="immutable"):
        v2.bind_native(record["reservation_id"], _bind_request(record, execution_mode="native_tui"))
    # Refused before any binding bytes were computed.
    assert v2.get(record["reservation_id"])["state"] == "launching"
    assert v2.get(record["reservation_id"])["binding"] is None


def test_bind_accepts_a_restated_mode_that_agrees(
    isolated_memory_db, worktree, tmp_path, monkeypatch
):
    _request, record = _launched(worktree, tmp_path, monkeypatch, **_NATIVE)
    bound = v2.bind_native(
        record["reservation_id"], _bind_request(record, execution_mode="native_tui")
    )
    assert bound["state"] == "bound"


def test_bind_without_a_restated_mode_uses_the_reserved_one(
    isolated_memory_db, worktree, tmp_path, monkeypatch
):
    """Silence is not a change: an un-restated bind keeps the reserved mode."""
    _request, record = _launched(
        worktree, tmp_path, monkeypatch, worker_class="persistent", **_NATIVE_PROVIDER
    )
    bound = v2.bind_native(record["reservation_id"], _bind_request(record))
    assert bound["binding"]["execution_mode"] == em.NATIVE_TUI


def test_legacy_generation_binds_as_acp(isolated_memory_db, worktree, tmp_path, monkeypatch):
    """A pre-contract row binds ACP, and cannot be bound native."""
    _request, record = _launched(worktree, tmp_path, monkeypatch)
    _set_columns(record["reservation_id"], execution_mode=None, execution_mode_source=None)

    with pytest.raises(ManagedLaunchConflict, match="immutable"):
        v2.bind_native(record["reservation_id"], _bind_request(record, execution_mode="native_tui"))
    bound = v2.bind_native(record["reservation_id"], _bind_request(record))
    assert bound["binding"]["execution_mode"] == em.ACP


def test_published_record_matches_the_journaled_record_exactly(
    isolated_memory_db, worktree, tmp_path, monkeypatch
):
    """Publication must reproduce the journaled bytes, mode included.

    The reconcile path compares the published file against the journaled
    ``binding_record`` for equality, so a tag that reaches one and not the
    other turns every crash-resumed bind into a permanent refusal to
    complete.
    """
    _request, record = _launched(worktree, tmp_path, monkeypatch, **_NATIVE)
    v2.bind_native(record["reservation_id"], _bind_request(record))

    with database.SessionLocal() as db:
        row = (
            db.query(database.ManagedLaunchV2ReservationModel)
            .filter(
                database.ManagedLaunchV2ReservationModel.reservation_id == record["reservation_id"]
            )
            .one()
        )
        journaled = json.loads(row.bind_intent_json)["binding_record"]

    path = binding_record_path(v2.COMPANION_DIR, record["terminal_id"], record["generation"])
    assert json.loads(path.read_text()) == journaled
    assert journaled["execution_mode"] == em.NATIVE_TUI


def test_native_bind_reconciles_after_a_crash_before_publication(
    isolated_memory_db, worktree, tmp_path, monkeypatch
):
    _request, record = _launched(worktree, tmp_path, monkeypatch, **_NATIVE)
    attempt_id = str(uuid.uuid4())
    real_write = v2.write_binding_record

    def _write_then_crash(*args, **kwargs):
        real_write(*args, **kwargs)
        raise RuntimeError("crash after publication, before the SQL commit")

    monkeypatch.setattr(v2, "write_binding_record", _write_then_crash)
    with pytest.raises(ManagedLaunchUnavailable):
        v2.bind_native(record["reservation_id"], _bind_request(record, attempt_id=attempt_id))
    monkeypatch.setattr(v2, "write_binding_record", real_write)

    bound = v2.bind_native(record["reservation_id"], _bind_request(record, attempt_id=attempt_id))
    assert bound["state"] == "bound"
    assert bound["binding"]["execution_mode"] == em.NATIVE_TUI


def test_an_intent_journaled_without_a_mode_publishes_without_the_key(
    isolated_memory_db, worktree, tmp_path, monkeypatch
):
    """A pre-contract journal must publish exactly as it was journaled.

    Grafting a mode onto those bytes at publication time would make the
    published record differ from the journal that a subsequent reconcile
    compares it against — the same mismatch, arrived at from the other
    direction.
    """
    _request, record = _launched(worktree, tmp_path, monkeypatch)
    attempt_id = str(uuid.uuid4())
    real_write = v2.write_binding_record

    def _crash(*args, **kwargs):
        raise RuntimeError("crash after intent journal, before publication")

    monkeypatch.setattr(v2, "write_binding_record", _crash)
    with pytest.raises(ManagedLaunchUnavailable):
        v2.bind_native(record["reservation_id"], _bind_request(record, attempt_id=attempt_id))
    monkeypatch.setattr(v2, "write_binding_record", real_write)

    # Rewrite the journal into the shape an older binary would have left.
    with database.SessionLocal() as db:
        row = (
            db.query(database.ManagedLaunchV2ReservationModel)
            .filter(
                database.ManagedLaunchV2ReservationModel.reservation_id == record["reservation_id"]
            )
            .one()
        )
        intent = json.loads(row.bind_intent_json)
        intent["binding_record"].pop("execution_mode")
        row.bind_intent_json = v2._canonical_json(intent)
        db.commit()

    bound = v2.bind_native(record["reservation_id"], _bind_request(record, attempt_id=attempt_id))
    assert bound["state"] == "bound"
    path = binding_record_path(v2.COMPANION_DIR, record["terminal_id"], record["generation"])
    published = json.loads(path.read_text())
    assert "execution_mode" not in published
    assert published == intent["binding_record"]
    # Absent still reads as ACP, never as native.
    assert em.mode_of_record(published) == em.ACP


# --------------------------------------------------------------------
# Bind: exclusive attachment ownership
# --------------------------------------------------------------------


def test_bind_refused_when_another_owner_holds_the_session(
    isolated_memory_db, worktree, tmp_path, monkeypatch
):
    _request, record = _launched(worktree, tmp_path, monkeypatch)
    native_attachment.declare(
        provider="codex",
        native_session_id=SESSION_ID,
        terminal_id="feedface",
        generation=str(uuid.uuid4()),
        execution_mode=em.NATIVE_TUI,
        intent=_intent(),
    )
    with pytest.raises(ManagedLaunchConflict, match="second concurrent attachment"):
        v2.bind_native(record["reservation_id"], _bind_request(record))
    assert v2.get(record["reservation_id"])["binding"] is None


def test_bind_refused_when_the_attachment_is_frozen_ambiguous(
    isolated_memory_db, worktree, tmp_path, monkeypatch
):
    """Ambiguity is exactly the state in which the owner is unknown."""
    _request, record = _launched(worktree, tmp_path, monkeypatch)
    native_attachment.declare(
        provider="codex",
        native_session_id=SESSION_ID,
        terminal_id=record["terminal_id"],
        generation=record["generation"],
        execution_mode=em.ACP,
        intent=_intent(),
    )
    native_attachment.mark_ambiguous(
        provider="codex",
        native_session_id=SESSION_ID,
        reason="pane vanished with an unproven survivor",
    )
    with pytest.raises(ManagedLaunchConflict, match="frozen ambiguous"):
        v2.bind_native(record["reservation_id"], _bind_request(record))


def test_bind_allowed_when_the_same_owner_holds_the_session(
    isolated_memory_db, worktree, tmp_path, monkeypatch
):
    """A generation re-binding its own session is not a second attachment."""
    _request, record = _launched(worktree, tmp_path, monkeypatch)
    native_attachment.declare(
        provider="codex",
        native_session_id=SESSION_ID,
        terminal_id=record["terminal_id"],
        generation=record["generation"],
        execution_mode=em.ACP,
        intent=_intent(),
    )
    bound = v2.bind_native(record["reservation_id"], _bind_request(record))
    assert bound["state"] == "bound"


def test_bind_refused_when_the_same_owner_holds_it_in_the_other_mode(
    isolated_memory_db, worktree, tmp_path, monkeypatch
):
    """Same owner is not a free pass — the recorded mode must match too."""
    _request, record = _launched(worktree, tmp_path, monkeypatch, execution_mode="acp")
    native_attachment.declare(
        provider="codex",
        native_session_id=SESSION_ID,
        terminal_id=record["terminal_id"],
        generation=record["generation"],
        execution_mode=em.NATIVE_TUI,
        intent=_intent(),
    )
    with pytest.raises(ManagedLaunchConflict, match="never cross"):
        v2.bind_native(record["reservation_id"], _bind_request(record))


def test_bind_allowed_when_a_prior_attachment_was_released(
    isolated_memory_db, worktree, tmp_path, monkeypatch
):
    """A detached row is not a live owner and must not block a bind."""
    _request, record = _launched(worktree, tmp_path, monkeypatch)
    other_generation = str(uuid.uuid4())
    native_attachment.declare(
        provider="codex",
        native_session_id=SESSION_ID,
        terminal_id="feedface",
        generation=other_generation,
        execution_mode=em.NATIVE_TUI,
        intent=_intent(),
    )
    native_attachment.mark_starting(
        provider="codex",
        native_session_id=SESSION_ID,
        terminal_id="feedface",
        generation=other_generation,
        execution_mode=em.NATIVE_TUI,
    )
    identity = native_attachment.process_identity(pid=4242, start_marker="Tue10:01:02")
    native_attachment.mark_attached(
        provider="codex",
        native_session_id=SESSION_ID,
        terminal_id="feedface",
        generation=other_generation,
        execution_mode=em.NATIVE_TUI,
        process_identity=identity,
    )
    native_attachment.release(
        provider="codex",
        native_session_id=SESSION_ID,
        terminal_id="feedface",
        generation=other_generation,
        execution_mode=em.NATIVE_TUI,
        proof=native_attachment.no_survivor_proof(
            provider="codex",
            native_session_id=SESSION_ID,
            terminal_id="feedface",
            generation=other_generation,
            execution_mode=em.NATIVE_TUI,
            pane_id=None,
            process_identity=identity,
            survivors=[],
            observed_at="2026-07-24T00:00:00Z",
            observer="test",
        ),
    )
    bound = v2.bind_native(record["reservation_id"], _bind_request(record))
    assert bound["state"] == "bound"


def test_bind_ownership_is_rechecked_on_the_reconciled_path(
    isolated_memory_db, worktree, tmp_path, monkeypatch
):
    """A crash-resumed bind re-validates the session it is about to publish.

    The journaled intent already exists, so the fresh-bind branch is
    skipped entirely.  If ownership were only checked while building the
    intent, a retry after a foreign attach would publish anyway.
    """
    _request, record = _launched(worktree, tmp_path, monkeypatch)
    attempt_id = str(uuid.uuid4())

    real_write = v2.write_binding_record

    def _write_then_crash(*args, **kwargs):
        raise RuntimeError("crash after intent journal, before publication")

    monkeypatch.setattr(v2, "write_binding_record", _write_then_crash)
    with pytest.raises(ManagedLaunchUnavailable):
        v2.bind_native(record["reservation_id"], _bind_request(record, attempt_id=attempt_id))
    # Restored by targeted setattr rather than monkeypatch.undo(), which
    # would also revert the isolated database this test is running on.
    monkeypatch.setattr(v2, "write_binding_record", real_write)

    # The intent is journaled; a foreign owner then takes the session.
    assert v2.get(record["reservation_id"])["state"] == "launching"
    native_attachment.declare(
        provider="codex",
        native_session_id=SESSION_ID,
        terminal_id="feedface",
        generation=str(uuid.uuid4()),
        execution_mode=em.NATIVE_TUI,
        intent=_intent(),
    )
    # Same attempt id, so the journaled intent is reused and the
    # intent-building branch is skipped entirely.
    with pytest.raises(ManagedLaunchConflict, match="second concurrent attachment"):
        v2.bind_native(record["reservation_id"], _bind_request(record, attempt_id=attempt_id))


# --------------------------------------------------------------------
# Run manifest
# --------------------------------------------------------------------


def test_manifest_projects_every_generation_with_a_concrete_mode(
    isolated_memory_db, worktree, tmp_path
):
    v2.reserve(_reserve_request(worktree, tmp_path, execution_mode="native_tui"))
    v2.reserve(_reserve_request(worktree, tmp_path, execution_mode="acp"))
    legacy = _reserve_request(worktree, tmp_path)
    v2.reserve(legacy)
    _set_columns(legacy.reservation_id, execution_mode=None, execution_mode_source=None)

    manifest = run_manifest.build("run-0001")
    assert manifest["schema"] == run_manifest.MANIFEST_SCHEMA
    assert manifest["run_id"] == "run-0001"
    assert len(manifest["entries"]) == 3
    assert manifest["counts_by_execution_mode"] == {em.ACP: 2, em.NATIVE_TUI: 1}
    assert manifest["legacy_entry_count"] == 1
    assert all(entry["execution_mode"] in em.EXECUTION_MODES for entry in manifest["entries"])


def test_manifest_is_scoped_to_one_run(isolated_memory_db, worktree, tmp_path):
    v2.reserve(_reserve_request(worktree, tmp_path, execution_mode="native_tui"))
    v2.reserve(_reserve_request(worktree, tmp_path, run_id="run-0002"))
    manifest = run_manifest.build("run-0001")
    assert len(manifest["entries"]) == 1
    assert manifest["counts_by_execution_mode"] == {em.ACP: 0, em.NATIVE_TUI: 1}


def test_manifest_of_an_unknown_run_is_empty_not_an_error(isolated_memory_db):
    manifest = run_manifest.build("run-does-not-exist")
    assert manifest["entries"] == []
    assert manifest["legacy_entry_count"] == 0


def test_manifest_rejects_an_empty_run_id(isolated_memory_db):
    with pytest.raises(ValueError):
        run_manifest.build("")


def test_manifest_digest_is_stable_and_excludes_the_clock(isolated_memory_db, worktree, tmp_path):
    """A digest that changed once per second could not pin anything."""
    v2.reserve(_reserve_request(worktree, tmp_path, execution_mode="native_tui"))
    first = run_manifest.build("run-0001")
    second = run_manifest.build("run-0001")
    assert run_manifest.manifest_digest(first) == run_manifest.manifest_digest(second)
    # Proven to be the clock that is excluded, not merely that two reads
    # happened to land in the same instant.
    tampered = dict(first)
    tampered["generated_at"] = "1999-01-01T00:00:00Z"
    assert run_manifest.manifest_digest(tampered) == run_manifest.manifest_digest(first)


def test_manifest_digest_changes_when_a_mode_changes(isolated_memory_db, worktree, tmp_path):
    request = _reserve_request(worktree, tmp_path, execution_mode="acp")
    v2.reserve(request)
    before = run_manifest.manifest_digest(run_manifest.build("run-0001"))
    _set_columns(request.reservation_id, execution_mode=em.NATIVE_TUI)
    after = run_manifest.manifest_digest(run_manifest.build("run-0001"))
    assert before != after


def test_manifest_carries_the_binding_mode_and_attachment(
    isolated_memory_db, worktree, tmp_path, monkeypatch
):
    _request, record = _launched(worktree, tmp_path, monkeypatch, **_NATIVE)
    v2.bind_native(record["reservation_id"], _bind_request(record))
    native_attachment.declare(
        provider=record["provider"],
        native_session_id=SESSION_ID,
        terminal_id=record["terminal_id"],
        generation=record["generation"],
        execution_mode=em.NATIVE_TUI,
        intent=_intent(),
    )

    entry = run_manifest.build("run-0001")["entries"][0]
    assert entry["native_session_id"] == SESSION_ID
    assert entry["execution_mode"] == em.NATIVE_TUI
    assert entry["binding_execution_mode"] == em.NATIVE_TUI
    assert entry["attachment"]["state"] == native_attachment.DECLARED
    assert entry["attachment"]["owner_terminal_id"] == record["terminal_id"]
    assert entry["attachment"]["owner_execution_mode"] == em.NATIVE_TUI


def test_manifest_reports_no_attachment_before_one_is_declared(
    isolated_memory_db, worktree, tmp_path, monkeypatch
):
    _request, record = _launched(worktree, tmp_path, monkeypatch)
    v2.bind_native(record["reservation_id"], _bind_request(record))
    entry = run_manifest.build("run-0001")["entries"][0]
    assert entry["native_session_id"] == SESSION_ID
    assert entry["attachment"] is None


# --------------------------------------------------------------------
# The launch branch gate
# --------------------------------------------------------------------
#
# ``reserve`` deliberately admits ``native_tui``: a reservation is a
# durable statement of intent that ``bind_native`` and the run manifest
# make about sessions this surface did not start.  ``launch_reserved``,
# by contrast, has exactly one branch — it starts the ACP bridge — so it
# must refuse any mode it cannot actually run.  Without that refusal,
# reserving ``worker_class="persistent"`` (which the resolver defaults to
# native) and calling ``launch_reserved`` would put an ACP bridge under a
# reservation row, binding receipt, run manifest, and public status that
# all say ``native_tui``.


class _ClaimReached(RuntimeError):
    """Sentinel proving control reached the claim, i.e. passed the gate."""


@pytest.mark.asyncio
async def test_launch_refuses_a_mode_it_has_no_branch_for(
    isolated_memory_db, worktree, tmp_path, monkeypatch
):
    """A reservation whose mode has no branch is refused, never rerouted.

    Both modes now have branches, so the withheld-branch case is staged
    by narrowing the launchable set.  That is the state this gate exists
    for: the mode vocabulary is closed and shared, but a given surface
    may only have built some of it, and the missing part must refuse
    rather than fall through to whichever branch does exist.
    """
    monkeypatch.setattr(v2, "LAUNCHABLE_EXECUTION_MODES", (em.ACP,))
    record, _ = v2.reserve(
        _reserve_request(worktree, tmp_path, worker_class="persistent", **_NATIVE_PROVIDER)
    )
    assert record["execution_mode"] == em.NATIVE_TUI

    def _explode(*args, **kwargs):
        raise AssertionError("a native reservation must never reach the ACP launch path")

    monkeypatch.setattr(v2, "claim_launch", _explode)

    with pytest.raises(ManagedLaunchConflict, match="no launch branch"):
        await v2.launch_reserved(record["reservation_id"])


@pytest.mark.asyncio
async def test_a_refused_launch_leaves_the_reservation_exactly_as_it_was(
    isolated_memory_db, worktree, tmp_path, monkeypatch
):
    """The refusal must not strand the row in ``launching``.

    A reservation parked in ``launching`` reads as "a launch is in
    flight" to every later reader, including the idempotent replay in
    ``claim_launch``, so refusing after the claim would trade a silent
    mode swap for a permanently unlaunchable reservation.
    """
    monkeypatch.setattr(v2, "LAUNCHABLE_EXECUTION_MODES", (em.ACP,))
    record, _ = v2.reserve(_reserve_request(worktree, tmp_path, **_NATIVE))

    with pytest.raises(ManagedLaunchConflict):
        await v2.launch_reserved(record["reservation_id"])

    after = v2.get(record["reservation_id"])
    assert after["state"] == "reserved"
    assert after["execution_mode"] == em.NATIVE_TUI
    assert after["terminal_id"] == record["terminal_id"]


@pytest.mark.asyncio
async def test_an_acp_reservation_still_passes_the_gate(
    isolated_memory_db, worktree, tmp_path, monkeypatch
):
    """The gate must be a mode check, not a blanket refusal.

    Proven by letting the claim raise a sentinel: reaching it means the
    gate admitted ACP and handed control to the real launch path.
    """
    record, _ = v2.reserve(_reserve_request(worktree, tmp_path, execution_mode="acp"))

    def _sentinel(reservation_id):
        raise _ClaimReached()

    monkeypatch.setattr(v2, "claim_launch", _sentinel)

    with pytest.raises(_ClaimReached):
        await v2.launch_reserved(record["reservation_id"])


def test_the_launchable_set_is_exactly_what_has_a_branch():
    """This tuple is a claim about real code, not about the vocabulary.

    Native TUI belongs here only because ``_launch_native_tui`` exists
    and is reached; widening it ahead of a branch is what reintroduces
    the silent ACP fallback.
    """
    assert v2.LAUNCHABLE_EXECUTION_MODES == (em.ACP, em.NATIVE_TUI)
    assert callable(v2._launch_native_tui)


def test_the_v1_surface_does_not_inherit_the_native_advertisement():
    """Two surfaces, two claims: only the one with a branch may say so.

    The v1 managed-launch surface still mints its session over the ACP
    bridge and has no step that starts a provider terminal.  Its
    advertised set must therefore stay narrower than v2's, and the
    capability endpoint publishes them separately for exactly that
    reason.
    """
    from cli_agent_orchestrator.services import managed_launch as v1

    assert v1.SUPPORTED_EXECUTION_MODES == (em.ACP,)
    assert em.NATIVE_TUI in v2.LAUNCHABLE_EXECUTION_MODES


def test_only_providers_with_a_native_branch_may_launch_native():
    """Mode support and provider support are separate claims.

    A native reservation for a provider with no native branch must stop,
    not fall back: the mode gate above would pass it, so the provider
    gate is the one that catches it.
    """
    assert v2.NATIVE_TUI_PROVIDERS == frozenset({"codex", "kimi_cli", "claude_code"})
    # The set is *derived* from the adapters that exist, not written out
    # by hand, so a provider cannot be advertised as native-launchable
    # while one of the three surfaces it needs is missing. Asserted as an
    # equality against each table rather than a subset: a provider that
    # appeared in only two of them would advertise a launch that fails
    # part-way through.
    assert set(v2._NATIVE_TUI_READINESS_RECEIPT_KINDS) == v2.NATIVE_TUI_PROVIDERS
    assert v2.NATIVE_TUI_PROVIDERS <= set(v2._ISSUANCE_SOURCES)
    assert v2.NATIVE_TUI_PROVIDERS <= set(v2._PINNED_PROVIDER)
    assert v2.NATIVE_TUI_PROVIDERS <= native_tui_launch.SUPPORTED_NATIVE_PROVIDERS
    # Disjoint from the ACP kinds, so neither table can accept the other's
    # evidence by accident.
    assert not set(v2._NATIVE_TUI_READINESS_RECEIPT_KINDS.values()) & set(
        v2._READINESS_RECEIPT_KINDS.values()
    )
