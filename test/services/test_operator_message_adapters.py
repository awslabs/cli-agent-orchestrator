"""The adapters' operator_message operation against a real store.

Lane C's at-most-once contract lives in the adapter operation store
(§8.3, OD6), so the replay, conflict, gating, and zero-bytes properties
are proven here against the real journaling code — the same discipline
the deployed queue/steer suites prove, with the whole-request digest as
the replay identity.
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.services import claude_native_control as cnc
from cli_agent_orchestrator.services import execution_mode as em
from cli_agent_orchestrator.services import kimi_native_control as knc
from cli_agent_orchestrator.services import native_attachment as na
from cli_agent_orchestrator.services.canonical_json import canonical_sha256

SESSION = "session_op9f21ac30"
TERMINAL = "terminal_op4d7b"
GENERATION = "gen_op1c0e"
DIGEST = canonical_sha256({"text": "hello", "attachments": [], "token_map": {}})


@pytest.fixture(autouse=True)
def _db(isolated_memory_db):
    return isolated_memory_db


class Recorder:
    """A transport that records exactly what it was asked to do."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def send_literal(self, text: str) -> None:
        self.calls.append(f"literal:{text}")

    def send_enter(self) -> None:
        self.calls.append("enter")

    def send_key(self, keystroke: str) -> None:
        self.calls.append(f"key:{keystroke}")


def _attach_kimi(**overrides):
    owner = {
        "provider": knc.PROVIDER,
        "native_session_id": SESSION,
        "terminal_id": TERMINAL,
        "generation": GENERATION,
        "execution_mode": em.NATIVE_TUI,
    }
    owner.update(overrides)
    intent = na.acquire_intent(
        acquisition_method=na.ACQUISITION_ACP_BOOTSTRAP,
        acquisition_receipt={"kind": "kimi-acp-session-new", "session_id": SESSION},
        admits_only_new_instructions=True,
        replays_task_bytes=False,
        bootstrap_sent_no_turn=True,
        bootstrap_detached_before_launch=True,
    )
    na.declare(**owner, intent=intent, pane_id="%7")
    na.mark_starting(**owner, pane_id="%7")
    return na.mark_attached(
        **owner,
        pane_id="%7",
        process_identity=na.process_identity(pid=4242, start_marker="88213"),
    )


def _attach_claude(**overrides):
    owner = {
        "provider": cnc.PROVIDER,
        "native_session_id": SESSION,
        "terminal_id": TERMINAL,
        "generation": GENERATION,
        "execution_mode": em.NATIVE_TUI,
    }
    owner.update(overrides)
    intent = na.acquire_intent(
        acquisition_method=na.ACQUISITION_ACP_BOOTSTRAP,
        acquisition_receipt={"kind": "claude-cli-session-id", "session_id": SESSION},
        admits_only_new_instructions=True,
        replays_task_bytes=False,
        bootstrap_sent_no_turn=True,
        bootstrap_detached_before_launch=True,
    )
    na.declare(**owner, intent=intent, pane_id="%7")
    na.mark_starting(**owner, pane_id="%7")
    return na.mark_attached(
        **owner,
        pane_id="%7",
        process_identity=na.process_identity(pid=4242, start_marker="88213"),
    )


def _idle_kimi():
    return knc.turn_observation(
        active_turn_id=None, observed_at="2026-07-29T00:00:00Z", observer="test"
    )


def _idle_claude():
    return cnc.turn_observation(
        active_turn_id=None, observed_at="2026-07-29T00:00:00Z", observer="test"
    )


def _kimi_message(
    transport,
    *,
    operation_id="op_1",
    text="hello",
    digest=DIGEST,
    observation=None,
    provider_version="0.29.2",
):
    return knc.operator_message(
        operation_id=operation_id,
        native_session_id=SESSION,
        terminal_id=TERMINAL,
        generation=GENERATION,
        execution_mode=em.NATIVE_TUI,
        text=text,
        payload_sha256=digest,
        observation=observation or _idle_kimi(),
        transport=transport,
        provider_version=provider_version,
    )


class TestKimiOperatorMessage:
    def test_happy_path_posts_with_the_plan_and_exactly_one_enter(self):
        _attach_kimi()
        transport = Recorder()
        record = _kimi_message(transport, text="line one\nline two")
        assert record["state"] == "posted"
        assert record["kind"] == "operator-message"
        assert record["payload_sha256"] == DIGEST
        # The proven composer plan: C-j between lines, End burst reset, one Enter.
        assert transport.calls == [
            "literal:line one",
            "key:C-j",
            "literal:line two",
            "key:End",
            "enter",
        ]
        assert record["intent"]["keystroke_plan"]["line_count"] == 2

    def test_an_identical_replay_never_reaches_the_transport(self):
        _attach_kimi()
        first_transport = Recorder()
        first = _kimi_message(first_transport)
        replay_transport = Recorder()
        replayed = _kimi_message(replay_transport)
        assert first["state"] == "posted"
        assert replayed["state"] == "posted"
        assert replay_transport.calls == []

    def test_a_divergent_digest_on_a_reused_id_conflicts(self):
        _attach_kimi()
        _kimi_message(Recorder())
        with pytest.raises(knc.NativeControlConflict):
            _kimi_message(Recorder(), digest=canonical_sha256({"text": "different"}))

    def test_an_active_turn_refuses_with_zero_bytes(self):
        _attach_kimi()
        transport = Recorder()
        busy = knc.turn_observation(
            active_turn_id="turn_1", observed_at="2026-07-29T00:00:01Z", observer="test"
        )
        record = _kimi_message(transport, observation=busy)
        assert record["state"] == "refused"
        assert record["refusal_reason"] == "active_turn_in_progress"
        assert transport.calls == []

    def test_an_unproven_build_refuses_multiline_with_zero_bytes(self):
        _attach_kimi()
        transport = Recorder()
        record = _kimi_message(transport, text="a\nb", provider_version="9.9.9")
        assert record["state"] == "refused"
        assert record["refusal_reason"] == "composer_newline_unproven"
        assert transport.calls == []

    def test_an_unresolved_ambiguity_blocks_the_session(self):
        _attach_kimi()
        first = _kimi_message(Recorder())
        knc.mark_ambiguous(operation_id=first["operation_id"], reason="lost mid-write")
        record = _kimi_message(
            Recorder(), operation_id="op_2", digest=canonical_sha256({"text": "second"})
        )
        assert record["state"] == "refused"
        assert record["refusal_reason"] == "unresolved_ambiguity"

    def test_an_owner_mismatch_refuses_with_zero_bytes(self):
        _attach_kimi(terminal_id="terminal_other")
        transport = Recorder()
        record = _kimi_message(transport)
        assert record["state"] == "refused"
        assert record["refusal_reason"] == "attachment_not_owned"
        assert transport.calls == []

    def test_exact_id_get_reads_the_journaled_record(self):
        _attach_kimi()
        _kimi_message(Recorder())
        found = knc.get("op_1")
        assert found is not None
        assert found["state"] == "posted"
        assert found["kind"] == "operator-message"


class TestClaudeOperatorMessage:
    def test_happy_path_posts_with_one_enter(self):
        _attach_claude()
        transport = Recorder()
        record = cnc.operator_message(
            operation_id="op_c1",
            native_session_id=SESSION,
            terminal_id=TERMINAL,
            generation=GENERATION,
            execution_mode=em.NATIVE_TUI,
            text="analyze this image: /staged/att-1.png",
            payload_sha256=DIGEST,
            observation=_idle_claude(),
            transport=transport,
            provider_version="2.1.220",
        )
        assert record["state"] == "posted"
        assert record["kind"] == "operator-message"
        assert transport.calls[0] == "literal:analyze this image: /staged/att-1.png"
        assert transport.calls[-1] == "enter"
        assert transport.calls.count("enter") == 1

    def test_an_identical_replay_never_reaches_the_transport(self):
        _attach_claude()
        cnc.operator_message(
            operation_id="op_c1",
            native_session_id=SESSION,
            terminal_id=TERMINAL,
            generation=GENERATION,
            execution_mode=em.NATIVE_TUI,
            text="hello",
            payload_sha256=DIGEST,
            observation=_idle_claude(),
            transport=Recorder(),
            provider_version="2.1.220",
        )
        replay_transport = Recorder()
        replayed = cnc.operator_message(
            operation_id="op_c1",
            native_session_id=SESSION,
            terminal_id=TERMINAL,
            generation=GENERATION,
            execution_mode=em.NATIVE_TUI,
            text="hello",
            payload_sha256=DIGEST,
            observation=_idle_claude(),
            transport=replay_transport,
            provider_version="2.1.220",
        )
        assert replayed["state"] == "posted"
        assert replay_transport.calls == []

    def test_a_divergent_digest_on_a_reused_id_conflicts(self):
        _attach_claude()
        cnc.operator_message(
            operation_id="op_c1",
            native_session_id=SESSION,
            terminal_id=TERMINAL,
            generation=GENERATION,
            execution_mode=em.NATIVE_TUI,
            text="hello",
            payload_sha256=DIGEST,
            observation=_idle_claude(),
            transport=Recorder(),
            provider_version="2.1.220",
        )
        with pytest.raises(cnc.NativeControlConflict):
            cnc.operator_message(
                operation_id="op_c1",
                native_session_id=SESSION,
                terminal_id=TERMINAL,
                generation=GENERATION,
                execution_mode=em.NATIVE_TUI,
                text="hello",
                payload_sha256=canonical_sha256({"text": "different"}),
                observation=_idle_claude(),
                transport=Recorder(),
                provider_version="2.1.220",
            )
