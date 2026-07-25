"""The v2 route-correct recovery surface (cond-0107).

A v2 native preflight failure used to be a dead end: the reservation row
carried ``state == preflight_blocked`` with its *cause discarded*, and no
v2 verb existed to finalize it — so ``conduct spawn --recover`` reached
for the v1 ``negative``/``reconcile``/``cleanup`` verbs, received 404, and
left the run and its breaker wedged.

This suite proves the two halves of the fix on the isolated v2 store:

1.  the immutable, redacted, GET-queryable preflight-failure evidence
    envelope (reason/detail, exact reservation/terminal/generation
    identity, timestamp, ``task_bytes_submitted: false``); and
2.  the idempotent, re-drivable ``negative``/``reconcile``/``cleanup``
    verbs that finalize a proven zero-byte failure and release its
    terminal record with zero task/provider I/O, and that refuse to reuse
    the blocked generation.

Every test drives the real service functions against a real (in-memory)
v2 store; nothing here mocks the store it is asserting about.
"""

from __future__ import annotations

import hashlib
import subprocess
import uuid

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.managed_launch_v2 import (
    PROTOCOL_VERSION_V2,
    ManagedLaunchV2BindRequest,
    ManagedLaunchV2CleanupRequest,
    ManagedLaunchV2NegativeRequest,
    ManagedLaunchV2ReserveRequest,
)
from cli_agent_orchestrator.services import managed_launch_v2 as v2
from cli_agent_orchestrator.services.managed_launch import (
    ManagedLaunchConflict,
    ManagedLaunchNotFound,
    ManagedLaunchUnavailable,
)

PREFLIGHT_SCHEMA = "cao-managed-launch-v2-preflight-failure-v1"


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


def _reserve(worktree, tmp_path, **changes) -> dict:
    executable = tmp_path / "fake-provider"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    payload = {
        "protocol_version": PROTOCOL_VERSION_V2,
        "reservation_id": str(uuid.uuid4()),
        "session_name": "cao-test",
        "provider": "kimi_cli",
        "agent_profile": "reviewer",
        "caller_id": "deadbeef",
        "working_directory": str(worktree),
        # trusted_project_root is codex-only; a native kimi reservation omits it.
        "expected_model": "gpt-5.6-sol",
        "expected_effort": "xhigh",
        "provider_executable": str(executable),
        "provider_executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "obligation_generation": "obgen-7c2e4a1b",
        "task_id": "self-heal-demo-task",
        "run_id": "run-0001",
        "delivery_id": str(uuid.uuid4()),
        "launch_nonce": "n" * 40,
        "execution_mode": "native_tui",
        "worker_class": "persistent",
    }
    payload.update(changes)
    record, _created = v2.reserve(ManagedLaunchV2ReserveRequest(**payload))
    return record


def _negative_request(record, **changes) -> ManagedLaunchV2NegativeRequest:
    payload = {
        "protocol_version": PROTOCOL_VERSION_V2,
        "finalize_id": str(uuid.uuid4()),
        "terminal_id": record["terminal_id"],
        "generation": record["generation"],
        "obligation_generation": record["obligation_generation"],
        "reason": "conduct recover: proven zero-byte native preflight failure",
    }
    payload.update(changes)
    return ManagedLaunchV2NegativeRequest(**payload)


def _cleanup_request(record, **changes) -> ManagedLaunchV2CleanupRequest:
    payload = {
        "protocol_version": PROTOCOL_VERSION_V2,
        "cleanup_id": str(uuid.uuid4()),
        "terminal_id": record["terminal_id"],
        "generation": record["generation"],
    }
    payload.update(changes)
    return ManagedLaunchV2CleanupRequest(**payload)


# --------------------------------------------------------------------
# A. The evidence envelope
# --------------------------------------------------------------------


class TestPreflightFailureEnvelope:
    def test_blocked_generation_records_identity_bound_zero_byte_evidence(
        self, isolated_memory_db, worktree, tmp_path
    ):
        record = _reserve(worktree, tmp_path)
        blocked = v2._mark_preflight_blocked(
            record["reservation_id"],
            "native session bootstrap failed: could not mint",
            reason=v2.PREFLIGHT_REASON_SESSION_BOOTSTRAP,
        )
        assert blocked["state"] == "preflight_blocked"
        env = blocked["preflight_failure"]
        assert env["schema"] == PREFLIGHT_SCHEMA
        assert env["reservation_id"] == record["reservation_id"]
        assert env["terminal_id"] == record["terminal_id"]
        assert env["generation"] == record["generation"]
        assert env["obligation_generation"] == record["obligation_generation"]
        assert env["provider"] == "kimi_cli"
        assert env["reason"] == v2.PREFLIGHT_REASON_SESSION_BOOTSTRAP
        assert env["task_bytes_submitted"] is False
        assert env["failed_at"].endswith("Z")

    def test_the_envelope_is_returned_verbatim_on_get(self, isolated_memory_db, worktree, tmp_path):
        record = _reserve(worktree, tmp_path)
        v2._mark_preflight_blocked(
            record["reservation_id"], "boom", reason=v2.PREFLIGHT_REASON_TUI_LAUNCH_REFUSED
        )
        fetched = v2.get(record["reservation_id"])
        assert fetched["preflight_failure"]["reason"] == v2.PREFLIGHT_REASON_TUI_LAUNCH_REFUSED
        assert fetched["preflight_failure"]["task_bytes_submitted"] is False

    def test_a_row_that_never_blocked_has_no_envelope(self, isolated_memory_db, worktree, tmp_path):
        record = _reserve(worktree, tmp_path)
        assert v2.get(record["reservation_id"])["preflight_failure"] is None

    def test_detail_is_credential_redacted(self, isolated_memory_db, worktree, tmp_path):
        record = _reserve(worktree, tmp_path)
        leak = "startup failed: Authorization: Bearer sk-abcdefghijklmnopqrstuvwxyz012345"
        blocked = v2._mark_preflight_blocked(
            record["reservation_id"], leak, reason=v2.PREFLIGHT_REASON_NATIVE_PREFLIGHT
        )
        env = blocked["preflight_failure"]
        assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in env["detail"]
        assert "[REDACTED:bearer_token]" in env["detail"]
        assert "bearer_token" in env["detail_redactions"]

    def test_evidence_is_immutable_across_a_differently_failing_redrive(
        self, isolated_memory_db, worktree, tmp_path
    ):
        record = _reserve(worktree, tmp_path)
        first = v2._mark_preflight_blocked(
            record["reservation_id"], "first cause", reason=v2.PREFLIGHT_REASON_SESSION_BOOTSTRAP
        )
        again = v2._mark_preflight_blocked(
            record["reservation_id"],
            "a completely different second cause",
            reason=v2.PREFLIGHT_REASON_TUI_LAUNCH_REFUSED,
        )
        # The first recorded cause stands: a recovery may already have read it.
        assert again["preflight_failure"] == first["preflight_failure"]
        assert again["preflight_failure"]["detail"] == "first cause"
        assert again["preflight_failure"]["reason"] == v2.PREFLIGHT_REASON_SESSION_BOOTSTRAP

    def test_storage_failure_fails_closed_and_writes_no_blocked_row(
        self, isolated_memory_db, worktree, tmp_path, monkeypatch
    ):
        record = _reserve(worktree, tmp_path)

        def _boom(_content):
            raise RuntimeError("redaction backend unavailable")

        monkeypatch.setattr(v2.secret_gate, "redact_secrets", _boom)
        with pytest.raises(ManagedLaunchUnavailable):
            v2._mark_preflight_blocked(
                record["reservation_id"], "detail", reason=v2.PREFLIGHT_REASON_GENERIC
            )
        # No half-written state: the row is neither blocked nor evidence-less.
        after = v2.get(record["reservation_id"])
        assert after["state"] == "reserved"
        assert after["preflight_failure"] is None


# --------------------------------------------------------------------
# B. finalize_negative
# --------------------------------------------------------------------


class TestFinalizeNegative:
    def _blocked(self, worktree, tmp_path):
        record = _reserve(worktree, tmp_path)
        v2._mark_preflight_blocked(
            record["reservation_id"],
            "native session bootstrap failed",
            reason=v2.PREFLIGHT_REASON_SESSION_BOOTSTRAP,
        )
        return record

    def test_finalizes_a_blocked_generation_to_negative(
        self, isolated_memory_db, worktree, tmp_path
    ):
        record = self._blocked(worktree, tmp_path)
        out = v2.finalize_negative(record["reservation_id"], _negative_request(record))
        assert out["state"] == "negative"
        assert out["admission"]["schema"] == "cao-managed-launch-v2-negative-v1"
        assert out["admission"]["task_bytes_submitted"] is False
        # The preflight evidence is preserved through finalization.
        assert out["preflight_failure"]["reason"] == v2.PREFLIGHT_REASON_SESSION_BOOTSTRAP

    def test_is_idempotent_on_replay(self, isolated_memory_db, worktree, tmp_path):
        record = self._blocked(worktree, tmp_path)
        req = _negative_request(record)
        first = v2.finalize_negative(record["reservation_id"], req)
        again = v2.finalize_negative(record["reservation_id"], req)
        assert again["state"] == "negative"
        assert again["admission"] == first["admission"]

    def test_a_second_finalize_id_does_not_rewrite_the_finalization(
        self, isolated_memory_db, worktree, tmp_path
    ):
        record = self._blocked(worktree, tmp_path)
        first = v2.finalize_negative(record["reservation_id"], _negative_request(record))
        # A different finalize_id against an already-negative row is an
        # idempotent success that does not alter the recorded finalization.
        second = v2.finalize_negative(record["reservation_id"], _negative_request(record))
        assert second["admission"]["finalize_id"] == first["admission"]["finalize_id"]

    def test_reason_is_credential_redacted(self, isolated_memory_db, worktree, tmp_path):
        record = self._blocked(worktree, tmp_path)
        out = v2.finalize_negative(
            record["reservation_id"],
            _negative_request(record, reason="recover token=sk-abcdefghijklmnop0123456789"),
        )
        assert "sk-abcdefghijklmnop0123456789" not in out["admission"]["reason"]

    def test_refuses_wrong_identity(self, isolated_memory_db, worktree, tmp_path):
        record = self._blocked(worktree, tmp_path)
        with pytest.raises(ManagedLaunchConflict):
            v2.finalize_negative(
                record["reservation_id"], _negative_request(record, generation=str(uuid.uuid4()))
            )

    def test_refuses_wrong_obligation_generation(self, isolated_memory_db, worktree, tmp_path):
        record = self._blocked(worktree, tmp_path)
        with pytest.raises(ManagedLaunchConflict):
            v2.finalize_negative(
                record["reservation_id"], _negative_request(record, obligation_generation="other")
            )

    def test_refuses_a_generation_that_never_blocked(self, isolated_memory_db, worktree, tmp_path):
        record = _reserve(worktree, tmp_path)  # still 'reserved'
        with pytest.raises(ManagedLaunchConflict):
            v2.finalize_negative(record["reservation_id"], _negative_request(record))

    def test_unknown_reservation_is_not_found(self, isolated_memory_db, worktree, tmp_path):
        record = self._blocked(worktree, tmp_path)
        with pytest.raises(ManagedLaunchNotFound):
            v2.finalize_negative(str(uuid.uuid4()), _negative_request(record))


# --------------------------------------------------------------------
# C. reconcile
# --------------------------------------------------------------------


class TestReconcile:
    def test_reports_facts_read_only_and_is_repeatable(
        self, isolated_memory_db, worktree, tmp_path
    ):
        record = _reserve(worktree, tmp_path)
        v2._mark_preflight_blocked(
            record["reservation_id"], "boom", reason=v2.PREFLIGHT_REASON_NATIVE_PREFLIGHT
        )
        first = v2.reconcile(record["reservation_id"])
        assert first["recovery_only"] is True
        assert first["terminal_record_present"] is False
        assert first["preflight_failure"]["reason"] == v2.PREFLIGHT_REASON_NATIVE_PREFLIGHT
        # Read-only: the state is unchanged and a second call is identical.
        second = v2.reconcile(record["reservation_id"])
        assert second["state"] == "preflight_blocked"
        assert second["terminal_record_present"] is False

    def test_sees_a_present_terminal_record(self, isolated_memory_db, worktree, tmp_path):
        record = _reserve(worktree, tmp_path)
        v2._mark_preflight_blocked(
            record["reservation_id"], "boom", reason=v2.PREFLIGHT_REASON_GENERIC
        )
        database.create_terminal_v2(
            terminal_id=record["terminal_id"],
            tmux_session="cao-test",
            tmux_window="w",
            provider="kimi_cli",
            generation=record["generation"],
        )
        assert v2.reconcile(record["reservation_id"])["terminal_record_present"] is True

    def test_a_freshly_reserved_row_is_not_recovery_only(
        self, isolated_memory_db, worktree, tmp_path
    ):
        record = _reserve(worktree, tmp_path)
        assert v2.reconcile(record["reservation_id"])["recovery_only"] is False


# --------------------------------------------------------------------
# D. cleanup
# --------------------------------------------------------------------


class TestCleanup:
    def _finalized_with_terminal(self, worktree, tmp_path):
        record = _reserve(worktree, tmp_path)
        v2._mark_preflight_blocked(
            record["reservation_id"], "boom", reason=v2.PREFLIGHT_REASON_SESSION_BOOTSTRAP
        )
        database.create_terminal_v2(
            terminal_id=record["terminal_id"],
            tmux_session="cao-test",
            tmux_window="w",
            provider="kimi_cli",
            generation=record["generation"],
        )
        v2.finalize_negative(record["reservation_id"], _negative_request(record))
        return record

    def test_releases_the_terminal_record(self, isolated_memory_db, worktree, tmp_path):
        record = self._finalized_with_terminal(worktree, tmp_path)
        out = v2.cleanup(record["reservation_id"], _cleanup_request(record))
        assert out["cleanup"]["terminal_record_removed"] is True
        assert v2.reconcile(record["reservation_id"])["terminal_record_present"] is False

    def test_is_idempotent_when_the_record_is_already_gone(
        self, isolated_memory_db, worktree, tmp_path
    ):
        record = self._finalized_with_terminal(worktree, tmp_path)
        v2.cleanup(record["reservation_id"], _cleanup_request(record))
        again = v2.cleanup(record["reservation_id"], _cleanup_request(record))
        assert again["cleanup"]["terminal_record_removed"] is False

    def test_refused_before_finalization(self, isolated_memory_db, worktree, tmp_path):
        record = _reserve(worktree, tmp_path)
        v2._mark_preflight_blocked(
            record["reservation_id"], "boom", reason=v2.PREFLIGHT_REASON_GENERIC
        )
        with pytest.raises(ManagedLaunchConflict):
            v2.cleanup(record["reservation_id"], _cleanup_request(record))

    def test_refuses_wrong_identity(self, isolated_memory_db, worktree, tmp_path):
        record = self._finalized_with_terminal(worktree, tmp_path)
        with pytest.raises(ManagedLaunchConflict):
            v2.cleanup(
                record["reservation_id"], _cleanup_request(record, generation=str(uuid.uuid4()))
            )


# --------------------------------------------------------------------
# E. The blocked generation is finalized and replaced, never reused
# --------------------------------------------------------------------


class TestNoGenerationReuse:
    def test_a_finalized_generation_cannot_be_bound_or_launched(
        self, isolated_memory_db, worktree, tmp_path
    ):
        record = _reserve(worktree, tmp_path)
        v2._mark_preflight_blocked(
            record["reservation_id"], "boom", reason=v2.PREFLIGHT_REASON_SESSION_BOOTSTRAP
        )
        v2.finalize_negative(record["reservation_id"], _negative_request(record))
        # Bind requires 'launching'; a finalized generation is refused.
        with pytest.raises(ManagedLaunchConflict):
            v2.bind_native(
                record["reservation_id"],
                ManagedLaunchV2BindRequest(
                    protocol_version=PROTOCOL_VERSION_V2,
                    terminal_id=record["terminal_id"],
                    generation=record["generation"],
                    attempt_id=str(uuid.uuid4()),
                ),
            )
        # Launch cannot re-claim it either — it is not 'reserved'.
        with pytest.raises((ManagedLaunchConflict, ManagedLaunchUnavailable)):
            v2.claim_launch(record["reservation_id"])
