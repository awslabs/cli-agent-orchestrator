"""M3-E reversible A -> B -> A worker handback (cond-0381)."""

from __future__ import annotations

import uuid

import pytest

from cli_agent_orchestrator.services import task_handoff as th
from cli_agent_orchestrator.services import task_occurrence as occ

SESSION = "cao-m3e-a"
_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64
_DIGEST_C = "c" * 64
_PACKET = "d" * 64


@pytest.fixture(autouse=True)
def _db(isolated_memory_db, monkeypatch, tmp_path):
    from cli_agent_orchestrator import constants

    monkeypatch.setattr(constants, "COMPANION_DIR", tmp_path / "companion")
    return isolated_memory_db


def _incarnation(suffix="1", generation=None):
    return occ.EffectIncarnation(
        incarnation_id=f"inc-{suffix}",
        terminal_id=f"term-{suffix}",
        generation=generation or f"gen-{suffix}",
        lineage_id=f"lin-{suffix}",
        native_session_id=f"native-{suffix}",
    )


def _complete_seed():
    return occ.TaskSeed(
        occ.SEED_COMPLETE,
        summary_digest=_DIGEST_B,
        artifacts=(
            occ.ArtifactReference(
                artifact_id="notes",
                kind="markdown",
                reference="/tmp/notes.md",
                content_digest=_DIGEST_C,
            ),
        ),
        produced_by="supervisor",
    )


def _open(agent_id, *, round_index=0, occurrence_id=None, suffix="1", seed=None):
    return occ.open_occurrence(
        occ.OpenRequest(
            task_occurrence_id=occurrence_id or str(uuid.uuid4()),
            session_name=SESSION,
            agent_id=agent_id,
            round_index=round_index,
            dispatch_digest=_DIGEST_A,
            incarnation=_incarnation(suffix),
            seed=seed or occ.EMPTY_SEED,
        )
    )


_OBSERVED_AT = "2026-08-16T12:00:00Z"


def _evidence(suffix="1", *, turn_state=th.TURN_TERMINAL, observed_at=_OBSERVED_AT, **kwargs):
    return th.QuiescenceEvidence(
        incarnation_id=f"inc-{suffix}",
        terminal_id=f"term-{suffix}",
        generation=f"gen-{suffix}",
        turn_state=turn_state,
        observed_at=observed_at,
        **kwargs,
    )


def _begin(donor_occurrence, to_agent_id, *, handoff_id=None, suffix="1", evidence=None):
    return th.begin_handoff(
        th.BeginRequest(
            handoff_id=handoff_id or str(uuid.uuid4()),
            session_name=SESSION,
            task_occurrence_id=donor_occurrence["task_occurrence_id"],
            to_agent_id=to_agent_id,
            packet_digest=_PACKET,
            evidence=evidence or _evidence(suffix),
            initiated_by="supervisor",
        )
    )


def _pair(*, donor_seed=None):
    """A donor holding an open round, and a dormant recipient."""
    donor_agent = str(uuid.uuid4())
    recipient_agent = str(uuid.uuid4())
    donor = _open(donor_agent, suffix="1", seed=donor_seed)
    return donor_agent, recipient_agent, donor


def _deliver(handoff):
    return th.record_packet_delivery(
        handoff["handoff_id"],
        delivered=True,
        outcome="accepted",
        receipt="receipt-1",
        expected_epoch=handoff["epoch"],
    )


def _occurrence_row(task_occurrence_id):
    """Every column of the occurrence, flattened, for byte-equality snapshots."""
    record = occ.get_occurrence(task_occurrence_id)
    record.pop("extensions", None)
    record.pop("seed_verdict", None)
    return record


# ---------------------------------------------------------------------------
# a pending handoff changes nothing about the task
# ---------------------------------------------------------------------------


def test_a_pending_handoff_never_changes_the_occurrence_row():
    # This is the rollback-correctness proof. Restoring the donor is only free
    # because entering the held window wrote nothing to the task.
    donor_agent, recipient_agent, donor = _pair()
    before = _occurrence_row(donor["task_occurrence_id"])

    _begin(donor, recipient_agent)

    assert _occurrence_row(donor["task_occurrence_id"]) == before
    assert before["state"] == occ.STATE_OPEN
    assert before["revision"] == 0


def test_a_held_donor_still_reads_as_the_open_authority_for_its_agent():
    # The named failure a third occurrence state would cause: the drain's
    # observer reads "no open occurrence" as positive parked, and under Stop
    # tears down the pane that is the rollback insurance.
    donor_agent, recipient_agent, donor = _pair()
    _begin(donor, recipient_agent)

    live = occ.open_occurrence_for_agent(donor_agent)
    assert live is not None
    assert live["task_occurrence_id"] == donor["task_occurrence_id"]
    assert live["state"] == occ.STATE_OPEN

    history = occ.occurrence_history(SESSION, donor_agent)
    assert history["open"]["task_occurrence_id"] == donor["task_occurrence_id"]
    assert history["finalized"] == []


def test_a_held_donor_may_still_record_a_boundary():
    # An in-flight turn finishing during the held window is ordinary. A design
    # that treated it as drift would convert a recoverable handoff into an
    # unrecoverable one.
    donor_agent, recipient_agent, donor = _pair()
    _begin(donor, recipient_agent)

    occ.record_boundary(
        occ.BoundaryRecord(
            task_occurrence_id=donor["task_occurrence_id"],
            expected_revision=0,
            recorded_by="worker",
            report_digest=_DIGEST_B,
        )
    )
    assert occ.get_occurrence(donor["task_occurrence_id"])["revision"] == 1


# ---------------------------------------------------------------------------
# exactly one task authority at every boundary
# ---------------------------------------------------------------------------


def test_only_one_pending_handoff_exists_per_occurrence():
    donor_agent, recipient_agent, donor = _pair()
    _begin(donor, recipient_agent)
    with pytest.raises(th.TaskHandoffConflict, match="already the donor of pending handoff"):
        _begin(donor, str(uuid.uuid4()))


def test_a_donor_is_party_to_one_handoff_at_a_time():
    donor_agent, recipient_agent, donor = _pair()
    _begin(donor, recipient_agent)
    # A second occurrence for the same donor cannot exist (one open per agent),
    # so the donor slot is proven through the typed refusal above; the third
    # index is what a *recipient* collision hits.
    other_donor = str(uuid.uuid4())
    other = _open(other_donor, suffix="9")
    with pytest.raises(th.TaskHandoffConflict, match="already the recipient of pending handoff"):
        th.begin_handoff(
            th.BeginRequest(
                handoff_id=str(uuid.uuid4()),
                session_name=SESSION,
                task_occurrence_id=other["task_occurrence_id"],
                to_agent_id=recipient_agent,
                packet_digest=_PACKET,
                evidence=_evidence("9"),
                initiated_by="supervisor",
            )
        )


def test_a_settled_handoff_frees_both_agents_to_hand_back_again():
    donor_agent, recipient_agent, donor = _pair()
    first = _begin(donor, recipient_agent)
    th.rollback_handoff(first["handoff_id"], rolled_back_by="supervisor", reason="quota returned")
    again = _begin(donor, recipient_agent)
    assert again["state"] == th.STATE_PENDING
    assert again["handoff_id"] != first["handoff_id"]


def test_a_handoff_needs_two_different_agents():
    donor_agent, _recipient, donor = _pair()
    with pytest.raises(th.TaskHandoffInvalid, match="needs two different agents"):
        _begin(donor, donor_agent)


def test_a_finalized_occurrence_is_never_handed_back():
    donor_agent, recipient_agent, donor = _pair()
    occ.finalize_occurrence(
        occ.FinalizeRequest(
            task_occurrence_id=donor["task_occurrence_id"],
            expected_revision=0,
            disposition=occ.DISPOSITION_REPORTED,
            finalized_by="supervisor",
        )
    )
    with pytest.raises(th.TaskHandoffConflict, match="only an open occurrence"):
        _begin(donor, recipient_agent)


# ---------------------------------------------------------------------------
# quiescence evidence
# ---------------------------------------------------------------------------


def test_a_donor_with_an_active_managed_turn_is_refused():
    donor_agent, recipient_agent, donor = _pair()
    with pytest.raises(th.TaskHandoffConflict, match="active managed turn"):
        _begin(donor, recipient_agent, evidence=_evidence("1", turn_state=th.TURN_ACTIVE))


def test_an_unproven_turn_is_accepted_and_recorded_as_unproven():
    # Not every installed provider runs a heartbeat producer. Refusing an
    # unproven turn would forbid handback on exactly the routes that need it;
    # recording it keeps the receipt from reading as proof.
    donor_agent, recipient_agent, donor = _pair()
    handoff = _begin(donor, recipient_agent, evidence=_evidence("1", turn_state=th.TURN_UNKNOWN))
    assert handoff["quiescence"]["turn_state"] == th.TURN_UNKNOWN
    assert handoff["quiescence"]["turn_proven"] is False
    proven = th.QuiescenceEvidence(
        incarnation_id="inc-1",
        terminal_id="term-1",
        turn_state=th.TURN_TERMINAL,
        observed_at=_OBSERVED_AT,
    )
    assert proven.turn_proven is True


def test_evidence_must_name_the_incarnation_that_is_executing_the_occurrence():
    donor_agent, recipient_agent, donor = _pair()
    with pytest.raises(th.TaskHandoffConflict, match="quiescence evidence names incarnation"):
        _begin(donor, recipient_agent, evidence=_evidence("7"))


def test_child_effects_are_recorded_as_untracked_rather_than_claimed_absent():
    # Nothing in this system tracks worker-spawned processes, and the
    # containment surface that would prove their absence fails closed forever.
    donor_agent, recipient_agent, donor = _pair()
    handoff = _begin(donor, recipient_agent)
    assert handoff["quiescence"]["known_child_effects"] == th.CHILD_EFFECTS_NONE_TRACKED
    with pytest.raises(th.TaskHandoffInvalid, match="known_child_effects"):
        th.QuiescenceEvidence(
            incarnation_id="inc-1",
            terminal_id="term-1",
            turn_state=th.TURN_TERMINAL,
            observed_at=_OBSERVED_AT,
            known_child_effects="proven-none",
        )


# ---------------------------------------------------------------------------
# the packet, delivered exactly once
# ---------------------------------------------------------------------------


def test_the_packet_control_id_is_derived_and_stable():
    handoff_id = str(uuid.uuid4())
    agent_id = str(uuid.uuid4())
    first = th.packet_control_id_for(handoff_id, agent_id)
    assert first == th.packet_control_id_for(handoff_id, agent_id)
    assert first != th.packet_control_id_for(handoff_id, str(uuid.uuid4()))
    assert first != th.packet_control_id_for(str(uuid.uuid4()), agent_id)


def test_a_delivered_packet_is_never_delivered_twice():
    donor_agent, recipient_agent, donor = _pair()
    handoff = _begin(donor, recipient_agent)
    delivered = _deliver(handoff)
    assert delivered["delivery_state"] == th.DELIVERY_DELIVERED
    with pytest.raises(th.TaskHandoffConflict, match="delivered exactly once"):
        th.record_packet_delivery(
            handoff["handoff_id"],
            delivered=True,
            outcome="different",
            expected_epoch=delivered["epoch"],
        )


def test_an_identical_delivery_record_adopts_after_a_lost_response():
    donor_agent, recipient_agent, donor = _pair()
    handoff = _begin(donor, recipient_agent)
    first = _deliver(handoff)
    replay = th.record_packet_delivery(
        handoff["handoff_id"],
        delivered=True,
        outcome="accepted",
        receipt="receipt-1",
        expected_epoch=handoff["epoch"],
    )
    assert replay["adopted"] is True
    assert replay["epoch"] == first["epoch"]


def test_a_refused_delivery_leaves_the_handoff_pending_and_rollbackable():
    donor_agent, recipient_agent, donor = _pair()
    handoff = _begin(donor, recipient_agent)
    refused = th.record_packet_delivery(
        handoff["handoff_id"], delivered=False, outcome="pane-busy", expected_epoch=handoff["epoch"]
    )
    assert refused["state"] == th.STATE_PENDING
    assert refused["delivery_state"] == th.DELIVERY_REFUSED
    rolled = th.rollback_handoff(
        handoff["handoff_id"], rolled_back_by="supervisor", reason="packet refused"
    )
    assert rolled["donor_restored"] is True


def test_a_transfer_without_a_delivered_packet_is_refused():
    donor_agent, recipient_agent, donor = _pair()
    handoff = _begin(donor, recipient_agent)
    with pytest.raises(th.TaskHandoffConflict, match="has not delivered its catch-up packet"):
        th.complete_handoff(
            handoff["handoff_id"],
            incarnation=_incarnation("2"),
            expected_revision=0,
            completed_by="supervisor",
        )


# ---------------------------------------------------------------------------
# transferring
# ---------------------------------------------------------------------------


def test_completing_a_handoff_finalizes_the_donor_and_opens_the_recipient():
    donor_agent, recipient_agent, donor = _pair(donor_seed=_complete_seed())
    handoff = _deliver(_begin(donor, recipient_agent))
    result = th.complete_handoff(
        handoff["handoff_id"],
        incarnation=_incarnation("2"),
        expected_revision=0,
        completed_by="supervisor",
    )

    closed = occ.get_occurrence(donor["task_occurrence_id"])
    assert closed["state"] == occ.STATE_FINALIZED
    assert closed["finalized"]["disposition"] == occ.DISPOSITION_SUPERSEDED
    assert occ.open_occurrence_for_agent(donor_agent) is None

    successor = result["successor_occurrence"]
    assert successor["agent_id"] == recipient_agent
    assert successor["state"] == occ.STATE_OPEN
    assert successor["incarnation_id"] == "inc-2"
    assert occ.open_occurrence_for_agent(recipient_agent)["task_occurrence_id"] == (
        successor["task_occurrence_id"]
    )
    assert result["state"] == th.STATE_TRANSFERRED
    assert result["receipt_digest"]


def test_the_recipient_occurrence_names_its_predecessor_in_dispatch_provenance():
    # The only link a later reconstruction -- the auditor, or the goal track's
    # own re-assignment -- has back to the round this one continues.
    donor_agent, recipient_agent, donor = _pair()
    handoff = _deliver(_begin(donor, recipient_agent))
    result = th.complete_handoff(
        handoff["handoff_id"],
        incarnation=_incarnation("2"),
        expected_revision=0,
        completed_by="supervisor",
    )
    provenance = result["successor_occurrence"]["dispatch_provenance"]
    assert provenance["kind"] == "m3e-handback"
    assert provenance["predecessor_occurrence_id"] == donor["task_occurrence_id"]
    assert provenance["predecessor_agent_id"] == donor_agent
    assert provenance["handoff_id"] == handoff["handoff_id"]
    assert provenance["packet_digest"] == _PACKET


def test_the_recipients_round_index_is_its_own_next_round_not_the_donors():
    # ix_task_occurrences_round is UNIQUE and not partial, so a finalized round
    # of the recipient's own past occupies the slot just as much as a live one.
    donor_agent, recipient_agent, donor = _pair()
    earlier = _open(recipient_agent, round_index=0, suffix="3")
    occ.finalize_occurrence(
        occ.FinalizeRequest(
            task_occurrence_id=earlier["task_occurrence_id"],
            expected_revision=0,
            disposition=occ.DISPOSITION_REPORTED,
            finalized_by="supervisor",
        )
    )
    assert donor["round_index"] == 0

    handoff = _deliver(_begin(donor, recipient_agent))
    result = th.complete_handoff(
        handoff["handoff_id"],
        incarnation=_incarnation("2"),
        expected_revision=0,
        completed_by="supervisor",
    )
    assert result["successor_occurrence"]["round_index"] == 1


def test_the_donors_seed_is_carried_forward_verbatim():
    donor_agent, recipient_agent, donor = _pair(donor_seed=_complete_seed())
    handoff = _deliver(_begin(donor, recipient_agent))
    result = th.complete_handoff(
        handoff["handoff_id"],
        incarnation=_incarnation("2"),
        expected_revision=0,
        completed_by="supervisor",
    )
    successor = result["successor_occurrence"]
    assert successor["current"]["seed_quality"] == occ.SEED_COMPLETE
    assert successor["current"]["seed"] == donor["current"]["seed"]


def test_a_stale_expected_revision_refuses_the_transfer_and_writes_nothing():
    donor_agent, recipient_agent, donor = _pair()
    handoff = _deliver(_begin(donor, recipient_agent))
    occ.record_boundary(
        occ.BoundaryRecord(
            task_occurrence_id=donor["task_occurrence_id"],
            expected_revision=0,
            recorded_by="worker",
            report_digest=_DIGEST_B,
        )
    )
    with pytest.raises(occ.TaskOccurrenceConflict):
        th.complete_handoff(
            handoff["handoff_id"],
            incarnation=_incarnation("2"),
            expected_revision=0,
            completed_by="supervisor",
        )
    # Atomic: the donor is still open and the recipient never gained a round.
    assert occ.get_occurrence(donor["task_occurrence_id"])["state"] == occ.STATE_OPEN
    assert occ.open_occurrence_for_agent(recipient_agent) is None
    assert th.get_handoff(handoff["handoff_id"])["state"] == th.STATE_PENDING


def test_a_failure_between_the_two_occurrence_writes_leaves_neither(monkeypatch):
    # The atomicity claim, exercised where it actually matters: the donor is
    # already finalized when the recipient's open fails. A gap here would be
    # zero task authority with no record of who should hold it -- and the
    # drain would then read the donor as parked and tear its pane down.
    donor_agent, recipient_agent, donor = _pair()
    handoff = _deliver(_begin(donor, recipient_agent))

    def _boom(*_args, **_kwargs):
        raise occ.TaskOccurrenceUnavailable("the store went away mid-transfer")

    # Scoped, not `monkeypatch.undo()`: undo would also revert the
    # isolated-database fixture's own SessionLocal patch, and every assertion
    # below would then read the real store instead of this test's.
    with monkeypatch.context() as patched:
        patched.setattr(occ, "open_occurrence", _boom)
        with pytest.raises(occ.TaskOccurrenceUnavailable):
            th.complete_handoff(
                handoff["handoff_id"],
                incarnation=_incarnation("2"),
                expected_revision=0,
                completed_by="supervisor",
            )

    assert occ.get_occurrence(donor["task_occurrence_id"])["state"] == occ.STATE_OPEN
    assert occ.open_occurrence_for_agent(donor_agent) is not None
    assert occ.open_occurrence_for_agent(recipient_agent) is None
    # Still pending, so the supervisor can retry or roll back.
    assert th.get_handoff(handoff["handoff_id"])["state"] == th.STATE_PENDING


def test_a_replayed_transfer_adopts_rather_than_opening_a_second_round():
    donor_agent, recipient_agent, donor = _pair()
    handoff = _deliver(_begin(donor, recipient_agent))
    first = th.complete_handoff(
        handoff["handoff_id"],
        incarnation=_incarnation("2"),
        expected_revision=0,
        completed_by="supervisor",
    )
    replay = th.complete_handoff(
        handoff["handoff_id"],
        incarnation=_incarnation("2"),
        expected_revision=0,
        completed_by="supervisor",
    )
    assert replay["adopted"] is True
    assert replay["successor_occurrence"]["task_occurrence_id"] == (
        first["successor_occurrence"]["task_occurrence_id"]
    )
    assert len(occ.list_occurrences(SESSION, agent_id=recipient_agent)) == 1


def test_the_transfer_refuses_the_donors_own_incarnation():
    donor_agent, recipient_agent, donor = _pair()
    handoff = _deliver(_begin(donor, recipient_agent))
    with pytest.raises(th.TaskHandoffConflict, match="the donor's own incarnation"):
        th.complete_handoff(
            handoff["handoff_id"],
            incarnation=_incarnation("1"),
            expected_revision=0,
            completed_by="supervisor",
        )


def test_a_transferred_handoff_is_never_rolled_back():
    donor_agent, recipient_agent, donor = _pair()
    handoff = _deliver(_begin(donor, recipient_agent))
    th.complete_handoff(
        handoff["handoff_id"],
        incarnation=_incarnation("2"),
        expected_revision=0,
        completed_by="supervisor",
    )
    with pytest.raises(th.TaskHandoffConflict, match="already transferred"):
        th.rollback_handoff(
            handoff["handoff_id"], rolled_back_by="supervisor", reason="changed my mind"
        )


# ---------------------------------------------------------------------------
# rolling back
# ---------------------------------------------------------------------------


def test_rollback_restores_the_donor_by_having_changed_nothing():
    donor_agent, recipient_agent, donor = _pair()
    before = _occurrence_row(donor["task_occurrence_id"])
    handoff = _begin(donor, recipient_agent)

    rolled = th.rollback_handoff(
        handoff["handoff_id"], rolled_back_by="supervisor", reason="the recipient never resumed"
    )

    assert rolled["state"] == th.STATE_ROLLED_BACK
    assert rolled["donor_restored"] is True
    assert _occurrence_row(donor["task_occurrence_id"]) == before
    assert th.hold_refusal(donor_agent) is None
    assert th.hold_refusal(recipient_agent) is None


def test_rollback_always_releases_even_when_the_donor_is_no_longer_usable():
    # Refusing here would leave both agents held with no forward path -- the
    # one outcome worse than an honest "the donor is gone; recover it".
    donor_agent, recipient_agent, donor = _pair()
    handoff = _begin(donor, recipient_agent)
    occ.finalize_occurrence(
        occ.FinalizeRequest(
            task_occurrence_id=donor["task_occurrence_id"],
            expected_revision=0,
            disposition=occ.DISPOSITION_LOST,
            finalized_by="recovery",
        )
    )

    rolled = th.rollback_handoff(
        handoff["handoff_id"], rolled_back_by="supervisor", reason="pane died"
    )
    assert rolled["state"] == th.STATE_ROLLED_BACK
    assert rolled["donor_restored"] is False
    assert "holds no task to resume" in rolled["donor_detail"]
    assert th.hold_refusal(donor_agent) is None
    assert th.hold_refusal(recipient_agent) is None


def test_a_replayed_rollback_adopts():
    donor_agent, recipient_agent, donor = _pair()
    handoff = _begin(donor, recipient_agent)
    th.rollback_handoff(handoff["handoff_id"], rolled_back_by="supervisor", reason="quota returned")
    replay = th.rollback_handoff(
        handoff["handoff_id"], rolled_back_by="supervisor", reason="quota returned"
    )
    assert replay["adopted"] is True
    assert replay["state"] == th.STATE_ROLLED_BACK


# ---------------------------------------------------------------------------
# the admission hold
# ---------------------------------------------------------------------------


def test_both_sides_of_a_pending_handoff_are_held():
    donor_agent, recipient_agent, donor = _pair()
    _begin(donor, recipient_agent)
    assert "donor of pending handoff" in th.hold_refusal(donor_agent)
    assert "recipient of pending handoff" in th.hold_refusal(recipient_agent)


def test_only_the_exact_packet_control_id_is_admitted_to_the_recipient():
    donor_agent, recipient_agent, donor = _pair()
    handoff = _begin(donor, recipient_agent)
    packet = handoff["packet_control_id"]

    assert th.hold_refusal(recipient_agent, control_id=packet) is None
    assert th.hold_refusal(recipient_agent, control_id=str(uuid.uuid4())) is not None
    assert th.hold_refusal(recipient_agent) is not None
    # The donor has no exemption at all: the packet is not for it.
    assert th.hold_refusal(donor_agent, control_id=packet) is not None


def test_an_unrelated_agent_is_never_held():
    donor_agent, recipient_agent, donor = _pair()
    _begin(donor, recipient_agent)
    assert th.hold_refusal(str(uuid.uuid4())) is None
    assert th.hold_refusal(None) is None
    assert th.hold_refusal("") is None
    # A caller-supplied non-uuid is not a hold and must not become one.
    assert th.hold_refusal("not-a-uuid") is None


@pytest.mark.parametrize(
    "settle",
    [
        pytest.param("transferred", id="transferred"),
        pytest.param("rolled-back", id="rolled-back"),
    ],
)
def test_settling_a_handoff_releases_both_holds(settle):
    donor_agent, recipient_agent, donor = _pair()
    handoff = _begin(donor, recipient_agent)
    if settle == "transferred":
        _deliver(handoff)
        th.complete_handoff(
            handoff["handoff_id"],
            incarnation=_incarnation("2"),
            expected_revision=0,
            completed_by="supervisor",
        )
    else:
        th.rollback_handoff(
            handoff["handoff_id"], rolled_back_by="supervisor", reason="quota returned"
        )
    assert th.hold_refusal(donor_agent) is None
    assert th.hold_refusal(recipient_agent) is None
    assert th.hold_for_agent(donor_agent) is None


def test_the_hold_names_which_side_the_agent_is_on():
    donor_agent, recipient_agent, donor = _pair()
    _begin(donor, recipient_agent)
    assert th.hold_for_agent(donor_agent)["role"] == th.ROLE_DONOR
    assert th.hold_for_agent(recipient_agent)["role"] == th.ROLE_RECIPIENT


# ---------------------------------------------------------------------------
# what is deliberately absent, and validation
# ---------------------------------------------------------------------------


def test_no_handoff_record_ever_carries_a_native_session_id():
    # A handback never moves a native conversation between workers. A copy of
    # that id here would be a second, unenforced statement of a fact that is
    # only ever true of one side.
    from cli_agent_orchestrator.clients import database

    columns = set(database.TaskOccurrenceHandoffModel.__table__.columns.keys())
    assert not [name for name in columns if "native" in name]
    assert not [name for name in columns if "harness" in name or "provider" in name]

    donor_agent, recipient_agent, donor = _pair()
    handoff = _begin(donor, recipient_agent)
    assert "native_session_id" not in handoff
    assert "native_session_id" not in handoff["quiescence"]


def test_the_donor_is_finalized_superseded_so_a_pending_extension_never_blocks_it():
    # A goal extension awaiting its decider blocks a *reported* finalize. A
    # handback is not a report, and the decider still owns the extension.
    donor_agent, recipient_agent, donor = _pair()
    occ.attach_extension(
        occ.ExtensionRecord(
            task_occurrence_id=donor["task_occurrence_id"],
            extension_id="goal-claim",
            extension_kind="goal.completion-claim",
            extension_version="v1",
            decider="goal-track",
            payload={"claim": "done"},
        )
    )
    handoff = _deliver(_begin(donor, recipient_agent))
    th.complete_handoff(
        handoff["handoff_id"],
        incarnation=_incarnation("2"),
        expected_revision=0,
        completed_by="supervisor",
    )
    closed = occ.get_occurrence(donor["task_occurrence_id"])
    assert closed["finalized"]["disposition"] == occ.DISPOSITION_SUPERSEDED
    # Still visible to its decider, not silently stranded.
    assert [item["extension_id"] for item in occ.pending_extensions(decider="goal-track")] == [
        "goal-claim"
    ]


def test_a_changed_replay_of_begin_conflicts_rather_than_overwriting():
    donor_agent, recipient_agent, donor = _pair()
    handoff_id = str(uuid.uuid4())
    _begin(donor, recipient_agent, handoff_id=handoff_id)
    adopted = _begin(donor, recipient_agent, handoff_id=handoff_id)
    assert adopted["adopted"] is True
    with pytest.raises(th.TaskHandoffConflict, match="different immutable content"):
        th.begin_handoff(
            th.BeginRequest(
                handoff_id=handoff_id,
                session_name=SESSION,
                task_occurrence_id=donor["task_occurrence_id"],
                to_agent_id=recipient_agent,
                packet_digest=_DIGEST_B,
                evidence=_evidence("1"),
                initiated_by="supervisor",
            )
        )


def test_reads_and_listings_separate_pending_from_settled():
    donor_agent, recipient_agent, donor = _pair()
    handoff = _begin(donor, recipient_agent)
    assert th.handoff_for_occurrence(donor["task_occurrence_id"])["handoff_id"] == (
        handoff["handoff_id"]
    )
    assert [item["handoff_id"] for item in th.list_handoffs(SESSION)] == [handoff["handoff_id"]]
    assert th.list_handoffs(SESSION, state=th.STATE_TRANSFERRED) == []

    th.rollback_handoff(handoff["handoff_id"], rolled_back_by="supervisor", reason="done")
    assert th.handoff_for_occurrence(donor["task_occurrence_id"]) is None
    assert len(th.list_handoffs(SESSION, state=th.STATE_ROLLED_BACK)) == 1


def test_unknown_ids_and_malformed_input_are_typed():
    with pytest.raises(th.TaskHandoffNotFound):
        th.get_handoff(str(uuid.uuid4()))
    with pytest.raises(th.TaskHandoffInvalid, match="canonical lowercase UUID"):
        th.get_handoff("nope")
    with pytest.raises(th.TaskHandoffInvalid, match="state must be one of"):
        th.list_handoffs(SESSION, state="wat")
    with pytest.raises(th.TaskHandoffInvalid, match="must be a BeginRequest"):
        th.begin_handoff({"handoff_id": str(uuid.uuid4())})  # type: ignore[arg-type]


def test_the_capability_says_the_store_is_live_but_nothing_starts_a_handback():
    capability = th.capability()
    assert capability["store_installed"] is True
    assert capability["admission_hold_enforced"] is True
    assert capability["automatic_producer"] is False
    assert capability["conductor_consumer_attached"] is False
    assert capability["schema_version"] == th.SCHEMA_VERSION


def test_a_receipt_records_whether_quiescence_was_proven():
    donor_agent, recipient_agent, donor = _pair()
    proven = _deliver(_begin(donor, recipient_agent))
    unproven_evidence = _evidence("1", turn_state=th.TURN_UNKNOWN)
    assert th.receipt_digest(proven) != th.receipt_digest(
        {**proven, "quiescence_digest": th.digest(unproven_evidence.payload())}
    )
