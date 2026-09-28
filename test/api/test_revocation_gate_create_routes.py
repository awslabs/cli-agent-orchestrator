"""Revoked principals cannot start new work on the create/run routes (#745).

``may_start_work`` was consulted only by inbox delivery and flow dispatch. A
revoked principal could still start work through the synchronous create routes.
``create_session``,
``create_terminal_in_session`` and ``run_step`` now refuse a revoked owner with
403 and create/journal nothing. An unrevoked principal is unaffected.
"""

import asyncio

import pytest

import cli_agent_orchestrator.api.main as main
from cli_agent_orchestrator.security.principal import LOCAL_ISSUER, Principal, revocation

REVOKED = Principal(subject="revoked-user", issuer=LOCAL_ISSUER)
ALLOWED = Principal(subject="allowed-user", issuer=LOCAL_ISSUER)


class _FakeRequest:
    pass


class _FakeBackgroundTasks:
    def add_task(self, *a, **k):
        pass


class _Result:
    terminal_id = "t1"
    last_message = "done"
    status = "completed"


@pytest.fixture(autouse=True)
def _revocation_reset(monkeypatch):
    # Isolate the process-wide revocation registry per test.
    monkeypatch.delenv("CAO_REVOKED_PRINCIPALS", raising=False)
    revocation.reset()
    yield
    revocation.reset()


def _revoke(monkeypatch, principal):
    monkeypatch.setenv("CAO_REVOKED_PRINCIPALS", principal.id)
    revocation.reset()  # re-arm lazy seeding so the env value is read


# --- create_session (POST /sessions) --------------------------------------


def test_create_session_refuses_revoked_owner(monkeypatch):
    _revoke(monkeypatch, REVOKED)
    called = {"create": False}

    async def fake_create(**kwargs):
        called["create"] = True

    monkeypatch.setattr(main.session_service, "create_session", fake_create)

    with pytest.raises(main.HTTPException) as ei:
        asyncio.run(
            main.create_session(
                _FakeRequest(),
                _FakeBackgroundTasks(),
                agent_profile="developer",
                provider="kiro_cli",
                principal=REVOKED,
            )
        )
    assert ei.value.status_code == 403
    assert "revoked and may not start new work" in ei.value.detail
    assert called["create"] is False  # nothing created


def test_create_session_allows_unrevoked_owner(monkeypatch):
    _revoke(monkeypatch, REVOKED)  # a DIFFERENT principal is revoked

    async def fake_create(**kwargs):
        return {"id": "session-term"}

    monkeypatch.setattr(main.session_service, "create_session", fake_create)
    monkeypatch.setattr(main, "get_plugin_registry", lambda request: None)

    result = asyncio.run(
        main.create_session(
            _FakeRequest(),
            _FakeBackgroundTasks(),
            agent_profile="developer",
            provider="kiro_cli",
            principal=ALLOWED,
        )
    )
    assert result == {"id": "session-term"}


# --- create_terminal_in_session (POST /sessions/{name}/terminals) ----------


def test_create_terminal_refuses_revoked_owner(monkeypatch):
    _revoke(monkeypatch, REVOKED)
    called = {"create": False}

    async def fake_create_terminal(**kwargs):
        called["create"] = True

    monkeypatch.setattr(main.terminal_service, "create_terminal", fake_create_terminal)
    monkeypatch.setattr(main, "get_plugin_registry", lambda request: None)

    with pytest.raises(main.HTTPException) as ei:
        asyncio.run(
            main.create_terminal_in_session(
                _FakeRequest(),
                session_name="cao-x",
                agent_profile="developer",
                provider="kiro_cli",
                principal=REVOKED,
            )
        )
    assert ei.value.status_code == 403
    assert "revoked and may not start new work" in ei.value.detail
    assert called["create"] is False


def test_create_terminal_allows_unrevoked_owner(monkeypatch):
    _revoke(monkeypatch, REVOKED)

    async def fake_create_terminal(**kwargs):
        return {"id": "worker-term"}

    monkeypatch.setattr(main.terminal_service, "create_terminal", fake_create_terminal)
    monkeypatch.setattr(main, "get_plugin_registry", lambda request: None)

    result = asyncio.run(
        main.create_terminal_in_session(
            _FakeRequest(),
            session_name="cao-x",
            agent_profile="developer",
            provider="kiro_cli",
            principal=ALLOWED,
        )
    )
    assert result == {"id": "worker-term"}


# --- run_step --------------------------------------------------------------


def test_run_step_refuses_revoked_caller_owner(monkeypatch):
    _revoke(monkeypatch, REVOKED)
    called = {"ran": False}
    states = []

    async def fake_run_agent_step(**kwargs):
        called["ran"] = True

    async def fake_record_job_state(job_id, state, **fields):
        states.append(state)

    # The caller's owner is the revoked principal.
    monkeypatch.setattr(main, "caller_owner_id", lambda caller_id: REVOKED.id)
    monkeypatch.setattr(main, "run_agent_step", fake_run_agent_step)
    monkeypatch.setattr(main, "_record_job_state", fake_record_job_state)
    monkeypatch.setattr(main, "get_plugin_registry", lambda request: None)

    body = main.RunStepRequest(
        provider="mock_cli",
        agent="supervisor",
        prompt="hi",
        caller_id="caller-term",
        job_id="0123456789abcdef0123456789abcdef",
    )
    with pytest.raises(main.HTTPException) as ei:
        asyncio.run(main.run_step(_FakeRequest(), _FakeBackgroundTasks(), body))

    assert ei.value.status_code == 403
    assert "revoked and may not start new work" in ei.value.detail
    assert called["ran"] is False  # nothing executed
    assert states == []  # nothing journaled (no "running" write)


def test_run_step_allows_unrevoked_caller_owner(monkeypatch):
    _revoke(monkeypatch, REVOKED)  # a different principal is revoked
    states = []

    async def fake_run_agent_step(**kwargs):
        return _Result()

    async def fake_record_job_state(job_id, state, **fields):
        states.append(state)

    monkeypatch.setattr(main, "caller_owner_id", lambda caller_id: ALLOWED.id)
    monkeypatch.setattr(main, "run_agent_step", fake_run_agent_step)
    monkeypatch.setattr(main, "_record_job_state", fake_record_job_state)
    monkeypatch.setattr(main, "_schedule_elastic_terminal_ended", lambda *a, **k: None)
    monkeypatch.setattr(main, "get_plugin_registry", lambda request: None)

    body = main.RunStepRequest(
        provider="mock_cli",
        agent="supervisor",
        prompt="hi",
        caller_id="caller-term",
        job_id="0123456789abcdef0123456789abcdef",
    )
    result = asyncio.run(main.run_step(_FakeRequest(), _FakeBackgroundTasks(), body))
    assert result.terminal_id == "t1"
    assert "running" in states  # got past the gate and journaled running
