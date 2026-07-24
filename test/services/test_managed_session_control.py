from __future__ import annotations

import hashlib
import threading
import time

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

    assert receipt["state"] in {SUBMITTED, COMPLETED}
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
