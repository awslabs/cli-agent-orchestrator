"""The Codex native control state machine, driven end to end.

The sibling module to the Kimi control tests, and separate for the same
reason the adapters are: a Kimi record must not be able to satisfy a
Codex check, so the two suites never share a store, a schema, or a
fixture.

Four properties are asserted repeatedly and are worth naming once, because
almost every test below is one of them in a different situation.

*The intent is durable before the first keystroke.* A replay finds the
journaled row and returns it without reaching the transport. That is
where at-most-once lives — not in a caller remembering what it sent.

*A refusal types nothing.* Every refused operation is checked against an
empty transport log. A refusal that already typed half a message is not a
refusal, and the durable row would be the only place that fact survived.

*Posted is not accepted.* Reaching the transport moves an operation to
``posted`` and no further. Nothing in this module reaches ``accepted``
without a separate observation naming that exact operation, because
deriving provider acceptance from a successful write is how a system
starts reporting deliveries that never happened.

*An exception is uncertainty, not failure.* A transport that raises has
not proved the bytes stayed home, so the operation becomes ambiguous and
the session closes to new work until a reconcile says what happened.
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import codex_native_control as cnc
from cli_agent_orchestrator.services import execution_mode as em
from cli_agent_orchestrator.services import native_attachment as na

SESSION = "6d1f0e34-0000-4000-8000-00000000abcd"
OTHER_SESSION = "8f31c2aa-0000-4000-8000-0000000012cd"
TERMINAL = "terminal_4d7b"
GENERATION = "gen_1c0e"
PINNED = "0.146.0"
TURN = "turn_88213"


@pytest.fixture(autouse=True)
def _db(isolated_memory_db):
    """Every test gets its own initialized database."""
    return isolated_memory_db


@pytest.fixture(autouse=True)
def _no_real_settle(monkeypatch):
    """Record the submit settle instead of spending it.

    The pinned 0.2s clears Codex's 120ms paste-burst Enter suppression
    window; the delay is real behaviour, not test slack. It is
    recorded here rather than slept so a suite that posts thirty times does
    not cost a minute of wall clock. ``TestSubmitSettle`` asserts the value
    that would have been slept, so replacing the call does not stop the
    settle from being checked.
    """
    slept: list[float] = []
    monkeypatch.setattr(cnc.time, "sleep", slept.append)
    return slept


class Recorder:
    """A transport that records exactly what it was asked to do.

    The call log is the evidence for both the zero-bytes and no-duplicate
    assertions: ``literal:`` entries carry the exact text, and ``enter``
    is its own entry so a submit can never hide inside a payload.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def send_literal(self, text: str) -> int:
        self.calls.append(f"literal:{text}")
        return len(text)

    def send_enter(self) -> None:
        self.calls.append("enter")

    def send_key(self, keystroke: str) -> None:
        self.calls.append(f"key:{keystroke}")


class FailsOnLiteral(Recorder):
    def send_literal(self, text: str) -> int:
        self.calls.append(f"literal:{text}")
        raise OSError("pane vanished mid-write")


class FailsOnEnter(Recorder):
    def send_enter(self) -> None:
        self.calls.append("enter")
        raise OSError("pane vanished before submit")


def test_split_submit_boundary_sends_only_settle_and_one_enter(_no_real_settle):
    recorder = Recorder()
    plan = cnc.plan_composer_keystrokes("observe before submit", provider_version=PINNED)

    typed = cnc.execute_composer_plan(plan=plan, transport=recorder, submit=False)
    assert typed == {"lines_typed": 1, "enter_sent": False}
    assert recorder.calls == ["literal:observe before submit"]

    cnc.submit_composer_plan(plan=plan, transport=recorder)
    assert _no_real_settle == [0.2]
    assert recorder.calls == ["literal:observe before submit", "enter"]


def _attach(
    *,
    session: str = SESSION,
    terminal_id: str = TERMINAL,
    generation: str = GENERATION,
    execution_mode: str = em.NATIVE_TUI,
) -> dict:
    """Take one Codex session all the way to attached, owned by this terminal.

    Codex returns this id from a persistent zero-turn app-server bootstrap.
    """
    intent = na.acquire_intent(
        acquisition_method=na.ACQUISITION_ZERO_TURN_BOOTSTRAP,
        acquisition_receipt={
            "provider": cnc.PROVIDER,
            "native_session_id": session,
            "id_source": "app_server_thread_start",
        },
        admits_only_new_instructions=True,
        replays_task_bytes=False,
        bootstrap_sent_no_turn=True,
        bootstrap_detached_before_launch=True,
    )
    owner = {
        "provider": cnc.PROVIDER,
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
    return cnc.turn_observation(
        active_turn_id=None,
        observed_at="2026-07-25T00:00:00Z",
        observer="pane-screen-read",
    )


def _busy(turn_id: str = TURN) -> dict:
    return cnc.turn_observation(
        active_turn_id=turn_id,
        observed_at="2026-07-25T00:00:01Z",
        observer="pane-screen-read",
    )


def _queue(operation_id: str = "op", *, text: str = "carry on", transport=None, **overrides):
    kwargs = {
        "operation_id": operation_id,
        "native_session_id": SESSION,
        "terminal_id": TERMINAL,
        "generation": GENERATION,
        "execution_mode": em.NATIVE_TUI,
        "text": text,
        "observation": _idle(),
        "transport": transport if transport is not None else Recorder(),
        "provider_version": PINNED,
    }
    kwargs.update(overrides)
    return cnc.queue(**kwargs)


class TestIntentBeforeKeystroke:
    def test_a_posted_queue_records_what_it_typed_and_nothing_more(self):
        _attach()
        recorder = Recorder()

        record = _queue("op-1", text="carry on", transport=recorder)

        assert record["state"] == cnc.POSTED
        assert recorder.calls == ["literal:carry on", "enter"]
        # The journaled intent exists and describes this exact operation,
        # so a crash immediately after the write leaves the fact behind.
        assert record["intent"]["operation_id"] == "op-1"
        assert record["intent"]["kind"] == cnc.KIND_QUEUE
        # The plan is journaled without the message text: the store holds
        # what was going to be typed, not the payload itself.
        assert "lines" not in record["intent"]["keystroke_plan"]

    def test_a_replay_returns_the_first_outcome_without_typing_again(self):
        _attach()
        first = Recorder()
        _queue("op-2", text="only once", transport=first)

        second = Recorder()
        replay = _queue("op-2", text="only once", transport=second)

        # The whole point: the second call reached the durable row and
        # stopped there. A duplicate here would be a second message to the
        # model that no caller asked for.
        assert second.calls == []
        assert replay["operation_id"] == "op-2"
        assert replay["state"] == cnc.POSTED

    def test_reusing_an_operation_id_for_different_text_is_refused(self):
        _attach()
        _queue("op-3", text="the first message")

        recorder = Recorder()
        with pytest.raises(cnc.NativeControlConflict, match="not reusable"):
            _queue("op-3", text="a different message", transport=recorder)
        assert recorder.calls == []

    def test_reusing_an_operation_id_across_kinds_is_refused(self):
        """A queue and a steer are not the same request wearing one id."""
        _attach()
        _queue("op-4", text="same text")

        recorder = Recorder()
        with pytest.raises(cnc.NativeControlConflict, match="kind"):
            cnc.steer(
                operation_id="op-4",
                native_session_id=SESSION,
                terminal_id=TERMINAL,
                generation=GENERATION,
                execution_mode=em.NATIVE_TUI,
                turn_id=TURN,
                text="same text",
                observation=_busy(),
                transport=recorder,
                provider_version=PINNED,
            )
        assert recorder.calls == []


class TestIdentityRefusals:
    def test_only_the_native_tui_mode_is_served(self):
        """An ACP caller arriving here is a bug that must surface."""
        _attach()
        with pytest.raises(cnc.NativeControlInvalid, match="native_tui"):
            _queue("op-5", execution_mode="acp")

    @pytest.mark.parametrize(
        "field", ["operation_id", "native_session_id", "terminal_id", "generation"]
    )
    def test_an_empty_identity_field_is_refused_before_anything_is_journaled(self, field):
        _attach()
        recorder = Recorder()
        blanked = {"operation_id": "op-6", field: ""}
        with pytest.raises(cnc.NativeControlInvalid, match=field):
            _queue(transport=recorder, **blanked)
        assert recorder.calls == []
        # Nothing was journaled, so there is no half-formed operation for a
        # later call to find and mistake for a replay.
        assert cnc.get("op-6") is None

    def test_a_session_this_process_does_not_hold_is_refused(self):
        """No attachment at all: nothing may be written to it."""
        recorder = Recorder()

        record = _queue("op-7", transport=recorder)

        assert record["state"] == cnc.REFUSED
        assert record["refusal_reason"] == cnc.REFUSED_ATTACHMENT
        assert recorder.calls == []

    def test_a_superseded_generation_never_writes_to_its_successors_session(self):
        _attach(generation="gen_successor")
        recorder = Recorder()

        record = _queue("op-8", generation=GENERATION, transport=recorder)

        assert record["state"] == cnc.REFUSED
        assert record["refusal_reason"] == cnc.REFUSED_ATTACHMENT
        assert "is held by" in record["observation"]["detail"]
        assert recorder.calls == []

    def test_an_unreadable_attachment_store_fails_closed(self, monkeypatch):
        """ "Could not look" is refused, never treated as ownership."""
        _attach()

        def _boom(*_args, **_kwargs):
            raise RuntimeError("attachment store unavailable")

        monkeypatch.setattr(cnc.native_attachment, "get", _boom)
        recorder = Recorder()

        record = _queue("op-9", transport=recorder)

        assert record["state"] == cnc.REFUSED
        assert record["refusal_reason"] == cnc.REFUSED_ATTACHMENT
        assert recorder.calls == []


class TestTurnGates:
    def test_a_queue_waits_for_idle_rather_than_landing_mid_turn(self):
        _attach()
        recorder = Recorder()

        record = _queue("op-10", observation=_busy(), transport=recorder)

        assert record["state"] == cnc.REFUSED
        assert record["refusal_reason"] == cnc.REFUSED_ACTIVE_TURN
        assert recorder.calls == []

    def test_a_steer_reaches_the_exact_turn_it_named(self):
        _attach()
        recorder = Recorder()

        record = cnc.steer(
            operation_id="op-11",
            native_session_id=SESSION,
            terminal_id=TERMINAL,
            generation=GENERATION,
            execution_mode=em.NATIVE_TUI,
            turn_id=TURN,
            text="actually, stop",
            observation=_busy(TURN),
            transport=recorder,
            provider_version=PINNED,
        )

        assert record["state"] == cnc.POSTED
        assert record["turn_id"] == TURN
        assert recorder.calls == ["literal:actually, stop", "enter"]

    def test_a_steer_with_nothing_to_steer_is_refused_not_downgraded(self):
        """An idle session is not a follow-up opportunity.

        Downgrading here would deliver a mid-turn correction as a fresh
        instruction after the work it was about had already finished.
        """
        _attach()
        recorder = Recorder()

        record = cnc.steer(
            operation_id="op-12",
            native_session_id=SESSION,
            terminal_id=TERMINAL,
            generation=GENERATION,
            execution_mode=em.NATIVE_TUI,
            turn_id=TURN,
            text="actually, stop",
            observation=_idle(),
            transport=recorder,
            provider_version=PINNED,
        )

        assert record["state"] == cnc.REFUSED
        assert record["refusal_reason"] == cnc.REFUSED_NO_ACTIVE_TURN
        assert recorder.calls == []

    def test_a_steer_whose_turn_already_ended_is_refused(self):
        _attach()
        recorder = Recorder()

        record = cnc.steer(
            operation_id="op-13",
            native_session_id=SESSION,
            terminal_id=TERMINAL,
            generation=GENERATION,
            execution_mode=em.NATIVE_TUI,
            turn_id=TURN,
            text="actually, stop",
            observation=_busy("turn_something_else"),
            transport=recorder,
            provider_version=PINNED,
        )

        assert record["state"] == cnc.REFUSED
        assert record["refusal_reason"] == cnc.REFUSED_TURN_MISMATCH
        assert recorder.calls == []

    def test_an_advertised_control_is_typed_when_idle(self):
        _attach()
        recorder = Recorder()

        record = cnc.control(
            operation_id="op-14",
            native_session_id=SESSION,
            terminal_id=TERMINAL,
            generation=GENERATION,
            execution_mode=em.NATIVE_TUI,
            command=cnc.CONTROL_COMPACT,
            observation=_idle(),
            transport=recorder,
            provider_version=PINNED,
        )

        assert record["state"] == cnc.POSTED
        assert recorder.calls == [f"literal:{cnc.CONTROL_COMPACT}", "enter"]

    def test_an_unadvertised_command_is_refused_rather_than_typed_as_prose(self):
        """At the transport a slash command is indistinguishable from text.

        An unrecognised one would not fail — it would be delivered to the
        model as prose that happens to start with a slash.
        """
        _attach()
        recorder = Recorder()

        record = cnc.control(
            operation_id="op-15",
            native_session_id=SESSION,
            terminal_id=TERMINAL,
            generation=GENERATION,
            execution_mode=em.NATIVE_TUI,
            command="/definitely-not-a-command",
            observation=_idle(),
            transport=recorder,
            provider_version=PINNED,
        )

        assert record["state"] == cnc.REFUSED
        assert record["refusal_reason"] == cnc.REFUSED_UNSUPPORTED_CONTROL
        assert recorder.calls == []

    def test_a_control_command_waits_for_idle(self):
        _attach()
        recorder = Recorder()

        record = cnc.control(
            operation_id="op-16",
            native_session_id=SESSION,
            terminal_id=TERMINAL,
            generation=GENERATION,
            execution_mode=em.NATIVE_TUI,
            command=cnc.CONTROL_COMPACT,
            observation=_busy(),
            transport=recorder,
            provider_version=PINNED,
        )

        assert record["state"] == cnc.REFUSED
        assert record["refusal_reason"] == cnc.REFUSED_ACTIVE_TURN
        assert recorder.calls == []

    def test_an_empty_command_is_refused_before_journaling(self):
        _attach()
        recorder = Recorder()
        with pytest.raises(cnc.NativeControlInvalid, match="command"):
            cnc.control(
                operation_id="op-17",
                native_session_id=SESSION,
                terminal_id=TERMINAL,
                generation=GENERATION,
                execution_mode=em.NATIVE_TUI,
                command="",
                observation=_idle(),
                transport=recorder,
                provider_version=PINNED,
            )
        assert recorder.calls == []


class TestMultiLineDelivery:
    def test_a_multi_line_message_breaks_with_the_pinned_keystroke(self):
        """Ten newlines are still one turn.

        The breaks are composer keystrokes, so exactly one Enter is sent
        no matter how many lines the payload has.
        """
        _attach()
        recorder = Recorder()

        record = _queue("op-18", text="first\nsecond\nthird", transport=recorder)

        assert record["state"] == cnc.POSTED
        assert recorder.calls == [
            "literal:first",
            "key:C-j",
            "literal:second",
            "key:C-j",
            "literal:third",
            "enter",
        ]
        assert recorder.calls.count("enter") == 1

    def test_a_blank_line_is_a_break_with_nothing_after_it(self):
        """The break comes first, so an empty line cannot be dropped."""
        _attach()
        recorder = Recorder()

        _queue("op-19", text="head\n\ntail", transport=recorder)

        assert recorder.calls == [
            "literal:head",
            "key:C-j",
            "key:C-j",
            "literal:tail",
            "enter",
        ]

    def test_an_unproven_build_refuses_multi_line_instead_of_improvising(self):
        """Which key inserts a newline is a fact about one build.

        Splitting the message across turns, pasting it, and flattening the
        newlines away are each a way of appearing to deliver something
        that was not delivered, so the refusal is durable and typed.
        """
        _attach()
        recorder = Recorder()

        record = _queue("op-20", text="one\ntwo", transport=recorder, provider_version="9.9.9")

        assert record["state"] == cnc.REFUSED
        assert record["refusal_reason"] == cnc.REFUSED_UNPROVEN_COMPOSER_NEWLINE
        assert recorder.calls == []

    def test_a_single_line_needs_no_proven_keystroke(self):
        _attach()
        recorder = Recorder()

        record = _queue("op-21", text="one line only", transport=recorder, provider_version=None)

        assert record["state"] == cnc.POSTED
        assert recorder.calls == ["literal:one line only", "enter"]


class TestSubmitSettle:
    def test_the_renderer_is_given_the_pinned_settle_before_the_enter(self, _no_real_settle):
        """Codex suppresses Enter briefly after a paste burst.

        That produces no error and no turn — the message simply sits in the
        prompt box unsent — so the delay is load-bearing, not padding.
        """
        _attach()
        _queue("op-22", transport=Recorder())

        assert _no_real_settle == [0.2]

    def test_an_unpinned_single_line_settles_for_nothing(self, _no_real_settle):
        _attach()
        _queue("op-23", transport=Recorder(), provider_version=None)

        assert _no_real_settle == []


class TestPostedIsNotAccepted:
    def test_reaching_the_transport_says_nothing_about_the_provider(self):
        _attach()

        record = _queue("op-24")

        assert record["state"] == cnc.POSTED
        assert record["posted"] is True
        # The whole separation: bytes were written, and that is all this
        # adapter is entitled to claim.
        assert record["provider_accepted"] is False
        assert record["observation"] is None

    def test_acceptance_requires_an_observation_naming_the_operation(self):
        _attach()
        _queue("op-25")

        accepted = cnc.record_observation(
            operation_id="op-25",
            observation=cnc.provider_observation(
                state=cnc.ACCEPTED,
                observed_at="2026-07-25T00:00:05Z",
                observer="pane-screen-read",
            ),
        )

        assert accepted["state"] == cnc.ACCEPTED
        assert accepted["provider_accepted"] is True

    def test_an_accepted_operation_can_later_be_observed_complete(self):
        _attach()
        _queue("op-26")
        cnc.record_observation(
            operation_id="op-26",
            observation=cnc.provider_observation(
                state=cnc.ACCEPTED,
                observed_at="2026-07-25T00:00:05Z",
                observer="pane-screen-read",
            ),
        )

        done = cnc.record_observation(
            operation_id="op-26",
            observation=cnc.provider_observation(
                state=cnc.COMPLETED,
                observed_at="2026-07-25T00:00:09Z",
                observer="pane-screen-read",
            ),
        )

        assert done["state"] == cnc.COMPLETED
        assert done["posted"] is True

    def test_an_observation_cannot_be_recorded_against_an_unposted_operation(self):
        _attach()
        recorder = Recorder()
        _queue("op-27", observation=_busy(), transport=recorder)  # refused, never posted

        with pytest.raises(cnc.NativeControlConflict):
            cnc.record_observation(
                operation_id="op-27",
                observation=cnc.provider_observation(
                    state=cnc.ACCEPTED,
                    observed_at="2026-07-25T00:00:05Z",
                    observer="pane-screen-read",
                ),
            )

    @pytest.mark.parametrize(
        "mangle, match",
        [
            ({"schema": "cao-kimi-native-control-observation-v1"}, "schema"),
            ({"provider": "kimi_cli"}, "not 'codex'"),
            ({"state": "intended"}, "not recordable"),
        ],
    )
    def test_a_foreign_or_unrecordable_observation_is_refused(self, mangle, match):
        """A Kimi observation is not a Codex observation."""
        _attach()
        _queue("op-28")
        observation = dict(
            cnc.provider_observation(
                state=cnc.ACCEPTED,
                observed_at="2026-07-25T00:00:05Z",
                observer="pane-screen-read",
            )
        )
        observation.update(mangle)

        with pytest.raises(cnc.NativeControlInvalid, match=match):
            cnc.record_observation(operation_id="op-28", observation=observation)

    def test_a_provider_observation_cannot_be_built_in_an_unrecordable_state(self):
        with pytest.raises(cnc.NativeControlInvalid, match="must be one of"):
            cnc.provider_observation(
                state=cnc.POSTED,
                observed_at="2026-07-25T00:00:05Z",
                observer="pane-screen-read",
            )

    def test_a_non_mapping_observation_is_refused(self):
        _attach()
        _queue("op-29")
        with pytest.raises(cnc.NativeControlInvalid, match="mapping"):
            cnc.record_observation(operation_id="op-29", observation=["not", "a", "mapping"])


class TestTurnObservationProvenance:
    def test_the_gate_consults_an_observation_not_a_caller_boolean(self):
        """An unlabelled dict is not an observation."""
        _attach()
        recorder = Recorder()
        with pytest.raises(cnc.NativeControlInvalid, match="schema"):
            _queue("op-30", observation={"active_turn_id": None}, transport=recorder)
        assert recorder.calls == []

    def test_a_kimi_turn_observation_cannot_satisfy_a_codex_gate(self):
        _attach()
        foreign = dict(_idle())
        foreign["schema"] = "cao-kimi-native-turn-observation-v1"
        recorder = Recorder()

        with pytest.raises(cnc.NativeControlInvalid, match="schema"):
            _queue("op-31", observation=foreign, transport=recorder)
        assert recorder.calls == []

    def test_an_observation_for_another_provider_is_refused(self):
        _attach()
        foreign = dict(_idle())
        foreign["provider"] = "kimi_cli"
        with pytest.raises(cnc.NativeControlInvalid):
            _queue("op-32", observation=foreign)

    def test_a_non_mapping_turn_observation_is_refused(self):
        _attach()
        with pytest.raises(cnc.NativeControlInvalid, match="mapping"):
            _queue("op-33", observation="idle, honest")

    def test_observing_idle_is_a_statement_and_omitting_the_observer_is_not(self):
        with pytest.raises(cnc.NativeControlInvalid, match="observer"):
            cnc.turn_observation(
                active_turn_id=None, observed_at="2026-07-25T00:00:00Z", observer=""
            )


class TestAmbiguity:
    def test_a_transport_that_raises_mid_message_is_ambiguous_not_failed(self):
        """A raised exception does not prove the bytes stayed home."""
        _attach()
        recorder = FailsOnLiteral()

        record = _queue("op-34", text="did this land", transport=recorder)

        assert record["state"] == cnc.AMBIGUOUS
        assert "no Enter was sent" in record["ambiguity_reason"]
        # Never posted, so nothing may read this as a delivery.
        assert record["posted"] is False
        assert "enter" not in recorder.calls

    def test_a_failure_at_the_submit_records_that_the_payload_was_written(self):
        """Which boundary was reached is recoverable information.

        "Typed but not submitted" is a real state an operator can resolve;
        "may have submitted" is not, and conflating them loses the
        difference.
        """
        _attach()
        recorder = FailsOnEnter()

        record = _queue("op-35", text="written but maybe unsent", transport=recorder)

        assert record["state"] == cnc.AMBIGUOUS
        assert "payload was written" in record["ambiguity_reason"]
        assert recorder.calls == ["literal:written but maybe unsent", "enter"]

    def test_an_unresolved_operation_closes_the_session_to_new_work(self):
        """Anything sent after it would be a second message of unknown order."""
        _attach()
        _queue("op-36", transport=FailsOnEnter())

        blocked = Recorder()
        record = _queue("op-37", transport=blocked)

        assert record["state"] == cnc.REFUSED
        assert record["refusal_reason"] == cnc.REFUSED_UNRESOLVED_AMBIGUITY
        assert blocked.calls == []

    def test_another_session_is_not_blocked_by_this_ones_ambiguity(self):
        _attach()
        _attach(session=OTHER_SESSION, terminal_id="terminal_other")
        _queue("op-38", transport=FailsOnEnter())

        recorder = Recorder()
        record = _queue(
            "op-39",
            native_session_id=OTHER_SESSION,
            terminal_id="terminal_other",
            transport=recorder,
        )

        assert record["state"] == cnc.POSTED
        assert recorder.calls == ["literal:carry on", "enter"]

    def test_only_a_reconcile_reopens_the_session(self):
        _attach()
        _queue("op-40", transport=FailsOnEnter())

        cnc.reconcile(
            operation_id="op-40",
            resolution=cnc.COMPLETED,
            evidence={"operation_id": "op-40", "observed": "the turn ran"},
        )

        recorder = Recorder()
        record = _queue("op-41", transport=recorder)
        assert record["state"] == cnc.POSTED
        assert recorder.calls == ["literal:carry on", "enter"]

    def test_a_retry_is_never_the_way_out_of_ambiguity(self):
        """Replaying the ambiguous id returns the row, and types nothing."""
        _attach()
        _queue("op-42", transport=FailsOnEnter())

        recorder = Recorder()
        replay = _queue("op-42", transport=recorder)

        assert replay["state"] == cnc.AMBIGUOUS
        assert recorder.calls == []

    def test_reconcile_evidence_must_name_this_exact_operation(self):
        """With two outstanding, "the ambiguous one" closes the wrong one."""
        _attach()
        _queue("op-43", transport=FailsOnEnter())

        with pytest.raises(cnc.NativeControlInvalid, match="must name operation"):
            cnc.reconcile(
                operation_id="op-43",
                resolution=cnc.COMPLETED,
                evidence={"operation_id": "op-99"},
            )
        assert cnc.get("op-43")["state"] == cnc.AMBIGUOUS

    def test_reconcile_refuses_a_resolution_that_is_not_terminal(self):
        _attach()
        _queue("op-44", transport=FailsOnEnter())

        with pytest.raises(cnc.NativeControlInvalid, match="must be one of"):
            cnc.reconcile(
                operation_id="op-44",
                resolution=cnc.POSTED,
                evidence={"operation_id": "op-44"},
            )

    def test_reconcile_refuses_evidence_that_is_not_a_mapping(self):
        _attach()
        _queue("op-45", transport=FailsOnEnter())

        with pytest.raises(cnc.NativeControlInvalid, match="mapping"):
            cnc.reconcile(operation_id="op-45", resolution=cnc.REFUSED, evidence="trust me")

    def test_reconcile_refuses_an_operation_that_is_not_ambiguous(self):
        _attach()
        _queue("op-46")

        with pytest.raises(cnc.NativeControlConflict):
            cnc.reconcile(
                operation_id="op-46",
                resolution=cnc.COMPLETED,
                evidence={"operation_id": "op-46"},
            )

    def test_a_posted_operation_can_become_ambiguous_later(self):
        """Uncertainty can arrive after the write, when a later read fails."""
        _attach()
        _queue("op-47")

        record = cnc.mark_ambiguous(
            operation_id="op-47", reason="the pane stopped answering before the turn was seen"
        )

        assert record["state"] == cnc.AMBIGUOUS
        assert cnc.unresolved_ambiguity(SESSION)["operation_id"] == "op-47"

    def test_marking_ambiguous_needs_a_reason(self):
        _attach()
        _queue("op-48")
        with pytest.raises(cnc.NativeControlInvalid, match="reason"):
            cnc.mark_ambiguous(operation_id="op-48", reason="")

    def test_the_oldest_unresolved_operation_is_the_blocking_one(self):
        """Two outstanding, and the first is the one that must be named.

        Which is why reconcile demands an operation id: resolving "the
        ambiguous one" would close whichever the reader happened to see.
        """
        _attach()
        # Both post cleanly first: once one is unresolved the session is
        # closed, so the only way to have two outstanding is for the
        # uncertainty to arrive after the writes — a later read failing to
        # confirm what the provider did with each.
        _queue("op-49", transport=Recorder())
        _queue("op-49b", transport=Recorder())
        cnc.mark_ambiguous(operation_id="op-49", reason="the turn was never confirmed")
        cnc.mark_ambiguous(operation_id="op-49b", reason="nor was this one")

        assert cnc.unresolved_ambiguity(SESSION)["operation_id"] == "op-49"
        assert cnc.unresolved_ambiguity(OTHER_SESSION) is None

    def test_an_operation_already_ambiguous_is_not_re_marked(self):
        """Re-marking would rewrite the reason that explains the block."""
        _attach()
        _queue("op-49c", transport=FailsOnEnter())

        with pytest.raises(cnc.NativeControlConflict):
            cnc.mark_ambiguous(operation_id="op-49c", reason="restating the same uncertainty")


class TestReads:
    def test_an_unknown_operation_reads_as_absent_rather_than_erroring(self):
        assert cnc.get("never-existed") is None

    def test_a_read_failure_fails_closed(self, monkeypatch):
        def _boom():
            raise RuntimeError("database gone")

        monkeypatch.setattr(database, "SessionLocal", _boom)
        with pytest.raises(cnc.NativeControlUnavailable, match="could not read"):
            cnc.get("op-any")
        with pytest.raises(cnc.NativeControlUnavailable, match="could not read"):
            cnc.unresolved_ambiguity(SESSION)

    def test_an_unwritable_journal_fails_closed_with_nothing_typed(self, monkeypatch):
        """A journal that cannot be written must not be followed by a write."""
        _attach()

        def _boom():
            raise RuntimeError("database gone")

        monkeypatch.setattr(database, "SessionLocal", _boom)
        recorder = Recorder()

        with pytest.raises(cnc.NativeControlUnavailable, match="could not journal"):
            _queue("op-50", transport=recorder)
        assert recorder.calls == []

    def test_a_concurrent_open_of_the_same_operation_is_read_as_a_replay(self, monkeypatch):
        """A primary-key collision means somebody else opened it first.

        That is a replay, not a failure: the retry re-reads and lets the
        identity check decide — which is what keeps a race from typing the
        message twice.
        """
        _attach()
        real_fetch = cnc._fetch
        seen: list[int] = []

        def _racing_fetch(db, operation_id):
            # The pre-insert read misses a row a concurrent caller is about
            # to commit; every later read sees it.
            seen.append(1)
            return None if len(seen) == 1 else real_fetch(db, operation_id)

        # A first, ordinary call puts the row in place.
        _queue("op-51", text="raced")
        monkeypatch.setattr(cnc, "_fetch", _racing_fetch)
        recorder = Recorder()

        replay = _queue("op-51", text="raced", transport=recorder)

        assert replay["operation_id"] == "op-51"
        assert recorder.calls == []

    def test_a_race_on_one_id_with_different_content_is_a_conflict_not_a_replay(self, monkeypatch):
        """The loser of the race must not be answered with the winner's outcome.

        Silently returning it would make at-most-once mean nothing: the
        second caller would be told its message was delivered when what
        was actually delivered was somebody else's.
        """
        _attach()
        _queue("op-52b", text="what the winner sent")

        real_fetch = cnc._fetch
        seen: list[int] = []

        def _racing_fetch(db, operation_id):
            seen.append(1)
            return None if len(seen) == 1 else real_fetch(db, operation_id)

        monkeypatch.setattr(cnc, "_fetch", _racing_fetch)
        recorder = Recorder()

        with pytest.raises(cnc.NativeControlConflict, match="not reusable"):
            _queue("op-52b", text="what the loser meant to send", transport=recorder)
        assert recorder.calls == []

    def test_a_racing_open_that_still_cannot_read_surfaces_the_real_failure(self, monkeypatch):
        _attach()

        def _always_boom(_db, _operation_id):
            raise RuntimeError("row unreadable")

        monkeypatch.setattr(cnc, "_fetch", _always_boom)
        recorder = Recorder()

        with pytest.raises(cnc.NativeControlUnavailable, match="could not journal"):
            _queue("op-52", transport=recorder)
        assert recorder.calls == []


class TestReplaysAcrossEveryKind:
    def test_a_steer_replay_never_types_again(self):
        _attach()
        first = Recorder()
        steer_args = {
            "native_session_id": SESSION,
            "terminal_id": TERMINAL,
            "generation": GENERATION,
            "execution_mode": em.NATIVE_TUI,
            "turn_id": TURN,
            "text": "steer once",
            "observation": _busy(TURN),
            "provider_version": PINNED,
        }
        cnc.steer(operation_id="op-60", transport=first, **steer_args)

        second = Recorder()
        replay = cnc.steer(operation_id="op-60", transport=second, **steer_args)

        assert replay["state"] == cnc.POSTED
        assert second.calls == []

    def test_a_control_replay_never_types_again(self):
        _attach()
        control_args = {
            "native_session_id": SESSION,
            "terminal_id": TERMINAL,
            "generation": GENERATION,
            "execution_mode": em.NATIVE_TUI,
            "command": cnc.CONTROL_COMPACT,
            "observation": _idle(),
            "provider_version": PINNED,
        }
        cnc.control(operation_id="op-61", transport=Recorder(), **control_args)

        second = Recorder()
        replay = cnc.control(operation_id="op-61", transport=second, **control_args)

        assert replay["state"] == cnc.POSTED
        assert second.calls == []


class TestDefensiveGuards:
    def test_an_undeliverable_plan_is_refused_at_the_transport_boundary_too(self):
        """Belt and braces, and the belt is load-bearing.

        Callers turn an undeliverable plan into a typed refusal before this
        point, so reaching here would be a bug in this module rather than a
        caller error — and it must not be discovered by half-typing a
        message into somebody's session.
        """
        _attach()
        plan = cnc.plan_composer_keystrokes("one\ntwo", provider_version="9.9.9")
        assert plan["deliverable"] is False
        recorder = Recorder()

        with pytest.raises(cnc.NativeControlInvalid, match="undeliverable plan"):
            cnc._post(operation_id="op-62", plan=plan, transport=recorder)
        assert recorder.calls == []

    def test_an_active_turn_id_that_is_not_a_string_is_refused(self):
        _attach()
        observation = dict(_idle())
        observation["active_turn_id"] = 17
        recorder = Recorder()

        with pytest.raises(cnc.NativeControlInvalid, match="must be a string or None"):
            _queue("op-63", observation=observation, transport=recorder)
        assert recorder.calls == []

    def test_an_empty_string_cannot_be_changed_by_trimming(self):
        """The invariance question has an answer even for nothing at all."""
        assert cnc._is_trim_invariant("") is True
        assert cnc._is_trim_invariant("x") is True
        assert cnc._is_trim_invariant(" x") is False
        assert cnc._is_trim_invariant("x ") is False

    def test_corrupt_journal_json_reads_as_absent_rather_than_raising(self):
        """A row that cannot be parsed must not take the whole read down."""
        _attach()
        _queue("op-64")
        with database.SessionLocal() as db:
            row = db.query(database.CodexNativeControlOperationModel).filter_by(
                operation_id="op-64"
            )
            row.update({"intent_json": "{not json at all"}, synchronize_session=False)
            db.commit()

        assert cnc.get("op-64")["intent"] is None


class TestUpdateIsCompareAndSwap:
    def test_an_update_against_a_missing_operation_is_not_found(self):
        with pytest.raises(cnc.NativeControlNotFound):
            cnc.mark_ambiguous(operation_id="never-opened", reason="nothing to mark")

    def test_a_lost_update_is_refused_rather_than_overwritten(self, monkeypatch):
        """The epoch is re-stated as a write condition, not just read.

        A row that moved between the read and the write loses the race
        instead of having the stale intention applied to it.
        """
        _attach()
        _queue("op-53")

        class StaleRow:
            operation_id = "op-53"
            state = cnc.POSTED
            epoch = 999

        monkeypatch.setattr(cnc, "_fetch", lambda _db, _op: StaleRow())

        with pytest.raises(cnc.NativeControlConflict, match="concurrent modification"):
            cnc.mark_ambiguous(operation_id="op-53", reason="stale writer")

    def test_an_unwritable_store_fails_closed_on_update(self, monkeypatch):
        _attach()
        _queue("op-59")

        def _boom():
            raise RuntimeError("database gone")

        monkeypatch.setattr(database, "SessionLocal", _boom)
        with pytest.raises(cnc.NativeControlUnavailable, match="update failed"):
            cnc.mark_ambiguous(operation_id="op-59", reason="cannot even record this")


class TestArtifactFreeText:
    @pytest.mark.parametrize("artifact", ["\x1b[200~payload", "^[[201~payload", "a\rb", "a\x1bb"])
    def test_artifacts_never_reach_a_composer(self, artifact):
        """tmux renders a paste sentinel as visible junk the model then reads."""
        _attach()
        recorder = Recorder()
        with pytest.raises(cnc.NativeControlInvalid):
            _queue("op-54", text=artifact, transport=recorder)
        assert recorder.calls == []

    def test_a_trailing_terminator_is_the_submit_and_is_not_typed(self):
        """Typing the newline and sending Enter would be two submissions."""
        _attach()
        recorder = Recorder()

        record = _queue("op-55", text="submit me\n", transport=recorder)

        assert recorder.calls == ["literal:submit me", "enter"]
        assert record["intent"]["keystroke_plan"]["trailing_terminator"] == "\n"

    def test_a_terminator_alone_is_not_a_message(self):
        _attach()
        with pytest.raises(cnc.NativeControlInvalid, match="no content"):
            _queue("op-56", text="\n")

    def test_the_model_input_digest_is_withheld_when_it_could_be_wrong(self):
        """This build's submit-time normalization was not read.

        A payload with no leading or trailing whitespace is invariant under
        trimming, so the digest cannot depend on the unknown. One that is
        not invariant would differ under the two possibilities, so the
        digest is withheld rather than guessed — a receipt naming a digest
        is read as evidence.
        """
        _attach()

        exact = _queue("op-57", text="no surrounding space")
        padded = _queue("op-58", text="  surrounded by space  ")

        exact_plan = exact["intent"]["keystroke_plan"]
        padded_plan = padded["intent"]["keystroke_plan"]
        assert exact_plan["model_input_sha256"] is not None
        assert exact_plan["model_input_is_composer_exact"] is True
        assert padded_plan["model_input_sha256"] is None
        assert padded_plan["submit_normalization_proven"] is False


class TestZeroTurnBootstrapIsAcceptedByTheAttachmentStore:
    def test_provider_bootstrap_is_not_mislabelled_as_acp(self):
        assert na.ACQUISITION_ZERO_TURN_BOOTSTRAP in na.ACQUISITION_METHODS

        intent = na.acquire_intent(
            acquisition_method=na.ACQUISITION_ZERO_TURN_BOOTSTRAP,
            acquisition_receipt={
                "native_session_id": SESSION,
                "id_source": "app_server_thread_start",
            },
            admits_only_new_instructions=True,
            replays_task_bytes=False,
            bootstrap_sent_no_turn=True,
            bootstrap_detached_before_launch=True,
        )

        # The store accepts only an intent it built itself, so this is the
        # check that the Codex launch path can actually claim a session.
        assert intent["schema"] == na.INTENT_SCHEMA
        record, acquired = na.declare(
            provider=cnc.PROVIDER,
            native_session_id=SESSION,
            terminal_id=TERMINAL,
            generation=GENERATION,
            execution_mode=em.NATIVE_TUI,
            intent=intent,
        )
        assert acquired is True
        assert record["state"] == na.DECLARED

    def test_bootstrap_proof_is_required(self):
        with pytest.raises(na.NativeAttachmentInvalid, match="zero-turn bootstrap"):
            na.acquire_intent(
                acquisition_method=na.ACQUISITION_ZERO_TURN_BOOTSTRAP,
                acquisition_receipt={"native_session_id": SESSION},
                admits_only_new_instructions=True,
                replays_task_bytes=False,
            )
