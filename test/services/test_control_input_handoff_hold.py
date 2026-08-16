"""M3-E: a pending handback suspends a stable agent's task authority (cond-0381).

The hold lives in ``provider_byte_admission`` because that is the narrowest
point every task-byte lane for a live managed pane already passes through:
typed control input, native inbox payloads, and operator messages. These tests
exercise the predicate at that seam rather than through tmux, so they prove the
gate itself rather than one caller's plumbing.
"""

from __future__ import annotations

import contextlib
import uuid

import pytest

from cli_agent_orchestrator.services import control_input_contract as contract
from cli_agent_orchestrator.services import control_input_service as cis
from cli_agent_orchestrator.services import task_handoff as th
from cli_agent_orchestrator.services import task_occurrence as occ

SESSION = "cao-m3e-hold"
_DIGEST_A = "a" * 64
_PACKET = "d" * 64
_OBSERVED_AT = "2026-08-16T12:00:00Z"


@pytest.fixture(autouse=True)
def _db(isolated_memory_db, monkeypatch, tmp_path):
    from cli_agent_orchestrator import constants

    monkeypatch.setattr(constants, "COMPANION_DIR", tmp_path / "companion")
    return isolated_memory_db


@pytest.fixture(autouse=True)
def _no_real_fence(monkeypatch):
    """The fence is not what is under test; its lock would need a real pane."""
    from cli_agent_orchestrator.services import generation_fence

    @contextlib.contextmanager
    def _open(*args, **kwargs):
        yield

    monkeypatch.setattr(generation_fence, "managed_admission_critical_section", _open)
    monkeypatch.setattr(generation_fence, "admission_critical_section", _open)


def _resolved(*, managed=True, reservation_id="res-1"):
    return cis.ResolvedControlIdentity(
        terminal_id="term-1",
        terminal_incarnation="inc-1",
        terminal_generation="gen-1",
        provider="claude_code",
        native_session_id="native-1",
        execution_mode="native_tui",
        session_name=SESSION,
        pane_id="%1",
        managed=managed,
        managed_reservation_id=reservation_id,
    )


def _bind_reservation(monkeypatch, stable_agent_id):
    from cli_agent_orchestrator.services import managed_launch_v2

    monkeypatch.setattr(
        managed_launch_v2,
        "get",
        lambda _rid: {
            "terminal_id": "term-1",
            "generation": "gen-1",
            "stable_agent_id": stable_agent_id,
            "binding": {"attempt_id": "att-1", "fencing_token_id": "tok-1"},
        },
    )


def _held_pair(monkeypatch):
    """A donor holding an open round, a dormant recipient, and a pending handoff."""
    donor_agent = str(uuid.uuid4())
    recipient_agent = str(uuid.uuid4())
    donor = occ.open_occurrence(
        occ.OpenRequest(
            task_occurrence_id=str(uuid.uuid4()),
            session_name=SESSION,
            agent_id=donor_agent,
            round_index=0,
            dispatch_digest=_DIGEST_A,
            incarnation=occ.EffectIncarnation(incarnation_id="inc-1", terminal_id="term-1"),
        )
    )
    handoff = th.begin_handoff(
        th.BeginRequest(
            handoff_id=str(uuid.uuid4()),
            session_name=SESSION,
            task_occurrence_id=donor["task_occurrence_id"],
            to_agent_id=recipient_agent,
            packet_digest=_PACKET,
            evidence=th.QuiescenceEvidence(
                incarnation_id="inc-1",
                terminal_id="term-1",
                turn_state=th.TURN_TERMINAL,
                observed_at=_OBSERVED_AT,
            ),
            initiated_by="supervisor",
        )
    )
    return donor_agent, recipient_agent, handoff


def _admit(monkeypatch, agent_id, *, control_id=None):
    _bind_reservation(monkeypatch, agent_id)
    with cis.provider_byte_admission(_resolved(), "term-1", "gen-1", control_id=control_id):
        return True


# ---------------------------------------------------------------------------
# the hold
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("side", [th.ROLE_DONOR, th.ROLE_RECIPIENT])
def test_a_held_agent_refuses_ordinary_task_bytes(monkeypatch, side):
    donor_agent, recipient_agent, _handoff = _held_pair(monkeypatch)
    agent_id = donor_agent if side == th.ROLE_DONOR else recipient_agent
    with pytest.raises(th.TaskHandoffHeld) as caught:
        _admit(monkeypatch, agent_id)
    assert side in str(caught.value)


@pytest.mark.parametrize("side", [th.ROLE_DONOR, th.ROLE_RECIPIENT])
def test_a_settled_handoff_restores_managed_input_on_both_sides(monkeypatch, side):
    donor_agent, recipient_agent, handoff = _held_pair(monkeypatch)
    agent_id = donor_agent if side == th.ROLE_DONOR else recipient_agent
    th.rollback_handoff(handoff["handoff_id"], rolled_back_by="supervisor", reason="quota returned")
    assert _admit(monkeypatch, agent_id) is True


def test_only_the_derived_packet_control_id_reaches_the_recipient(monkeypatch):
    donor_agent, recipient_agent, handoff = _held_pair(monkeypatch)
    packet = handoff["packet_control_id"]

    assert _admit(monkeypatch, recipient_agent, control_id=packet) is True
    with pytest.raises(th.TaskHandoffHeld):
        _admit(monkeypatch, recipient_agent, control_id=str(uuid.uuid4()))
    # The packet is not for the donor, so it is not the donor's exemption.
    with pytest.raises(th.TaskHandoffHeld):
        _admit(monkeypatch, donor_agent, control_id=packet)


def test_an_agent_with_no_pending_handoff_is_never_held(monkeypatch):
    _held_pair(monkeypatch)
    assert _admit(monkeypatch, str(uuid.uuid4())) is True


def test_a_reservation_without_a_stable_agent_id_is_not_held(monkeypatch):
    # A reservation that predates the M3-A roster cannot be party to a
    # handback, and must not be refused as though it were.
    _held_pair(monkeypatch)
    assert _admit(monkeypatch, None) is True


def test_unmanaged_and_legacy_managed_writers_are_unaffected(monkeypatch):
    donor_agent, _recipient, _handoff = _held_pair(monkeypatch)
    _bind_reservation(monkeypatch, donor_agent)
    with cis.provider_byte_admission(_resolved(managed=False), "term-1", "gen-1"):
        pass
    with cis.provider_byte_admission(_resolved(reservation_id=None), "term-1", "gen-1"):
        pass


# ---------------------------------------------------------------------------
# the refusal a caller sees
# ---------------------------------------------------------------------------


def test_a_hold_is_reported_as_its_own_reason_not_as_a_generation_fence():
    # A fence is permanent and tells the caller to advance to a successor. A
    # caller that read this hold as a fence would abandon the pane the handoff
    # is keeping alive as rollback insurance.
    from cli_agent_orchestrator.services import generation_fence

    assert cis._admission_refusal_reason(th.TaskHandoffHeld("held")) == contract.REASON_HANDOFF_HELD
    assert (
        cis._admission_refusal_reason(generation_fence.FencedError("fenced"))
        == contract.REASON_GENERATION_FENCED
    )
    assert contract.REASON_HANDOFF_HELD != contract.REASON_GENERATION_FENCED


def test_the_hold_refusal_is_decided_before_any_byte_so_it_is_reattemptable():
    assert contract.outcome_for_reason(contract.REASON_HANDOFF_HELD) == contract.REFUSED
    assert contract.REASON_HANDOFF_HELD in contract.CONTROL_INPUT_REASON_CODES
    assert contract.REFUSED in contract.REATTEMPTABLE_OUTCOMES


def test_an_unreadable_handoff_store_holds_rather_than_admitting(monkeypatch):
    # The store failing is the anomaly, not the input. Admitting bytes the hold
    # exists to refuse is the one outcome this seam must never produce, and the
    # neighbouring reservation read already refuses on the same grounds.
    def _boom(*_args, **_kwargs):
        raise th.TaskHandoffUnavailable("store is gone")

    monkeypatch.setattr(th, "hold_for_agent", _boom)
    assert "could not be read" in th.hold_refusal(str(uuid.uuid4()))
