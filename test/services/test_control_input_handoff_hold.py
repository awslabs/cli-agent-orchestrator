"""M3-E: a pending handback suspends a stable agent's task authority (cond-0381).

The hold lives at the top of ``provider_byte_admission`` and resolves its agent
from the **roster**, not from a managed-launch reservation. Both facts are
load-bearing and both are tested here.

A handback resumes the recipient's own native session through M3-B, and the
exact executor deliberately allocates a terminal id it has proven absent from
the reservation table -- so the recipient's pane is not "managed" and a
reservation-keyed hold placed after the ``managed`` early return would be inert
in exactly the flow the recipient-side hold exists for. The same is true of any
donor that was ever recovered.

These tests bind real roster incarnations rather than hand-building a resolved
identity, so a restored pane is modelled the way the executor actually leaves
one: a roster incarnation with no reservation behind it.
"""

from __future__ import annotations

import contextlib
import uuid

import pytest
from sqlalchemy import text

from cli_agent_orchestrator.services import control_input_contract as contract
from cli_agent_orchestrator.services import control_input_service as cis
from cli_agent_orchestrator.services import stable_agent_roster as roster
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
def _fence_entries(monkeypatch):
    """Record fence entry order so a test can prove the hold precedes it.

    The reason code's whole justification is that the refusal is decided before
    the first provider byte (`control_input_contract.py`, `handoff-held`). A
    fixture that only neutralises the fence cannot tell whether the hold runs
    before it, after it, or after a literal chunk -- so this one keeps the
    ordering observable.
    """
    from cli_agent_orchestrator.services import generation_fence

    entries: list[str] = []

    def _recording(name):
        @contextlib.contextmanager
        def _open(*_args, **_kwargs):
            entries.append(name)
            yield

        return _open

    monkeypatch.setattr(
        generation_fence, "managed_admission_critical_section", _recording("managed")
    )
    monkeypatch.setattr(generation_fence, "admission_critical_section", _recording("generation"))
    return entries


def _bind(agent_id, *, suffix="1"):
    """A real roster incarnation, the way any live worker has one."""
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
            generation=f"gen-{suffix}",
            pane_id=f"%{suffix}",
            pane_pid=5000 + int(suffix),
            process_identity={"pid": 5000 + int(suffix), "start_marker": f"marker-{suffix}"},
            execution_mode="native_tui",
            admitted=True,
        )
    )


def _resolved(*, suffix="1", managed=True, reservation_id="res-1"):
    return cis.ResolvedControlIdentity(
        terminal_id=f"term-{suffix}",
        terminal_incarnation=f"inc-{suffix}",
        terminal_generation=f"gen-{suffix}",
        provider="claude_code",
        native_session_id=f"native-{suffix}",
        execution_mode="native_tui",
        session_name=SESSION,
        pane_id=f"%{suffix}",
        managed=managed,
        managed_reservation_id=reservation_id,
    )


def _bind_reservation(monkeypatch, suffix="1"):
    """A managed pane: a reservation row exists behind the terminal."""
    from cli_agent_orchestrator.services import managed_launch_v2

    monkeypatch.setattr(
        managed_launch_v2,
        "get",
        lambda _rid: {
            "terminal_id": f"term-{suffix}",
            "generation": f"gen-{suffix}",
            "binding": {"attempt_id": "att-1", "fencing_token_id": "tok-1"},
        },
    )


def _held_pair(*, donor_suffix="1", recipient_suffix="2"):
    """A donor holding an open round, a dormant recipient, one pending handoff.

    Both are bound in the roster, because both are real workers; whether either
    also has a reservation is what the individual tests vary.
    """
    donor_agent = str(uuid.uuid4())
    recipient_agent = str(uuid.uuid4())
    donor_binding = _bind(donor_agent, suffix=donor_suffix)
    _bind(recipient_agent, suffix=recipient_suffix)

    donor = occ.open_occurrence(
        occ.OpenRequest(
            task_occurrence_id=str(uuid.uuid4()),
            session_name=SESSION,
            agent_id=donor_agent,
            round_index=0,
            dispatch_digest=_DIGEST_A,
            incarnation=occ.EffectIncarnation(
                incarnation_id=donor_binding["incarnation"]["incarnation_id"],
                terminal_id=f"term-{donor_suffix}",
                generation=f"gen-{donor_suffix}",
            ),
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
                incarnation_id=donor_binding["incarnation"]["incarnation_id"],
                terminal_id=f"term-{donor_suffix}",
                generation=f"gen-{donor_suffix}",
                turn_state=th.TURN_TERMINAL,
                observed_at=_OBSERVED_AT,
            ),
            initiated_by="supervisor",
            expected_donor_revision=0,
        )
    )
    return donor_agent, recipient_agent, handoff


def _admit(resolved, *, control_id=None):
    with cis.provider_byte_admission(
        resolved, resolved.terminal_id, resolved.terminal_generation, control_id=control_id
    ):
        return True


# ---------------------------------------------------------------------------
# the hold reaches the panes a handback actually runs on
# ---------------------------------------------------------------------------


def test_a_held_agent_is_refused_on_a_restored_pane():
    """The pane a handback's recipient always runs on.

    M3-B's exact executor allocates a terminal id proven absent from the
    reservation table, so the restored pane resolves as unmanaged. A hold that
    only consulted the reservation would admit task bytes to the recipient it
    had just held -- and to any donor that was ever recovered.
    """
    _donor, recipient_agent, _handoff = _held_pair()
    restored = _resolved(suffix="2", managed=False, reservation_id=None)

    with pytest.raises(th.TaskHandoffHeld) as caught:
        _admit(restored)
    assert "recipient of pending handoff" in str(caught.value)


def test_a_restored_donor_is_refused_too():
    donor_agent, _recipient, _handoff = _held_pair()
    restored = _resolved(suffix="1", managed=False, reservation_id=None)
    with pytest.raises(th.TaskHandoffHeld, match="donor of pending handoff"):
        _admit(restored)


def test_a_held_agent_is_refused_on_a_managed_pane(monkeypatch):
    donor_agent, _recipient, _handoff = _held_pair()
    _bind_reservation(monkeypatch, suffix="1")
    with pytest.raises(th.TaskHandoffHeld, match="donor of pending handoff"):
        _admit(_resolved(suffix="1"))


@pytest.mark.parametrize("side", [th.ROLE_DONOR, th.ROLE_RECIPIENT])
def test_a_settled_handoff_restores_managed_input_on_both_sides(side):
    donor_agent, recipient_agent, handoff = _held_pair()
    suffix = "1" if side == th.ROLE_DONOR else "2"
    th.rollback_handoff(handoff["handoff_id"], rolled_back_by="supervisor", reason="quota returned")
    assert _admit(_resolved(suffix=suffix, managed=False, reservation_id=None)) is True


def test_only_the_derived_packet_control_id_reaches_the_recipient():
    donor_agent, recipient_agent, handoff = _held_pair()
    packet = handoff["packet_control_id"]
    recipient = _resolved(suffix="2", managed=False, reservation_id=None)
    donor = _resolved(suffix="1", managed=False, reservation_id=None)

    assert _admit(recipient, control_id=packet) is True
    with pytest.raises(th.TaskHandoffHeld):
        _admit(recipient, control_id=str(uuid.uuid4()))
    with pytest.raises(th.TaskHandoffHeld):
        _admit(recipient)
    # The packet is not for the donor, so it is not the donor's exemption.
    with pytest.raises(th.TaskHandoffHeld):
        _admit(donor, control_id=packet)


def test_an_agent_with_no_pending_handoff_is_never_held():
    _held_pair()
    _bind(str(uuid.uuid4()), suffix="7")
    assert _admit(_resolved(suffix="7", managed=False, reservation_id=None)) is True


def test_a_terminal_with_no_roster_incarnation_is_not_held():
    # An agent with no incarnation cannot be a handoff party, because a handoff
    # names agents that hold or will hold a task occurrence. Not a bypass.
    _held_pair()
    assert _admit(_resolved(suffix="9", managed=False, reservation_id=None)) is True


# ---------------------------------------------------------------------------
# where the refusal sits, and what it says
# ---------------------------------------------------------------------------


def test_the_hold_is_decided_before_any_fence_is_entered(_fence_entries):
    """The zero-bytes proof the reason code's justification rests on.

    Observed as ordering rather than asserted from a constant table: moving the
    check below the fence, or after a literal chunk, has to fail this.
    """
    _donor, _recipient, _handoff = _held_pair()
    with pytest.raises(th.TaskHandoffHeld):
        _admit(_resolved(suffix="2", managed=False, reservation_id=None))
    assert _fence_entries == []

    # And an unheld pane still reaches the fence, so the empty list above is a
    # refusal rather than a fixture that never records anything.
    _bind(str(uuid.uuid4()), suffix="8")
    assert _admit(_resolved(suffix="8", managed=False, reservation_id=None)) is True
    assert _fence_entries == []  # unmanaged takes no fence
    _bind(str(uuid.uuid4()), suffix="6")
    assert _admit(_resolved(suffix="6", reservation_id=None)) is True
    assert _fence_entries == ["generation"]


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
    assert contract.outcome_for_reason(contract.REASON_HANDOFF_HELD) == contract.REFUSED
    assert contract.REFUSED in contract.REATTEMPTABLE_OUTCOMES


def test_an_unreadable_handback_table_refuses_as_undecidable_not_as_a_pending_handback(
    monkeypatch,
):
    """The table is there and could not be answered, so a pending row may exist.

    Still refused — admitting bytes to a held donor invalidates the packet
    digest its recipient is about to act on — but refused as undecidable. The
    reason code is the diagnosis an operator acts on, and ``handoff-held``
    would send them hunting a handback that nothing ever observed.
    """

    def _boom(*_args, **_kwargs):
        raise th.TaskHandoffUnavailable("store is gone")

    monkeypatch.setattr(th, "hold_for_agent", _boom)
    with pytest.raises(th.TaskHandoffHoldUndecidable) as raised:
        th.hold_refusal(str(uuid.uuid4()))
    assert "could not answer" in str(raised.value)
    assert cis._admission_refusal_reason(raised.value) == contract.REASON_HANDOFF_HOLD_UNDECIDABLE


def test_an_unreadable_roster_refuses_as_undecidable_not_as_a_pending_handback(monkeypatch):
    """A roster that raises leaves the pane's agent unnamed.

    That is the one case with no honest answer at all: the hold question cannot
    be asked, let alone answered "no". It stays fail-closed — and unlike a
    missing handback table it is not a small fault being amplified, since every
    managed lane reads the roster — but it is not a pending handback either.
    """
    from cli_agent_orchestrator.services import stable_agent_roster

    def _boom(*_args, **_kwargs):
        raise RuntimeError("roster is gone")

    monkeypatch.setattr(stable_agent_roster, "get_incarnation_by_terminal", _boom)
    with pytest.raises(th.TaskHandoffHoldUndecidable) as raised:
        th.hold_refusal_for_terminal("term-1", "gen-1")
    assert "could not name the agent" in str(raised.value)
    assert cis._admission_refusal_reason(raised.value) == contract.REASON_HANDOFF_HOLD_UNDECIDABLE


def test_a_store_with_no_handback_table_admits_instead_of_refusing_every_write(_db):
    """The operator's own live store, and the brittleness this closes.

    A pending handoff is a *row* in ``task_occurrence_handoffs``. No table means
    no rows, so no agent on this installation can be party to a handback —
    ``begin_handoff`` writes that table and could not have succeeded. Refusing
    here would cost every steer, tell and operator message in every session on
    a store whose only fault is that M3-E has not run on it yet, and protect
    nothing at all.
    """
    agent_id = str(uuid.uuid4())
    _bind(agent_id, suffix="1")
    with _db.begin() as conn:
        conn.execute(text("DROP TABLE task_occurrence_handoffs"))

    assert th.hold_refusal(agent_id) is None
    assert th.hold_refusal_for_terminal("term-1", "gen-1") is None
    # And the whole admission path still lets the operator steer that agent.
    assert _admit(_resolved(suffix="1", reservation_id=None)) is True


def test_a_shape_drifted_handback_table_is_undecidable_rather_than_handoff_held(_db):
    """cond-0433's own failure mode, end to end and unmocked.

    A table at a shape the ORM cannot read makes every hold query raise. Before
    this repair that surfaced as ``handoff-held`` on every managed write on the
    installation, asserting a handback was pending for agents party to none.
    """
    agent_id = str(uuid.uuid4())
    _bind(agent_id, suffix="1")
    with _db.begin() as conn:
        conn.execute(text("DROP TABLE task_occurrence_handoffs"))
        conn.execute(
            text(
                "CREATE TABLE task_occurrence_handoffs ("
                "handoff_id TEXT NOT NULL PRIMARY KEY, state TEXT NOT NULL, "
                "from_agent_id TEXT, to_agent_id TEXT)"
            )
        )

    with pytest.raises(th.TaskHandoffHoldUndecidable) as raised:
        th.hold_refusal(agent_id)
    reason = cis._admission_refusal_reason(raised.value)
    assert reason == contract.REASON_HANDOFF_HOLD_UNDECIDABLE
    assert reason != contract.REASON_HANDOFF_HELD
    # The refusal still reaches the caller through the admission seam, typed,
    # rather than escaping as an untyped failure on a lane whose whole contract
    # is that every path produces a typed outcome.
    with pytest.raises(th.TaskHandoffHeld):
        _admit(_resolved(suffix="1", reservation_id=None))


def test_a_corrupt_store_file_fails_closed_as_undecidable_not_as_an_untyped_error(
    tmp_path, monkeypatch
):
    """A torn store file is not an ``OperationalError``, so it escaped unwrapped.

    "file is not a database" raises ``sqlalchemy.exc.DatabaseError``, which is
    not an ``OperationalError`` subclass — so ``_with_session`` used to pass it
    through raw: past ``hold_refusal``'s typed catch, past the probe that
    answers present-or-unknown, and past the admission seam's
    ``except (FencedError, TaskHandoffHeld)`` at every call site. Every managed
    write on the installation then surfaced an untyped 500 with a SQLAlchemy
    traceback, on the one seam whose contract is that every path produces a
    typed outcome. It must arrive as ``handoff-hold-undecidable``.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from cli_agent_orchestrator.clients import database

    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"this is not a sqlite database file" * 128)
    engine = create_engine(f"sqlite:///{corrupt}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(
        database, "SessionLocal", sessionmaker(autocommit=False, autoflush=False, bind=engine)
    )

    with pytest.raises(th.TaskHandoffHoldUndecidable) as raised:
        th.hold_refusal(str(uuid.uuid4()))
    assert "could not answer" in str(raised.value)
    reason = cis._admission_refusal_reason(raised.value)
    assert reason == contract.REASON_HANDOFF_HOLD_UNDECIDABLE
    engine.dispose()


def test_the_no_handback_table_admission_leaves_a_trace(_db, caplog):
    """The admit path is a deliberate decision, so it must be observable.

    No table only proves no pending handoff is recoverable *from this file*.
    An operator who restored a pre-M3-E backup while a handback was pending
    would be admitted past this hold with the pending row gone, and the first
    typed sign of the duplicate authority would be ``complete_handoff``
    failing at settlement. A warning here is the difference between a
    diagnosable admission and an invisible one.
    """
    import logging

    agent_id = str(uuid.uuid4())
    _bind(agent_id, suffix="1")
    with _db.begin() as conn:
        conn.execute(text("DROP TABLE task_occurrence_handoffs"))

    with caplog.at_level(logging.WARNING):
        assert th.hold_refusal(agent_id) is None
    admissions = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and "no handback table" in record.message
    ]
    assert admissions, (
        "the hold admitted on a store with no handback table and left no trace; "
        f"captured: {[(r.levelname, r.message) for r in caplog.records]}"
    )


def test_the_undecidable_reason_is_a_first_class_refusal_in_both_vocabularies():
    """It has to be, or an operator message carrying it raises on construction.

    ``OperatorMessageOutcome`` validates its reason against its own map, so a
    reason present in the control contract but missing there would turn a
    refusal into an unhandled ``ValueError`` — a worse outage than the
    misdiagnosis this reason exists to fix.
    """
    from cli_agent_orchestrator.services import operator_message_service as oms

    assert contract.REASON_HANDOFF_HOLD_UNDECIDABLE != contract.REASON_HANDOFF_HELD
    assert contract.outcome_for_reason(contract.REASON_HANDOFF_HOLD_UNDECIDABLE) == contract.REFUSED
    assert contract.REFUSED in contract.REATTEMPTABLE_OUTCOMES
    assert oms._REASON_OUTCOMES[contract.REASON_HANDOFF_HOLD_UNDECIDABLE] == contract.REFUSED
    # A subclass of the hold, so every existing admission caller still catches
    # it; a sibling would have escaped as an untyped 500.
    assert issubclass(th.TaskHandoffHoldUndecidable, th.TaskHandoffHeld)


# ---------------------------------------------------------------------------
# the reason a v2 admit needs no hold of its own
# ---------------------------------------------------------------------------


def test_a_v2_reservation_mints_a_fresh_stable_agent_so_it_can_never_be_held():
    """Why `managed_launch_v2`'s admit lane carries no hold.

    The declared guard would have been dead code: a reservation's stable agent
    id is minted fresh at reserve time and takes no caller input, so it can
    never equal an agent that holds or will hold an occurrence. Pinned as a
    fact rather than left as prose, because an implementer who later makes that
    column caller-supplied or reusable opens the lane silently.
    """
    import ast
    import inspect

    from cli_agent_orchestrator.services import managed_launch_v2

    tree = ast.parse(inspect.getsource(managed_launch_v2))
    assignments = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.keyword) and node.arg == "stable_agent_id"
    ]
    assert assignments, "stable_agent_id is no longer assigned by keyword; re-verify this reason"
    for node in assignments:
        # `str(uuid.uuid4())` and nothing else. A caller-supplied or reused id
        # would put a reservation agent into the same population as a handoff
        # party, and the v2 admit lane would need the hold this PR omits.
        rendered = ast.unparse(node.value)
        assert rendered == "str(uuid.uuid4())", (
            f"stable_agent_id is assigned {rendered!r}; if a reservation can now name an "
            "existing agent, the managed-launch admit lane needs a handoff hold"
        )

    # And the consequence the omission rests on: a fresh uuid is never held.
    assert th.hold_refusal(str(uuid.uuid4())) is None


def test_a_generationless_restored_pane_is_held_too():
    """cond-0413: the hold is one of the fences an unmanaged row is left with.

    A legacy row records no generation at all (``TerminalModel.generation`` is
    nullable), and such a row is unmanaged by construction. The hold resolves
    the pane's agent from the roster's unique live incarnation when no exact
    generation is supplied, so it still names the party and still refuses --
    which is why admitting a generation-less unmanaged write removes nothing.
    """
    _donor, _recipient, _handoff = _held_pair()
    legacy = cis.ResolvedControlIdentity(
        terminal_id="term-2",
        terminal_incarnation=None,
        terminal_generation=None,
        provider="claude_code",
        native_session_id="native-2",
        execution_mode="native_tui",
        session_name=SESSION,
        pane_id="%2",
        managed=False,
        managed_reservation_id=None,
    )
    with pytest.raises(th.TaskHandoffHeld, match="recipient of pending handoff"):
        _admit(legacy)
