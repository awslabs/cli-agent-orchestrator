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


@pytest.mark.asyncio
async def test_cancel_after_bind_calls_release_at_once(runtime_store, monkeypatch):
    env, name, _, factory = runtime_store
    token = claim(env, name)["claim_id"]
    factory.return_value.initialize.side_effect = asyncio.CancelledError()
    release = Mock()
    monkeypatch.setattr(env[0], "release", release)
    with pytest.raises(asyncio.CancelledError):
        await terminal_service.create_terminal(
            "claude_code", name, session_name="cao-session", caller_id=CALLER, claim_id=token
        )
    release.assert_called_once_with(CHILD, "launch_failed")


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


@pytest.fixture
def release_runtime(runtime_store, monkeypatch):
    from cli_agent_orchestrator.backends import registry
    from cli_agent_orchestrator.services import session_service

    env, name, backend, factory = runtime_store
    backend.get_history.return_value = ""
    backend.get_pane_working_directory.return_value = str(env[2])
    backend.session_exists_strict.return_value = False
    monkeypatch.setattr(registry, "get_backend", lambda: backend)
    monkeypatch.setattr(session_service, "get_backend", lambda: backend)
    logs = env[2] / "logs"
    logs.mkdir()
    monkeypatch.setattr(terminal_service, "TERMINAL_LOG_DIR", logs)
    return env, name, backend, factory


async def launch_runtime(runtime):
    env, name, _, _ = runtime
    return await terminal_service.create_terminal(
        "claude_code", name, session_name="cao-session", caller_id=CALLER
    )


def assert_released(env, name, reason):
    row = database.get_ephemeral_agent(name)
    assert (
        row["state"] == "gc" and row["gc_reason"] == reason and row["launched_terminal_id"] == CHILD
    )
    assert not list((env[2] / "ephemeral/live").iterdir())
    assert audit(name)["gc_reason"] == reason
    assert audit(name)["events"][-1]["event"] == "released"
    assert database.is_ephemeral_terminal(CHILD)


@pytest.mark.asyncio
async def test_raw_api_launch_and_delete_releases(release_runtime, monkeypatch):
    from cli_agent_orchestrator.api import main
    from cli_agent_orchestrator.plugins.registry import PluginRegistry

    env, name, _, _ = release_runtime
    monkeypatch.setattr(main.app.state, "plugin_registry", PluginRegistry(), raising=False)
    client = TestClient(main.app, base_url="http://localhost")
    response = client.post(
        "/sessions/cao-session/terminals",
        params=dict(provider="claude_code", agent_profile=name, caller_id=CALLER),
    )
    assert response.status_code == 201 and response.json()["ephemeral"] is True
    response = client.delete("/terminals/" + CHILD)
    assert response.status_code == 200
    assert_released(env, name, "terminal_deleted")


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["session", "herdr"])
async def test_session_delete_and_herdr_exit_release(release_runtime, monkeypatch, path):
    from cli_agent_orchestrator.services import herdr_inbox_service, session_service

    env, name, _, _ = release_runtime
    await launch_runtime(release_runtime)
    if path == "session":
        assert "cao-session" in session_service.delete_session("cao-session")["deleted"]
    else:
        service = herdr_inbox_service.HerdrInboxService(socket_path=str(env[2] / "fake.sock"))
        service._pane_to_terminal = {"child": CHILD, "parent": CALLER}
        service._terminal_to_pane = {CHILD: "child", CALLER: "parent"}
        monkeypatch.setattr(service, "_label_still_live", lambda _: False)
        service._handle_lifecycle_event("pane.closed", {"pane_id": "child"})
    assert_released(env, name, "terminal_deleted")


@pytest.mark.asyncio
async def test_retention_and_stale_row_delete_release(release_runtime, monkeypatch):
    from sqlalchemy import update

    from cli_agent_orchestrator.services import cleanup_service

    env, name, _, _ = release_runtime
    await launch_runtime(release_runtime)
    with env[1]() as db:
        db.execute(
            update(database.TerminalModel)
            .where(database.TerminalModel.id == CHILD)
            .values(last_active=NOW - timedelta(days=30))
        )
        db.commit()
    monkeypatch.setattr(cleanup_service, "SessionLocal", env[1])
    cleanup_service.cleanup_old_data()
    assert database.get_terminal_metadata(CHILD) is None
    assert_released(env, name, "terminal_gone")


@pytest.mark.asyncio
async def test_stale_session_purge_releases(release_runtime):
    env, name, _, _ = release_runtime
    await launch_runtime(release_runtime)
    terminal_service._purge_stale_session_rows_for_recreate("cao-session")
    assert_released(env, name, "terminal_gone")


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "cancel", "extraction", "timeout"])
async def test_run_step_release_and_timeout_residue(release_runtime, monkeypatch, outcome):
    from cli_agent_orchestrator.providers.base import OutputExtractionError
    from cli_agent_orchestrator.services import agent_step

    env, name, _, _ = release_runtime
    token = claim(env, name)["claim_id"]
    monkeypatch.setattr(agent_step, "wait_until_status", AsyncMock(return_value=True))
    wait = AsyncMock()
    if outcome == "cancel":
        wait.side_effect = agent_step.StepCancelledError(CHILD)
    if outcome == "timeout":
        wait.side_effect = agent_step.StepExecutionError("timeout", terminal_id=CHILD)
    monkeypatch.setattr(agent_step, "_wait_for_completion", wait)
    monkeypatch.setattr(agent_step.frozen_run_memory, "frozen_memory_for", lambda *args: None)
    monkeypatch.setattr(terminal_service, "send_input", lambda *args, **kw: None)
    if outcome == "extraction":

        def extract(*args):
            raise OutputExtractionError("no marker")

    else:

        def extract(*args):
            return "done"

    monkeypatch.setattr(terminal_service, "get_output", extract)
    call = agent_step.run_agent_step(
        "claude_code", name, "inspect", session_name="cao-session", caller_id=CALLER, claim_id=token
    )
    if outcome == "success":
        assert (await call).last_message == "done"
    else:
        expected = {
            "cancel": agent_step.StepCancelledError,
            "timeout": agent_step.StepExecutionError,
            "extraction": OutputExtractionError,
        }[outcome]
        with pytest.raises(expected):
            await call
    if outcome == "timeout":
        assert database.get_ephemeral_agent(name)["state"] == "launched"
        assert (env[2] / "ephemeral/live" / (name + ".md")).exists()
    else:
        assert_released(env, name, "terminal_deleted")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancel", "exception"])
async def test_postbind_failure_collects_immediately(release_runtime, failure):
    env, name, _, factory = release_runtime
    token = claim(env, name)["claim_id"]
    factory.return_value.initialize.side_effect = (
        asyncio.CancelledError() if failure == "cancel" else RuntimeError("launch failed")
    )
    with pytest.raises(asyncio.CancelledError if failure == "cancel" else RuntimeError):
        await terminal_service.create_terminal(
            "claude_code",
            name,
            session_name="cao-session",
            caller_id=CALLER,
            claim_id=token,
            initial_message="inspect",
        )
    assert_released(env, name, "launch_failed")
    assert not terminal_service.initial_delivery_pending(CHILD)
    metadata = database.get_terminal_metadata(CHILD)
    assert metadata is None or (metadata.get("initial_delivery") or {}).get("state") != "pending"


@pytest.mark.asyncio
async def test_deferred_failure_releases_even_retained_worker(release_runtime):
    env, name, _, _ = release_runtime
    await launch_runtime(release_runtime)
    terminal_service._notify_caller_of_deferred_failure(CHILD, "unavailable", None, False)
    assert_released(env, name, "launch_failed")
    assert database.get_terminal_metadata(CHILD) is not None


@pytest.mark.asyncio
async def test_deferred_provider_cleanup_waits_for_delete(release_runtime, monkeypatch):
    env, name, _, _ = release_runtime
    await launch_runtime(release_runtime)
    monkeypatch.setattr(terminal_service.provider_manager, "cleanup_provider", lambda _: False)
    terminal_service._roll_back_failed_create(
        CHILD,
        "cao-session",
        None,
        session_created=False,
        window_created=False,
        worktree_repo_root=None,
    )
    assert database.get_ephemeral_agent(name)["state"] == "launched"
    monkeypatch.setattr(terminal_service.provider_manager, "cleanup_provider", lambda _: True)
    assert terminal_service.delete_terminal(CHILD)
    assert_released(env, name, "terminal_deleted")


def test_double_release_is_noop_and_recleans_gc_files(claimed_store):
    env, name = claimed_store
    env[0].bind_ephemeral_agent(name, CHILD, CALLER, "claude_code", None, None)
    env[0].release(CHILD, "terminal_deleted")
    before = audit(name)
    for suffix in [".md", ".spec.json"]:
        (env[2] / "ephemeral/live" / (name + suffix)).write_text("residue")
    env[0].release(CHILD, "terminal_gone")
    assert_released(env, name, "terminal_deleted")
    assert audit(name) == before


def test_release_skips_symlink_and_never_raises_on_database_failure(claimed_store, monkeypatch):
    env, name = claimed_store
    env[0].bind_ephemeral_agent(name, CHILD, CALLER, "claude_code", None, None)
    path = env[2] / "ephemeral/live" / (name + ".md")
    outside = env[2] / "outside.md"
    outside.write_text("outside")
    path.unlink()
    path.symlink_to(outside)
    env[0].release(CHILD, "terminal_deleted")
    assert path.is_symlink() and outside.read_text() == "outside"
    assert database.get_ephemeral_agent(name)["state"] == "gc"
    with monkeypatch.context() as patch:
        patch.setattr(
            database, "SessionLocal", Mock(side_effect=RuntimeError("database unavailable"))
        )
        env[0].release(CHILD, "terminal_deleted")


def test_crash_after_bind_leaves_launched_registry_without_terminal(claimed_store):
    env, name = claimed_store
    token = claim(env, name)["claim_id"]
    env[0].bind_ephemeral_agent(name, CHILD, CALLER, "claude_code", token, None)
    assert database.get_terminal_metadata(CHILD) is None
    with pytest.raises(env[0].EphemeralPolicyError, match="already_claimed"):
        claim(env, name)
    assert database.get_ephemeral_agent(name)["state"] == "launched"


@pytest.mark.asyncio
async def test_claim_does_not_enter_step_fingerprint(release_runtime, monkeypatch):
    from dataclasses import fields

    from cli_agent_orchestrator.models.terminal import Terminal, TerminalStatus
    from cli_agent_orchestrator.services import agent_step
    from cli_agent_orchestrator.services.step_fingerprint import StepCallFields

    assert "claim_id" not in [f.name for f in fields(StepCallFields)]
    env, name, _, _ = release_runtime
    terminal = Terminal(
        id=CHILD,
        name="child",
        provider="claude_code",
        session_name="cao-session",
        status=TerminalStatus.IDLE,
        last_active=NOW,
    )
    create = AsyncMock(return_value=terminal)
    monkeypatch.setattr(terminal_service, "create_terminal", create)
    monkeypatch.setattr(terminal_service, "send_input", lambda *args, **kwargs: None)
    monkeypatch.setattr(terminal_service, "get_output", lambda *args: "done")
    monkeypatch.setattr(agent_step, "wait_until_status", AsyncMock(return_value=True))
    monkeypatch.setattr(agent_step, "_wait_for_completion", AsyncMock())
    monkeypatch.setattr(agent_step.frozen_run_memory, "frozen_memory_for", lambda *args: None)
    seen = []
    for token in ["a" * 32, "b" * 32]:
        await agent_step.run_agent_step(
            "claude_code",
            name,
            "inspect",
            session_name="cao-session",
            working_directory=str(env[2]),
            claim_id=token,
            teardown=False,
            on_step_terminal_ready=lambda tid, fp: seen.append(fp),
        )
        assert create.await_args.kwargs["claim_id"] == token
    assert seen[0] == seen[1]


@pytest.mark.asyncio
async def test_bound_child_denied_by_real_mcp_gate_before_and_after_release(
    release_runtime, monkeypatch
):
    from cli_agent_orchestrator.mcp_server import server
    from cli_agent_orchestrator.models.terminal import TerminalStatus

    env, name, _, _ = release_runtime
    await launch_runtime(release_runtime)
    monkeypatch.setenv("CAO_TERMINAL_ID", CHILD)
    monkeypatch.setattr(
        terminal_service.status_monitor, "get_status", lambda _: TerminalStatus.IDLE
    )
    monkeypatch.setattr(
        server.mcp_utils, "get_json", lambda *args, **kwargs: terminal_service.get_terminal(CHILD)
    )
    monkeypatch.setattr(server.requests, "get", Mock(return_value=Mock(status_code=404)))
    for released in [False, True]:
        if released:
            env[0].release(CHILD, "terminal_deleted")
        assert server._get_terminal_context_from_env()["ephemeral"] is True
        for tool in [
            "assign",
            "handoff",
            "assign_elastic",
            "workflow_run",
            "workflow_resume",
            "workflow_start",
        ]:
            assert server._tool_denied_reason(tool) is not None
        assert server._tool_denied_reason("create_ephemeral_agent") is not None
        assert server._tool_denied_reason("send_message") is None


def test_no_claim_handler_text_still_refuses(runtime_store):
    from cli_agent_orchestrator.utils import orchestration

    _, name, backend, factory = runtime_store
    with pytest.raises(ValueError) as exc:
        orchestration._refuse_ephemeral_target_without_claim(name)
    assert (
        str(exc.value)
        == "Ephemeral targets cannot be launched through assign or handoff in this version."
    )
    backend.create_window.assert_not_called()
    factory.assert_not_called()


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


def test_archive_events_never_contain_claim_id(claimed_store):
    env, name = claimed_store
    token = claim(env, name)["claim_id"]
    env[0].bind_ephemeral_agent(name, CHILD, CALLER, "claude_code", token, None)
    env[0].release(CHILD, "terminal_deleted")
    events = audit(name)["events"]
    assert [e["event"] for e in events] == ["created", "claimed", "finalized", "bound", "released"]
    assert all("claim_id" not in e for e in events)


def test_release_refuses_symlink_live_directory(claimed_store, tmp_path):
    env, name = claimed_store
    token = claim(env, name)["claim_id"]
    env[0].bind_ephemeral_agent(name, CHILD, CALLER, "claude_code", token, None)
    live = env[2] / "ephemeral/live"
    target = tmp_path / "target"
    live.rename(target)
    live.symlink_to(target, target_is_directory=True)
    env[0].release(CHILD, "terminal_deleted")
    assert (target / (name + ".md")).is_file()
    assert (target / (name + ".spec.json")).is_file()
    assert database.get_ephemeral_agent(name)["state"] == "gc"


def test_collected_name_relaunch_is_structured(release_runtime, monkeypatch):
    from cli_agent_orchestrator.api import main
    from cli_agent_orchestrator.plugins.registry import PluginRegistry

    env, name, _, _ = release_runtime
    monkeypatch.setattr(main.app.state, "plugin_registry", PluginRegistry(), raising=False)
    client = TestClient(main.app, base_url="http://localhost")
    params = dict(provider="claude_code", agent_profile=name, caller_id=CALLER)
    response = client.post("/sessions/cao-session/terminals", params=params)
    assert response.status_code == 201
    assert client.delete("/terminals/" + CHILD).status_code == 200
    response = client.post("/sessions/cao-session/terminals", params=params)
    assert response.status_code == 409
    assert response.json()["detail"]["rule"] == "ephemeral_expired"


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


def test_release_ignores_live_swapped_after_open(claimed_store, tmp_path, monkeypatch):
    import os

    env, name = claimed_store
    token = claim(env, name)["claim_id"]
    env[0].bind_ephemeral_agent(name, CHILD, CALLER, "claude_code", token, None)
    live = env[2] / "ephemeral/live"
    target = tmp_path / "target"
    target.mkdir()
    for suffix in (".md", ".spec.json"):
        (target / (name + suffix)).write_text("decoy")
    real_lstat = os.lstat
    swapped = []

    def lstat(path, *args, **kwargs):
        if not swapped:
            swapped.append(True)
            live.rename(tmp_path / "moved")
            live.symlink_to(target, target_is_directory=True)
        return real_lstat(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(env[0].os, "lstat", lstat)
        env[0].release(CHILD, "terminal_deleted")
    assert swapped
    assert (target / (name + ".md")).is_file()
    assert (target / (name + ".spec.json")).is_file()
    assert not list((tmp_path / "moved").iterdir())


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


@pytest.mark.asyncio
async def test_postbind_initial_message_failure_leaves_no_pending_delivery(
    release_runtime, monkeypatch
):
    env, name, _, factory = release_runtime
    token = claim(env, name)["claim_id"]
    factory.side_effect = RuntimeError("launch failed")
    schedule = Mock(side_effect=AssertionError("provider factory failed before scheduling"))
    monkeypatch.setattr(terminal_service, "_schedule_deferred_init", schedule)
    with pytest.raises(RuntimeError, match="launch failed"):
        await terminal_service.create_terminal(
            "claude_code",
            name,
            session_name="cao-session",
            caller_id=CALLER,
            claim_id=token,
            initial_message="inspect",
            defer_init=True,
        )
    assert_released(env, name, "launch_failed")
    assert not terminal_service.initial_delivery_pending(CHILD)
    assert database.get_terminal_metadata(CHILD) is None
    schedule.assert_not_called()


@pytest.mark.asyncio
async def test_deferred_ephemeral_failure_settles_initial_delivery(release_runtime, monkeypatch):
    env, name, _, factory = release_runtime
    factory.return_value.initialize.side_effect = RuntimeError("launch failed")
    schedule = terminal_service._schedule_deferred_init
    owned = []

    def capture(*args, **kwargs):
        task = schedule(*args, **kwargs)
        owned.append(task)
        return task

    monkeypatch.setattr(terminal_service, "_schedule_deferred_init", capture)
    await terminal_service.create_terminal(
        "claude_code",
        name,
        session_name="cao-session",
        caller_id=CALLER,
        initial_message="inspect",
        defer_init=True,
    )
    assert len(owned) == 1 and owned[0] is not None
    await asyncio.wait_for(owned[0], timeout=10)
    assert_released(env, name, "launch_failed")
    assert not terminal_service.initial_delivery_pending(CHILD)
    metadata = database.get_terminal_metadata(CHILD)
    assert metadata is None or (metadata.get("initial_delivery") or {}).get("state") == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend_exists", [False, True])
async def test_flow_recycling_releases_ephemeral_rows(claimed_store, monkeypatch, backend_exists):
    from types import SimpleNamespace

    from cli_agent_orchestrator.services import flow_service

    env, name = claimed_store
    token = claim(env, name)["claim_id"]
    env[0].bind_ephemeral_agent(name, CHILD, CALLER, "claude_code", token, None)
    session = "cao-flow-test"
    database.create_terminal(CHILD, session, "child", "claude_code", agent_profile=name)
    database.create_terminal("dddddddd", session, "ordinary", "claude_code")
    rows = database.list_terminals_by_session(session)
    backend = Mock()
    backend.session_exists.return_value = backend_exists
    monkeypatch.setattr(flow_service, "get_backend", lambda: backend)
    monkeypatch.setattr(
        flow_service,
        "get_flow",
        lambda _: SimpleNamespace(
            name="test",
            file_path=str(env[2] / "flow.md"),
            schedule="* * * * *",
            script=None,
            provider="claude_code",
            agent_profile="developer",
            engine=None,
        ),
    )
    monkeypatch.setattr(flow_service, "db_update_flow_run_times", Mock())
    monkeypatch.setattr(flow_service, "_parse_flow_file", lambda _: ({}, "inspect"))
    monkeypatch.setattr(flow_service, "list_current_session_terminals", lambda *a, **kw: rows)
    monkeypatch.setattr(flow_service, "_is_terminal_busy", lambda _: False)
    monkeypatch.setattr(flow_service.provider_manager, "cleanup_provider", lambda _: True)
    monkeypatch.setattr(flow_service.fifo_manager, "stop_reader", Mock())
    monkeypatch.setattr(flow_service.status_monitor, "clear_terminal", Mock())
    monkeypatch.setattr(
        flow_service, "create_terminal", AsyncMock(return_value=SimpleNamespace(id="ffffffff"))
    )
    monkeypatch.setattr(flow_service, "send_input", Mock())
    assert await flow_service.execute_flow("test") is True
    assert database.get_terminal_metadata(CHILD) is None
    assert database.get_terminal_metadata("dddddddd") is None
    assert_released(env, name, "terminal_gone")


def test_session_bulk_fallback_releases_ephemeral_rows(release_runtime, monkeypatch):
    from cli_agent_orchestrator.services import session_service

    env, name, backend, _ = release_runtime
    token = claim(env, name)["claim_id"]
    env[0].bind_ephemeral_agent(name, CHILD, CALLER, "claude_code", token, None)
    database.create_terminal(CHILD, "cao-session", "child", "claude_code", agent_profile=name)
    database.create_terminal("dddddddd", "cao-session", "ordinary", "claude_code")
    rows = [database.get_terminal_metadata(t) for t in [CHILD, "dddddddd"]]
    monkeypatch.setattr(session_service, "list_terminals_by_session", lambda _: rows)
    monkeypatch.setattr(
        terminal_service, "capture_terminal_snapshot", database.get_terminal_metadata
    )
    monkeypatch.setattr(terminal_service, "dismantle_terminal_runtime", lambda *a, **kw: True)
    monkeypatch.setattr(
        terminal_service,
        "delete_terminal_row",
        Mock(side_effect=RuntimeError("first delete failed")),
    )
    monkeypatch.setattr(session_service, "dispatch_plugin_event", Mock())
    result = session_service.delete_session("cao-session")
    assert result["deleted"] == ["cao-session"]
    assert database.get_terminal_metadata(CHILD) is None
    assert database.get_terminal_metadata("dddddddd") is None
    assert_released(env, name, "terminal_gone")
