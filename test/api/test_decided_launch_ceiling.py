"""Restricted creators retain #948's delegation ceiling on decision-routed launches.

Exercise #954's real HTTP handlers with a real ``LaunchPlan`` so the decided
branch is taken for local and remote delegation and script-tier workflow steps.
Refused launches still prepare exactly one decision request before the ceiling
check; this pins the current ordering pending the architect's separate ruling.
"""

from test.api.test_delegation_script_tier import TestRunStepHandler as _ScriptTier
from test.api.test_delegation_script_tier import isolated_journal  # noqa: F401
from types import SimpleNamespace

import pytest

from cli_agent_orchestrator.constants import (
    CALLBACK_TERMINAL_ID_ENV,
    CALLBACK_URL_ENV,
    ROLE_TOOL_DEFAULTS,
    TERMINALS_RUN_STEP_ROUTE,
)
from cli_agent_orchestrator.decisions.types import LaunchPlan

REVIEWER = ROLE_TOOL_DEFAULTS["reviewer"]


@pytest.fixture(autouse=True)
def decisions_on(monkeypatch):
    from cli_agent_orchestrator.api import main
    from cli_agent_orchestrator.decisions import engine

    state = SimpleNamespace(requests=[], binds=[])

    async def fake_prepare(req, **kw):
        state.requests.append(req)
        return LaunchPlan(model=None)

    monkeypatch.setattr(main, "prepare_launch", fake_prepare)
    record_bind = lambda plan, **k: state.binds.append(k.get("launch_status"))
    monkeypatch.setattr(main, "bind_launch", record_bind)
    monkeypatch.setattr(main, "launch_note", lambda *a, **k: None)
    monkeypatch.setattr(engine, "bind_launch", record_bind)
    return state


class TestScriptTierUnderDecisionsOn(_ScriptTier):
    """#948's script-tier handler suite, re-run with the decision branch live."""


@pytest.mark.usefixtures("isolated_memory_db", "isolated_journal")
class TestDecidedLaunchKeepsCreatorCeiling:
    def _refused(self, resp, backend, decisions_on, tools=True):
        assert len(decisions_on.requests) == 1, "the decision-routed branch must be taken"
        # 76b: a ceiling refusal is recorded as not launched, never as a failed launch.
        assert decisions_on.binds and set(decisions_on.binds) == {"not_launched"}
        assert resp.status_code == 403, resp.text
        detail = resp.json()["detail"]
        assert "Delegation refused" in detail
        if tools:
            assert "execute_bash" in detail
        backend.create_window.assert_not_called()
        assert _ScriptTier._developer_rows() == 0

    def test_in_session_decided_handoff_by_reviewer(self, client, monkeypatch, decisions_on):
        backend = _ScriptTier._launch_capable(monkeypatch)
        resp = client.post(
            "/sessions/cao-s/terminals",
            params={
                "agent_profile": "developer",
                "provider": "claude_code",
                "caller_id": "0000beef",
            },
            json={"delegation_message": "run make deploy", "delegation_kind": "handoff"},
        )
        self._refused(resp, backend, decisions_on)

    def test_in_session_decided_assign_by_reviewer(self, client, monkeypatch, decisions_on):
        backend = _ScriptTier._launch_capable(monkeypatch)
        resp = client.post(
            "/sessions/cao-s/terminals",
            params={
                "agent_profile": "developer",
                "provider": "claude_code",
                "caller_id": "0000beef",
                "defer_init": "true",
            },
            json={
                "delegation_message": "run make deploy",
                "initial_message": "run make deploy",
                "initial_message_orchestration_type": "assign",
            },
        )
        self._refused(resp, backend, decisions_on)

    def test_local_run_step_decided_handoff_by_reviewer(
        self, client, monkeypatch, decisions_on, tmp_path
    ):
        backend = _ScriptTier._launch_capable(monkeypatch)
        resp = client.post(
            TERMINALS_RUN_STEP_ROUTE,
            json={
                "provider": "claude_code",
                "agent": "developer",
                "prompt": "run make deploy",
                "session_name": "cao-s",
                "working_directory": str(tmp_path),
                "caller_id": "0000beef",
                "orchestration_type": "handoff",
                "delegation_message": "run make deploy",
            },
        )
        self._refused(resp, backend, decisions_on)

    def test_remote_run_step_decided_handoff_by_asserted_ceiling(
        self, client, monkeypatch, decisions_on
    ):
        backend = _ScriptTier._launch_capable(monkeypatch)
        backend.session_exists.return_value = False
        resp = client.post(
            TERMINALS_RUN_STEP_ROUTE,
            json={
                "provider": "claude_code",
                "agent": "developer",
                "prompt": "run make deploy",
                "caller_ceiling": REVIEWER,
                "orchestration_type": "handoff",
                "delegation_message": "run make deploy",
            },
        )
        self._refused(resp, backend, decisions_on)

    def test_remote_create_session_decided_assign_by_asserted_ceiling(
        self, client, monkeypatch, decisions_on
    ):
        backend = _ScriptTier._launch_capable(monkeypatch)
        backend.session_exists.return_value = False
        resp = client.post(
            "/sessions",
            params={"agent_profile": "developer", "provider": "claude_code"},
            json={
                "caller_ceiling": REVIEWER,
                "env_vars": {
                    CALLBACK_URL_ENV: "http://cao-supervisor:9889",
                    CALLBACK_TERMINAL_ID_ENV: "0000feed",
                },
                "initial_message": "run make deploy",
                "initial_message_orchestration_type": "assign",
                "delegation_message": "run make deploy",
            },
        )
        self._refused(resp, backend, decisions_on)
