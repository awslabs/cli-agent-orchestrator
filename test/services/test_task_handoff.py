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


def _begin(
    donor_occurrence,
    to_agent_id,
    *,
    handoff_id=None,
    suffix="1",
    evidence=None,
    expected_donor_revision=None,
):
    return th.begin_handoff(
        th.BeginRequest(
            handoff_id=handoff_id or str(uuid.uuid4()),
            session_name=SESSION,
            task_occurrence_id=donor_occurrence["task_occurrence_id"],
            to_agent_id=to_agent_id,
            packet_digest=_PACKET,
            evidence=evidence or _evidence(suffix),
            initiated_by="supervisor",
            expected_donor_revision=(
                donor_occurrence["revision"]
                if expected_donor_revision is None
                else expected_donor_revision
            ),
        )
    )


def _pair(*, donor_seed=None):
    """A donor holding an open round, and a dormant recipient."""
    donor_agent = str(uuid.uuid4())
    recipient_agent = str(uuid.uuid4())
    donor = _open(donor_agent, suffix="1", seed=donor_seed)
    return donor_agent, recipient_agent, donor


def _deliver(handoff, suffix="2"):
    return th.record_packet_delivery(
        handoff["handoff_id"],
        delivered=True,
        incarnation=_incarnation(suffix),
        outcome="accepted",
        receipt="receipt-1",
    )


def _occurrence_row(task_occurrence_id):
    """Every column of the occurrence, flattened, for byte-equality snapshots."""
    record = occ.get_occurrence(task_occurrence_id)
    record.pop("extensions", None)
    record.pop("seed_verdict", None)
    return record


def _bind_roster(agent_id, *, suffix, generation=None):
    """A real roster incarnation, the way any live worker has one."""
    from cli_agent_orchestrator.services import stable_agent_roster as roster

    return roster.bind_generation(
        roster.BindingContract(
            agent_id=agent_id,
            session_name=SESSION,
            role=roster.ROLE_WORKER,
            profile_family="developer",
            harness="claude_code",
            native_session_id=f"native-{suffix}",
            acquisition_method="chosen_session_id",
            terminal_id=f"term-{suffix}",
            generation=generation or f"gen-{suffix}",
            pane_id=f"%{suffix}",
            pane_pid=6000 + int(suffix),
            process_identity={"pid": 6000 + int(suffix), "start_marker": f"marker-{suffix}"},
            execution_mode="native_tui",
            admitted=True,
        )
    )


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


def test_a_concurrent_drain_never_reads_a_held_donor_as_parked(monkeypatch):
    """The reason `handoff-pending` is not a `task_occurrences.state`.

    Both halves are executed. First the shipped design: a held donor still has
    an open round, so the drain's own observer refuses to call it parked.
    Then the rejected design's observable condition -- no open occurrence while
    a finalized one exists -- which is exactly what a third occurrence state
    produces, and the observer turns that into positive `parked` carrying the
    *previous* round's digests. Under Stop that is what authorizes teardown of
    the pane the handback is keeping as rollback insurance.
    """
    from cli_agent_orchestrator.services import control_input_service as cis
    from cli_agent_orchestrator.services import supervisor_drain as drain

    class _Live:
        terminal_generation = "gen-1"
        pane_dead = False

    monkeypatch.setattr(cis, "resolve_control_identity", lambda _tid: _Live())

    donor_agent = str(uuid.uuid4())
    recipient_agent = str(uuid.uuid4())

    # A finished earlier round, whose digests are the stale ones at risk.
    earlier = _open(donor_agent, round_index=0, suffix="1")
    occ.record_boundary(
        occ.BoundaryRecord(
            task_occurrence_id=earlier["task_occurrence_id"],
            expected_revision=0,
            recorded_by="worker",
            report_digest=_DIGEST_B,
        )
    )
    occ.finalize_occurrence(
        occ.FinalizeRequest(
            task_occurrence_id=earlier["task_occurrence_id"],
            expected_revision=1,
            disposition=occ.DISPOSITION_REPORTED,
            finalized_by="supervisor",
        )
    )
    # The live round the handback is about.
    live = _open(donor_agent, round_index=1, suffix="1")
    handoff = _begin(live, recipient_agent)
    assert handoff["state"] == th.STATE_PENDING

    member = {
        "agent_id": donor_agent,
        "session_name": SESSION,
        "terminal_id": "term-1",
        "generation": "gen-1",
    }

    # Shipped design: the donor is held, and the drain still sees a live round.
    shipped = drain._default_observer(member, "before")
    assert shipped.state != drain.OBS_PARKED
    assert shipped.quiescent is False
    assert shipped.task_occurrence_id == live["task_occurrence_id"]
    assert drain._classify(shipped, steered=True)[0] == drain.MEMBER_RECONCILIATION_REQUIRED

    # Rejected design, simulated at exactly the seam a third state moves: an
    # occurrence in `handoff-pending` is still stored, still un-finalized, and
    # simply stops answering the one-open-round query. Nothing else changes.
    with monkeypatch.context() as patched:
        patched.setattr(occ, "open_occurrence_for_agent", lambda *_a, **_k: None)
        rejected = drain._default_observer(member, "before")

    assert rejected.state == drain.OBS_PARKED
    assert rejected.quiescent is True
    # Carrying the *earlier* round's digests -- the stale-evidence half.
    assert rejected.task_occurrence_id == earlier["task_occurrence_id"]
    assert rejected.report_digest == _DIGEST_B
    member_state, _detail = drain._classify(rejected, steered=True)
    assert member_state == drain.MEMBER_PARKED
    # Terminal, so the drain settles complete and mints a spendable receipt;
    # under Stop that is what announces teardown of the donor's pane.
    assert member_state in drain.MEMBER_TERMINAL


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
                expected_donor_revision=0,
            )
        )


def test_a_settled_handoff_frees_both_agents_to_hand_back_again():
    donor_agent, recipient_agent, donor = _pair()
    first = _begin(donor, recipient_agent)
    th.rollback_handoff(first["handoff_id"], rolled_back_by="supervisor", reason="quota returned")
    again = _begin(donor, recipient_agent)
    assert again["state"] == th.STATE_PENDING
    assert again["handoff_id"] != first["handoff_id"]


def test_two_concurrent_begins_naming_one_recipient_leave_one_pending_row(tmp_path, monkeypatch):
    """Two concurrent begins naming one agent as **recipient** of both.

    Precisely what this covers, because an earlier docstring here claimed more
    than the body does: both threads pass the same `to_agent`, so this is a
    collision on the *recipient slot*, which
    `ix_task_occurrence_handoffs_pending_recipient` forbids outright. The
    second INSERT raises `IntegrityError`, `_with_session` retries the whole
    function, and the retry meets the precondition against committed state.
    What it proves is the outcome: one accepted, one typed refusal, one pending
    row, and a hot path that stays typed rather than raising
    `MultipleResultsFound`.

    The **cross-slot** case -- one agent as donor of one handoff and recipient
    of another -- is the one no index expresses, and it is *not* exercised
    here. It is guarded by the recipient-must-be-free precondition plus the
    anchored read-then-insert in `_begin_once`, not by any index and not by the
    retry (nothing raises, so nothing retries). A forced interleave of that
    case ended safe with the write lock both on and off, so there is no
    discriminating test to write; see `_anchor_caller_transaction` for what is
    argued from measurement rather than from a failing test.
    """
    import threading

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from cli_agent_orchestrator.clients import database

    path = tmp_path / "concurrent.db"
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    database.Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(
        database, "SessionLocal", sessionmaker(autocommit=False, autoflush=False, bind=engine)
    )

    shared = str(uuid.uuid4())
    donor_a, donor_b = str(uuid.uuid4()), str(uuid.uuid4())
    round_a = _open(donor_a, suffix="1")
    round_b = _open(donor_b, suffix="4")

    start = threading.Barrier(2)
    results: list[Any] = []

    def _begin_pair(occurrence_record, to_agent, suffix):
        start.wait(timeout=5)
        try:
            results.append(
                th.begin_handoff(
                    th.BeginRequest(
                        handoff_id=str(uuid.uuid4()),
                        session_name=SESSION,
                        task_occurrence_id=occurrence_record["task_occurrence_id"],
                        to_agent_id=to_agent,
                        packet_digest=_PACKET,
                        evidence=_evidence(suffix),
                        initiated_by="supervisor",
                        expected_donor_revision=0,
                    )
                )
            )
        except Exception as exc:  # noqa: BLE001 - the refusal is the assertion
            results.append(exc)

    # Two different donors, both naming the same recipient: one recipient slot,
    # two claimants. `shared` holds no occurrence anywhere in this test, so it
    # is never a donor and this is not the cross-slot case.
    threads = [
        threading.Thread(target=_begin_pair, args=(round_a, shared, "1")),
        threading.Thread(target=_begin_pair, args=(round_b, shared, "4")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    accepted = [item for item in results if isinstance(item, dict)]
    refused = [item for item in results if isinstance(item, Exception)]
    assert len(accepted) == 1, results
    assert len(refused) == 1, results
    assert isinstance(refused[0], th.TaskHandoffConflict)

    pending = [
        item
        for item in th.list_handoffs(SESSION, state=th.STATE_PENDING)
        if shared in (item["from_agent_id"], item["to_agent_id"])
    ]
    assert len(pending) == 1
    # And the hot path stays typed rather than raising MultipleResultsFound.
    assert th.hold_refusal(shared) is not None
    engine.dispose()


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
# the revision the packet digest describes (cond-0439)
# ---------------------------------------------------------------------------


def test_begin_refuses_a_donor_that_moved_since_the_packet_was_digested():
    # The packet digest is taken BEFORE begin. If a still-admitted boundary
    # write moves the donor in the observe-then-begin window, anchoring
    # `donor_revision` at begin time records a revision the packet never
    # described -- and the transfer check then passes a stale packet.
    donor_agent, recipient_agent, donor = _pair()
    # The supervisor digests the packet describing revision 0, then the donor's
    # in-flight turn records another boundary before begin lands.
    occ.record_boundary(
        occ.BoundaryRecord(
            task_occurrence_id=donor["task_occurrence_id"],
            expected_revision=0,
            recorded_by="worker",
            report_digest=_DIGEST_B,
        )
    )
    with pytest.raises(th.TaskHandoffConflict, match="packet digest describes"):
        _begin(donor, recipient_agent, expected_donor_revision=0)
    # Nothing is held: the caller re-observes and retries with a fresh packet.
    assert th.hold_refusal(donor_agent) is None
    assert th.list_handoffs(SESSION, state=th.STATE_PENDING) == []


def test_begin_pins_the_donor_revision_the_packet_digest_describes():
    # Companion pin, not the gate: this passes even with the begin-time CAS
    # deleted. The sibling that discriminates the CAS is
    # test_begin_refuses_a_donor_that_moved_since_the_packet_was_digested;
    # this pins the recorded value the transfer check later compares against.
    donor_agent, recipient_agent, donor = _pair()
    occ.record_boundary(
        occ.BoundaryRecord(
            task_occurrence_id=donor["task_occurrence_id"],
            expected_revision=0,
            recorded_by="worker",
            report_digest=_DIGEST_B,
        )
    )
    handoff = _begin(donor, recipient_agent, expected_donor_revision=1)
    assert handoff["donor_revision"] == 1


def test_a_replayed_begin_adopts_against_the_recorded_revision_not_the_live_one():
    # A caller that lost the begin response retries the identical request. The
    # donor may have moved since the first attempt recorded; the retry adopts
    # the recorded effect rather than re-running the CAS against live state.
    donor_agent, recipient_agent, donor = _pair()
    handoff_id = str(uuid.uuid4())
    first = _begin(donor, recipient_agent, handoff_id=handoff_id, expected_donor_revision=0)
    assert first["adopted"] is False
    occ.record_boundary(
        occ.BoundaryRecord(
            task_occurrence_id=donor["task_occurrence_id"],
            expected_revision=0,
            recorded_by="worker",
            report_digest=_DIGEST_B,
        )
    )
    replay = _begin(donor, recipient_agent, handoff_id=handoff_id, expected_donor_revision=0)
    assert replay["adopted"] is True
    assert replay["donor_revision"] == 0
    # A retry naming a different expected revision is not the same request.
    with pytest.raises(th.TaskHandoffConflict, match="different immutable content"):
        _begin(donor, recipient_agent, handoff_id=handoff_id, expected_donor_revision=1)


def test_begin_refuses_a_caller_session_already_in_a_transaction():
    # cond-0439-batch P3-b. `begin_handoff` anchors BEGIN IMMEDIATE so the
    # read-then-insert precondition holds the write lock while it reads. A
    # caller-supplied session already inside a transaction silently downgrades
    # that to the deferred lock the caller already holds -- an invisible
    # weakening, so it is refused rather than allowed to proceed.
    from cli_agent_orchestrator.clients import database

    donor_agent, recipient_agent, donor = _pair()
    session = database.SessionLocal()
    try:
        session.connection().exec_driver_sql("BEGIN")
        with pytest.raises(th.TaskHandoffInvalid, match="already inside a transaction"):
            th.begin_handoff(
                th.BeginRequest(
                    handoff_id=str(uuid.uuid4()),
                    session_name=SESSION,
                    task_occurrence_id=donor["task_occurrence_id"],
                    to_agent_id=recipient_agent,
                    packet_digest=_PACKET,
                    evidence=_evidence("1"),
                    initiated_by="supervisor",
                    expected_donor_revision=0,
                ),
                db=session,
            )
    finally:
        session.close()
    # The refusal wrote nothing.
    assert th.list_handoffs(SESSION, state=th.STATE_PENDING) == []


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
            incarnation=_incarnation("2"),
            outcome="different",
        )


def test_an_identical_delivery_record_adopts_after_a_lost_response():
    donor_agent, recipient_agent, donor = _pair()
    handoff = _begin(donor, recipient_agent)
    first = _deliver(handoff)
    replay = _deliver(handoff)
    assert replay["adopted"] is True
    assert replay["to_incarnation_id"] == first["to_incarnation_id"]


def test_a_delivered_packet_records_which_incarnation_received_it():
    # The derived control id binds no terminal, and the control-input journal
    # refuses to re-deliver a known id to a replacement pane -- so "delivered"
    # is only meaningful alongside "to whom".
    donor_agent, recipient_agent, donor = _pair()
    handoff = _begin(donor, recipient_agent)
    delivered = _deliver(handoff, suffix="2")
    assert delivered["to_incarnation_id"] == "inc-2"
    assert delivered["to_terminal_id"] == "term-2"
    assert delivered["recipient_briefed"] is True

    other = _begin(_open(str(uuid.uuid4()), suffix="7"), str(uuid.uuid4()), evidence=_evidence("7"))
    with pytest.raises(th.TaskHandoffInvalid, match="must name the recipient incarnation"):
        th.record_packet_delivery(other["handoff_id"], delivered=True)


def test_a_packet_is_never_recorded_as_delivered_to_the_donor():
    donor_agent, recipient_agent, donor = _pair()
    handoff = _begin(donor, recipient_agent)
    with pytest.raises(th.TaskHandoffConflict, match="donor's own incarnation"):
        th.record_packet_delivery(
            handoff["handoff_id"], delivered=True, incarnation=_incarnation("1")
        )


def test_a_refused_delivery_leaves_the_handoff_pending_and_retryable():
    donor_agent, recipient_agent, donor = _pair()
    handoff = _begin(donor, recipient_agent)
    refused = th.record_packet_delivery(handoff["handoff_id"], delivered=False, outcome="pane-busy")
    assert refused["state"] == th.STATE_PENDING
    assert refused["delivery_state"] == th.DELIVERY_REFUSED
    assert refused["recipient_briefed"] is False
    # The packet never landed, so the lane stays open for a real delivery.
    assert _deliver(handoff)["delivery_state"] == th.DELIVERY_DELIVERED

    rolled = th.rollback_handoff(
        handoff["handoff_id"], rolled_back_by="supervisor", reason="quota returned"
    )
    assert rolled["donor_restored"] is True


def test_a_replayed_delivery_with_a_different_receipt_conflicts():
    # P3-a. The adopt comparison must cover everything the first call recorded.
    # A replay that agrees on state, outcome and incarnation but carries a
    # different delivery receipt is not the same delivery, and silently
    # adopting it would let a caller believe its own receipt was stored.
    donor_agent, recipient_agent, donor = _pair()
    handoff = _begin(donor, recipient_agent)
    _deliver(handoff)
    with pytest.raises(th.TaskHandoffConflict, match="delivered exactly once"):
        th.record_packet_delivery(
            handoff["handoff_id"],
            delivered=True,
            incarnation=_incarnation("2"),
            outcome="accepted",
            receipt="receipt-2",
        )
    # An identical replay still adopts.
    assert _deliver(handoff)["adopted"] is True


# ---------------------------------------------------------------------------
# the delivered-to incarnation, checked against the roster (cond-0441)
# ---------------------------------------------------------------------------


def test_a_delivery_to_a_pane_the_roster_binds_to_another_agent_is_refused():
    # The supervisor's roster view is stale: it believes term-9 is the
    # recipient's pane, but the roster -- the one authority -- binds that pane
    # to a different worker. Recording the delivery would stamp the recipient's
    # successor occurrence with another worker's pane, and the finalize-by-
    # occurrence-id recovery paths would then act on the wrong physical worker.
    donor_agent, recipient_agent, donor = _pair()
    handoff = _begin(donor, recipient_agent)
    _bind_roster(str(uuid.uuid4()), suffix="9")
    with pytest.raises(th.TaskHandoffConflict, match="not the handoff's recipient"):
        th.record_packet_delivery(
            handoff["handoff_id"], delivered=True, incarnation=_incarnation("9")
        )
    # The refusal wrote nothing: the handoff stays pending and rollback-able.
    record = th.get_handoff(handoff["handoff_id"])
    assert record["delivery_state"] == th.DELIVERY_PENDING
    assert record["recipient_briefed"] is False
    rolled = th.rollback_handoff(
        handoff["handoff_id"], rolled_back_by="supervisor", reason="delivered to the wrong pane"
    )
    assert rolled["donor_restored"] is True


def test_a_stale_incarnation_assertion_is_corrected_to_the_rosters_binding():
    # The packet went to the recipient's pane; the supervisor only misnamed the
    # incarnation. The roster binds that exact terminal and generation, so its
    # identity is the physical truth -- record it rather than refusing a
    # delivery that genuinely reached the recipient.
    donor_agent, recipient_agent, donor = _pair()
    handoff = _begin(donor, recipient_agent)
    binding = _bind_roster(recipient_agent, suffix="2")
    roster_incarnation = binding["incarnation"]["incarnation_id"]
    assert roster_incarnation != "inc-2"

    delivered = _deliver(handoff)  # asserts the stale incarnation id "inc-2"
    assert delivered["to_incarnation_id"] == roster_incarnation
    assert delivered["to_terminal_id"] == "term-2"

    # And the transfer names what the roster -- and now the record -- says.
    result = th.complete_handoff(
        handoff["handoff_id"],
        incarnation=occ.EffectIncarnation(
            incarnation_id=roster_incarnation, terminal_id="term-2", generation="gen-2"
        ),
        expected_revision=0,
        completed_by="supervisor",
    )
    assert result["state"] == th.STATE_TRANSFERRED
    assert result["successor_occurrence"]["incarnation_id"] == roster_incarnation


def test_a_roster_silent_delivery_is_recorded_as_caller_asserted():
    # Companion pin, not the gate: this passes even with the roster
    # resolution deleted entirely. The siblings that discriminate the
    # mechanism are test_a_delivery_to_a_pane_the_roster_binds_to_another_agent_is_refused
    # and the two live-fallback tests below.
    #
    # Deliberate posture, matching the admission hold's: a terminal with no
    # live roster incarnation under ANY generation is not refused. An agent
    # with no incarnation cannot be a handoff party in a real flow, and a
    # pane the roster cannot vouch for is indistinguishable from the ordinary
    # lost-pane state the system already recovers. The record says what the
    # caller asserted; it does not claim roster proof.
    donor_agent, recipient_agent, donor = _pair()
    handoff = _begin(donor, recipient_agent)
    delivered = _deliver(handoff)
    assert delivered["to_incarnation_id"] == "inc-2"
    assert delivered["delivery_state"] == th.DELIVERY_DELIVERED


def test_a_delivery_naming_a_generation_the_roster_binds_elsewhere_is_refused():
    # cond-0441 F1 face A. The supervisor's view is one staleness hop deeper
    # than a misnamed incarnation: the roster live-binds term-9 to another
    # worker under gen-9, while the stale assertion names (term-9, gen-1) for
    # the recipient. The exact lookup finds no gen-1 row, but that is not
    # silence -- the roster positively vouches for the pane under another
    # generation, and the live binding says it is not the recipient's.
    # Recording would stamp the successor occurrence with another worker's
    # live pane, and the finalize-by-occurrence-id recovery paths would act
    # on the wrong physical worker.
    donor_agent, recipient_agent, donor = _pair()
    handoff = _begin(donor, recipient_agent)
    _bind_roster(str(uuid.uuid4()), suffix="9")
    with pytest.raises(th.TaskHandoffConflict, match="not the handoff's recipient"):
        th.record_packet_delivery(
            handoff["handoff_id"],
            delivered=True,
            incarnation=occ.EffectIncarnation(
                incarnation_id="inc-stale", terminal_id="term-9", generation="gen-1"
            ),
        )
    # The refusal wrote nothing: the handoff stays pending and rollback-able.
    record = th.get_handoff(handoff["handoff_id"])
    assert record["delivery_state"] == th.DELIVERY_PENDING
    assert record["recipient_briefed"] is False


def test_a_delivery_naming_a_retired_incarnation_is_corrected_to_the_live_binding():
    # cond-0441 F1 face B. The recipient was bound at (term-2, gen-1) when
    # the supervisor read its roster view, then ordinary recovery rebound it
    # to (term-2, gen-2) mid-window; the gen-1 row remains as retired
    # history. The stale assertion names the retired row, which matches the
    # recipient by agent and id -- but a retired incarnation read no packet
    # (the roster's own assert_admission_ready refuses that same pair), and
    # the bytes could only have landed on the live gen-2 pane. Record the
    # live binding rather than a conclusion the check never established.
    from cli_agent_orchestrator.services import stable_agent_roster as roster

    donor_agent, recipient_agent, donor = _pair()
    handoff = _begin(donor, recipient_agent)
    retired = _bind_roster(recipient_agent, suffix="2", generation="gen-1")
    retired_incarnation = retired["incarnation"]["incarnation_id"]
    roster.retire_incarnation(terminal_id="term-2", generation="gen-1", reason="rebound")
    live = _bind_roster(recipient_agent, suffix="2", generation="gen-2")
    live_incarnation = live["incarnation"]["incarnation_id"]

    delivered = th.record_packet_delivery(
        handoff["handoff_id"],
        delivered=True,
        incarnation=occ.EffectIncarnation(
            incarnation_id=retired_incarnation, terminal_id="term-2", generation="gen-1"
        ),
    )
    assert delivered["to_incarnation_id"] == live_incarnation
    assert delivered["to_terminal_id"] == "term-2"
    assert delivered["to_generation"] == "gen-2"

    # And the transfer names what the roster -- and now the record -- says.
    result = th.complete_handoff(
        handoff["handoff_id"],
        incarnation=occ.EffectIncarnation(
            incarnation_id=live_incarnation, terminal_id="term-2", generation="gen-2"
        ),
        expected_revision=0,
        completed_by="supervisor",
    )
    assert result["state"] == th.STATE_TRANSFERRED
    assert result["successor_occurrence"]["incarnation_id"] == live_incarnation


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
# the successor's stamp resolves from the handoff row (cond-0478)
# ---------------------------------------------------------------------------


def test_the_successors_full_triple_comes_from_the_row_not_the_callers_body():
    # cond-0441 roster-corrects the delivery and records the corrected triple
    # on the row; the transfer's incarnation CAS proves only the id. A caller
    # whose body still carries the pre-correction generation must not stamp it
    # onto the successor occurrence.
    from cli_agent_orchestrator.services import stable_agent_roster as roster

    donor_agent, recipient_agent, donor = _pair()
    handoff = _begin(donor, recipient_agent)
    retired = _bind_roster(recipient_agent, suffix="2", generation="gen-1")
    retired_incarnation = retired["incarnation"]["incarnation_id"]
    roster.retire_incarnation(terminal_id="term-2", generation="gen-1", reason="rebound")
    live = _bind_roster(recipient_agent, suffix="2", generation="gen-2")
    live_incarnation = live["incarnation"]["incarnation_id"]
    delivered = th.record_packet_delivery(
        handoff["handoff_id"],
        delivered=True,
        incarnation=occ.EffectIncarnation(
            incarnation_id=retired_incarnation, terminal_id="term-2", generation="gen-1"
        ),
    )
    assert delivered["to_generation"] == "gen-2"

    result = th.complete_handoff(
        handoff["handoff_id"],
        # The CAS-passing caller still asserts the pre-rebind generation.
        incarnation=occ.EffectIncarnation(
            incarnation_id=live_incarnation, terminal_id="term-2", generation="gen-1"
        ),
        expected_revision=0,
        completed_by="supervisor",
    )
    successor = result["successor_occurrence"]
    record = th.get_handoff(handoff["handoff_id"])
    assert successor["incarnation_id"] == record["to_incarnation_id"] == live_incarnation
    assert successor["terminal_id"] == record["to_terminal_id"] == "term-2"
    assert successor["generation"] == record["to_generation"] == "gen-2"


def test_a_caller_naming_a_foreign_live_pane_does_not_stamp_it_on_the_successor():
    # The caller's incarnation id matches the row, but its terminal and
    # generation name another worker's live pane. The row's roster-verified
    # copy wins; stamping the foreign pane would send the
    # finalize-by-occurrence-id recovery paths to a pane that is not the
    # recipient's.
    donor_agent, recipient_agent, donor = _pair()
    handoff = _deliver(_begin(donor, recipient_agent))  # row: inc-2 / term-2 / gen-2
    foreign = _bind_roster(str(uuid.uuid4()), suffix="9")
    assert foreign["incarnation"]["terminal_id"] == "term-9"

    result = th.complete_handoff(
        handoff["handoff_id"],
        incarnation=occ.EffectIncarnation(
            incarnation_id="inc-2", terminal_id="term-9", generation="gen-9"
        ),
        expected_revision=0,
        completed_by="supervisor",
    )
    successor = result["successor_occurrence"]
    record = th.get_handoff(handoff["handoff_id"])
    assert successor["incarnation_id"] == record["to_incarnation_id"] == "inc-2"
    assert successor["terminal_id"] == record["to_terminal_id"] == "term-2"
    assert successor["generation"] == record["to_generation"] == "gen-2"


def test_a_null_generation_on_the_row_stamps_null_not_the_callers_value():
    # generation is the one nullable column of the triple: terminal_id cannot
    # be NULL on a delivered handoff, because the delivery required a named
    # incarnation and EffectIncarnation refuses an empty terminal. A NULL
    # generation means the delivery established none, and the complete-time
    # caller's assertion was never verified against anything, so the successor
    # records none rather than adopting it.
    donor_agent, recipient_agent, donor = _pair()
    handoff = _begin(donor, recipient_agent)
    delivered = th.record_packet_delivery(
        handoff["handoff_id"],
        delivered=True,
        incarnation=occ.EffectIncarnation(
            incarnation_id="inc-2", terminal_id="term-2", generation=None
        ),
    )
    assert delivered["to_generation"] is None

    result = th.complete_handoff(
        handoff["handoff_id"],
        incarnation=occ.EffectIncarnation(
            incarnation_id="inc-2", terminal_id="term-2", generation="gen-late"
        ),
        expected_revision=0,
        completed_by="supervisor",
    )
    successor = result["successor_occurrence"]
    assert successor["incarnation_id"] == "inc-2"
    assert successor["terminal_id"] == "term-2"
    assert successor["generation"] is None


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
    # The donor has not drifted; the caller simply named the wrong revision.
    # M3-D's own CAS is what refuses, and it refuses before either write.
    donor_agent, recipient_agent, donor = _pair()
    handoff = _deliver(_begin(donor, recipient_agent))
    with pytest.raises(occ.TaskOccurrenceConflict):
        th.complete_handoff(
            handoff["handoff_id"],
            incarnation=_incarnation("2"),
            expected_revision=7,
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
        # A non-retryable occurrence error, so this test stays about the
        # transaction boundary rather than about the retry policy.
        raise occ.TaskOccurrenceConflict("the recipient's round could not be opened")

    # Scoped, not `monkeypatch.undo()`: undo would also revert the
    # isolated-database fixture's own SessionLocal patch, and every assertion
    # below would then read the real store instead of this test's.
    with monkeypatch.context() as patched:
        patched.setattr(occ, "open_occurrence", _boom)
        with pytest.raises(occ.TaskOccurrenceConflict):
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


def test_a_transfer_refuses_an_incarnation_that_never_received_the_packet():
    # The pane the packet reached died; recovery exact-resumed the recipient
    # onto a new incarnation. The packet cannot follow -- the derived control
    # id is stable across incarnations and the control-input journal refuses a
    # known id whose target moved -- so transferring here would leave the sole
    # task authority holding no context while the record asserts that it read
    # the packet, with the donor already superseded and unrecoverable.
    donor_agent, recipient_agent, donor = _pair()
    handoff = _deliver(_begin(donor, recipient_agent), suffix="2")

    with pytest.raises(th.TaskHandoffConflict, match="cannot be re-delivered to a replacement"):
        th.complete_handoff(
            handoff["handoff_id"],
            incarnation=_incarnation("5"),
            expected_revision=0,
            completed_by="supervisor",
        )
    assert occ.get_occurrence(donor["task_occurrence_id"])["state"] == occ.STATE_OPEN
    assert occ.open_occurrence_for_agent(recipient_agent) is None

    # The recorded way forward is a rollback and a new handoff, which yields a
    # new derived control id and so a deliverable packet.
    rolled = th.rollback_handoff(
        handoff["handoff_id"], rolled_back_by="supervisor", reason="recipient pane died"
    )
    assert rolled["recipient_briefed"] is True
    again = _deliver(_begin(donor, recipient_agent), suffix="5")
    assert again["packet_control_id"] != handoff["packet_control_id"]
    assert (
        th.complete_handoff(
            again["handoff_id"],
            incarnation=_incarnation("5"),
            expected_revision=0,
            completed_by="supervisor",
        )["state"]
        == th.STATE_TRANSFERRED
    )


def test_a_donor_that_moved_while_held_refuses_the_transfer():
    # The packet describes one exact revision. If something wrote to the donor
    # after the digest was taken, the context the recipient read is not the
    # round it is being given.
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
    with pytest.raises(th.TaskHandoffConflict, match="no longer describes the round"):
        th.complete_handoff(
            handoff["handoff_id"],
            incarnation=_incarnation("2"),
            expected_revision=1,
            completed_by="supervisor",
        )
    assert occ.get_occurrence(donor["task_occurrence_id"])["state"] == occ.STATE_OPEN
    assert th.get_handoff(handoff["handoff_id"])["state"] == th.STATE_PENDING


def test_a_replayed_transfer_naming_a_different_incarnation_conflicts():
    donor_agent, recipient_agent, donor = _pair()
    handoff = _deliver(_begin(donor, recipient_agent))
    th.complete_handoff(
        handoff["handoff_id"],
        incarnation=_incarnation("2"),
        expected_revision=0,
        completed_by="supervisor",
    )
    with pytest.raises(th.TaskHandoffConflict, match="already transferred to incarnation"):
        th.complete_handoff(
            handoff["handoff_id"],
            incarnation=_incarnation("6"),
            expected_revision=0,
            completed_by="supervisor",
        )


def test_a_recipient_that_already_holds_a_round_is_refused_before_it_is_held():
    # Refusing here is free. Past this point the recipient's own unrelated
    # round is suspended and the catch-up packet is typed into its context by
    # the exemption -- and typed bytes are not reversible.
    donor_agent, recipient_agent, donor = _pair()
    busy = _open(recipient_agent, suffix="3")
    with pytest.raises(th.TaskHandoffConflict, match="must be free to take the round"):
        _begin(donor, recipient_agent)
    assert th.hold_refusal(recipient_agent) is None
    assert occ.get_occurrence(busy["task_occurrence_id"])["state"] == occ.STATE_OPEN


def test_a_recipient_dispatched_mid_window_settles_failed_and_releases_both_holds():
    # cond-0440. The begin-time recipient-is-free check cannot hold for the
    # whole window: opening an occurrence is a store operation that consults no
    # handoff state, and refusing that dispatch would fail an unrelated command
    # for a reason its caller did not cause and cannot clear. What must not
    # happen is the wedge -- transfer refused, both agents still held, the
    # recipient already briefed, recovery only by hand. So the transfer settles
    # the handoff as failed instead: the donor is never finalized, both holds
    # release, and recipient_briefed keeps the cancellation owed. Two task
    # authorities -- the invariant -- is still never created.
    donor_agent, recipient_agent, donor = _pair()
    handoff = _deliver(_begin(donor, recipient_agent))
    # An ordinary, unrelated dispatch opens a round for the recipient
    # mid-window. This is legal and takes no lock the handoff holds.
    dispatched = _open(recipient_agent, suffix="3")

    result = th.complete_handoff(
        handoff["handoff_id"],
        incarnation=_incarnation("2"),
        expected_revision=0,
        completed_by="supervisor",
    )
    assert result["state"] == th.STATE_FAILED
    assert result["adopted"] is False
    assert result["recipient_briefed"] is True
    assert result["donor_restored"] is True
    assert dispatched["task_occurrence_id"] in result["detail"]

    # Nobody transferred: the donor was never finalized, and the recipient's
    # unrelated round is untouched -- it was never the problem.
    assert occ.get_occurrence(donor["task_occurrence_id"])["state"] == occ.STATE_OPEN
    assert (
        occ.open_occurrence_for_agent(donor_agent)["task_occurrence_id"]
        == donor["task_occurrence_id"]
    )
    assert occ.get_occurrence(dispatched["task_occurrence_id"])["state"] == occ.STATE_OPEN

    # The wedge is gone: both holds released, both agents steerable again.
    assert th.hold_refusal(donor_agent) is None
    assert th.hold_refusal(recipient_agent) is None

    settled = th.get_handoff(handoff["handoff_id"])
    assert settled["state"] == th.STATE_FAILED
    assert settled["receipt_digest"]
    assert settled["settled_at"] is not None

    # A response-loss replay adopts the settled failure rather than raising
    # "only a pending handoff transfers".
    replay = th.complete_handoff(
        handoff["handoff_id"],
        incarnation=_incarnation("2"),
        expected_revision=0,
        completed_by="supervisor",
    )
    assert replay["adopted"] is True
    assert replay["state"] == th.STATE_FAILED

    # Rollback is no longer the exit -- the handoff is already settled.
    with pytest.raises(th.TaskHandoffConflict, match="settled as failed"):
        th.rollback_handoff(handoff["handoff_id"], rolled_back_by="supervisor", reason="too late")

    # And both agents may hand back again: the failed row left the pending
    # slots, exactly as a settled row does.
    again = _begin(donor, str(uuid.uuid4()))
    assert again["state"] == th.STATE_PENDING


def test_a_rollback_after_delivery_reports_that_the_recipient_was_briefed():
    # The rollback still releases -- one that could refuse would leave both
    # agents held -- but it is not the same outcome as releasing nobody, and
    # the receipt has to be able to say so or the cancellation is never owed.
    donor_agent, recipient_agent, donor = _pair()
    handoff = _deliver(_begin(donor, recipient_agent))
    rolled = th.rollback_handoff(
        handoff["handoff_id"], rolled_back_by="supervisor", reason="readiness proof failed"
    )
    assert rolled["state"] == th.STATE_ROLLED_BACK
    assert rolled["recipient_briefed"] is True
    assert rolled["donor_restored"] is True

    unbriefed = _begin(donor, str(uuid.uuid4()))
    quiet = th.rollback_handoff(
        unbriefed["handoff_id"], rolled_back_by="supervisor", reason="changed my mind"
    )
    assert quiet["recipient_briefed"] is False

    # The receipt must turn on *this* field and not merely on the delivery
    # state and recipient incarnation that happen to travel with it -- a
    # comparison between two whole records would pass even if the receipt were
    # blind to briefing, which is the thing E2 reads to know a cancellation is
    # owed.
    assert th.receipt_digest(rolled) != th.receipt_digest({**rolled, "recipient_briefed": False})
    assert th.receipt_digest(quiet) != th.receipt_digest({**quiet, "recipient_briefed": True})


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
                expected_donor_revision=0,
            )
        )


def test_reads_and_listings_separate_pending_from_settled():
    donor_agent, recipient_agent, donor = _pair()
    handoff = _begin(donor, recipient_agent)
    assert [item["handoff_id"] for item in th.list_handoffs(SESSION)] == [handoff["handoff_id"]]
    assert th.list_handoffs(SESSION, state=th.STATE_TRANSFERRED) == []

    th.rollback_handoff(handoff["handoff_id"], rolled_back_by="supervisor", reason="done")
    assert th.list_handoffs(SESSION, state=th.STATE_PENDING) == []
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
    assert capability["schema_version"] == th.SCHEMA_VERSION
    # No hand-maintained claim about the *other* repository: this module cannot
    # observe whether a conductor client exists, and a field it must remember
    # to edit when E2 lands is one that will publish a falsehood instead.
    assert "conductor_consumer_attached" not in capability


def test_a_receipt_records_whether_quiescence_was_proven():
    donor_agent, recipient_agent, donor = _pair()
    proven = _deliver(_begin(donor, recipient_agent))
    unproven_evidence = _evidence("1", turn_state=th.TURN_UNKNOWN)
    assert th.receipt_digest(proven) != th.receipt_digest(
        {**proven, "quiescence_digest": th.digest(unproven_evidence.payload())}
    )
