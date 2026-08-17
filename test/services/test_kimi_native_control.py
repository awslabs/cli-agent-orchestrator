"""Provider-native control of a Kimi TUI: queue, steer, and slash input.

Covers the acceptance matrix row for native human-surface control. The
positives are the three operations landing correctly; the negatives are
the ones that make the feature safe rather than merely working — a queue
entering an active turn is refused, a steer cannot replay or blind-retry,
a tmux write is never read as provider acceptance, and an unresolved
response loss blocks the session instead of being resent.

Two properties are asserted almost everywhere and are worth naming once.
*Zero bytes on refusal*: a refused operation must leave the transport
untouched, because a refusal that already typed something is not a
refusal. *Posted is not accepted*: reaching the transport moves an
operation to ``posted`` and no further, so nothing in this module can
reach ``accepted`` without an observation naming that exact operation.
"""

from __future__ import annotations

import threading

import pytest

from cli_agent_orchestrator.services import execution_mode as em
from cli_agent_orchestrator.services import kimi_native_control as knc
from cli_agent_orchestrator.services import native_attachment as na
from cli_agent_orchestrator.services.canonical_json import canonical_sha256

SESSION = "session_9f21ac30"
TERMINAL = "terminal_4d7b"
GENERATION = "gen_1c0e"
OTHER_SESSION = "session_becc4471"


@pytest.fixture(autouse=True)
def _db(isolated_memory_db):
    """Every test gets its own initialized database."""
    return isolated_memory_db


class Recorder:
    """A transport that records exactly what it was asked to do.

    The call log is the evidence for both no-duplicate and zero-bytes
    assertions: ``literal:`` entries carry the exact payload, and
    ``enter`` appears as its own entry so a submit can never hide inside
    a payload.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def send_literal(self, text: str) -> None:
        self.calls.append(f"literal:{text}")

    def send_enter(self) -> None:
        self.calls.append("enter")

    def send_key(self, keystroke: str) -> None:
        self.calls.append(f"key:{keystroke}")


class FailsOnLiteral(Recorder):
    def send_literal(self, text: str) -> None:
        self.calls.append(f"literal:{text}")
        raise OSError("pane vanished mid-write")


class FailsOnEnter(Recorder):
    def send_enter(self) -> None:
        self.calls.append("enter")
        raise OSError("pane vanished before submit")


def _attach(
    *,
    session: str = SESSION,
    terminal_id: str = TERMINAL,
    generation: str = GENERATION,
    execution_mode: str = em.NATIVE_TUI,
) -> dict:
    """Take one session all the way to attached, owned by this terminal."""
    intent = na.acquire_intent(
        acquisition_method=na.ACQUISITION_ACP_BOOTSTRAP,
        acquisition_receipt={"kind": "kimi-acp-session-new", "session_id": session},
        admits_only_new_instructions=True,
        replays_task_bytes=False,
        bootstrap_sent_no_turn=True,
        bootstrap_detached_before_launch=True,
    )
    owner = {
        "provider": knc.PROVIDER,
        "native_session_id": session,
        "terminal_id": terminal_id,
        "generation": generation,
        "execution_mode": execution_mode,
    }
    na.declare(**owner, intent=intent, pane_id="%7")
    na.mark_starting(**owner, pane_id="%7")
    return na.mark_attached(
        **owner,
        pane_id="%7",
        process_identity=na.process_identity(pid=4242, start_marker="88213"),
    )


def _idle() -> dict:
    return knc.turn_observation(
        active_turn_id=None,
        observed_at="2026-07-24T00:00:00Z",
        observer="status_monitor",
    )


def _busy(turn_id: str = "turn_a1") -> dict:
    return knc.turn_observation(
        active_turn_id=turn_id,
        observed_at="2026-07-24T00:00:01Z",
        observer="status_monitor",
    )


def _evidence(operation_id: str, *, entered_turn_id: str | None = None) -> dict:
    return knc.provider_observation(
        operation_id=operation_id,
        observed_at="2026-07-24T00:00:02Z",
        observer="status_monitor",
        entered_turn_id=entered_turn_id,
        evidence={"transcript_marker": "composer accepted"},
    )


def _queue(
    transport: Recorder,
    *,
    operation_id: str = "op_queue_1",
    session: str = SESSION,
    terminal_id: str = TERMINAL,
    generation: str = GENERATION,
    execution_mode: str = em.NATIVE_TUI,
    text: str = "please also update the changelog",
    observation: dict | None = None,
    provider_version: str | None = None,
) -> dict:
    return knc.queue(
        operation_id=operation_id,
        native_session_id=session,
        terminal_id=terminal_id,
        generation=generation,
        execution_mode=execution_mode,
        text=text,
        observation=_idle() if observation is None else observation,
        transport=transport,
        provider_version=provider_version,
    )


def _steer(
    transport: Recorder,
    *,
    operation_id: str = "op_steer_1",
    turn_id: str = "turn_a1",
    text: str = "stop and summarize what you have",
    observation: dict | None = None,
    provider_version: str | None = None,
) -> dict:
    return knc.steer(
        operation_id=operation_id,
        native_session_id=SESSION,
        terminal_id=TERMINAL,
        generation=GENERATION,
        execution_mode=em.NATIVE_TUI,
        turn_id=turn_id,
        text=text,
        observation=_busy() if observation is None else observation,
        transport=transport,
        provider_version=provider_version,
    )


def _control(
    transport: Recorder,
    *,
    operation_id: str = "op_control_1",
    command: str = knc.CONTROL_COMPACT,
    advertised: list[str] | None = None,
    observation: dict | None = None,
) -> dict:
    return knc.control(
        operation_id=operation_id,
        native_session_id=SESSION,
        terminal_id=TERMINAL,
        generation=GENERATION,
        execution_mode=em.NATIVE_TUI,
        command=command,
        advertised_commands=[knc.CONTROL_COMPACT] if advertised is None else advertised,
        observation=_idle() if observation is None else observation,
        transport=transport,
    )


# --------------------------------------------------------------------------
# The three operations, on their happy paths
# --------------------------------------------------------------------------


def test_an_idle_queue_is_posted_and_goes_no_further():
    _attach()
    transport = Recorder()

    record = _queue(transport)

    assert record["kind"] == knc.KIND_QUEUE
    assert record["state"] == knc.POSTED
    assert record["posted"] is True
    assert record["posted_at"] is not None
    assert transport.calls == ["literal:please also update the changelog", "enter"]
    # The whole point of the state: bytes reached a terminal, and that is
    # all anyone may conclude from it.
    assert record["provider_accepted"] is False
    assert record["provider_completed"] is False
    assert record["is_resolved"] is False


def test_a_steer_binds_to_the_exact_running_turn():
    _attach()
    transport = Recorder()

    record = _steer(transport)

    assert record["kind"] == knc.KIND_STEER
    assert record["state"] == knc.POSTED
    assert record["turn_id"] == "turn_a1"
    assert record["intent"]["turn_observation"]["active_turn_id"] == "turn_a1"
    assert transport.calls == ["literal:stop and summarize what you have", "enter"]


def test_an_advertised_slash_command_is_typed_literally_then_submitted():
    _attach()
    transport = Recorder()

    record = _control(transport)

    assert record["kind"] == knc.KIND_CONTROL
    assert record["state"] == knc.POSTED
    # The command carries no newline of its own; Enter is a separate act.
    assert transport.calls == [f"literal:{knc.CONTROL_COMPACT}", "enter"]
    assert record["transport"]["enter_sent_separately"] is True


def test_queue_and_steer_are_separate_kinds_with_separate_evidence():
    """Distinct operations, not one 'send text' with a flag."""
    _attach()
    queued = _queue(Recorder(), operation_id="op_q")
    steered = _steer(Recorder(), operation_id="op_s")

    assert queued["kind"] != steered["kind"]
    assert queued["turn_id"] is None
    assert steered["turn_id"] == "turn_a1"


# --------------------------------------------------------------------------
# A pane write is never provider acceptance
# --------------------------------------------------------------------------


def test_acceptance_requires_an_observation_naming_the_operation():
    _attach()
    record = _queue(Recorder())
    assert record["provider_accepted"] is False

    accepted = knc.record_observation(
        operation_id="op_queue_1",
        observation=_evidence("op_queue_1", entered_turn_id="turn_b2"),
        outcome=knc.ACCEPTED,
    )

    assert accepted["state"] == knc.ACCEPTED
    assert accepted["provider_accepted"] is True
    assert accepted["provider_completed"] is False
    assert accepted["observation"]["entered_turn_id"] == "turn_b2"


def test_completion_is_a_separate_fact_from_acceptance():
    _attach()
    _queue(Recorder())
    knc.record_observation(
        operation_id="op_queue_1",
        observation=_evidence("op_queue_1"),
        outcome=knc.ACCEPTED,
    )

    completed = knc.record_observation(
        operation_id="op_queue_1",
        observation=_evidence("op_queue_1"),
        outcome=knc.COMPLETED,
    )

    assert completed["state"] == knc.COMPLETED
    assert completed["provider_completed"] is True
    assert completed["is_resolved"] is True


def test_evidence_for_another_operation_cannot_resolve_this_one():
    _attach()
    _queue(Recorder())

    with pytest.raises(knc.NativeControlInvalid, match="names operation"):
        knc.record_observation(
            operation_id="op_queue_1",
            observation=_evidence("op_somebody_else"),
            outcome=knc.ACCEPTED,
        )

    assert knc.get("op_queue_1")["state"] == knc.POSTED


def test_a_provider_refusal_is_recorded_as_the_providers_answer():
    _attach()
    _queue(Recorder())

    refused = knc.record_observation(
        operation_id="op_queue_1",
        observation=_evidence("op_queue_1"),
        outcome=knc.REFUSED,
    )

    assert refused["state"] == knc.REFUSED
    assert refused["refusal_reason"] == knc.REFUSED_PROVIDER
    assert refused["provider_accepted"] is False


def test_a_resolved_operation_cannot_be_observed_again():
    _attach()
    _queue(Recorder())
    knc.record_observation(
        operation_id="op_queue_1",
        observation=_evidence("op_queue_1"),
        outcome=knc.COMPLETED,
    )

    with pytest.raises(knc.NativeControlConflict):
        knc.record_observation(
            operation_id="op_queue_1",
            observation=_evidence("op_queue_1"),
            outcome=knc.ACCEPTED,
        )


# --------------------------------------------------------------------------
# Gating: the negatives that keep the two operations distinct
# --------------------------------------------------------------------------


def test_a_queue_entering_an_active_turn_is_refused_with_zero_bytes():
    _attach()
    transport = Recorder()

    record = _queue(transport, observation=_busy("turn_running"))

    assert record["state"] == knc.REFUSED
    assert record["refusal_reason"] == knc.REFUSED_ACTIVE_TURN
    assert record["posted"] is False
    assert transport.calls == []


def test_a_steer_with_nothing_running_is_refused_not_downgraded():
    """An idle steer is not quietly turned into ordinary follow-up."""
    _attach()
    transport = Recorder()

    record = _steer(transport, observation=_idle())

    assert record["state"] == knc.REFUSED
    assert record["refusal_reason"] == knc.REFUSED_NO_ACTIVE_TURN
    assert transport.calls == []


def test_a_steer_whose_turn_already_ended_is_refused():
    """Without this the steer would arrive inside whatever turn came next."""
    _attach()
    transport = Recorder()

    record = _steer(transport, turn_id="turn_intended", observation=_busy("turn_that_replaced_it"))

    assert record["state"] == knc.REFUSED
    assert record["refusal_reason"] == knc.REFUSED_TURN_MISMATCH
    assert transport.calls == []


def test_an_observation_that_was_never_made_cannot_satisfy_the_idle_gate():
    _attach()
    with pytest.raises(knc.NativeControlInvalid, match="active_turn_id"):
        knc.queue(
            operation_id="op_no_look",
            native_session_id=SESSION,
            terminal_id=TERMINAL,
            generation=GENERATION,
            execution_mode=em.NATIVE_TUI,
            text="hello",
            observation={"schema": knc.TURN_OBSERVATION_SCHEMA, "observer": "guesswork"},
            transport=Recorder(),
        )
    assert knc.get("op_no_look") is None


def test_a_foreign_observation_schema_is_refused():
    _attach()
    with pytest.raises(knc.NativeControlInvalid, match="schema"):
        _queue(Recorder(), observation={"schema": "something-else", "active_turn_id": None})


# --------------------------------------------------------------------------
# Capability gating for slash controls
# --------------------------------------------------------------------------


def test_an_unadvertised_control_is_refused_with_zero_bytes():
    """Including /compact: an unsupported slash line posts as chat text."""
    _attach()
    transport = Recorder()

    record = _control(transport, command=knc.CONTROL_COMPACT, advertised=["/model"])

    assert record["state"] == knc.REFUSED
    assert record["refusal_reason"] == knc.REFUSED_UNSUPPORTED_CONTROL
    assert transport.calls == []


def test_an_advertised_route_control_needs_no_constant_in_this_module():
    _attach()
    transport = Recorder()

    record = _control(transport, command="/model", advertised=["/model", knc.CONTROL_COMPACT])

    assert record["state"] == knc.POSTED
    assert transport.calls == ["literal:/model", "enter"]


def test_ordinary_text_cannot_be_smuggled_through_the_control_path():
    _attach()
    transport = Recorder()

    with pytest.raises(knc.NativeControlInvalid, match="does not start with"):
        _control(transport, command="just some text", advertised=["just some text"])

    assert knc.get("op_control_1") is None
    assert transport.calls == []


def test_a_bare_string_is_not_a_capability_list():
    """A string would pass the membership test for every one of its prefixes."""
    _attach()
    with pytest.raises(knc.NativeControlInvalid, match="not a single string"):
        knc.control(
            operation_id="op_bad_caps",
            native_session_id=SESSION,
            terminal_id=TERMINAL,
            generation=GENERATION,
            execution_mode=em.NATIVE_TUI,
            command="/comp",
            advertised_commands="/compact",  # type: ignore[arg-type]
            observation=_idle(),
            transport=Recorder(),
        )
    assert knc.get("op_bad_caps") is None


# --------------------------------------------------------------------------
# Identity binding
# --------------------------------------------------------------------------


def test_acp_sessions_are_not_controlled_by_typing_into_a_pane():
    _attach()
    transport = Recorder()

    with pytest.raises(knc.NativeControlInvalid, match=em.NATIVE_TUI):
        _queue(transport, execution_mode=em.ACP)

    assert knc.get("op_queue_1") is None
    assert transport.calls == []


@pytest.mark.parametrize("mode", ["", "native", "NATIVE_TUI", "tui", None])
def test_a_mode_outside_the_closed_set_never_reaches_the_transport(mode):
    _attach()
    transport = Recorder()

    with pytest.raises(knc.NativeControlInvalid):
        _queue(transport, execution_mode=mode)

    assert transport.calls == []


def test_an_operation_minted_for_a_replaced_generation_is_refused():
    """The pane that replaced it must not receive the previous owner's input."""
    _attach(generation="gen_current")
    transport = Recorder()

    record = _queue(transport, generation="gen_previous")

    assert record["state"] == knc.REFUSED
    assert record["refusal_reason"] == knc.REFUSED_ATTACHMENT
    assert transport.calls == []


def test_another_terminals_ownership_is_refused_rather_than_borrowed():
    _attach(terminal_id="terminal_owner")
    transport = Recorder()

    record = _queue(transport, terminal_id="terminal_intruder")

    assert record["state"] == knc.REFUSED
    assert record["refusal_reason"] == knc.REFUSED_ATTACHMENT
    assert transport.calls == []


def test_a_session_that_is_not_yet_attached_accepts_no_control_input():
    na.declare(
        provider=knc.PROVIDER,
        native_session_id=SESSION,
        terminal_id=TERMINAL,
        generation=GENERATION,
        execution_mode=em.NATIVE_TUI,
        pane_id="%7",
        intent=na.acquire_intent(
            acquisition_method=na.ACQUISITION_RESUME,
            acquisition_receipt={"kind": "kimi-session-resume", "session_id": SESSION},
            admits_only_new_instructions=True,
            replays_task_bytes=False,
        ),
    )
    transport = Recorder()

    record = _queue(transport)

    assert record["state"] == knc.REFUSED
    assert record["refusal_reason"] == knc.REFUSED_ATTACHMENT
    assert transport.calls == []


def test_an_unclaimed_session_accepts_no_control_input():
    transport = Recorder()

    record = _queue(transport)

    assert record["state"] == knc.REFUSED
    assert record["refusal_reason"] == knc.REFUSED_ATTACHMENT
    assert transport.calls == []


# --------------------------------------------------------------------------
# Artifact-free payloads
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        "\x1b[200~pasted\x1b[201~",  # bracketed-paste sentinels
        "\x1b[A",  # a cursor key, which would move the composer
        "carriage\rreturn",
    ],
    ids=["bracketed-paste", "cursor-key", "carriage-return"],
)
def test_artifact_bearing_payloads_are_refused_not_sanitized(payload):
    """Refusing beats stripping: stripping changes the message silently."""
    _attach()
    transport = Recorder()

    with pytest.raises(knc.NativeControlInvalid):
        _queue(transport, text=payload)

    assert knc.get("op_queue_1") is None
    assert transport.calls == []


def test_the_escape_class_is_refused_wherever_it_appears():
    assert knc.assert_artifact_free("plain text") == "plain text"
    for payload in ("\x1bleading", "trailing\x1b", "mid\x1bdle"):
        with pytest.raises(knc.NativeControlInvalid):
            knc.assert_artifact_free(payload)


def test_a_steer_payload_is_held_to_the_same_rule():
    _attach()
    transport = Recorder()

    with pytest.raises(knc.NativeControlInvalid):
        _steer(transport, text="interrupt\rand stop")

    assert transport.calls == []


# --------------------------------------------------------------------------
# Intent is durable before any effect
# --------------------------------------------------------------------------


def test_the_intent_is_already_journaled_when_the_first_byte_is_written():
    """A crash between the decision and the keystroke stays adjudicable."""
    _attach()
    seen: dict = {}

    class ObservingTransport(Recorder):
        def send_literal(self, text: str) -> None:
            seen["at_write_time"] = knc.get("op_intent_first")
            super().send_literal(text)

    _queue(ObservingTransport(), operation_id="op_intent_first")

    assert seen["at_write_time"] is not None
    assert seen["at_write_time"]["state"] == knc.INTENDED
    assert seen["at_write_time"]["posted"] is False
    assert seen["at_write_time"]["intent"]["schema"] == knc.INTENT_SCHEMA


def test_the_intent_records_the_binding_and_the_observation_it_acted_on():
    _attach()
    record = _steer(Recorder(), operation_id="op_intent_detail")

    intent = record["intent"]
    assert intent["kind"] == knc.KIND_STEER
    assert intent["provider"] == knc.PROVIDER
    assert intent["native_session_id"] == SESSION
    assert intent["terminal_id"] == TERMINAL
    assert intent["generation"] == GENERATION
    assert intent["execution_mode"] == em.NATIVE_TUI
    assert intent["turn_id"] == "turn_a1"
    assert intent["payload_sha256"] == record["payload_sha256"]
    assert intent["turn_observation"]["observer"] == "status_monitor"


# --------------------------------------------------------------------------
# Response loss: ambiguity, never a retry
# --------------------------------------------------------------------------


def test_a_failure_writing_the_payload_is_ambiguous_not_failed():
    """A raised transport does not prove the bytes did not land."""
    _attach()
    transport = FailsOnLiteral()

    record = _queue(transport)

    assert record["state"] == knc.AMBIGUOUS
    assert record["posted"] is False
    assert record["provider_accepted"] is False
    assert record["is_resolved"] is False
    assert "typing line 1 of 1" in record["ambiguity_reason"]


def test_a_failure_at_the_enter_boundary_is_its_own_ambiguity():
    """Typed but unsubmitted is a real state: the composer may hold text."""
    _attach()
    transport = FailsOnEnter()

    record = _queue(transport)

    assert record["state"] == knc.AMBIGUOUS
    assert transport.calls == ["literal:please also update the changelog", "enter"]
    assert "composer may hold unsubmitted text" in record["ambiguity_reason"]


def test_an_unresolved_ambiguity_blocks_the_session():
    _attach()
    _queue(FailsOnLiteral(), operation_id="op_lost")

    later = Recorder()
    record = _queue(later, operation_id="op_after", text="anything at all")

    assert record["state"] == knc.REFUSED
    assert record["refusal_reason"] == knc.REFUSED_UNRESOLVED_AMBIGUITY
    assert later.calls == []


def test_the_blocking_operation_can_be_named_before_being_refused():
    _attach()
    _queue(FailsOnLiteral(), operation_id="op_lost")

    blocking = knc.unresolved_ambiguity(SESSION)

    assert blocking["operation_id"] == "op_lost"
    assert blocking["state"] == knc.AMBIGUOUS


def test_an_ambiguity_blocks_only_its_own_session():
    _attach()
    _attach(session=OTHER_SESSION)
    _queue(FailsOnLiteral(), operation_id="op_lost")

    transport = Recorder()
    record = _queue(transport, operation_id="op_other_session", session=OTHER_SESSION)

    assert record["state"] == knc.POSTED
    assert transport.calls == ["literal:please also update the changelog", "enter"]


def test_reconciling_resolves_the_ambiguity_and_unblocks_the_session():
    _attach()
    _queue(FailsOnLiteral(), operation_id="op_lost")

    resolved = knc.reconcile(
        operation_id="op_lost",
        observation=_evidence("op_lost"),
        outcome=knc.COMPLETED,
    )
    assert resolved["state"] == knc.COMPLETED
    assert knc.unresolved_ambiguity(SESSION) is None

    transport = Recorder()
    assert _queue(transport, operation_id="op_after")["state"] == knc.POSTED


def test_reconciling_needs_evidence_naming_that_exact_operation():
    """Otherwise a generic 'the provider looks fine' would clear anything."""
    _attach()
    _queue(FailsOnLiteral(), operation_id="op_lost")

    with pytest.raises(knc.NativeControlInvalid, match="names operation"):
        knc.reconcile(
            operation_id="op_lost",
            observation=_evidence("op_unrelated"),
            outcome=knc.COMPLETED,
        )

    assert knc.get("op_lost")["state"] == knc.AMBIGUOUS


def test_reconciling_sends_nothing():
    """The lawful exit from ambiguity takes evidence, not another attempt."""
    _attach()
    transport = FailsOnLiteral()
    _queue(transport, operation_id="op_lost")
    calls_before = list(transport.calls)

    knc.reconcile(
        operation_id="op_lost",
        observation=_evidence("op_lost"),
        outcome=knc.REFUSED,
    )

    assert transport.calls == calls_before


def test_only_an_ambiguous_operation_can_be_reconciled():
    _attach()
    _queue(Recorder(), operation_id="op_fine")

    with pytest.raises(knc.NativeControlConflict):
        knc.reconcile(
            operation_id="op_fine",
            observation=_evidence("op_fine"),
            outcome=knc.COMPLETED,
        )


def test_reporting_the_same_ambiguity_twice_is_not_a_conflict():
    _attach()
    _queue(FailsOnLiteral(), operation_id="op_lost")

    again = knc.mark_ambiguous(operation_id="op_lost", reason="still cannot tell")

    assert again["state"] == knc.AMBIGUOUS
    assert again["ambiguity_reason"] == "still cannot tell"


def test_a_resolved_operation_cannot_be_reopened_as_ambiguous():
    _attach()
    _queue(Recorder(), operation_id="op_done")
    knc.record_observation(
        operation_id="op_done",
        observation=_evidence("op_done"),
        outcome=knc.COMPLETED,
    )

    with pytest.raises(knc.NativeControlConflict, match="already resolved"):
        knc.mark_ambiguous(operation_id="op_done", reason="second-guessing")


def test_marking_an_unknown_operation_ambiguous_is_not_found():
    with pytest.raises(knc.NativeControlNotFound):
        knc.mark_ambiguous(operation_id="op_never_existed", reason="who knows")


# --------------------------------------------------------------------------
# At-most-once: a replay is free, a duplicate is impossible
# --------------------------------------------------------------------------


def test_replaying_an_identical_operation_types_nothing_a_second_time():
    _attach()
    transport = Recorder()

    first = _queue(transport, operation_id="op_replay")
    second = _queue(transport, operation_id="op_replay")

    assert transport.calls == ["literal:please also update the changelog", "enter"]
    assert second["state"] == first["state"] == knc.POSTED
    assert second["posted_at"] == first["posted_at"]
    assert second["epoch"] == first["epoch"]


def test_replaying_an_ambiguous_operation_is_not_a_retry():
    """The one action that could turn uncertainty into a duplicate."""
    _attach()
    transport = FailsOnLiteral()
    _queue(transport, operation_id="op_lost")
    calls_after_first = list(transport.calls)

    replay = _queue(transport, operation_id="op_lost")

    assert replay["state"] == knc.AMBIGUOUS
    assert transport.calls == calls_after_first


def test_replaying_a_refused_operation_does_not_re_ask():
    _attach()
    transport = Recorder()
    _queue(transport, operation_id="op_refused", observation=_busy("turn_running"))

    replay = _queue(transport, operation_id="op_refused", observation=_busy("turn_running"))

    assert replay["state"] == knc.REFUSED
    assert transport.calls == []


def test_reusing_an_id_for_different_bytes_is_a_conflict():
    _attach()
    transport = Recorder()
    _queue(transport, operation_id="op_id", text="the original message")

    with pytest.raises(knc.NativeControlConflict, match="payload_sha256"):
        _queue(transport, operation_id="op_id", text="a different message")

    assert transport.calls == ["literal:the original message", "enter"]


def test_reusing_an_id_across_kinds_is_a_conflict():
    _attach()
    _queue(Recorder(), operation_id="op_shared")

    with pytest.raises(knc.NativeControlConflict, match="kind"):
        _steer(Recorder(), operation_id="op_shared")


def test_reusing_an_id_for_a_different_session_is_a_conflict():
    _attach()
    _attach(session=OTHER_SESSION)
    _queue(Recorder(), operation_id="op_shared")

    with pytest.raises(knc.NativeControlConflict, match="native_session_id"):
        _queue(Recorder(), operation_id="op_shared", session=OTHER_SESSION)


def test_reusing_a_steer_id_for_a_different_turn_is_a_conflict():
    """Otherwise a steer written for one turn could be redirected at another."""
    _attach()
    _steer(Recorder(), operation_id="op_steer", turn_id="turn_a1")

    with pytest.raises(knc.NativeControlConflict, match="turn_id"):
        _steer(
            Recorder(),
            operation_id="op_steer",
            turn_id="turn_a2",
            observation=_busy("turn_a2"),
        )


# --------------------------------------------------------------------------
# Concurrency
# --------------------------------------------------------------------------


def test_a_state_change_during_the_write_is_not_overwritten():
    """A competing observer freezing the row wins over a late post."""
    _attach()

    class RacingTransport(Recorder):
        def send_enter(self) -> None:
            # Another actor concludes the response was lost while this
            # write is still in flight.
            knc.mark_ambiguous(operation_id="op_raced", reason="observer saw the pane die")
            super().send_enter()

    with pytest.raises(knc.NativeControlConflict):
        _queue(RacingTransport(), operation_id="op_raced")

    assert knc.get("op_raced")["state"] == knc.AMBIGUOUS


def test_concurrent_callers_of_one_operation_id_post_exactly_once():
    _attach()
    transport = Recorder()
    lock = threading.Lock()
    barrier = threading.Barrier(4)
    outcomes: list[object] = []

    class SerializedTransport(Recorder):
        def send_literal(self, text: str) -> None:
            with lock:
                transport.send_literal(text)

        def send_enter(self) -> None:
            with lock:
                transport.send_enter()

    def attempt() -> None:
        barrier.wait()
        try:
            outcomes.append(_queue(SerializedTransport(), operation_id="op_concurrent"))
        except knc.NativeControlError as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # The invariant under contention: at most one caller ever types.
    # Fail-closed errors from the losers are acceptable; a second write
    # is not.
    assert transport.calls.count("literal:please also update the changelog") == 1
    assert transport.calls.count("enter") == 1
    assert knc.get("op_concurrent")["state"] == knc.POSTED
    assert any(isinstance(outcome, dict) for outcome in outcomes)


def test_a_lost_update_is_caught_by_the_epoch_and_not_silently_applied(monkeypatch):
    """The state guard alone is not enough; the write itself must be a CAS.

    Interleaved deterministically: the competing writer fires from inside
    the timestamp call, which happens after the state check has already
    passed on a now-stale read. Only the epoch predicate on the UPDATE can
    catch this, so the test fails if that predicate is dropped.
    """
    _attach()
    _queue(Recorder(), operation_id="op_stale")

    fired: list[str] = []
    real_now = knc._now

    def _now_with_a_competing_writer() -> str:
        # Fires exactly once, and re-entrantly delegates: the competing
        # write needs a timestamp of its own.
        if not fired:
            fired.append("once")
            knc.mark_ambiguous(operation_id="op_stale", reason="another observer got there first")
        return real_now()

    monkeypatch.setattr(knc, "_now", _now_with_a_competing_writer)

    with pytest.raises(knc.NativeControlConflict, match="concurrent modification"):
        knc.record_observation(
            operation_id="op_stale",
            observation=_evidence("op_stale"),
            outcome=knc.ACCEPTED,
        )

    assert fired == ["once"]
    assert knc.get("op_stale")["state"] == knc.AMBIGUOUS


def test_two_sessions_are_controlled_independently():
    _attach()
    _attach(session=OTHER_SESSION)

    first = Recorder()
    second = Recorder()
    _queue(first, operation_id="op_first", text="for the first session")
    _queue(second, operation_id="op_second", session=OTHER_SESSION, text="for the second")

    assert first.calls == ["literal:for the first session", "enter"]
    assert second.calls == ["literal:for the second", "enter"]


# --------------------------------------------------------------------------
# Query surface
# --------------------------------------------------------------------------


def test_an_unknown_operation_id_reads_as_absent_not_as_an_error():
    assert knc.get("op_never_minted") is None


def test_a_session_with_nothing_pending_has_no_blocking_operation():
    _attach()
    _queue(Recorder())

    assert knc.unresolved_ambiguity(SESSION) is None


def test_the_record_keeps_transport_and_provider_truth_in_separate_fields():
    _attach()
    record = _queue(Recorder())

    assert record["schema"] == knc.RECORD_SCHEMA
    assert set(record) >= {
        "posted",
        "posted_at",
        "transport",
        "provider_accepted",
        "provider_completed",
        "observation",
    }
    # Posted is a transport fact and carries transport evidence; the
    # provider fields are still empty because nothing was observed.
    assert record["transport"]["payload_sha256"]
    assert record["observation"] is None


def test_the_outcome_vocabulary_is_closed():
    _attach()
    _queue(Recorder())

    with pytest.raises(knc.NativeControlInvalid, match="outcome must be one of"):
        knc.record_observation(
            operation_id="op_queue_1",
            observation=_evidence("op_queue_1"),
            outcome="probably_fine",
        )


def test_ambiguous_is_frozen_rather_than_resolved():
    """It still blocks, so it must never count as a finished outcome."""
    assert knc.AMBIGUOUS not in knc.RESOLVED_STATES
    assert knc.RESOLVED_STATES == {knc.COMPLETED, knc.REFUSED}
    assert knc.AMBIGUOUS in knc.OPERATION_STATES


def test_replaying_a_steer_or_a_control_is_equally_free():
    """Replay-without-effect is a property of every kind, not just queue."""
    _attach()
    steer_transport = Recorder()
    control_transport = Recorder()
    _steer(steer_transport, operation_id="op_s")
    _control(control_transport, operation_id="op_c")

    replayed_steer = _steer(steer_transport, operation_id="op_s")
    replayed_control = _control(control_transport, operation_id="op_c")

    assert replayed_steer["state"] == replayed_control["state"] == knc.POSTED
    assert steer_transport.calls == ["literal:stop and summarize what you have", "enter"]
    assert control_transport.calls == [f"literal:{knc.CONTROL_COMPACT}", "enter"]


# --------------------------------------------------------------------------
# Malformed input, and a store that cannot be trusted
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"operation_id": ""},
        {"operation_id": "   "},
        {"session": ""},
        {"terminal_id": ""},
        {"generation": ""},
        {"text": ""},
    ],
    ids=["blank-id", "whitespace-id", "no-session", "no-terminal", "no-generation", "empty-text"],
)
def test_an_incomplete_binding_is_a_caller_bug_with_no_record(kwargs):
    _attach()
    transport = Recorder()

    with pytest.raises(knc.NativeControlInvalid):
        _queue(transport, **kwargs)

    assert transport.calls == []


def test_a_steer_without_a_target_turn_is_a_caller_bug():
    _attach()
    with pytest.raises(knc.NativeControlInvalid, match="turn_id"):
        _steer(Recorder(), turn_id="")


def test_an_observation_must_be_a_mapping():
    _attach()
    with pytest.raises(knc.NativeControlInvalid, match="turn_observation"):
        _queue(Recorder(), observation="idle")  # type: ignore[arg-type]

    _queue(Recorder(), operation_id="op_ok")
    with pytest.raises(knc.NativeControlInvalid, match="provider_observation"):
        knc.record_observation(
            operation_id="op_ok",
            observation="looked fine",  # type: ignore[arg-type]
            outcome=knc.ACCEPTED,
        )


def test_evidence_may_not_be_empty():
    """An observation with nothing in it is not evidence of anything."""
    with pytest.raises(knc.NativeControlInvalid, match="non-empty mapping"):
        knc.provider_observation(
            operation_id="op_x",
            observed_at="2026-07-24T00:00:00Z",
            observer="status_monitor",
            evidence={},
        )


def test_reconcile_holds_the_same_closed_outcome_vocabulary():
    _attach()
    _queue(FailsOnLiteral(), operation_id="op_lost")

    with pytest.raises(knc.NativeControlInvalid, match="outcome must be one of"):
        knc.reconcile(
            operation_id="op_lost",
            observation=_evidence("op_lost"),
            outcome=knc.AMBIGUOUS,
        )


def test_an_unreadable_store_fails_closed_rather_than_reporting_nothing_pending(monkeypatch):
    """A store that cannot answer must not be read as 'no ambiguity here'."""

    def _broken():
        raise RuntimeError("database file is gone")

    monkeypatch.setattr(knc.database, "SessionLocal", _broken)

    with pytest.raises(knc.NativeControlUnavailable):
        knc.get("op_anything")
    with pytest.raises(knc.NativeControlUnavailable):
        knc.unresolved_ambiguity(SESSION)


def test_an_unreadable_store_never_lets_a_write_proceed_unjournaled(monkeypatch):
    _attach()
    transport = Recorder()

    def _broken():
        raise RuntimeError("database file is gone")

    monkeypatch.setattr(knc.database, "SessionLocal", _broken)

    with pytest.raises(knc.NativeControlUnavailable):
        _queue(transport)

    assert transport.calls == []


def test_an_unreadable_attachment_store_is_not_read_as_ownership(monkeypatch):
    _attach()
    transport = Recorder()

    def _broken(*args, **kwargs):
        raise na.NativeAttachmentUnavailable("attachment store unreadable")

    monkeypatch.setattr(na, "get", _broken)

    with pytest.raises(knc.NativeControlUnavailable):
        _queue(transport)

    assert transport.calls == []


# --------------------------------------------------------------------------
# Content-lossless multi-line delivery
#
# The defect these cover: an ordinary task file ends with a newline, and
# a rule that refused every newline refused every ordinary task. The fix
# has to deliver newlines as *content* without ever letting one of them
# do the submitting.
# --------------------------------------------------------------------------

PINNED = "0.29.0"


def test_an_ordinary_newline_terminated_task_is_delivered_not_refused():
    """The reproduced defect: a trailing newline is the submit, not content."""
    _attach()
    transport = Recorder()

    record = _queue(transport, text="review the diff and report\n", provider_version=PINNED)

    assert record["state"] == knc.POSTED
    # The terminator became the Enter; it was never typed as a character.
    assert transport.calls == ["literal:review the diff and report", "key:End", "enter"]
    assert record["transport"]["encoding"] == knc.ENCODING_SINGLE_LINE
    assert record["transport"]["line_count"] == 1


def test_embedded_and_blank_lines_arrive_as_content_in_exactly_one_turn():
    _attach()
    transport = Recorder()

    record = _queue(
        transport,
        text="first line\n\nthird line\n",
        provider_version=PINNED,
    )

    assert record["state"] == knc.POSTED
    # The blank middle line is a break with nothing typed after it, and
    # every break is a composer key -- so one Enter, one turn.
    assert transport.calls == [
        "literal:first line",
        "key:C-j",
        "key:C-j",
        "literal:third line",
        "key:End",
        "enter",
    ]
    assert transport.calls.count("enter") == 1
    assert record["transport"]["enter_count"] == 1
    assert record["transport"]["encoding"] == knc.ENCODING_SOFT_NEWLINE
    assert record["transport"]["line_count"] == 3


def test_the_final_enter_is_never_typed_as_a_newline_character():
    """No literal write may contain CR or LF, whatever the payload holds."""
    _attach()
    transport = Recorder()

    _queue(transport, text="alpha\nbeta\ngamma\n", provider_version=PINNED)

    for call in transport.calls:
        if call.startswith("literal:"):
            assert "\n" not in call and "\r" not in call


def test_digests_are_computed_over_the_original_bytes_not_the_encoding():
    """Encoding must never redefine what the caller asked to send."""
    _attach()
    original = "line one\nline two\n"

    record = _queue(Recorder(), text=original, provider_version=PINNED)

    assert record["payload_sha256"] == canonical_sha256(original)
    assert record["transport"]["payload_sha256"] == canonical_sha256(original)
    # The composer image is the content without the submitting terminator.
    assert record["transport"]["composer_sha256"] == canonical_sha256("line one\nline two")


def test_the_keystroke_plan_is_journaled_before_any_composer_io():
    _attach()
    transport = FailsOnLiteral()

    record = _queue(transport, text="a\nb\n", provider_version=PINNED)

    # Nothing was typed, yet the plan that was about to run is durable.
    plan = knc.get("op_queue_1")["intent"]["keystroke_plan"]
    assert plan["schema"] == knc.KEYSTROKE_PLAN_SCHEMA
    assert plan["encoding"] == knc.ENCODING_SOFT_NEWLINE
    assert plan["line_count"] == 2
    assert plan["plan_sha256"]
    # The plan records shape and digests, never a second copy of the text.
    assert "lines" not in plan
    assert record["state"] == knc.AMBIGUOUS


def test_model_input_is_recorded_as_the_provider_normalized_image():
    """Say what the model got, not what we wish it got."""
    _attach()

    exact = _queue(Recorder(), text="body\n", provider_version=PINNED)
    assert exact["transport"]["model_input_is_composer_exact"] is True
    assert exact["transport"]["provider_normalization"] == knc.NORMALIZATION_JOIN_LF_THEN_TRIM

    # A blank final line survives into the composer but the provider trims
    # it on submit. The receipt says so rather than claiming exactness.
    trimmed = _queue(
        Recorder(),
        operation_id="op_queue_trim",
        text="body\n\n",
        provider_version=PINNED,
    )
    assert trimmed["transport"]["model_input_is_composer_exact"] is False
    assert trimmed["transport"]["composer_sha256"] == canonical_sha256("body\n")
    assert trimmed["transport"]["model_input_sha256"] == canonical_sha256("body")


def test_interior_bytes_are_exact_even_when_the_edges_are_trimmed():
    _attach()

    record = _queue(
        Recorder(),
        text="  keep\n\ninterior\n",
        provider_version=PINNED,
    )

    # Leading spaces go; everything between the edges is untouched.
    assert record["transport"]["model_input_sha256"] == canonical_sha256("keep\n\ninterior")


def test_nel_is_not_trimmed_by_the_provider_and_is_not_treated_as_whitespace():
    """U+0085 is a line terminator in other standards but not in ECMAScript.

    Folding it in by analogy would understate the model's input by a
    character, so the normalization must leave it alone.
    """
    _attach()

    record = _queue(Recorder(), text="\x85body\x85\n", provider_version=PINNED)

    assert record["transport"]["model_input_sha256"] == canonical_sha256("\x85body\x85")
    assert record["transport"]["model_input_is_composer_exact"] is True


def test_the_paste_burst_window_is_cleared_before_the_submitting_enter():
    """Enter inside the provider's paste-burst window inserts a newline.

    Without clearing it the task is silently never submitted: no error,
    no turn, the message left sitting in the composer.
    """
    _attach()
    transport = Recorder()

    _queue(transport, text="anything\n", provider_version=PINNED)

    assert transport.calls[-2:] == ["key:End", "enter"]


# --------------------------------------------------------------------------
# No proven mechanism: a durable, typed, zero-byte refusal
# --------------------------------------------------------------------------


def test_an_unprovable_multiline_payload_is_a_durable_refusal_not_a_raise():
    """A well-formed payload an installed build cannot take is operational.

    It is a fact about the provider, not caller fiction, so it has to be
    findable afterwards -- otherwise a lost response leaves the caller
    unable to tell "refused, nothing sent" from "maybe sent".
    """
    _attach()
    transport = Recorder()

    record = _queue(transport, text="a\nb\n", provider_version="99.99.99")

    assert record["state"] == knc.REFUSED
    assert record["refusal_reason"] == knc.REFUSED_UNPROVEN_COMPOSER_NEWLINE
    assert record["is_resolved"] is True
    assert transport.calls == []
    assert record["posted"] is False


def test_a_lost_response_finds_the_refusal_by_exact_id_without_retyping():
    _attach()
    transport = Recorder()

    _queue(transport, text="a\nb\n", provider_version="99.99.99")

    # The caller never saw the response; it asks by the id it minted.
    found = knc.get("op_queue_1")
    assert found["state"] == knc.REFUSED
    assert found["refusal_reason"] == knc.REFUSED_UNPROVEN_COMPOSER_NEWLINE
    assert found["payload_sha256"] == canonical_sha256("a\nb\n")

    # Replaying the identical request returns the same record and types
    # nothing: at-most-once survives the refusal path too.
    replay = _queue(transport, text="a\nb\n", provider_version="99.99.99")
    assert replay["state"] == knc.REFUSED
    assert transport.calls == []


def test_an_absent_provider_version_refuses_multiline_but_still_sends_one_line():
    """Old callers keep working; only the case that needs a pin needs one."""
    _attach()

    single = Recorder()
    record = _queue(single, text="just one line")
    assert record["state"] == knc.POSTED
    # No pin, so no burst-reset key -- the pre-existing behaviour exactly.
    assert single.calls == ["literal:just one line", "enter"]

    multi = Recorder()
    refused = _queue(multi, operation_id="op_queue_2", text="two\nlines")
    assert refused["state"] == knc.REFUSED
    assert multi.calls == []


def test_an_unproven_build_gets_the_floor_settle_marked_unproven():
    """A floor is not evidence, and the plan must say so.

    ``0.25`` on an unproven build is the longest proven interval for
    this provider selected as a floor, not a measurement of this
    build — a receipt that cannot tell the two apart would let a
    fallback masquerade as proof.
    """
    plan = knc.plan_composer_keystrokes("one line only", provider_version="9.9.9")
    assert plan["deliverable"] is True
    assert plan["submit_settle_seconds"] == 0.25
    assert plan["submit_settle_proven"] is False
    assert plan["composer_evidence"] is None


def test_a_proven_build_marks_its_settle_proven():
    plan = knc.plan_composer_keystrokes("one line only", provider_version=PINNED)
    assert plan["submit_settle_seconds"] == 0.25
    assert plan["submit_settle_proven"] is True


def test_a_payload_that_is_only_a_terminator_has_no_content_to_send():
    _attach()
    transport = Recorder()

    with pytest.raises(knc.NativeControlInvalid):
        _queue(transport, text="\n", provider_version=PINNED)

    assert transport.calls == []
    assert knc.get("op_queue_1") is None


# --------------------------------------------------------------------------
# Response loss around the keystrokes
# --------------------------------------------------------------------------


def test_loss_midway_through_typing_never_sends_a_second_enter():
    _attach()

    class FailsOnSecondLine(Recorder):
        def send_literal(self, text: str) -> None:
            if any(call.startswith("literal:") for call in self.calls):
                raise RuntimeError("pane vanished between lines")
            super().send_literal(text)

    transport = FailsOnSecondLine()
    record = _queue(transport, text="first\nsecond\n", provider_version=PINNED)

    assert record["state"] == knc.AMBIGUOUS
    assert record["posted"] is False
    assert "enter" not in transport.calls
    assert "typing line 2 of 2" in record["ambiguity_reason"]


def test_loss_at_the_enter_boundary_stays_ambiguous_and_is_never_resent():
    _attach()

    class FailsOnEnter(Recorder):
        def send_enter(self) -> None:
            self.calls.append("enter")
            raise RuntimeError("pane vanished at submit")

    transport = FailsOnEnter()
    record = _queue(transport, text="body\n", provider_version=PINNED)

    assert record["state"] == knc.AMBIGUOUS
    assert "composer may hold unsubmitted text" in record["ambiguity_reason"]
    # Exactly one Enter was ever attempted, and the replay adds none.
    assert transport.calls.count("enter") == 1
    replay = _queue(transport, text="body\n", provider_version=PINNED)
    assert replay["state"] == knc.AMBIGUOUS
    assert transport.calls.count("enter") == 1


class TestMultiKbMultilineComposerExecution:
    """A multi-KB multiline inbox payload on a proven build types as one
    sequence: literal lines in byte order, proven soft-newline keystrokes
    between them, exactly one burst reset and one Enter at the end, and no
    framing bytes anywhere."""

    class _RecordingTransport:
        def __init__(self):
            self.events = []

        def send_literal(self, text):
            self.events.append(("literal", text))

        def send_key(self, keystroke):
            self.events.append(("key", keystroke))

        def send_enter(self):
            self.events.append(("enter", None))

    def test_multi_kb_multiline_payload_types_in_order_with_one_submit(self):
        paragraph = "ordinary agent report line with detail and numbers 0123456789\n"
        payload = paragraph * 70  # ~4.9 KB, 70 embedded newlines
        assert len(payload.encode("utf-8")) >= 4096

        plan = knc.plan_composer_keystrokes(payload, provider_version="0.29.2")
        assert plan["deliverable"] is True
        assert plan["encoding"] == knc.ENCODING_SOFT_NEWLINE

        transport = self._RecordingTransport()
        knc.execute_composer_plan(plan=plan, transport=transport, submit=True)

        keys = [event for event in transport.events if event[0] == "key"]
        enters = [event for event in transport.events if event[0] == "enter"]
        literals = [event[1] for event in transport.events if event[0] == "literal"]

        # Byte order preserved: one soft-newline keystroke between each pair
        # of plan lines (a trailing payload newline is the plan's trailing
        # terminator, not an extra line), and the literal stream
        # reconstructs the payload's text content exactly.
        assert sum(1 for key in keys if key[1] == "C-j") == len(plan["lines"]) - 1
        assert len(plan["lines"]) == payload.count("\n")
        assert [key[1] for key in keys if key[1] != "C-j"] == ["End"]
        assert len(enters) == 1
        assert transport.events[-1] == ("enter", None)
        # The submit sequence (End burst reset, then Enter) happens exactly
        # once, after the final literal — never per line.
        end_index = next(i for i, event in enumerate(transport.events) if event == ("key", "End"))
        assert all(
            i < end_index for i, event in enumerate(transport.events) if event[0] == "literal"
        )
        assert "".join(literals) == payload.replace("\n", "")
        # No framing bytes are introduced anywhere in the stream.
        for event in transport.events:
            if event[1] is not None:
                assert "\x1b" not in event[1]

    def test_undeliverable_build_is_refused_before_any_keystroke(self):
        plan = knc.plan_composer_keystrokes("line one\nline two", provider_version="0.29.3")
        assert plan["deliverable"] is False
        transport = self._RecordingTransport()
        with pytest.raises(knc.NativeControlInvalid):
            knc.execute_composer_plan(plan=plan, transport=transport, submit=True)
        assert transport.events == []
