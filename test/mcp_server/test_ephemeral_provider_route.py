"""#1099: provider.route for a spec that declared provider auto, through the real routes.

The claim, the decision, the recheck, the bind and the provider commands are real; only the
backend and the provider process are mocked (#1068's route harness). With today's capability
table no set validates, so these cases substitute Codex's row with the contained launch's,
sandboxed and without web_fetch. Real Claude Code and Codex are the only providers built: the
"launches on" assertions read each provider's own command line, which for a routed launch is
built from a live profile rendered for the fallback.
"""

import ast
import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from test.mcp_server.test_ephemeral_auto_route import (  # noqa: F401
    BOUNDS,
    CHILD,
    EFFORT_BOUNDS,
    PATHS,
    case,
    decided_events,
    delete_child,
    drain,
    e5,
    invoke,
    launch_http,
    operator_edit,
    raw_launch,
    release_runtime,
    runtime_store,
)
from test.mcp_server.test_ephemeral_effort_auto_route import (  # noqa: F401
    finalized,
    live_profile,
    rt,
)
from test.mcp_server.test_ephemeral_launch_handlers import _create_store  # noqa: F401
from test.services.test_ephemeral_claim import NOW, audit, claim
from test.services.test_ephemeral_service import CALLER, SPEC, create
from types import SimpleNamespace

import frontmatter
import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.decisions.settings import PointSettings
from cli_agent_orchestrator.decisions.types import PointState
from cli_agent_orchestrator.models.ephemeral import TOOL_ATOMS
from cli_agent_orchestrator.utils import ephemeral_capabilities as caps
from cli_agent_orchestrator.utils import orchestration

BOTH_TIERS = {
    provider: {t: f"{provider}-{t}" for t in ("small", "medium", "large")}
    for provider in ("claude_code", "codex")
}
SET = [["claude_code", "codex"]]
DEFAULTS = {"default_tier": "medium", "default_effort": "medium"}
DESCRIPTION = "Reviewer of synthetic logs."
BRIEF = "CREATOR_BRIEF_IS_NEVER_SENT"


def C(**values):
    """A case: provider auto, both tables and the declared set unless overridden."""
    return {"tiers": BOTH_TIERS, **values}


def codex_row(kind):
    if kind == "advisory":
        return caps.CAPABILITIES["codex"]
    if kind == "no_effort":
        # dataclasses.replace re-runs the capability table's import-time invariant (#1074),
        # so only a stub can model a provider that honours the model but not effort.
        return SimpleNamespace(
            enforcement=caps.SANDBOXED,
            tool_atoms=frozenset(TOOL_ATOMS) - {"web_fetch"},
            honors_model=True,
            effort_config=None,
            supports_ephemeral=True,
            honors_effort=False,
        )
    return replace(
        caps.CAPABILITIES["codex"],
        enforcement=caps.SANDBOXED,
        tool_atoms=frozenset(TOOL_ATOMS) - {"web_fetch"},
    )


@pytest.fixture
def create_store(_create_store, case, monkeypatch):  # noqa: F811
    monkeypatch.setitem(caps.CAPABILITIES, "codex", codex_row(case.get("codex", "sandboxed")))
    config = _create_store[4]
    config["model_tiers"] = case.get("tiers", BOTH_TIERS)
    config["ephemeral"].update(
        allowed_providers=["claude_code", "codex"],
        provider_route={"equivalent": case.get("sets", SET)},
    )
    config["ephemeral"].update(BOUNDS if case.get("tier") == "auto" else {})
    config["ephemeral"].update(EFFORT_BOUNDS if case.get("effort") == "auto" else {})
    config["ephemeral"].update(case.get("policy", {}))
    if case.get("creator_tools"):
        with _create_store[1]() as db:
            db.get(database.TerminalModel, CALLER).allowed_tools = json.dumps(case["creator_tools"])
            db.commit()
    return _create_store


@pytest.fixture
def claimed_store(create_store, case, monkeypatch):  # noqa: F811
    monkeypatch.setattr(database, "_utcnow", lambda: NOW)
    spec = {**SPEC, "description": DESCRIPTION, "brief": BRIEF}
    if case.get("provider", "auto") is not None:
        spec["provider"] = case.get("provider", "auto")
    for key in ("tier", "effort", "tools"):
        if case.get(key):
            spec["model_tier" if key == "tier" else key] = case[key]
    name = create(create_store, spec)["name"]
    tokens = iter(range(1, 100))
    monkeypatch.setattr(create_store[0].secrets, "token_hex", lambda n: f"{next(tokens):0{n * 2}x}")
    return create_store, name


@pytest.fixture
def pr(rt, case):  # noqa: F811
    """#1068's route runtime, with provider.route declared by the decider and in settings."""
    rt.decider.points = frozenset({"model.route", "effort.route", "provider.route"})
    rt.decider.answers["provider.route"] = case.get("provider_answer", "codex")
    base = rt.engine.settings_loader()
    state = PointState(case.get("provider_state", case.get("state", "on")))
    rt.settings = replace(
        base, points={**base.points, "provider.route": PointSettings(state, rt.decider.name)}
    )
    rt.engine.settings_loader = lambda: rt.settings
    return rt


def ran(rt):
    """The CLI each launch built a command for, read from the command line itself."""
    return [args[0] for args in rt.commands]


def flag(args, name):
    return [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == name]


def reasoning(args):
    return [arg for arg in args if arg.startswith("model_reasoning_effort=")]


def records(rt, point=None):
    rows = sorted(rt.store.list(), key=lambda row: row["id"])
    return [row for row in rows if point is None or row["point"] == point]


def bound(name):
    return [event for event in audit(name)["events"] if event["event"] == "bound"]


def run_step_prompt(rt):
    (call,) = [c for c in rt.calls if c[1] == "/terminals/run-step" and "claim_id" in c[2]["json"]]
    return call[2]["json"]["prompt"]


def assert_shared_state_names_the_fallback(rt):
    """The registry row, the live profile and the archive keep the stored fallback.

    The profile is the one the launch loaded (a handoff's teardown collects the live files),
    or the live file when nothing launched.
    """
    row = database.get_ephemeral_agent(rt.name)
    assert row["provider"] == "claude_code"
    payload, loaded = rt.profiles[-1] if rt.profiles else (live_profile(rt), row)
    assert loaded["provider"] == "claude_code"
    assert frontmatter.loads(payload.decode())["provider"] == "claude_code"
    assert hashlib.sha256(payload).hexdigest() == row["profile_sha256"]
    archive = audit(rt.name)
    assert archive["provider"] == "claude_code"
    assert archive["profile_sha256"] == row["profile_sha256"]
    assert len(finalized(rt.name)) == 1


# AC1: off asks nothing, writes no record and launches the fallback.


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("case", [C(provider_state="off", tier="auto")], indirect=True)
async def test_AC1_off_asks_nothing_and_launches_the_fallback(pr, path):
    result = await invoke(path, pr.name)
    assert result["success"], result
    drain(pr)
    assert records(pr, "provider.route") == []
    assert all(request.points != ("provider.route",) for request in pr.decider.requests)
    assert ran(pr) == ["claude"]
    (args,) = pr.commands
    assert flag(args, "--model") == [BOTH_TIERS["claude_code"]["large"]]
    delete_child()


# AC2: shadow records one pending row and never waits for it or applies it.


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("case", [C(state="shadow", tier="auto")], indirect=True)
async def test_AC2_shadow_records_once_and_launches_the_fallback_without_waiting(pr, path):
    gate = pr.portal.call(asyncio.Event)
    pr.decider.gate = gate
    result = await invoke(path, pr.name)
    assert result["success"], result
    # The launch returned on the fallback while the decider was still blocked.
    assert ran(pr) == ["claude"]
    (args,) = pr.commands
    assert flag(args, "--model") == [BOTH_TIERS["claude_code"]["medium"]]
    (pending,) = records(pr, "provider.route")
    assert pending["decision_status"] == "pending" and pending["state"] == "shadow"
    assert pending["options"] == ["claude_code", "codex"]
    pr.portal.call(gate.set)
    drain(pr)
    (record,) = records(pr, "provider.route")
    assert record["outcome"] == "shadow" and record["answer"] == "codex"
    assert record["applied_value"] is None and record["provider"] == "claude_code"
    assert all(request.facts.provider == "claude_code" for request in pr.decider.requests)
    assert bound(pr.name)[0].get("provider") is None
    delete_child()


# AC3: on, a confident in-set answer runs the decided provider, through the plan only.


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("case", [C(tier="auto")], indirect=True)
async def test_AC3_on_runs_the_decided_provider_and_shared_state_keeps_the_fallback(pr, path):
    result = await invoke(path, pr.name)
    assert result["success"], result
    assert ran(pr) == ["codex"]
    (args,) = pr.commands
    assert flag(args, "--model") == [BOTH_TIERS["codex"]["large"]]
    provider_ask, model_ask = pr.decider.requests
    assert provider_ask.points == ("provider.route",) and model_ask.points == ("model.route",)
    assert provider_ask.request_id == model_ask.request_id
    assert provider_ask.options == ("claude_code", "codex")
    assert provider_ask.facts.provider == "claude_code"
    assert model_ask.facts.provider == "codex" and model_ask.options is None
    (route,) = records(pr, "provider.route")
    assert (route["outcome"], route["applied_value"], route["provider"]) == (
        "applied",
        "codex",
        "codex",
    )
    assert route["fallback_value"] == "claude_code" and route["fallback_source"] == "creator"
    assert route["options"] == ["claude_code", "codex"]
    assert (route["fallback_enforcement"], route["applied_enforcement"]) == ("hard", "sandboxed")
    assert route["launch_status"] == "launched"
    (model,) = records(pr, "model.route")
    assert model["provider"] == "codex" and model["candidate_model"] == BOTH_TIERS["codex"]["large"]
    assert_shared_state_names_the_fallback(pr)
    (event,) = bound(pr.name)
    assert event["provider"] == "codex"
    assert database.get_launch_record(CHILD)["provider"] == "codex"
    if path in ("assign", "nowait"):
        # The handoff paths tear their terminal down; the launch record outlives it.
        assert database.get_terminal_metadata(CHILD)["provider"] == "codex"
    if path == "default":
        assert run_step_prompt(pr).startswith("[CAO Handoff]")
        assert "(codex)" not in result["message"] and "(claude_code)" not in result["message"]
    delete_child()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("case", [C(policy=DEFAULTS)], indirect=True)
async def test_AC3_a_provider_only_auto_launch_maps_its_stored_values_for_the_decided_provider(
    pr, path
):
    result = await invoke(path, pr.name)
    assert result["success"], result
    assert [request.points for request in pr.decider.requests] == [("provider.route",)]
    assert ran(pr) == ["codex"]
    (args,) = pr.commands
    # The stored tier under Codex's table, and the stored effort as Codex's override, though the
    # live profile names Claude Code's id and carries a claudeConfig section.
    assert flag(args, "--model") == [BOTH_TIERS["codex"]["medium"]]
    assert reasoning(args) == ['model_reasoning_effort="medium"'] and flag(args, "--effort") == []
    ((loaded, _),) = pr.profiles
    profile = frontmatter.loads(loaded.decode())
    assert profile["model"] == BOTH_TIERS["claude_code"]["medium"]
    assert profile["claudeConfig"] == {"effort": "medium"} and "codexConfig" not in profile.metadata
    assert_shared_state_names_the_fallback(pr)
    delete_child()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [C(policy=DEFAULTS)], indirect=True)
async def test_AC3_a_routed_launch_runs_when_its_records_cannot_be_written(pr, monkeypatch):
    # No record means no _recorded_tier: the recheck reverse-maps the plan's model with the
    # decided provider's table, and the plan still carries the provider.
    monkeypatch.setattr(pr.store, "insert", lambda *a, **k: None)
    result = await invoke("assign", pr.name)
    assert result["success"], result
    assert ran(pr) == ["codex"]
    (args,) = pr.commands
    assert flag(args, "--model") == [BOTH_TIERS["codex"]["medium"]]
    delete_child()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [C(tier="auto", answer="large")], indirect=True)
async def test_AC3_an_unrecorded_decided_tier_is_reverse_mapped_with_the_decided_table(
    pr, monkeypatch
):
    monkeypatch.setattr(pr.store, "insert", lambda *a, **k: None)
    result = await invoke("assign", pr.name)
    assert result["success"], result
    (args,) = pr.commands
    assert args[0] == "codex" and flag(args, "--model") == [BOTH_TIERS["codex"]["large"]]
    delete_child()


# AC4: an answer outside this launch's options, or any unusable one, runs the fallback.


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "default"])
@pytest.mark.parametrize(
    "case,reason",
    [
        (C(policy=DEFAULTS, provider_answer="kiro_cli"), "invalid_answer"),
        (C(policy=DEFAULTS, provider_answer="unsure"), "unsure"),
        (C(policy=DEFAULTS, confidence=0.2), "low_confidence"),
    ],
    indirect=["case"],
)
async def test_AC4_an_unusable_answer_runs_the_fallback(pr, path, reason):
    result = await invoke(path, pr.name)
    assert result["success"], result
    assert ran(pr) == ["claude"]
    (record,) = records(pr, "provider.route")
    assert (record["outcome"], record["reason"]) == ("fallback", reason)
    assert "kiro_cli" not in record["options"] and record["provider"] == "claude_code"
    assert "provider" not in bound(pr.name)[0]
    delete_child()


# AC5: one option asks nothing and records single_option.


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize(
    "case",
    [
        C(policy=DEFAULTS, sets=[]),
        # A set exists, but the stored tools' atoms leave one member: Codex refuses web_fetch.
        C(
            policy=DEFAULTS,
            tools=["fs_read", "web_fetch"],
            creator_tools=["@cao-mcp-server", "fs_read", "web_fetch"],
        ),
        # ... Codex's table doesn't map the explicit tier.
        C(
            tier="large",
            tiers={**BOTH_TIERS, "codex": {"small": "codex-small", "medium": "codex-medium"}},
        ),
        # ... a provider that can't apply an effort, for an effort-bearing launch.
        C(policy=DEFAULTS, codex="no_effort"),
    ],
    indirect=True,
)
async def test_AC5_one_option_asks_nothing_and_records_single_option(pr, path):
    result = await invoke(path, pr.name)
    assert result["success"], result
    assert pr.decider.requests == []
    (record,) = records(pr, "provider.route")
    assert (record["outcome"], record["reason"]) == ("fallback", "single_option")
    assert record["options"] == ["claude_code"] and record["decider"] is None
    assert ran(pr) == ["claude"]
    delete_child()


# AC7: an explicit provider is never routed, whatever is declared and on.


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize(
    "case", [C(provider="codex", policy=DEFAULTS), C(provider="codex", tier="auto")], indirect=True
)
async def test_AC7_an_explicit_provider_never_asks_provider_route(pr, path):
    result = await invoke(path, pr.name)
    assert result["success"], result
    drain(pr)
    assert records(pr, "provider.route") == []
    assert all(request.points != ("provider.route",) for request in pr.decider.requests)
    assert ran(pr) == ["codex"]
    delete_child()


@pytest.mark.parametrize(
    "case,auto",
    [(C(policy=DEFAULTS), True), (C(provider="codex", policy=DEFAULTS), False)],
    indirect=["case"],
)
def test_AC7_the_claim_says_whether_the_spec_declared_provider_auto(create_store, auto):
    name = create(create_store, {**SPEC, "provider": "auto" if auto else "codex"})["name"]
    response = claim(create_store, name)
    assert response["provider_auto"] is auto
    assert response["provider"] == ("claude_code" if auto else "codex")


@pytest.mark.parametrize("tamper", ["rewrite", "delete", "directory"])
@pytest.mark.parametrize("case", [C(policy=DEFAULTS)], indirect=True)
def test_AC7_a_spec_changed_after_finalize_fails_the_claim_closed(
    create_store, monkeypatch, tamper
):
    """#1099: provider_auto comes only from the spec finalize verified, read again strictly.

    A rewrite that would flip provider_auto must refuse rather than be reported; a deleted or
    replaced file must refuse rather than read as an explicit provider.
    """
    service, _, home, _, _ = create_store
    monkeypatch.setattr(database, "_utcnow", lambda: NOW)
    name = create(create_store, {**SPEC, "provider": "auto"})["name"]
    spec_path = home / "ephemeral/live" / (name + ".spec.json")
    verified = service.finalize

    def finalize_then_tamper(*args, **kwargs):
        verified(*args, **kwargs)
        if tamper == "rewrite":
            # Still valid JSON and a valid spec: only the hash check can tell.
            stored = json.loads(spec_path.read_bytes())
            spec_path.write_text(json.dumps({**stored, "provider": "claude_code"}))
        else:
            spec_path.unlink()
            if tamper == "directory":
                spec_path.mkdir()

    monkeypatch.setattr(service, "finalize", finalize_then_tamper)
    with pytest.raises(service.EphemeralPolicyError) as err:
        claim(create_store, name)
    assert (err.value.rule, err.value.status_code) == ("unexpected_failure", 500)
    row = database.get_ephemeral_agent(name)
    assert row["state"] == "pending" and row["claim_id"] is None
    assert row["provider"] == "claude_code"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [C(policy=DEFAULTS)], indirect=True)
async def test_a_claimless_launch_of_an_auto_spec_runs_the_stored_provider(pr):
    response = pr.client.post(
        "/sessions/cao-session/terminals",
        params={"provider": "claude_code", "agent_profile": pr.name, "caller_id": CALLER},
    )
    assert response.status_code < 300, response.text
    assert pr.decider.requests == [] and records(pr) == []
    assert ran(pr) == ["claude"]
    delete_child()


# AC10: cao decisions off sends nothing and launches the fallback.


@pytest.mark.asyncio
@pytest.mark.parametrize("halted", [True, False])
@pytest.mark.parametrize("path", ["assign", "default"])
@pytest.mark.parametrize("case", [C(policy=DEFAULTS)], indirect=True)
async def test_AC10_decisions_off_sends_nothing(pr, monkeypatch, path, halted):
    from cli_agent_orchestrator.decisions import egress
    from cli_agent_orchestrator.decisions.settings import load_settings

    pr.env[4]["decisions"] = {"points": {"provider.route": {"state": "on", "decider": "e5_test"}}}
    monkeypatch.setattr(egress, "is_halted", lambda: halted)
    constructed = []
    original = pr.engine.registry._sources["e5_test"]
    pr.engine.registry._sources["e5_test"] = lambda: constructed.append(1) or original()
    pr.engine.settings_loader = lambda: load_settings(environment={})
    result = await invoke(path, pr.name)
    assert result["success"], result
    assert len(constructed) == len(pr.decider.requests) == (0 if halted else 1)
    assert len(records(pr, "provider.route")) == (0 if halted else 1)
    assert ran(pr) == (["claude"] if halted else ["codex"])
    delete_child()


# AC8, and the failure matrix: a routed launch refused before the bind constructs no Codex
# process, binds every record not_launched, and leaves the name pending with only its stored
# values, so a claimless launch runs the stored provider and a re-claim decides again.


def _assert_only_the_stored_provider_survives(rt, status="not_launched"):
    """``status``: a refusal before create_terminal binds not_launched; one inside it, before
    the bind, binds launch_failed, as for every decided launch (#1068's classification)."""
    row = database.get_ephemeral_agent(rt.name)
    assert row["state"] == "pending" and row["provider"] == "claude_code"
    assert row["claim_id"] is None
    assert rt.profiles == [] and rt.commands == []
    assert_shared_state_names_the_fallback(rt)
    assert bound(rt.name) == []
    assert {record["launch_status"] for record in records(rt)} == {status}


async def _claimless(rt):
    from cli_agent_orchestrator.services import terminal_service

    rt.commands.clear()
    return await terminal_service.create_terminal(
        "claude_code", rt.name, session_name="cao-session", caller_id=CALLER
    )


async def _relaunch_decides_again(rt):
    asked = len(rt.decider.requests)
    first = rt.decider.requests[0].request_id
    rt.commands.clear()
    result = await invoke("assign", rt.name)
    assert result["success"], result
    assert ran(rt) == ["codex"]
    again = rt.decider.requests[asked]
    assert again.points == ("provider.route",) and again.request_id != first
    delete_child()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "default"])
@pytest.mark.parametrize("case", [C(policy=DEFAULTS)], indirect=True)
async def test_a_terminal_limit_before_the_bind_leaves_only_the_stored_provider(
    pr, monkeypatch, path
):
    from cli_agent_orchestrator.services import terminal_service

    monkeypatch.setattr(terminal_service, "get_max_terminals", lambda: 0)
    result = await invoke(path, pr.name)
    assert not result["success"], result
    _assert_only_the_stored_provider_survives(pr)
    monkeypatch.setattr(terminal_service, "get_max_terminals", lambda: None)
    await _relaunch_decides_again(pr)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "default"])
@pytest.mark.parametrize("case", [C(policy=DEFAULTS)], indirect=True)
async def test_an_unresolvable_ceiling_before_the_bind_leaves_only_the_stored_provider(
    pr, monkeypatch, path
):
    from cli_agent_orchestrator.utils import delegation

    original = delegation.ceiling_from_row
    monkeypatch.setattr(delegation, "ceiling_from_row", lambda row: None)
    result = await invoke(path, pr.name)
    assert not result["success"], result
    _assert_only_the_stored_provider_survives(pr)
    monkeypatch.setattr(delegation, "ceiling_from_row", original)
    await _claimless(pr)
    assert ran(pr) == ["claude"]
    delete_child()


@pytest.mark.parametrize("site", ["terminal", "step"])
@pytest.mark.parametrize("case", [C(policy=DEFAULTS)], indirect=True)
def test_an_owner_chosen_tool_mismatch_before_the_bind_leaves_only_the_stored_provider(pr, site):
    extra = "fs_read,fs_write" if site == "terminal" else ["fs_read", "fs_write"]
    token = claim(pr.env, pr.name)["claim_id"]
    response = raw_launch(pr, site, token, allowed_tools=extra)
    assert response.status_code == 400
    assert response.json()["detail"]["rule"] == "tool_exceeds_stored"
    _assert_only_the_stored_provider_survives(pr, "launch_failed")
    # The raw HTTP claimless route runs the stored provider.
    response = pr.client.post(
        "/sessions/cao-session/terminals",
        params={"provider": "claude_code", "agent_profile": pr.name, "caller_id": CALLER},
    )
    assert response.status_code < 300, response.text
    assert ran(pr) == ["claude"]
    delete_child()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [C(policy=DEFAULTS)], indirect=True)
async def test_a_lapsed_lease_after_the_recheck_leaves_only_the_stored_provider(pr, monkeypatch):
    service = pr.env[0]
    original = service.recheck_decided

    def recheck_then_lapse(*args, **kwargs):
        decided = original(*args, **kwargs)
        monkeypatch.setattr(database, "_utcnow", lambda: NOW + timedelta(seconds=61))
        return decided

    monkeypatch.setattr(service, "recheck_decided", recheck_then_lapse)
    result = await invoke("assign", pr.name)
    assert not result["success"] and result["rule"] == "claim_expired", result
    _assert_only_the_stored_provider_survives(pr, "launch_failed")
    monkeypatch.setattr(database, "_utcnow", lambda: NOW)
    await _claimless(pr)
    assert ran(pr) == ["claude"]
    delete_child()


@pytest.mark.asyncio
@pytest.mark.parametrize("sweep", [True, False])
@pytest.mark.parametrize("case", [C(policy=DEFAULTS)], indirect=True)
async def test_a_crash_before_the_bind_leaves_only_the_stored_provider(pr, monkeypatch, sweep):
    # No cleanup runs, as if the CAO process died between the recheck and the bind.
    from cli_agent_orchestrator.services import terminal_service

    service = pr.env[0]
    real_end_claim = service.end_claim
    monkeypatch.setattr(service, "end_claim", lambda *a, **k: None)
    monkeypatch.setattr(terminal_service, "get_max_terminals", lambda: 0)
    result = await invoke("assign", pr.name)
    assert not result["success"], result
    row = database.get_ephemeral_agent(pr.name)
    assert row["state"] == "claimed" and row["provider"] == "claude_code"
    monkeypatch.setattr(service, "end_claim", real_end_claim)
    monkeypatch.setattr(terminal_service, "get_max_terminals", lambda: None)
    monkeypatch.setattr(database, "_utcnow", lambda: NOW + timedelta(seconds=61))
    if sweep:
        service.sweep(True)
        _assert_only_the_stored_provider_survives(pr)
    else:
        # Without a sweep, the claimless bind's own _lapse returns the name inline.
        assert_shared_state_names_the_fallback(pr)
    await _claimless(pr)
    assert ran(pr) == ["claude"]
    delete_child()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [C(policy=DEFAULTS)], indirect=True)
async def test_after_a_failed_routed_launch_the_claimless_path_checks_only_the_stored_provider(
    pr, monkeypatch
):
    from cli_agent_orchestrator.services import terminal_service

    monkeypatch.setattr(terminal_service, "get_max_terminals", lambda: 0)
    result = await invoke("assign", pr.name)
    assert not result["success"], result
    monkeypatch.setattr(terminal_service, "get_max_terminals", lambda: None)
    operator_edit(pr, {"allowed_providers": ["codex"]})
    with pytest.raises(pr.env[0].EphemeralPolicyError) as err:
        await _claimless(pr)
    assert err.value.rule == "policy_changed_since_create:provider_not_allowed"
    row = database.get_ephemeral_agent(pr.name)
    assert row["state"] == "gc" and row["gc_reason"] == "policy_changed"
    assert pr.commands == [] and database.get_terminal_metadata(CHILD) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [C(policy=DEFAULTS)], indirect=True)
async def test_the_control_a_routed_launch_without_a_failure_runs_codex(pr):
    # Proves the read-backs above aren't vacuous: with no failure the command is Codex's while
    # the row and the live profile still name the fallback.
    result = await invoke("assign", pr.name)
    assert result["success"], result
    assert ran(pr) == ["codex"]
    assert_shared_state_names_the_fallback(pr)
    delete_child()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [C(policy=DEFAULTS)], indirect=True)
async def test_a_routed_launch_refused_after_the_bind_tries_no_other_provider(pr):
    pr.factory.return_value.initialize.side_effect = RuntimeError("synthetic identity refusal")
    result = await invoke("default", pr.name)
    assert not result["success"], result
    assert ran(pr) == ["codex"]
    row = database.get_ephemeral_agent(pr.name)
    assert row["state"] == "gc" and row["gc_reason"] == "launch_failed"
    assert row["provider"] == "claude_code"
    (record,) = records(pr, "provider.route")
    assert record["applied_value"] == "codex" and record["launch_status"] == "launch_failed"


# A policy changed during the decision: the decided provider is rechecked before the bind and
# the name collects; another provider never runs silently.


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "default"])
@pytest.mark.parametrize(
    "case,change,rule",
    [
        (C(policy=DEFAULTS), {"provider_route": {"equivalent": []}}, "provider_not_equivalent"),
        (C(policy=DEFAULTS), {"allowed_providers": ["claude_code"]}, "provider_not_allowed"),
        # An explicit tier with no ceiling: the block stays valid once Codex stops mapping it.
        (
            C(tier="large"),
            {"model_tiers": {**BOTH_TIERS, "codex": {"small": "codex-small"}}},
            "tier_unmapped",
        ),
    ],
    indirect=["case"],
)
async def test_a_policy_change_during_the_decision_rechecks_the_decided_provider(
    pr, path, change, rule
):
    pr.decider.during = lambda: operator_edit(pr, change)
    result = await invoke(path, pr.name)
    assert not result["success"]
    assert result["rule"] == "policy_changed_since_create:" + rule, result
    row = database.get_ephemeral_agent(pr.name)
    assert row["state"] == "gc" and row["gc_reason"] == "policy_changed"
    assert pr.commands == []
    assert {record["launch_status"] for record in records(pr)} == {"not_launched"}


# AC9: no query, body or MCP argument reaches the provider, the options or the plan.


@pytest.mark.parametrize("site", ["terminal", "step"])
@pytest.mark.parametrize("case", [C(policy=DEFAULTS)], indirect=True)
def test_AC9_a_requested_provider_other_than_the_rows_is_refused_unasked(pr, site):
    token = claim(pr.env, pr.name)["claim_id"]
    response = raw_launch(pr, site, token, provider="codex")
    assert response.status_code == 400
    assert response.json()["detail"]["rule"] == "provider_mismatch"
    assert pr.decider.requests == [] and records(pr) == [] and pr.commands == []
    assert database.get_ephemeral_agent(pr.name)["state"] == "pending"


SMUGGLED = {
    "options": ["kiro_cli"],
    "provider_options": {"options": ["kiro_cli"], "labels": {"kiro_cli": "hard"}},
    "launch_plan": {"provider": "kiro_cli", "model": "smuggled-model"},
    "launch_provider": "kiro_cli",
    "provider_auto": False,
}


@pytest.mark.parametrize("site", ["terminal", "step"])
@pytest.mark.parametrize("case", [C(policy=DEFAULTS)], indirect=True)
def test_AC9_smuggled_body_keys_change_nothing(pr, site):
    token = claim(pr.env, pr.name)["claim_id"]
    if site == "terminal":
        response = pr.client.post(
            "/sessions/cao-session/terminals",
            params={
                "provider": "claude_code",
                "agent_profile": pr.name,
                "caller_id": CALLER,
                "claim_id": token,
                "options": "kiro_cli",
                "launch_provider": "kiro_cli",
            },
            json={"delegation_message": "inspect", "delegation_kind": "handoff", **SMUGGLED},
        )
    else:
        response = raw_launch(pr, site, token, session_name="cao-session", **SMUGGLED)
    assert response.status_code < 300, response.text
    (record,) = records(pr, "provider.route")
    assert record["options"] == ["claude_code", "codex"]
    assert pr.decider.requests[0].options == ("claude_code", "codex")
    assert ran(pr) == ["codex"]
    (args,) = pr.commands
    assert "smuggled-model" not in args
    delete_child()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("case", [C(policy=DEFAULTS)], indirect=True)
async def test_AC9_the_mcp_helpers_request_the_claims_provider(pr, path):
    import inspect

    for impl in (orchestration._assign_impl, orchestration._handoff_impl):
        assert "provider" not in inspect.signature(impl).parameters
    result = await invoke(path, pr.name)
    assert result["success"], result
    launches = [c for c in pr.calls if c[0] == "post" and not c[1].endswith("/claim")]
    first = launches[0][2]
    requested = (first.get("json") or {}).get("provider") or first["params"]["provider"]
    assert requested == "claude_code"
    assert ran(pr) == ["codex"]
    delete_child()


SRC = Path(__file__).resolve().parents[2] / "src" / "cli_agent_orchestrator"


def _calls(name):
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                if called == name:
                    yield path.relative_to(SRC).as_posix(), node


def test_AC9_only_the_engine_builds_or_rebinds_a_routed_plan():
    routed = [
        where
        for where, node in _calls("LaunchPlan")
        if len(node.args) >= 4 or any(kw.arg == "provider" for kw in node.keywords)
    ]
    assert routed == ["decisions/engine.py"]
    rebinds = sorted(
        (where, ast.unparse(node.args[0]))
        for where, node in _calls("replace")
        if any(kw.arg == "provider" for kw in node.keywords)
    )
    # The engine rebinds its DelegationRequest after the provider ask, and the facts that the
    # later points' requests carry; nothing else replaces a provider.
    assert rebinds == [("decisions/engine.py", "req"), ("decisions/engine.py", "shared.facts")]


def test_AC9_no_launch_request_model_declares_a_routing_field():
    """A model that carries a launch (a claim, a profile or a provider) has no routing field."""
    forbidden = {"options", "provider_options", "launch_plan", "launch_provider", "provider_auto"}
    launch_fields = {"claim_id", "agent_profile", "agent", "provider", "delegation_message"}
    models = {}
    for path in sorted((SRC / "api").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ClassDef):
                fields = {
                    stmt.target.id
                    for stmt in node.body
                    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
                }
                models[node.name] = (fields, [ast.unparse(b) for b in node.bases])

    def fields_of(name):
        fields, bases = models.get(name, (set(), []))
        return fields.union(*(fields_of(base) for base in bases if base in models))

    launches = {name for name in models if fields_of(name) & launch_fields}
    assert {"CreateTerminalBody", "RunStepRequest"} <= launches
    assert {name: fields_of(name) & forbidden for name in launches} == dict.fromkeys(
        launches, set()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,banner",
    [
        (C(provider_state="off", policy=DEFAULTS), True),
        (C(provider="claude_code", policy=DEFAULTS), False),
        (C(provider=None, policy=DEFAULTS), False),
    ],
    indirect=["case"],
)
async def test_a_provider_auto_claim_always_gets_the_handoff_banner(pr, banner):
    """Even unrouted, an auto claim's pre-launch text is the same whichever CLI runs."""
    result = await invoke("default", pr.name)
    assert result["success"], result
    assert ran(pr) == ["claude"]
    prompt = run_step_prompt(pr)
    assert prompt.startswith("[CAO Handoff]") is banner
    assert prompt.endswith("inspect")
    assert ("(claude_code)" in result["message"]) is not banner


@pytest.mark.parametrize(
    "case,suffixed",
    [(C(policy=DEFAULTS), False), (C(provider="claude_code", policy=DEFAULTS), True)],
    indirect=["case"],
)
def test_assign_drops_the_suffix_from_an_at_task_for_a_provider_auto_claim(pr, suffixed):
    task = "@agent-sop:code-analysis query=logs"
    result = orchestration._assign_impl(pr.name, task, working_directory="/workspace")
    assert result["success"], result
    (create,) = [c for c in pr.calls if c[0] == "post" and c[1].endswith("/terminals")]
    initial = create[2]["json"]["initial_message"]
    assert initial.startswith(task) and (initial != task) is suffixed
    delete_child()
