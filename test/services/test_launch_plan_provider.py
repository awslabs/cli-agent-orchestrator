"""#1099: a claimed ephemeral launch runs on its launch plan's provider.

The plans here are built by hand: only the decision engine sets ``LaunchPlan.provider`` in
production, and ``test/`` is outside the scan that pins that. The request's provider stays the
claim's identity (fingerprint, launch checks, bind); everything that depends on which CLI runs
uses the launch provider. Real ``claude_code`` and ``codex`` are the only providers built.
"""

import asyncio
import shlex
from dataclasses import replace
from test.services.test_ephemeral_bind import CHILD, runtime_store  # noqa: F401
from test.services.test_ephemeral_claim import NOW, audit, claim, claimed_store  # noqa: F401
from test.services.test_ephemeral_service import CALLER, SPEC, create, create_store  # noqa: F401
from unittest.mock import AsyncMock, Mock, patch

import frontmatter
import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.decisions.types import LaunchPlan
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.ephemeral import TOOL_ATOMS
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers import claude_code, codex
from cli_agent_orchestrator.services import agent_step, terminal_service
from cli_agent_orchestrator.utils import ephemeral_capabilities as caps
from cli_agent_orchestrator.utils import orchestration

ROUTED = LaunchPlan(model="codex-medium", provider="codex")
RETRY = "replay-1"
TIERS = {
    provider: {t: f"{provider}-{t}" for t in ("small", "medium", "large")}
    for provider in ("claude_code", "codex")
}


def bound_events(name):
    return [event for event in audit(name)["events"] if event["event"] == "bound"]


async def launch(env, name, plan, *, claimed=None, **kwargs):
    return await terminal_service.create_terminal(
        "claude_code",
        name,
        session_name="cao-session",
        caller_id=CALLER,
        claim_id=claimed,
        launch_plan=plan,
        **kwargs,
    )


# The resolvers.


def test_the_launch_provider_is_the_plans_when_it_names_one():
    assert terminal_service.resolve_launch_provider(None, "claude_code") == "claude_code"
    assert terminal_service.resolve_launch_provider(LaunchPlan(), "claude_code") == "claude_code"
    assert terminal_service.resolve_launch_provider(ROUTED, "claude_code") == "codex"


def test_a_routed_launch_never_takes_the_profiles_model():
    profile = AgentProfile(name="n", description="d", model="claude_code-medium")
    assert terminal_service.resolve_launch_model(None, profile, ROUTED) == "codex-medium"
    assert terminal_service.resolve_launch_model(None, profile, replace(ROUTED, model=None)) is None
    # Unrouted launches keep today's precedence.
    assert (
        terminal_service.resolve_launch_model(None, profile, LaunchPlan()) == "claude_code-medium"
    )
    assert terminal_service.resolve_launch_model(None, profile, None) == "claude_code-medium"
    assert terminal_service.resolve_launch_model(None, profile, LaunchPlan(model="m")) == "m"


# A routed claimed launch: the CLI, the terminal, the bound event and the launch record name
# the provider that ran; the registry row, the live profile and the archive keep the fallback.


@pytest.mark.asyncio
async def test_a_routed_launch_runs_the_planned_provider_and_records_it(runtime_store):
    env, name, backend, factory = runtime_store
    profile_before = (env[2] / "ephemeral/live" / (name + ".md")).read_bytes()
    claimed = claim(env, name)["claim_id"]
    terminal = await launch(env, name, ROUTED, claimed=claimed)
    assert factory.call_args.args[0] == "codex"
    assert factory.call_args.kwargs["model"] == "codex-medium"
    assert terminal.provider == "codex"
    assert database.get_terminal_metadata(CHILD)["provider"] == "codex"
    row = database.get_ephemeral_agent(name)
    assert row["state"] == "launched" and row["provider"] == "claude_code"
    assert audit(name)["provider"] == "claude_code"
    live = (env[2] / "ephemeral/live" / (name + ".md")).read_bytes()
    assert live == profile_before and frontmatter.loads(live.decode())["provider"] == "claude_code"
    (bound,) = bound_events(name)
    assert bound["provider"] == "codex" and bound["terminal_id"] == CHILD
    assert database.get_launch_record(CHILD)["provider"] == "codex"


@pytest.mark.asyncio
async def test_an_unrouted_launch_records_its_own_provider_only(runtime_store):
    env, name, backend, factory = runtime_store
    claimed = claim(env, name)["claim_id"]
    terminal = await launch(env, name, LaunchPlan(model="claude_code-medium"), claimed=claimed)
    assert factory.call_args.args[0] == "claude_code"
    assert terminal.provider == "claude_code"
    (bound,) = bound_events(name)
    assert "provider" not in bound
    assert database.get_launch_record(CHILD)["provider"] == "claude_code"


# The guard: a plan that names a provider for anything but a claimed ephemeral launch is a bug,
# refused before any allocation.


@pytest.mark.asyncio
async def test_a_claimless_launch_whose_plan_names_a_provider_refuses_before_allocation(
    runtime_store,
):
    env, name, backend, factory = runtime_store
    with pytest.raises(RuntimeError, match="not a claimed ephemeral"):
        await launch(env, name, ROUTED)
    backend.create_window.assert_not_called()
    factory.assert_not_called()
    assert database.get_terminal_metadata(CHILD) is None
    assert database.get_ephemeral_agent(name)["state"] == "pending"
    assert bound_events(name) == []


@pytest.mark.asyncio
async def test_an_installed_launch_whose_plan_names_a_provider_refuses_before_allocation(
    runtime_store, monkeypatch
):
    from cli_agent_orchestrator.utils import agent_profiles

    env, name, backend, factory = runtime_store
    installed = AgentProfile(name="developer", description="d")
    monkeypatch.setattr(
        agent_profiles,
        "load_launch_profile",
        lambda _: (installed, agent_profiles.ProfileSource.INSTALLED),
    )
    with pytest.raises(RuntimeError, match="not a claimed ephemeral"):
        await terminal_service.create_terminal(
            "claude_code",
            "developer",
            session_name="cao-session",
            caller_id=CALLER,
            claim_id="a" * 32,
            launch_plan=ROUTED,
        )
    backend.create_window.assert_not_called()
    factory.assert_not_called()


# A keyed replay of a routed launch reattaches: the fingerprint keeps the request's provider.


@pytest.mark.asyncio
async def test_a_keyed_replay_of_a_routed_launch_reattaches(runtime_store):
    env, name, backend, factory = runtime_store
    claimed = claim(env, name, idempotency_key=RETRY)["claim_id"]
    first = await launch(env, name, ROUTED, claimed=claimed, idempotency_key=RETRY)
    # A raw replay names the claim's provider and carries no plan, as the claim is now bound.
    again = await launch(env, name, None, claimed=claimed, idempotency_key=RETRY)
    assert again.id == first.id == CHILD and again.provider == "codex"
    assert factory.call_count == 1 and backend.create_window.call_count == 1


# Kiro's probe-failure fallback reruns the profile's model; a routed launch refuses instead.


@pytest.mark.asyncio
async def test_a_routed_launch_whose_planned_model_the_kiro_probe_refuses_is_refused(
    runtime_store,
):
    from cli_agent_orchestrator.models.kiro_engine import KiroEngine
    from cli_agent_orchestrator.providers.kiro_capabilities import KiroCapabilityError

    env, name, backend, factory = runtime_store
    calls = []

    def probe(engine, requested):
        calls.append(set(requested))
        if len(calls) == 1:
            raise KiroCapabilityError("unsupported", KiroEngine.V2, "synthetic probe refusal")

    claimed = claim(env, name)["claim_id"]
    plan = LaunchPlan(model="kiro-planned", provider="kiro_cli")
    with pytest.raises(KiroCapabilityError):
        await launch(env, name, plan, claimed=claimed, kiro_capability_probe=probe)
    assert len(calls) == 1
    factory.assert_not_called()
    backend.create_window.assert_not_called()
    row = database.get_ephemeral_agent(name)
    assert row["state"] == "pending" and row["provider"] == "claude_code"


# run_agent_step classifies the output with the provider that ran.


def test_a_step_classifies_with_the_launched_provider():
    terminal = Mock(id="abc12345", provider="codex")
    classify = Mock(return_value=None)
    with (
        patch.object(
            agent_step.terminal_service, "create_terminal", new=AsyncMock(return_value=terminal)
        ),
        patch.object(agent_step.terminal_service, "send_input", return_value=True),
        patch.object(agent_step.terminal_service, "delete_terminal", return_value=True),
        patch.object(agent_step.terminal_service, "get_output", return_value="the answer"),
        patch.object(agent_step.terminal_service, "exit_terminal_cli", return_value=None),
        patch.object(agent_step, "wait_until_status", new=AsyncMock(return_value=True)),
        patch.object(
            agent_step.status_monitor, "get_status", return_value=TerminalStatus.COMPLETED
        ),
        patch.object(agent_step.frozen_run_memory, "frozen_memory_for", return_value=None),
        patch.object(agent_step, "classify_provider_error", classify),
    ):
        asyncio.run(agent_step.run_agent_step("claude_code", "dev", "x", launch_plan=ROUTED))
    assert classify.call_args.args[0] == "codex"


# _create_terminal returns the created terminal's provider, which the early and no-wait
# handoff paths reuse and shape with.


def test_create_terminal_returns_the_created_terminals_provider(monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", CALLER)
    responses = {
        "get": Mock(
            status_code=200,
            json=lambda: {"provider": "claude_code", "session_name": "s", "allowed_tools": None},
        ),
        "post": Mock(status_code=201, json=lambda: {"id": "eeeeeeee", "provider": "codex"}),
    }
    monkeypatch.setattr(orchestration.requests, "get", lambda *a, **kw: responses["get"])
    monkeypatch.setattr(orchestration.requests, "post", lambda *a, **kw: responses["post"])
    claim = orchestration.EphemeralClaim("claude_code", ["fs_read", "@cao-mcp-server"], "b" * 32)
    from cli_agent_orchestrator.utils import agent_profiles

    monkeypatch.setattr(agent_profiles, "routes_to_ephemeral_store", lambda _: True)
    terminal_id, provider = orchestration._create_terminal(
        "Ramones-log_triage-3f9a", "/workspace", claim=claim
    )
    assert (terminal_id, provider) == ("eeeeeeee", "codex")


# Provider pairs: the decided provider's command is built from a profile rendered for the
# fallback. Its model and effort come only from the plan, and nothing from the fallback's
# provider-specific section reaches it.


def _command(provider, terminal_id, name, model, effort):
    cls = claude_code.ClaudeCodeProvider if provider == "claude_code" else codex.CodexProvider
    instance = cls(
        terminal_id,
        "cao-session",
        "child",
        name,
        allowed_tools=["fs_read", "@cao-mcp-server"],
        model=model,
        effort=effort,
    )
    command = (
        instance._build_claude_command()
        if provider == "claude_code"
        else instance._build_codex_command()
    )
    return shlex.split(command.split("; ")[-1])


def _effort_flags(args):
    efforts = [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == "--effort"]
    reasoning = [arg for arg in args if arg.startswith("model_reasoning_effort=")]
    return efforts, reasoning


@pytest.mark.parametrize("fallback,decided", [("claude_code", "codex"), ("codex", "claude_code")])
def test_the_decided_providers_command_ignores_the_fallbacks_rendered_section(
    create_store, monkeypatch, fallback, decided
):
    monkeypatch.setattr(claude_code, "CAO_HOME_DIR", create_store[2])
    monkeypatch.setattr(codex, "CAO_HOME_DIR", create_store[2])
    monkeypatch.setitem(
        caps.CAPABILITIES,
        "codex",
        replace(
            caps.CAPABILITIES["codex"],
            enforcement=caps.SANDBOXED,
            tool_atoms=frozenset(TOOL_ATOMS) - {"web_fetch"},
        ),
    )
    config = create_store[4]
    config["model_tiers"] = TIERS
    config["ephemeral"].update(
        allowed_providers=["claude_code", "codex"], default_tier="medium", default_effort="low"
    )
    name = create(create_store, {**SPEC, "provider": fallback})["name"]
    claim(create_store, name)
    live = frontmatter.loads(
        (create_store[2] / "ephemeral/live" / (name + ".md")).read_bytes().decode()
    )
    assert live["provider"] == fallback and live["model"] == TIERS[fallback]["medium"]
    section = "claudeConfig" if fallback == "claude_code" else "codexConfig"
    assert section in live.metadata
    args = _command(decided, "abcd0001", name, TIERS[decided]["medium"], "high")
    models = [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == "--model"]
    assert models == [TIERS[decided]["medium"]]
    assert TIERS[fallback]["medium"] not in " ".join(args)
    efforts, reasoning = _effort_flags(args)
    if decided == "codex":
        assert reasoning == ['model_reasoning_effort="high"'] and efforts == []
    else:
        assert efforts == ["high"] and reasoning == []
    # Control: the fallback's own command does carry its section, so the check above can see it.
    own_efforts, own_reasoning = _effort_flags(_command(fallback, "abcd0002", name, None, None))
    assert (own_efforts or own_reasoning) and "low" in " ".join(own_efforts + own_reasoning)
