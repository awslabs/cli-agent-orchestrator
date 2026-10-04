"""Owner-only bind, pre-allocation refusal and failure compensation."""

import asyncio
import json
from datetime import timedelta
from itertools import combinations
from test.services.test_ephemeral_claim import (  # noqa: F401
    NOW,
    audit,
    claim,
    claimed_store,
    row_update,
)
from test.services.test_ephemeral_service import CALLER, create_store  # noqa: F401
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import terminal_service

CHILD = "eeeeeeee"


@pytest.fixture
def runtime_store(claimed_store, monkeypatch):
    env, name = claimed_store
    backend = Mock()
    backend.session_exists.return_value = True
    backend.create_window.return_value = "child"
    backend.supports_event_inbox.return_value = True
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(terminal_service, "get_max_terminals", lambda: None)
    monkeypatch.setattr(terminal_service, "generate_terminal_id", lambda: CHILD)
    monkeypatch.setattr(terminal_service, "_resolve_working_directory", lambda p: str(env[2]))
    monkeypatch.setattr(terminal_service, "build_skill_catalog", lambda p: None)
    monkeypatch.setattr(terminal_service, "get_herdr_inbox_service", lambda: None)
    provider = Mock(initialize=AsyncMock(), shell_baseline=None, runtime_variant=None)
    factory = Mock(return_value=provider)
    monkeypatch.setattr(terminal_service.provider_manager, "create_provider", factory)
    monkeypatch.setattr(terminal_service.provider_manager, "cleanup_provider", lambda _: True)
    return env, name, backend, factory


@pytest.mark.asyncio
@pytest.mark.parametrize("with_claim", [False, True])
async def test_owner_launch_is_bound_and_marked_ephemeral(runtime_store, with_claim):
    env, name, backend, factory = runtime_store
    token = claim(env, name)["claim_id"] if with_claim else None
    terminal = await terminal_service.create_terminal(
        "claude_code", name, session_name="cao-session", caller_id=CALLER, claim_id=token
    )
    assert terminal.id == CHILD and terminal.ephemeral is True
    assert terminal.allowed_tools == ["fs_read", "@cao-mcp-server"]
    row = database.get_ephemeral_agent(name)
    assert (
        row["state"] == "launched"
        and row["launched_terminal_id"] == CHILD
        and row["bound_at"] == NOW
    )
    assert audit(name)["events"][-1]["event"] == "bound"
    assert audit(name)["events"][-1]["terminal_id"] == CHILD
    assert audit(name)["events"][-1]["effective_tools"] == terminal.allowed_tools
    assert factory.call_args.args[5] == terminal.allowed_tools


@pytest.mark.parametrize(
    "state,token,caller,provider,rule",
    [
        ("missing", "a" * 32, CALLER, "claude_code", "unknown_ephemeral"),
        ("claimed", "a" * 32, CALLER, "codex", "provider_mismatch"),
        ("claimed", "a" * 32, "ffffffff", "claude_code", "not_owner"),
        ("pending", "a" * 32, CALLER, "claude_code", "claim_expired"),
        ("claimed", "b" * 32, CALLER, "claude_code", "already_claimed"),
        ("launched", "a" * 32, CALLER, "claude_code", "already_claimed"),
        ("gc", "a" * 32, CALLER, "claude_code", "ephemeral_expired"),
    ],
)
def test_bind_classifies_every_zero_row(claimed_store, state, token, caller, provider, rule):
    env, name = claimed_store
    row_update(
        env, name, state="claimed", claim_id="a" * 32, claim_expires_at=NOW + timedelta(seconds=30)
    )
    if state == "missing":
        name = "Ramones-missing_name-abcd"
    else:
        row_update(env, name, state=state)
    with pytest.raises(env[0].EphemeralPolicyError) as e:
        env[0].bind_ephemeral_agent(
            name, CHILD, caller, provider, token, ["fs_read", "@cao-mcp-server"]
        )
    assert e.value.rule == rule
    assert e.value.status_code == (
        404 if state == "missing" else 400 if rule in ["not_owner", "provider_mismatch"] else 409
    )


@pytest.mark.parametrize("expired_name", [False, True])
def test_late_bind_lapses_and_collects_only_expired_name(claimed_store, expired_name):
    env, name = claimed_store
    token = claim(env, name)["claim_id"]
    row_update(
        env,
        name,
        claim_expires_at=NOW,
        expires_at=NOW if expired_name else NOW + timedelta(seconds=900),
    )
    with pytest.raises(env[0].EphemeralPolicyError) as e:
        env[0].bind_ephemeral_agent(name, CHILD, CALLER, "claude_code", token, None)
    assert e.value.rule == ("ephemeral_expired" if expired_name else "claim_expired")
    row = database.get_ephemeral_agent(name)
    assert row["state"] == ("gc" if expired_name else "pending")
    if expired_name:
        assert not list((env[2] / "ephemeral/live").iterdir())
    else:
        assert claim(env, name)["replayed"] is False


@pytest.mark.parametrize(
    "setting,value,rule,state",
    [
        ("enabled", False, "ephemeral_disabled", "pending"),
        ("max_depth", 2, "policy_config_error:max_depth", "pending"),
        ("allowed_providers", ["codex"], "policy_changed_since_create:provider_not_allowed", "gc"),
    ],
)
def test_claimless_policy_check(claimed_store, setting, value, rule, state):
    env, name = claimed_store
    env[4]["ephemeral"][setting] = value
    with pytest.raises(env[0].EphemeralPolicyError) as e:
        env[0].bind_ephemeral_agent(name, CHILD, CALLER, "claude_code", None, None)
    assert e.value.rule == rule and database.get_ephemeral_agent(name)["state"] == state


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cap", "key", "load", "engine", "tools", "provider", "cancel"])
async def test_prebind_failure_ends_claim_and_allows_new_claim(runtime_store, monkeypatch, failure):
    env, name, backend, factory = runtime_store
    token = claim(env, name)["claim_id"]
    args = dict(
        session_name="cao-session",
        caller_id=CALLER,
        claim_id=token,
        initial_message="inspect",
        defer_init=True,
    )
    provider = "claude_code"
    expected = ValueError
    if failure == "cap":
        expected = terminal_service.TerminalLimitError
        monkeypatch.setattr(terminal_service, "get_max_terminals", lambda: 0)
        monkeypatch.setattr(terminal_service, "count_runtime_allocated_terminals", lambda: 1)
    elif failure == "key":
        expected = terminal_service.IdempotencyKeyConflict
        args["idempotency_key"] = "key"
        monkeypatch.setattr(
            terminal_service,
            "get_idempotency_record",
            lambda _: Mock(terminal_id="dddddddd", request_fingerprint="wrong"),
        )
        monkeypatch.setattr(terminal_service, "get_terminal", lambda _: dict(id="dddddddd"))
    elif failure in ("load", "cancel"):

        def fail(_):
            raise asyncio.CancelledError() if failure == "cancel" else ValueError("load failed")

        monkeypatch.setattr(terminal_service.agent_profiles, "load_launch_profile", fail)
        if failure == "cancel":
            expected = asyncio.CancelledError
    elif failure == "engine":
        args["engine"] = "v2"
    elif failure == "tools":
        expected = env[0].EphemeralPolicyError
        args["allowed_tools"] = ["execute_bash"]
    elif failure == "provider":
        expected = env[0].EphemeralPolicyError
        provider = "codex"
    with pytest.raises(expected):
        await terminal_service.create_terminal(provider, name, **args)
    assert database.get_ephemeral_agent(name)["state"] == "pending"
    assert claim(env, name)["replayed"] is False
    backend.create_window.assert_not_called()
    factory.assert_not_called()
    assert not terminal_service.initial_delivery_pending(CHILD)
    assert database.get_terminal_metadata(CHILD) is None


def test_workflow_claim_end_collects_and_stale_end_preserves_row(claimed_store):
    env, name = claimed_store
    token = claim(env, name)["claim_id"]
    row_update(env, name, owner_kind="workflow_step")
    env[0].end_claim(name, "stale")
    assert database.get_ephemeral_agent(name)["state"] == "claimed"
    env[0].end_claim(name, token)
    assert database.get_ephemeral_agent(name)["gc_reason"] == "launch_failed"
    assert not list((env[2] / "ephemeral/live").iterdir())


def fingerprint(**overrides):
    args = dict(
        provider="claude_code",
        agent_profile="reviewer",
        session_name="cao-session",
        working_directory=None,
        caller_id=CALLER,
        model=None,
        use_worktree=False,
        engine=None,
        allowed_tools=None,
        env_vars=None,
        resume_session_id=None,
        initial_message=None,
        initial_message_orchestration_type=None,
    )
    args.update(overrides)
    return terminal_service._request_fingerprint(**args)


def test_optional_claim_preserves_golden_fingerprint():
    assert (
        fingerprint(claim_id=None)
        == "d90daa142c3110f7a169da36d6044671a6de9642b14210bcac9030c3370587a1"
    )
    assert fingerprint(claim_id="a" * 32) != fingerprint(claim_id="b" * 32)
    assert fingerprint(claim_id="a" * 32) != fingerprint()


@pytest.mark.asyncio
async def test_different_claim_is_not_an_idempotent_replay(runtime_store):
    env, name, _, _ = runtime_store
    token = claim(env, name)["claim_id"]
    first = await terminal_service.create_terminal(
        "claude_code",
        name,
        session_name="cao-session",
        caller_id=CALLER,
        claim_id=token,
        idempotency_key="key",
    )
    assert first.id == CHILD
    with pytest.raises(terminal_service.IdempotencyKeyConflict):
        await terminal_service.create_terminal(
            "claude_code",
            name,
            session_name="cao-session",
            caller_id=CALLER,
            claim_id="b" * 32,
            idempotency_key="key",
        )
    assert database.get_ephemeral_agent(name)["state"] == "launched"


@pytest.mark.parametrize("extra", [{}, {"claim_id": "a" * 32}])
def test_fresh_session_still_has_no_owner_and_ignores_claim_query(runtime_store, extra):
    from cli_agent_orchestrator.api import main

    env, name, backend, factory = runtime_store
    monkey = pytest.MonkeyPatch()
    from cli_agent_orchestrator.plugins.registry import PluginRegistry

    monkey.setattr(main.app.state, "plugin_registry", PluginRegistry(), raising=False)
    try:
        before = database.get_ephemeral_agent(name)
        response = TestClient(main.app, base_url="http://localhost").post(
            "/sessions", params=dict(provider="claude_code", agent_profile=name, **extra)
        )
        assert response.status_code == 400 and response.json()["detail"]["rule"] == "not_owner"
        assert database.get_ephemeral_agent(name) == before
        backend.create_session.assert_not_called()
        backend.create_window.assert_not_called()
        factory.assert_not_called()
        assert len(database.list_all_terminals()) == 1
    finally:
        monkey.undo()


@pytest.mark.parametrize("route", ["terminal", "run_step"])
def test_api_claim_is_forwarded_and_refusals_structured(runtime_store, monkeypatch, route):
    from cli_agent_orchestrator.api import main
    from cli_agent_orchestrator.plugins.registry import PluginRegistry

    env, name, _, _ = runtime_store
    token = claim(env, name)["claim_id"]
    forwarded = []

    async def fail(*args, **kwargs):
        forwarded.append(kwargs)
        raise env[0].EphemeralPolicyError("claim_expired", status_code=409)

    monkeypatch.setattr(main.app.state, "plugin_registry", PluginRegistry(), raising=False)
    monkeypatch.setattr(main.terminal_service, "create_terminal", fail)
    monkeypatch.setattr(main, "run_agent_step", fail)
    client = TestClient(main.app, base_url="http://localhost")
    if route == "terminal":
        response = client.post(
            "/sessions/cao-session/terminals",
            params=dict(
                agent_profile=name, provider="claude_code", caller_id=CALLER, claim_id=token
            ),
        )
    else:
        response = client.post(
            "/terminals/run-step",
            json=dict(
                provider="claude_code",
                agent=name,
                prompt="inspect",
                caller_id=CALLER,
                claim_id=token,
            ),
        )
    assert response.status_code == 409 and response.json()["detail"]["rule"] == "claim_expired"
    assert forwarded[0]["claim_id"] == token


@pytest.mark.parametrize("bad", ["", "A" * 32, "a" * 31, "a" * 33])
def test_claim_id_route_shape_and_reuse_rejection(runtime_store, bad):
    from cli_agent_orchestrator.api.main import app

    env, name, _, _ = runtime_store
    client = TestClient(app, base_url="http://localhost")
    assert (
        client.post(
            "/sessions/cao-session/terminals", params=dict(agent_profile=name, claim_id=bad)
        ).status_code
        == 422
    )
    body = dict(provider="claude_code", agent=name, prompt="inspect", claim_id=bad)
    assert client.post("/terminals/run-step", json=body).status_code == 422
    body.update(claim_id="a" * 32, reuse_terminal_id=CALLER)
    assert client.post("/terminals/run-step", json=body).status_code == 422


def test_tool_excess_is_sorted_and_secret_atoms_redacted(claimed_store, caplog):
    env, name = claimed_store
    row_update(env, name, effective_tools=json.dumps(["@cao-mcp-server"]))
    with pytest.raises(env[0].EphemeralPolicyError) as e:
        env[0].prepare_ephemeral_launch(
            name, None, ["fs_write", "web_fetch", "fs_list", "execute_bash", "fs_read"]
        )
    assert e.value.detail == "execute_bash, fs_list, fs_read, fs_write, web_fetch"
    atoms = ["fs_read", "fs_list", "fs_write", "execute_bash", "web_fetch"]
    for size in range(2, 6):
        for group in combinations(atoms, size):
            with pytest.raises(env[0].EphemeralPolicyError) as e:
                env[0].prepare_ephemeral_launch(name, None, list(reversed(group)))
            assert e.value.detail == ", ".join(sorted(group))
    with pytest.raises(env[0].EphemeralPolicyError) as e:
        env[0].prepare_ephemeral_launch(name, None, ["SECRET_VALUE"])
    assert "SECRET_VALUE" not in str(e.value) + caplog.text


@pytest.mark.asyncio
async def test_lease_expires_during_load_without_runtime_allocation(runtime_store, monkeypatch):
    env, name, backend, factory = runtime_store
    token = claim(env, name)["claim_id"]
    load = terminal_service.agent_profiles.load_launch_profile

    def slow(name):
        profile = load(name)
        monkeypatch.setattr(database, "_utcnow", lambda: NOW + timedelta(seconds=60))
        return profile

    monkeypatch.setattr(terminal_service.agent_profiles, "load_launch_profile", slow)
    with pytest.raises(env[0].EphemeralPolicyError, match="claim_expired"):
        await terminal_service.create_terminal(
            "claude_code", name, session_name="cao-session", caller_id=CALLER, claim_id=token
        )
    assert database.get_ephemeral_agent(name)["state"] == "pending"
    backend.create_window.assert_not_called()
    factory.assert_not_called()
    assert claim(env, name)["replayed"] is False


@pytest.mark.asyncio
async def test_mismatched_provider_refuses_before_kiro_probe(runtime_store, monkeypatch):
    env, name, backend, factory = runtime_store
    token = claim(env, name)["claim_id"]
    probe = Mock(side_effect=AssertionError("no probe for a mismatched provider"))
    monkeypatch.setattr(terminal_service, "probe_kiro_capabilities", probe)
    with pytest.raises(env[0].EphemeralPolicyError, match="provider_mismatch"):
        await terminal_service.create_terminal(
            "kiro_cli", name, session_name="cao-session", caller_id=CALLER, claim_id=token
        )
    probe.assert_not_called()
    backend.create_window.assert_not_called()
    factory.assert_not_called()
    assert database.get_ephemeral_agent(name)["state"] == "pending"



@pytest.mark.asyncio
@pytest.mark.parametrize("with_claim", [False, True])
async def test_model_override_refuses_before_any_runtime(runtime_store, with_claim):
    env, name, backend, factory = runtime_store
    token = claim(env, name)["claim_id"] if with_claim else None
    before = database.get_ephemeral_agent(name)
    with pytest.raises(env[0].EphemeralPolicyError) as exc:
        await terminal_service.create_terminal(
            "claude_code",
            name,
            session_name="cao-session",
            caller_id=CALLER,
            claim_id=token,
            model="per-call-model",
        )
    assert exc.value.rule == "model_override_not_allowed"
    assert database.get_ephemeral_agent(name)["state"] == "pending"
    if not with_claim:
        assert database.get_ephemeral_agent(name) == before
    backend.create_window.assert_not_called()
    factory.assert_not_called()



def test_claimless_expired_bind_collects_name(claimed_store):
    env, name = claimed_store
    row_update(env, name, expires_at=NOW)
    with pytest.raises(env[0].EphemeralPolicyError) as exc:
        env[0].bind_ephemeral_agent(name, CHILD, CALLER, "claude_code", None, None)
    assert exc.value.rule == "ephemeral_expired" and exc.value.status_code == 409
    row = database.get_ephemeral_agent(name)
    assert row["state"] == "gc" and row["gc_reason"] == "ephemeral_expired"
    assert not list((env[2] / "ephemeral/live").iterdir())
    assert audit(name)["events"][-1]["event"] == "released"



def test_claimless_expiry_between_transactions_cannot_bind(claimed_store, monkeypatch):
    env, name = claimed_store
    original = env[0]._recheck_policy

    def expire(*args):
        original(*args)
        row_update(env, name, expires_at=NOW)

    monkeypatch.setattr(env[0], "_recheck_policy", expire)
    with pytest.raises(env[0].EphemeralPolicyError, match="ephemeral_expired"):
        env[0].bind_ephemeral_agent(name, CHILD, CALLER, "claude_code", None, None)
    assert database.get_ephemeral_agent(name)["state"] == "gc"



@pytest.mark.parametrize("route", ["terminal", "session"])
def test_never_created_name_launch_is_structured(runtime_store, monkeypatch, route):
    from cli_agent_orchestrator.api import main
    from cli_agent_orchestrator.plugins.registry import PluginRegistry

    monkeypatch.setattr(main.app.state, "plugin_registry", PluginRegistry(), raising=False)
    params = dict(provider="claude_code", agent_profile="Ramones-unknown_name-abcd")
    if route == "terminal":
        params["caller_id"] = CALLER
    response = TestClient(main.app, base_url="http://localhost").post(
        "/sessions/cao-session/terminals" if route == "terminal" else "/sessions", params=params
    )
    assert response.status_code == 404
    assert response.json()["detail"]["rule"] == "unknown_ephemeral"



@pytest.mark.asyncio
async def test_missing_live_profile_is_structured(runtime_store):
    env, name, _, _ = runtime_store
    (env[2] / "ephemeral/live" / (name + ".md")).unlink()
    with pytest.raises(env[0].EphemeralPolicyError) as exc:
        await terminal_service.create_terminal(
            "claude_code", name, session_name="cao-session", caller_id=CALLER
        )
    assert exc.value.rule == "spec_unavailable" and exc.value.status_code == 409
    assert exc.value.detail == "live profile unavailable; re-create the ephemeral agent"
    assert audit(name)["refusals"]["spec_unavailable"]["count"] == 1



@pytest.mark.parametrize("field", ["job_id", "claim_id"])
def test_run_step_validation_names_the_invalid_field(runtime_store, field):
    from cli_agent_orchestrator.api.main import app

    _, name, _, _ = runtime_store
    response = TestClient(app, base_url="http://localhost").post(
        "/terminals/run-step",
        json=dict(provider="claude_code", agent=name, prompt="inspect", **{field: "A" * 32}),
    )
    assert response.status_code == 422
    assert response.json()["detail"][0]["msg"].startswith("Value error, " + field + " must be")


@pytest.mark.parametrize("unknown", [False, True])
def test_fresh_session_initial_message_refuses_before_delivery(runtime_store, monkeypatch, unknown):
    from cli_agent_orchestrator.api import main
    from cli_agent_orchestrator.plugins.registry import PluginRegistry

    env, name, backend, factory = runtime_store
    if unknown:
        name = "Ramones-unknown_name-abcd"
    before = database.list_all_terminals()
    pending_before = set(terminal_service._pending_initial_delivery)
    schedule = Mock(side_effect=AssertionError("refusal must precede scheduling"))
    monkeypatch.setattr(main.app.state, "plugin_registry", PluginRegistry(), raising=False)
    monkeypatch.setattr(terminal_service, "_schedule_deferred_init", schedule)
    response = TestClient(main.app, base_url="http://localhost").post(
        "/sessions",
        params=dict(provider="claude_code", agent_profile=name),
        json=dict(initial_message="inspect"),
    )
    assert response.status_code == (404 if unknown else 400)
    assert response.json()["detail"]["rule"] == ("unknown_ephemeral" if unknown else "not_owner")
    assert database.list_all_terminals() == before
    assert set(terminal_service._pending_initial_delivery) == pending_before
    schedule.assert_not_called()
    backend.create_session.assert_not_called()
    backend.create_window.assert_not_called()
    factory.assert_not_called()
