"""The v2 route-correct recovery surface.

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


@pytest.fixture(autouse=True)
def _stub_native_teardown(monkeypatch):
    """The managed cleanup now drives the generation-bound terminal teardown.

    This suite asserts the recovery state machine, not tmux process
    teardown, so the exact teardown is stubbed to a confirming no-op. The
    teardown contract itself is exercised in ``test_v2_cleanup_teardown.py``.
    """

    def _delete_terminal(
        terminal_id, *, registry=None, expected_generation=None, expected_session=None, **_
    ):
        return True

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.delete_terminal",
        _delete_terminal,
    )


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


def _emitted_preflight_reasons() -> dict[str, str]:
    """Every reason this module can actually write, read out of its source.

    Enumerated from the AST rather than from a hand-kept list, because a
    hand-kept list is exactly what drifted: two call sites passed codes
    that were never in the contract, and no test could notice because no
    test knew the call sites existed. Parsing the real file means a new
    emission added tomorrow is enumerated tomorrow, whether or not anyone
    remembers this suite.

    Returns ``{source location: reason value}`` so a failure names the
    line to fix instead of only the offending string.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(v2))
    emitted: dict[str, str] = {}

    def _resolve(node: ast.AST, where: str) -> None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            # A bare literal at a call site — lawful only if it happens to
            # spell a contract member, and recorded either way.
            emitted[where] = node.value
            return
        if isinstance(node, ast.Name):
            value = getattr(v2, node.id, None)
            assert isinstance(value, str), f"{where}: {node.id} is not a string constant"
            emitted[where] = value
            return
        raise AssertionError(
            f"{where}: a preflight reason must be a module constant or a literal, "
            f"so that it can be enumerated; got {type(node).__name__}"
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_mark_preflight_blocked":
            # The default is an emission too: every caller that omits the
            # keyword writes this value.
            for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
                if arg.arg == "reason" and default is not None:
                    _resolve(default, f"_mark_preflight_blocked default (line {node.lineno})")
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name != "_mark_preflight_blocked":
            continue
        for keyword in node.keywords:
            if keyword.arg == "reason":
                _resolve(keyword.value, f"call at line {node.lineno}")

    return emitted


# --------------------------------------------------------------------
# A. The evidence envelope
# --------------------------------------------------------------------


class TestPreflightReasonsAreTheClosedContractSet:
    """The reason codes are a closed enum, and every emission is in it.

    A recovering conductor is entitled to branch on ``reason``. An
    unrecognised code is therefore worse for it than a coarse one: there
    is no safe default branch for "a failure class I have never heard
    of", so it must treat the whole envelope as untrusted and cannot use
    the zero-byte proof the envelope exists to carry.
    """

    #: The contract's six, written out as literals on purpose. Importing
    #: the module's own set here would make this test agree with whatever
    #: the code currently says, which is the one thing it must not do.
    CONTRACT = {
        "provider-unsupported",
        "native-preflight",
        "session-bootstrap",
        "tui-launch-refused",
        "readiness-receipt",
        "native-generic",
    }

    def test_the_module_defines_exactly_the_contract_set(self):
        assert v2.PREFLIGHT_REASONS == self.CONTRACT

    def test_no_constant_holds_a_value_outside_the_contract(self):
        """Catches a stray code that is defined but not yet wired up.

        A constant is an invitation: the next failure site reaches for
        the name that reads well at that call site, and an out-of-contract
        string ships the moment one does.
        """
        stray = {
            name: getattr(v2, name)
            for name in dir(v2)
            if name.startswith("PREFLIGHT_REASON_") and getattr(v2, name) not in self.CONTRACT
        }
        assert stray == {}

    def test_every_emitted_reason_is_a_contract_member(self):
        emitted = _emitted_preflight_reasons()
        # The enumeration itself must have found something; an AST walk
        # that silently matched nothing would pass every assertion below.
        assert len(emitted) >= 6, f"only found {emitted}"
        offenders = {where: value for where, value in emitted.items() if value not in self.CONTRACT}
        assert offenders == {}

    def test_every_contract_member_is_actually_emitted(self):
        """No paper-only codes.

        A member nothing writes is either a deletion that stopped halfway
        or a failure class the launcher forgot to report, and both look
        the same from the contract.
        """
        assert set(_emitted_preflight_reasons().values()) == self.CONTRACT

    @pytest.mark.parametrize(
        "reason",
        sorted(
            {
                "provider-unsupported",
                "native-preflight",
                "session-bootstrap",
                "tui-launch-refused",
                "readiness-receipt",
                "native-generic",
            }
        ),
    )
    def test_each_reason_reaches_a_reader_verbatim(
        self, isolated_memory_db, worktree, tmp_path, reason
    ):
        """The stored envelope, as a recovery reads it back off the GET.

        Source agreement is not delivery: the value a conductor branches
        on is the one that survived being written, canonicalised into the
        evidence JSON and re-parsed, so that is what is asserted.
        """
        record = _reserve(worktree, tmp_path)
        v2._mark_preflight_blocked(record["reservation_id"], "cause", reason=reason)
        assert v2.get(record["reservation_id"])["preflight_failure"]["reason"] == reason

    def test_an_out_of_contract_reason_is_refused_at_the_write(
        self, isolated_memory_db, worktree, tmp_path
    ):
        """The enum is enforced where it is written, not only where it is read.

        The static enumeration above proves today's call sites are lawful,
        but it can only see literals and module constants in this file. The
        write itself is the last place an unlawful code can still be
        stopped: past it the record is immutable and terminal, so a
        recovering conductor inherits a value it has no branch for and no
        way to correct.
        """
        record = _reserve(worktree, tmp_path)

        with pytest.raises(ManagedLaunchUnavailable):
            v2._mark_preflight_blocked(record["reservation_id"], "cause", reason="launch-request")

        # Refused *before* anything moved: the generation is still
        # reserved, so a redrive can reach the right answer once the caller
        # is fixed. A guard that rejected the reason but left the row
        # blocked-without-evidence would be the worse of both outcomes.
        after = v2.get(record["reservation_id"])
        assert after["state"] == "reserved"
        assert after["preflight_failure"] is None

    def test_the_refusal_names_the_offending_code_and_the_lawful_set(
        self, isolated_memory_db, worktree, tmp_path
    ):
        """Whoever hits this is reading a traceback, not this test."""
        record = _reserve(worktree, tmp_path)

        with pytest.raises(ManagedLaunchUnavailable) as raised:
            v2._mark_preflight_blocked(record["reservation_id"], "cause", reason="provider-launch")

        message = str(raised.value)
        assert "provider-launch" in message
        assert "native-generic" in message

    def test_a_replay_cannot_smuggle_an_unlawful_reason_past_the_early_return(
        self, isolated_memory_db, worktree, tmp_path
    ):
        """The guard sits ahead of the already-blocked short circuit.

        That path returns the stored row without building an envelope, so
        a check placed at envelope construction would wave this through and
        report success for a code the contract does not contain.
        """
        record = _reserve(worktree, tmp_path)
        v2._mark_preflight_blocked(
            record["reservation_id"], "first cause", reason=v2.PREFLIGHT_REASON_NATIVE_PREFLIGHT
        )

        with pytest.raises(ManagedLaunchUnavailable):
            v2._mark_preflight_blocked(record["reservation_id"], "again", reason="launch-request")

        # The first cause still stands, untouched.
        assert (
            v2.get(record["reservation_id"])["preflight_failure"]["reason"]
            == v2.PREFLIGHT_REASON_NATIVE_PREFLIGHT
        )


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


class TestFinalizeNegativeFromBound:
    """A launch that dies after binding but before admitting.

    That row is bound, so the older rule refused to finalize it, and a
    generation is non-reusable once issued — leaving it neither finalizable nor
    replaceable, which wedges the run for good. Bind-before-admit is what
    makes the narrow exception provable: an admission journals its intent
    before it touches the transport, so a bound row carrying no admission
    has never reached the write path and no task byte can have crossed.

    These tests set the durable row shape directly rather than driving a
    real bind, because the property under test belongs to the recovery
    verb's state machine, not to how the row got there.
    """

    def _bound(self, worktree, tmp_path, *, admission=None, state="bound"):
        record = _reserve(worktree, tmp_path)
        with database.SessionLocal() as db:
            row = (
                db.query(database.ManagedLaunchV2ReservationModel)
                .filter(
                    database.ManagedLaunchV2ReservationModel.reservation_id
                    == record["reservation_id"]
                )
                .first()
            )
            row.state = state
            row.binding_json = '{"schema":"cao-native-binding-v1","provider":"kimi_cli"}'
            row.admission_json = admission
            db.commit()
        return record

    def test_finalizes_a_bound_generation_with_no_admission(
        self, isolated_memory_db, worktree, tmp_path
    ):
        record = self._bound(worktree, tmp_path)

        out = v2.finalize_negative(record["reservation_id"], _negative_request(record))

        assert out["state"] == "negative"
        assert out["admission"]["task_bytes_submitted"] is False
        # The finalization says which zero-byte state proved it, so an
        # auditor need not infer whether the launch died before or after
        # its binding.
        assert out["admission"]["finalized_from_state"] == "bound"

    def test_is_idempotent_from_bound(self, isolated_memory_db, worktree, tmp_path):
        record = self._bound(worktree, tmp_path)
        req = _negative_request(record)

        first = v2.finalize_negative(record["reservation_id"], req)
        again = v2.finalize_negative(record["reservation_id"], req)

        assert again["admission"] == first["admission"]

    def test_refuses_a_bound_row_that_carries_an_admission(
        self, isolated_memory_db, worktree, tmp_path
    ):
        """The row reached the write path, so zero bytes cannot be claimed.

        Asserted on the reason, not merely on the refusal: the write's own
        compare-and-set would also reject this row, but only after the verb
        had decided the finalization was lawful. The refusal has to be the
        decision, so that what the caller is told is "submission was
        attempted" rather than "a database write raced".
        """
        record = self._bound(
            worktree,
            tmp_path,
            admission='{"schema":"cao-kimi-native-control-v1","state":"AMBIGUOUS"}',
        )

        with pytest.raises(ManagedLaunchConflict, match="carries an admission record"):
            v2.finalize_negative(record["reservation_id"], _negative_request(record))

    @pytest.mark.parametrize("state", ["admitting", "admitted"])
    def test_refuses_every_admission_bearing_state(
        self, isolated_memory_db, worktree, tmp_path, state
    ):
        record = self._bound(worktree, tmp_path, state=state)

        with pytest.raises(ManagedLaunchConflict):
            v2.finalize_negative(record["reservation_id"], _negative_request(record))

    def test_a_refused_finalization_leaves_the_row_untouched(
        self, isolated_memory_db, worktree, tmp_path
    ):
        """The refusal is not a partial write: the run stays recoverable."""
        record = self._bound(worktree, tmp_path, state="admitted")

        with pytest.raises(ManagedLaunchConflict):
            v2.finalize_negative(record["reservation_id"], _negative_request(record))

        after = v2.get(record["reservation_id"])
        assert after["state"] == "admitted"
        assert after["admission"] is None


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

    def test_a_retry_replays_the_byte_identical_proof(self, isolated_memory_db, worktree, tmp_path):
        """Reconciled to §24.12, which names the old behaviour as a defect.

        This previously asserted that a retry reports
        ``terminal_record_removed: false`` -- truthful about *that* delete
        and misleading about the cleanup, and it meant the proof a consumer
        persisted depended on when it happened to ask. The removal is now
        attributed permanently to the call that performed it.

        A retry is the SAME ``cleanup_id``; a different one is a second
        cleanup, asserted just below.
        """
        record = self._finalized_with_terminal(worktree, tmp_path)
        request = _cleanup_request(record)

        first = v2.cleanup(record["reservation_id"], request)
        again = v2.cleanup(record["reservation_id"], request)

        assert again["cleanup"] == first["cleanup"]
        assert again["cleanup"]["terminal_record_removed"] is True

    def test_a_different_cleanup_id_is_a_conflict(self, isolated_memory_db, worktree, tmp_path):
        record = self._finalized_with_terminal(worktree, tmp_path)
        v2.cleanup(record["reservation_id"], _cleanup_request(record))

        with pytest.raises(ManagedLaunchConflict):
            v2.cleanup(record["reservation_id"], _cleanup_request(record))

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
