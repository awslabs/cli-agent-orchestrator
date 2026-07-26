"""Execution-mode selection on the v1 managed-launch surface.

The property under test throughout is that an *accepted* mode is a mode
that will actually run.  A caller verifies the request echo to confirm
what it asked for; that verification is only worth anything if this
surface refuses the modes it cannot honour instead of quietly running a
different one.
"""

from __future__ import annotations

import hashlib
import json
import uuid

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.managed_launch import (
    PROTOCOL_VERSION,
    ManagedLaunchReserveRequest,
)
from cli_agent_orchestrator.services import execution_mode as em
from cli_agent_orchestrator.services import managed_launch
from cli_agent_orchestrator.services.managed_launch import (
    ManagedLaunchConflict,
    ManagedLaunchUnavailable,
)

DELIVERY_ID = "33333333-3333-4333-8333-333333333333"


def _reserve_request(tmp_path, **changes):
    executable = tmp_path / "fake-provider"
    if not executable.exists():
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
    payload = {
        "protocol_version": PROTOCOL_VERSION,
        "reservation_id": str(uuid.uuid4()),
        "session_name": "cao-test",
        "provider": "codex",
        "agent_profile": "reviewer-sol-max",
        "caller_id": "deadbeef",
        "project": "test-project",
        "task_id": "test-task",
        "delivery_id": DELIVERY_ID,
        "working_directory": str(tmp_path),
        "trusted_project_root": str(tmp_path),
        "expected_model": "gpt-5.6-sol",
        "expected_effort": "xhigh",
        "provider_executable": str(executable),
        "provider_executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
    }
    payload.update(changes)
    return ManagedLaunchReserveRequest(**payload)


def _rewrite_request(reservation_id, mutate):
    """Rewrite a reservation's persisted request bytes.

    Used to manufacture rows the current write path cannot produce — a
    reservation stored before the execution-mode fields existed, or one
    whose stored request has since been corrupted.  Both are states a
    real database can be in after an upgrade or a rollback, and neither
    is reachable through ``reserve``.
    """
    with database.SessionLocal() as db:
        row = (
            db.query(database.ManagedLaunchReservationModel)
            .filter(database.ManagedLaunchReservationModel.reservation_id == reservation_id)
            .first()
        )
        stored = json.loads(row.request_json)
        mutate(stored)
        row.request_json = json.dumps(stored, sort_keys=True, separators=(",", ":"))
        db.commit()


def _drop_mode_keys(stored):
    stored.pop("execution_mode", None)
    stored.pop("worker_class", None)


# --------------------------------------------------------------------------
# Resolution at reserve time
# --------------------------------------------------------------------------


def test_reserve_without_mode_or_class_stays_acp(isolated_memory_db, tmp_path):
    """An old client that names nothing keeps its existing behaviour."""
    record, created = managed_launch.reserve(_reserve_request(tmp_path))

    assert created is True
    assert record["execution_mode"] == "acp"
    assert record["execution_mode_source"] == em.SOURCE_CLASS_DEFAULT
    assert record["is_legacy_execution_mode"] is False


def test_v1_control_projection_names_its_only_supported_mode(isolated_memory_db, tmp_path):
    record, _ = managed_launch.reserve(_reserve_request(tmp_path))

    identity = managed_launch.managed_control_identity(record["terminal_id"])

    assert identity is not None
    assert identity["vintage"] == "v1"
    assert identity["execution_mode"] == em.ACP


def test_request_echo_is_faithful_rather_than_resolved(isolated_memory_db, tmp_path):
    """The echo mirrors the request; the resolved mode is separate.

    A caller compares the echo against what it sent, so the echo must
    stay null when it sent nothing.  Substituting the resolved default
    there would make every omitted-field reservation look like an
    explicit ACP request and defeat the comparison.
    """
    record, _ = managed_launch.reserve(_reserve_request(tmp_path))

    assert record["request"]["execution_mode"] is None
    assert record["request"]["worker_class"] is None
    assert record["execution_mode"] == "acp"


def test_reserve_explicit_acp_is_recorded_and_echoed(isolated_memory_db, tmp_path):
    request = _reserve_request(tmp_path, execution_mode="acp")
    record, _ = managed_launch.reserve(request)

    assert record["request"]["execution_mode"] == "acp"
    assert record["execution_mode"] == "acp"
    assert record["execution_mode_source"] == em.SOURCE_LAUNCH


def test_reserve_refuses_native_until_a_native_branch_exists(isolated_memory_db, tmp_path):
    """A mode this surface cannot run is refused, not silently downgraded.

    This is the guarantee the request echo rests on.  Were the native
    request accepted, the reservation would echo ``native_tui`` back
    while an ACP bridge did the work, and a caller checking that echo
    would have its mistaken belief confirmed rather than caught.
    """
    request = _reserve_request(tmp_path, execution_mode="native_tui")

    with pytest.raises(ManagedLaunchConflict) as excinfo:
        managed_launch.reserve(request)

    assert "native_tui" in str(excinfo.value)
    assert "not supported" in str(excinfo.value)
    # Refused before any durable effect: no reservation exists to resume.
    with pytest.raises(managed_launch.ManagedLaunchNotFound):
        managed_launch.get(request.reservation_id)


def test_reserve_refuses_a_class_whose_default_is_native(isolated_memory_db, tmp_path):
    """A native default arrived at by class is refused just as an explicit one is.

    The refusal is about the mode that would take effect, not about how
    the caller spelled it; a class default that resolves to an
    unsupported mode is exactly as unrunnable as naming it outright.
    """
    request = _reserve_request(tmp_path, worker_class="persistent")

    with pytest.raises(ManagedLaunchConflict) as excinfo:
        managed_launch.reserve(request)

    assert "native_tui" in str(excinfo.value)
    assert em.SOURCE_CLASS_DEFAULT in str(excinfo.value)


@pytest.mark.parametrize("worker_class", ["one_shot", "hands_off", "unspecified"])
def test_reserve_accepts_a_class_whose_default_is_acp(isolated_memory_db, tmp_path, worker_class):
    record, _ = managed_launch.reserve(_reserve_request(tmp_path, worker_class=worker_class))

    assert record["execution_mode"] == "acp"
    assert record["execution_mode_source"] == em.SOURCE_CLASS_DEFAULT
    assert record["request"]["worker_class"] == worker_class


def test_explicit_acp_outranks_a_native_defaulting_class(isolated_memory_db, tmp_path):
    """An explicit mode fills the slot the class default would have filled.

    So a long-lived worker class does not lock a caller out of this
    surface: naming ACP explicitly is a supported, accepted request.
    """
    request = _reserve_request(tmp_path, worker_class="persistent", execution_mode="acp")
    record, _ = managed_launch.reserve(request)

    assert record["execution_mode"] == "acp"
    assert record["execution_mode_source"] == em.SOURCE_LAUNCH


def test_reserve_rejects_a_mode_outside_the_closed_enum(tmp_path):
    """Rejected at request validation, before the service is ever entered."""
    with pytest.raises(ValueError):
        _reserve_request(tmp_path, execution_mode="native")


def test_reserve_rejects_a_worker_class_outside_the_closed_enum(tmp_path):
    with pytest.raises(ValueError):
        _reserve_request(tmp_path, worker_class="daemon")


# --------------------------------------------------------------------------
# Idempotent replay, including across the upgrade boundary
# --------------------------------------------------------------------------


def test_reserve_replay_is_idempotent_and_keeps_the_mode(isolated_memory_db, tmp_path):
    request = _reserve_request(tmp_path, execution_mode="acp")
    first, created_first = managed_launch.reserve(request)
    second, created_second = managed_launch.reserve(request)

    assert created_first is True
    assert created_second is False
    assert second["execution_mode"] == first["execution_mode"]
    assert second["generation"] == first["generation"]


def test_reserve_replay_with_a_different_mode_conflicts(isolated_memory_db, tmp_path):
    request = _reserve_request(tmp_path, worker_class="one_shot")
    managed_launch.reserve(request)

    replay = _reserve_request(
        tmp_path,
        reservation_id=request.reservation_id,
        worker_class="hands_off",
    )
    with pytest.raises(ManagedLaunchConflict):
        managed_launch.reserve(replay)


def test_reserve_replay_across_the_upgrade_boundary_stays_idempotent(isolated_memory_db, tmp_path):
    """A reservation in flight across the deploy still replays cleanly.

    The stored request predates the mode fields, so a byte comparison
    against a payload that now carries two explicit nulls would fail and
    turn an ordinary retry into a permanent conflict.
    """
    request = _reserve_request(tmp_path)
    first, _ = managed_launch.reserve(request)
    _rewrite_request(request.reservation_id, _drop_mode_keys)

    replayed, created = managed_launch.reserve(request)

    assert created is False
    assert replayed["generation"] == first["generation"]
    assert replayed["execution_mode"] == "acp"


def test_reserve_replay_that_newly_names_a_mode_still_conflicts(isolated_memory_db, tmp_path):
    """Absence is only equal to silence, never to an explicit value."""
    request = _reserve_request(tmp_path)
    managed_launch.reserve(request)
    _rewrite_request(request.reservation_id, _drop_mode_keys)

    replay = _reserve_request(
        tmp_path,
        reservation_id=request.reservation_id,
        execution_mode="acp",
    )
    with pytest.raises(ManagedLaunchConflict):
        managed_launch.reserve(replay)


# --------------------------------------------------------------------------
# Projection of stored rows
# --------------------------------------------------------------------------


def test_legacy_reservation_projects_as_acp_with_a_legacy_source(isolated_memory_db, tmp_path):
    """A pre-contract row reads as ACP and says so, and never as native."""
    request = _reserve_request(tmp_path)
    managed_launch.reserve(request)
    _rewrite_request(request.reservation_id, _drop_mode_keys)

    record = managed_launch.get(request.reservation_id)

    assert record["execution_mode"] == "acp"
    assert record["execution_mode_source"] == em.SOURCE_LEGACY
    assert record["is_legacy_execution_mode"] is True


def test_a_stored_request_with_a_corrupt_mode_fails_closed(isolated_memory_db, tmp_path):
    """A mode that no longer resolves refuses rather than defaulting to ACP.

    Absence means legacy ACP, but a *present* unresolvable value means
    the stored request cannot be trusted — corruption, or a request
    written by a newer binary and then rolled back.  Reading it as ACP
    would answer for a reservation that may have been accepted under a
    mode this binary cannot reconstruct.
    """
    request = _reserve_request(tmp_path)
    managed_launch.reserve(request)

    def _corrupt(stored):
        stored["execution_mode"] = "native"

    _rewrite_request(request.reservation_id, _corrupt)

    with pytest.raises(ManagedLaunchUnavailable):
        managed_launch.get(request.reservation_id)


def test_public_projection_never_omits_the_mode_keys(isolated_memory_db, tmp_path):
    """Every public read carries all three keys, so none needs inferring."""
    request = _reserve_request(tmp_path, execution_mode="acp")
    reserved, _ = managed_launch.reserve(request)
    fetched = managed_launch.get(request.reservation_id)
    launching, _ = managed_launch.claim_launch(request.reservation_id)

    for surface in (reserved, fetched, launching):
        assert surface["execution_mode"] == "acp"
        assert surface["execution_mode_source"] == em.SOURCE_LAUNCH
        assert surface["is_legacy_execution_mode"] is False


# --------------------------------------------------------------------------
# The support set is the contract
# --------------------------------------------------------------------------


def test_native_is_not_advertised_while_no_native_branch_exists(isolated_memory_db):
    """The support set names only modes with a real launch path.

    Nothing in this package starts, owns, or resumes a human-visible
    provider terminal, so native TUI is not runnable here and must not
    appear in the set a consumer gates on.
    """
    assert em.NATIVE_TUI not in managed_launch.SUPPORTED_EXECUTION_MODES
    assert managed_launch.SUPPORTED_EXECUTION_MODES == (em.ACP,)


def test_every_supported_mode_is_actually_reservable(isolated_memory_db, tmp_path):
    for mode in managed_launch.SUPPORTED_EXECUTION_MODES:
        record, _ = managed_launch.reserve(_reserve_request(tmp_path, execution_mode=mode))
        assert record["execution_mode"] == mode


def test_every_unsupported_mode_is_refused(isolated_memory_db, tmp_path):
    """The two sets partition the enum: named-and-runnable, or refused.

    Pinning both directions is what makes the advertisement meaningful —
    a mode is either advertised and accepted, or absent and refused, with
    no third category that is accepted but not honoured.
    """
    unsupported = set(em.EXECUTION_MODES) - set(managed_launch.SUPPORTED_EXECUTION_MODES)
    assert unsupported, "the partition test is vacuous if every mode is supported"

    for mode in sorted(unsupported):
        with pytest.raises(ManagedLaunchConflict):
            managed_launch.reserve(_reserve_request(tmp_path, execution_mode=mode))


def test_widening_the_support_set_admits_the_new_mode(isolated_memory_db, tmp_path, monkeypatch):
    """Reserve consults the support set rather than hardcoding a refusal.

    This is what makes adding a native launch branch a one-line change
    here instead of a hunt for scattered mode checks — and it proves the
    refusal above is driven by the set, not by a coincidental rejection
    of the string.
    """
    monkeypatch.setattr(managed_launch, "SUPPORTED_EXECUTION_MODES", (em.ACP, em.NATIVE_TUI))

    record, _ = managed_launch.reserve(_reserve_request(tmp_path, execution_mode="native_tui"))

    assert record["execution_mode"] == "native_tui"
    assert record["execution_mode_source"] == em.SOURCE_LAUNCH
