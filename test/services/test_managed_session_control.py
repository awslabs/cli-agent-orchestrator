from __future__ import annotations

import contextlib
import hashlib
import itertools
import threading
import time
from unittest.mock import MagicMock

import pytest

from cli_agent_orchestrator.services import managed_provider_bridge as bridge
from cli_agent_orchestrator.services.managed_session_control import (
    ACCEPTED,
    AMBIGUOUS,
    COMPLETED,
    QUEUED,
    REFUSED,
    SUBMITTED,
    SessionControlJournal,
    SessionControlRefused,
)


def _journal_begin(journal, operation_id="op-1", request_sha256="a" * 64):
    return journal.begin(
        operation_id=operation_id,
        terminal_id="deadbeef",
        generation="gen-1",
        action="follow-up",
        request_sha256=request_sha256,
        provider="kimi_cli",
        provider_session_id="session-1",
    )


def test_journal_enforces_identity_and_append_only_states(tmp_path):
    journal = SessionControlJournal(tmp_path / "control.db")

    assert _journal_begin(journal)["state"] == QUEUED
    assert journal.transition("op-1", SUBMITTED)["state"] == SUBMITTED
    assert journal.transition("op-1", ACCEPTED)["state"] == ACCEPTED
    assert journal.transition("op-1", COMPLETED, result={"ok": True})["state"] == COMPLETED
    assert journal.get("op-1")["result"] == {"ok": True}

    with pytest.raises(SessionControlRefused, match="different request bytes"):
        _journal_begin(journal, request_sha256="b" * 64)
    with pytest.raises(SessionControlRefused, match="illegal managed-session transition"):
        journal.transition("op-1", SUBMITTED)


def test_ambiguous_is_terminal_for_automated_replay(tmp_path):
    journal = SessionControlJournal(tmp_path / "control.db")
    _journal_begin(journal)
    journal.transition("op-1", SUBMITTED)
    journal.transition("op-1", AMBIGUOUS, reason_code="response_lost")

    with pytest.raises(SessionControlRefused, match="illegal managed-session transition"):
        journal.transition("op-1", ACCEPTED)


def test_journal_idempotent_lookup_repeat_and_latest_paths(tmp_path):
    journal = SessionControlJournal(tmp_path / "control.db")
    opened = _journal_begin(journal)

    assert _journal_begin(journal) == opened
    assert journal.transition("op-1", QUEUED) == opened
    journal.transition("op-1", SUBMITTED)
    journal.transition("op-1", ACCEPTED)
    journal.transition("op-1", COMPLETED, result={"answer": 42})
    assert journal.latest(limit=0)[0]["result"] == {"answer": 42}

    with pytest.raises(SessionControlRefused, match="unknown managed-session operation"):
        journal.get("missing")
    with pytest.raises(SessionControlRefused, match="unknown managed-session operation"):
        journal.transition("missing", REFUSED)


class _Rpc:
    def __init__(self, *, commands=None):
        self.calls = []
        self.commands = commands or []

    def notifications_since(self, _index):
        return (
            [
                {
                    "method": "session/update",
                    "params": {
                        "sessionId": "session-1",
                        "update": {
                            "sessionUpdate": "available_commands_update",
                            "availableCommands": [{"name": name} for name in self.commands],
                        },
                    },
                }
            ],
            1,
        )

    def notification_count(self):
        return 0

    def start_request(self, method, params):
        self.calls.append((method, params))
        return 7

    def wait_notification(self, predicate, *, start_index, timeout):
        item = {
            "method": "session/update",
            "params": {
                "sessionId": "session-1",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "Compacting conversation context…"},
                },
            },
        }
        assert predicate(item)
        return item

    def wait_response(self, request_id, timeout):
        assert request_id == 7
        return {"stopReason": "end_turn"}

    def request(self, method, params, timeout=30):
        self.calls.append((method, params))
        if method == "session/set_config_option":
            return {
                "configOptions": [
                    {
                        "id": "model",
                        "category": "model",
                        "currentValue": params["value"],
                    },
                    {
                        "id": "thinking",
                        "category": "thought_level",
                        "currentValue": "max",
                    },
                ]
            }
        raise AssertionError(method)

    def notify(self, method, params):
        self.calls.append((method, params))


def _session(rpc):
    session = object.__new__(bridge._ProviderSession)
    session.request = {
        "reservation_id": "reservation-1",
        "terminal_id": "deadbeef",
        "generation": "gen-1",
        "provider": "kimi_cli",
        "model": "kimi-code/k3",
        "effort": "max",
        "working_directory": "/tmp/worktree",
    }
    session.provider = "kimi_cli"
    session.rpc = rpc
    session.provider_session_id = "session-1"
    session.readiness = {"provider_version": "0.29.0"}
    session.current_model = "kimi-code/k3"
    session.current_effort = "max"
    session._config_options = []
    session._active_prompt_lock = threading.Lock()
    session._active_prompt_request_id = None
    session._current_turn_id = None
    session._turn_sequence = 0
    return session


def _begin(journal, session, command):
    journal.begin(
        operation_id=command["operation_id"],
        terminal_id=command["terminal_id"],
        generation=command["generation"],
        action=command["action"],
        request_sha256=bridge._digest(command),
        provider=session.provider,
        provider_session_id=session.provider_session_id,
    )


def _command(action, **values):
    return {
        "op": "session.op.begin",
        "reservation_id": "reservation-1",
        "terminal_id": "deadbeef",
        "generation": "gen-1",
        "operation_id": f"op-{action}",
        "action": action,
        **values,
    }


def test_compact_uses_capability_gated_acp_prompt(tmp_path):
    rpc = _Rpc(commands=["compact"])
    session = _session(rpc)
    journal = SessionControlJournal(tmp_path / "control.db")
    command = _command("compact", instruction="preserve decisions")
    _begin(journal, session, command)

    receipt = session.session_operation(command, journal)

    assert receipt["state"] in {SUBMITTED, ACCEPTED, COMPLETED}
    for _ in range(100):
        receipt = session.reconcile_session_operation(journal, command["operation_id"])
        if receipt["state"] == COMPLETED:
            break
        time.sleep(0.01)
    assert receipt["state"] == COMPLETED
    assert rpc.calls == [
        (
            "session/prompt",
            {
                "sessionId": "session-1",
                "prompt": [{"type": "text", "text": "/compact preserve decisions"}],
            },
        )
    ]


def test_compact_without_advertised_capability_refuses_before_provider_io(tmp_path):
    rpc = _Rpc()
    session = _session(rpc)
    journal = SessionControlJournal(tmp_path / "control.db")
    command = _command("compact")
    _begin(journal, session, command)

    receipt = session.session_operation(command, journal)

    assert receipt["state"] == REFUSED
    assert receipt["reason_code"] == "capability_unsupported"
    assert rpc.calls == []


def test_latest_capability_update_revokes_stale_compact_before_provider_io(tmp_path):
    rpc = _Rpc()
    rpc.notifications_since = lambda _index: (
        [
            {
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "available_commands_update",
                        "availableCommands": [{"name": "compact"}],
                    }
                },
            },
            {
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "available_commands_update",
                        "availableCommands": [],
                    }
                },
            },
        ],
        2,
    )
    session = _session(rpc)
    journal = SessionControlJournal(tmp_path / "control.db")
    command = _command("compact")
    _begin(journal, session, command)

    receipt = session.session_operation(command, journal)

    assert receipt["state"] == REFUSED
    assert receipt["reason_code"] == "capability_unsupported"
    assert rpc.calls == []


def test_route_change_uses_config_option_and_updates_attested_route(tmp_path):
    rpc = _Rpc()
    session = _session(rpc)
    journal = SessionControlJournal(tmp_path / "control.db")
    command = _command("route-set", config_id="model", value="kimi-code/k2.7")
    _begin(journal, session, command)

    receipt = session.session_operation(command, journal)

    assert receipt["state"] == COMPLETED
    assert receipt["model"] == "kimi-code/k2.7"
    assert rpc.calls[0][0] == "session/set_config_option"


def test_follow_up_receipt_persists_digest_not_message(tmp_path, monkeypatch):
    rpc = _Rpc()
    session = _session(rpc)
    journal_path = tmp_path / "control.db"
    journal = SessionControlJournal(journal_path)
    message = "sensitive human follow-up"
    command = _command("follow-up", message=message)
    _begin(journal, session, command)
    monkeypatch.setattr(
        session,
        "_submit_provider_turn",
        lambda *_args, **_kwargs: (
            "turn-1",
            "kimi-session-update",
            {"provider_request_id": 9},
        ),
    )
    monkeypatch.setattr(bridge, "_write_route_receipt", lambda *_args, **_kwargs: None)

    receipt = session.session_operation(command, journal)

    assert receipt["state"] == ACCEPTED
    assert receipt["result"]["message_sha256"] == hashlib.sha256(message.encode()).hexdigest()
    assert message.encode() not in journal_path.read_bytes()


_OPERATION_SEQUENCE = itertools.count()


def _run_operation(journal, session, action, **values):
    command = _command(action, **values)
    command["operation_id"] = f"op-{action}-{next(_OPERATION_SEQUENCE)}"
    _begin(journal, session, command)
    return session.session_operation(command, journal)


def test_route_query_resume_and_existing_operation_paths(tmp_path):
    session = _session(_Rpc(commands=["compact"]))
    journal = SessionControlJournal(tmp_path / "control.db")

    route_command = _command("route-query")
    _begin(journal, session, route_command)
    route = session.session_operation(route_command, journal)
    assert route["state"] == COMPLETED
    assert route["result"]["capabilities"]["compact"] is True
    assert session.session_operation(route_command, journal)["state"] == COMPLETED

    resumed = _run_operation(journal, session, "resume")
    assert resumed["state"] == REFUSED
    assert resumed["reason_code"] == "resume_requires_new_generation"

    session.provider = "codex"
    unsupported = _run_operation(journal, session, "resume-status")
    assert unsupported["state"] == REFUSED
    assert unsupported["reason_code"] == "capability_unsupported"
    unsupported = _run_operation(
        journal,
        session,
        "route-set",
        config_id="model",
        value="gpt-5.6-sol",
    )
    assert unsupported["state"] == REFUSED
    assert unsupported["reason_code"] == "capability_unsupported"
    inactive = _run_operation(journal, session, "cancel")
    assert inactive["state"] == REFUSED
    assert inactive["reason_code"] == "turn_not_active"


def test_kimi_resume_status_success_and_response_loss(tmp_path):
    session = _session(_Rpc())
    journal = SessionControlJournal(tmp_path / "control.db")

    session.rpc.request = lambda *_args, **_kwargs: {
        "sessions": [
            "ignored",
            {"sessionId": "other"},
            {"sessionId": "session-1", "cwd": "/tmp/worktree"},
        ]
    }
    receipt = _run_operation(journal, session, "resume-status")
    assert receipt["state"] == COMPLETED
    assert receipt["result"]["resumable_session"]["sessionId"] == "session-1"

    def unavailable(*_args, **_kwargs):
        raise TimeoutError("provider did not respond")

    session.rpc.request = unavailable
    receipt = _run_operation(journal, session, "resume-status")
    assert receipt["state"] == AMBIGUOUS
    assert receipt["reason_code"] == "provider_response_unavailable"


def test_kimi_cancel_inactive_accepted_and_ambiguous_paths(tmp_path):
    session = _session(_Rpc())
    journal = SessionControlJournal(tmp_path / "control.db")

    inactive = _run_operation(journal, session, "cancel")
    assert inactive["reason_code"] == "turn_not_active"

    session._active_prompt_request_id = 17
    accepted = _run_operation(journal, session, "cancel")
    assert accepted["state"] == ACCEPTED
    assert accepted["result"]["cancelled_provider_request_id"] == 17

    def notify_unavailable(*_args, **_kwargs):
        raise BrokenPipeError("provider channel lost")

    session.rpc.notify = notify_unavailable
    ambiguous = _run_operation(journal, session, "cancel")
    assert ambiguous["state"] == AMBIGUOUS
    assert ambiguous["reason_code"] == "cancel_delivery_ambiguous"


def test_codex_cancel_success_and_response_loss_paths(tmp_path):
    session = _session(_Rpc())
    session.provider = "codex"
    session._current_turn_id = "turn-1"
    journal = SessionControlJournal(tmp_path / "control.db")

    session.rpc.request = lambda *_args, **_kwargs: {"interrupted": True}
    completed = _run_operation(journal, session, "cancel")
    assert completed["state"] == COMPLETED
    assert completed["result"] == {"interrupted": True}

    def unavailable(*_args, **_kwargs):
        raise TimeoutError("turn interrupt response lost")

    session.rpc.request = unavailable
    ambiguous = _run_operation(journal, session, "cancel")
    assert ambiguous["state"] == AMBIGUOUS
    assert ambiguous["reason_code"] == "cancel_outcome_ambiguous"


def test_route_set_busy_invalid_refused_ambiguous_and_unattested_paths(tmp_path):
    session = _session(_Rpc())
    journal = SessionControlJournal(tmp_path / "control.db")

    session._active_prompt_request_id = 3
    busy = _run_operation(
        journal,
        session,
        "route-set",
        config_id="model",
        value="kimi-code/k2.7",
    )
    assert busy["reason_code"] == "turn_busy"
    session._active_prompt_request_id = None

    invalid = _run_operation(
        journal,
        session,
        "route-set",
        config_id="invalid",
        value="value",
    )
    assert invalid["reason_code"] == "invalid_route"

    def refused(*_args, **_kwargs):
        raise bridge.BridgeError("provider request failed: route denied")

    session.rpc.request = refused
    receipt = _run_operation(
        journal,
        session,
        "route-set",
        config_id="model",
        value="kimi-code/k2.7",
    )
    assert receipt["state"] == REFUSED
    assert receipt["reason_code"] == "route_refused"

    def ambiguous(*_args, **_kwargs):
        raise TimeoutError("response lost")

    session.rpc.request = ambiguous
    receipt = _run_operation(
        journal,
        session,
        "route-set",
        config_id="model",
        value="kimi-code/k2.7",
    )
    assert receipt["state"] == AMBIGUOUS
    assert receipt["reason_code"] == "route_outcome_ambiguous"

    session.rpc.request = lambda *_args, **_kwargs: {
        "configOptions": [{"id": "model", "category": "model", "currentValue": "kimi-code/k3"}]
    }
    receipt = _run_operation(
        journal,
        session,
        "route-set",
        config_id="model",
        value="kimi-code/k2.7",
    )
    assert receipt["state"] == REFUSED
    assert receipt["reason_code"] == "route_not_applied"


def test_compact_and_follow_up_typed_refusal_and_ambiguity_paths(tmp_path, monkeypatch):
    session = _session(_Rpc(commands=["compact"]))
    journal = SessionControlJournal(tmp_path / "control.db")

    session._active_prompt_request_id = 9
    busy = _run_operation(journal, session, "compact")
    assert busy["reason_code"] == "turn_busy"
    session._active_prompt_request_id = None

    invalid = _run_operation(journal, session, "compact", instruction={"not": "text"})
    assert invalid["reason_code"] == "invalid_instruction"

    empty = _run_operation(journal, session, "follow-up", message=" ")
    assert empty["reason_code"] == "message_empty"

    def provider_busy(*_args, **_kwargs):
        raise bridge.SessionOperationRefused("turn_busy", "wait for the active turn")

    monkeypatch.setattr(session, "_submit_provider_turn", provider_busy)
    refused = _run_operation(journal, session, "follow-up", message="continue")
    assert refused["state"] == REFUSED
    assert refused["reason_code"] == "turn_busy"

    def response_lost(*_args, **_kwargs):
        raise TimeoutError("provider response lost")

    monkeypatch.setattr(session, "_submit_provider_turn", response_lost)
    ambiguous = _run_operation(journal, session, "follow-up", message="continue")
    assert ambiguous["state"] == AMBIGUOUS
    assert ambiguous["reason_code"] == "prompt_outcome_ambiguous"


def test_session_operation_validation_unknown_action_and_close(tmp_path):
    session = _session(_Rpc())
    journal = SessionControlJournal(tmp_path / "control.db")

    ready_rpc = session.rpc
    session.rpc = None
    with pytest.raises(bridge.BridgeError, match="native session is not ready"):
        session.session_operation(_command("route-query"), journal)
    session.rpc = ready_rpc

    with pytest.raises(bridge.BridgeError, match="exact bridge generation"):
        session.session_operation(
            _command("route-query", generation="wrong-generation"),
            journal,
        )
    with pytest.raises(bridge.BridgeError, match="omitted operation_id"):
        session.session_operation(
            {
                "reservation_id": "reservation-1",
                "terminal_id": "deadbeef",
                "generation": "gen-1",
                "action": "route-query",
            },
            journal,
        )
    with pytest.raises(bridge.BridgeError, match="omitted action"):
        session.session_operation(
            {
                "reservation_id": "reservation-1",
                "terminal_id": "deadbeef",
                "generation": "gen-1",
                "operation_id": "op-no-action",
            },
            journal,
        )

    unsupported = _run_operation(journal, session, "future-control")
    assert unsupported["state"] == REFUSED
    assert unsupported["reason_code"] == "capability_unsupported"

    session.rpc = MagicMock()
    session.close()
    session.rpc.close.assert_called_once_with()


def test_compact_submission_and_watcher_loss_are_durably_ambiguous(tmp_path, monkeypatch):
    session = _session(_Rpc(commands=["compact"]))
    journal = SessionControlJournal(tmp_path / "control.db")
    monkeypatch.setattr(
        session,
        "_admission_critical_section",
        lambda: contextlib.nullcontext(),
    )

    def submission_lost(*_args, **_kwargs):
        raise TimeoutError("compact submission response lost")

    session.rpc.start_request = submission_lost
    receipt = _run_operation(journal, session, "compact")
    assert receipt["state"] == AMBIGUOUS
    assert receipt["reason_code"] == "compact_submission_ambiguous"

    command = _command("compact")
    command["operation_id"] = "op-compact-watcher"
    _begin(journal, session, command)
    journal.transition(command["operation_id"], SUBMITTED)
    session._active_prompt_request_id = 41

    def acceptance_lost(*_args, **_kwargs):
        raise TimeoutError("compact acceptance response lost")

    session.rpc.wait_notification = acceptance_lost
    session._watch_compact_operation(
        journal,
        command["operation_id"],
        41,
        {"sessionId": "session-1"},
        0,
    )
    operation = journal.get(command["operation_id"])
    assert operation["state"] == AMBIGUOUS
    assert operation["reason_code"] == "compact_outcome_ambiguous"
    assert session._active_prompt_request_id is None


def test_effort_route_and_generation_fence_paths(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services import generation_fence

    session = _session(_Rpc())
    journal = SessionControlJournal(tmp_path / "control.db")
    session.rpc.request = lambda *_args, **_kwargs: {
        "configOptions": [
            {
                "id": "thinking",
                "category": "thought_level",
                "currentValue": "high",
            }
        ]
    }
    receipt = _run_operation(
        journal,
        session,
        "route-set",
        config_id="thinking",
        value="high",
    )
    assert receipt["state"] == COMPLETED
    assert session.current_effort == "high"

    monkeypatch.setattr(generation_fence, "assert_admission_open", lambda *_args: None)
    session._assert_fence_open()

    def fenced(*_args):
        raise generation_fence.FencedError("generation is sealed")

    monkeypatch.setattr(generation_fence, "assert_admission_open", fenced)
    with pytest.raises(bridge.BridgeError, match="generation is sealed"):
        session._assert_fence_open()


def test_capability_filter_terminal_refusal_and_reconciliation_paths(tmp_path):
    session = _session(_Rpc())
    journal = SessionControlJournal(tmp_path / "control.db")

    rpc = session.rpc
    session.rpc = None
    assert session._available_command_names() == set()
    session.rpc = rpc
    session.rpc.notifications_since = lambda _index: (
        [
            {"method": "future/event"},
            {
                "method": "session/update",
                "params": {"update": {"sessionUpdate": "usage_update"}},
            },
            {
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "available_commands_update",
                        "availableCommands": [
                            {"name": "compact"},
                            {"name": 42},
                            "ignored",
                        ],
                    }
                },
            },
        ],
        3,
    )
    assert session._available_command_names() == {"compact"}

    command = _command("future-control")
    command["operation_id"] = "op-terminal-refusal"
    _begin(journal, session, command)
    refused = session._refuse_control(journal, command["operation_id"], "unsupported", "no")
    assert refused["state"] == REFUSED
    assert (
        session._refuse_control(journal, command["operation_id"], "ignored", "ignored") == refused
    )

    follow_up = _command("follow-up", message="continue")
    follow_up["operation_id"] = "op-reconcile-follow-up"
    _begin(journal, session, follow_up)
    journal.transition(follow_up["operation_id"], SUBMITTED)
    journal.transition(follow_up["operation_id"], ACCEPTED)
    receipt = session.reconcile_session_operation(journal, follow_up["operation_id"])
    assert receipt["state"] == COMPLETED
    assert receipt["result"] == {"native_turn_active": False}


def test_inbox_validation_and_generation_fence_refusal(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services import generation_fence

    session = _session(_Rpc())
    message = "continue"
    command = {
        "reservation_id": "reservation-1",
        "terminal_id": "deadbeef",
        "generation": "gen-1",
        "message": message,
        "message_id": "message-1",
        "message_sha256": hashlib.sha256(message.encode()).hexdigest(),
    }

    rpc = session.rpc
    session.rpc = None
    with pytest.raises(bridge.BridgeError, match="native session is not ready"):
        session.deliver_inbox(command)
    session.rpc = rpc

    with pytest.raises(bridge.BridgeError, match="omitted the message"):
        session.deliver_inbox({**command, "message": ""})
    with pytest.raises(bridge.BridgeError, match="exact message id"):
        session.deliver_inbox({**command, "message_id": ""})

    @contextlib.contextmanager
    def fenced_generation():
        raise generation_fence.FencedError("generation was sealed")
        yield

    monkeypatch.setattr(session, "_admission_critical_section", fenced_generation)
    with pytest.raises(bridge.BridgeError, match="generation was sealed"):
        session.deliver_inbox(command)
