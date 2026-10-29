"""#1083: effort.route for a spec that declared effort auto, through the real routes.

The claim, bind, second finalize and provider construction are real; only the backend and the
provider process are mocked. The launched effort is read back from the provider's own command
line, built from the hash-verified live profile.
"""

import asyncio
import hashlib
import json
import shlex
from datetime import timedelta
from test.mcp_server.test_ephemeral_auto_route import (  # noqa: F401
    BROKEN_BLOCKS,
    CHILD,
    PATHS,
    TIERS,
    assert_returned_to_pending,
    case,
    claimed_store,
    create_store,
    decided_events,
    delete_child,
    drain,
    e5,
    invoke,
    launch_http,
    launched_model,
    operator_edit,
    raw_launch,
    release_runtime,
    runtime_store,
)
from test.mcp_server.test_ephemeral_launch_handlers import (  # noqa: F401
    _create_store,
    record_decision_calls,
)
from test.services.test_ephemeral_claim import NOW, audit, claim
from test.services.test_ephemeral_service import CALLER

import frontmatter
import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.providers import claude_code, codex
from cli_agent_orchestrator.utils import orchestration

AUTO = {"effort": "auto"}


@pytest.fixture
def rt(e5, monkeypatch):
    """#1068's route runtime, with each provider command built from the live profile at launch."""
    monkeypatch.setattr(claude_code, "CAO_HOME_DIR", e5.env[2])
    monkeypatch.setattr(codex, "CAO_HOME_DIR", e5.env[2])
    provider = e5.factory.return_value
    e5.commands, e5.profiles = [], []

    def build(*args, **kwargs):
        if "model" not in kwargs:
            return provider  # A later lookup, not the launch: see launched_model.
        # The live profile and its registered hash as the provider loads them.
        path = e5.env[2] / "ephemeral/live" / (args[4] + ".md")
        e5.profiles.append((path.read_bytes(), database.get_ephemeral_agent(args[4])))
        cls = claude_code.ClaudeCodeProvider if args[0] == "claude_code" else codex.CodexProvider
        instance = cls(
            *args[1:5],
            allowed_tools=args[5],
            skill_prompt=kwargs.get("skill_prompt"),
            model=kwargs.get("model"),
            effort=kwargs.get("effort"),
        )
        command = (
            instance._build_claude_command()
            if args[0] == "claude_code"
            else instance._build_codex_command()
        )
        e5.commands.append(shlex.split(command.split("; ")[-1]))
        return provider

    e5.factory.side_effect = build
    return e5


def launched_effort(rt):
    """The effort the child's command line carries, or None."""
    (args,) = rt.commands
    assert args.count("--effort") <= 1
    return args[args.index("--effort") + 1] if "--effort" in args else None


def records(rt, point=None):
    rows = sorted(rt.store.list(), key=lambda row: row["id"])
    return [row for row in rows if point is None or row["point"] == point]


def finalized(name):
    return [e for e in audit(name)["events"] if e["event"] == "finalized"]


def live_profile(rt):
    return (rt.env[2] / "ephemeral/live" / (rt.name + ".md")).read_bytes()


def asked_points(rt):
    return [request.points for request in rt.decider.requests]


# AC1: off asks nothing, writes no effort record and launches the stored effort.


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("case", [{**AUTO, "state": "off"}], indirect=True)
async def test_AC1_off_asks_nothing_and_runs_the_stored_effort(rt, path):
    result = await invoke(path, rt.name)
    assert result["success"], result
    drain(rt)
    assert rt.decider.requests == [] and rt.store.list() == []
    assert launched_effort(rt) == "medium"
    assert decided_events(rt.name) == [] and len(finalized(rt.name)) == 1
    delete_child()


# AC6: a halted platform constructs and calls no decider; the control does call it.


@pytest.mark.asyncio
@pytest.mark.parametrize("halted", [True, False])
@pytest.mark.parametrize("path", ["assign", "default"])
@pytest.mark.parametrize("case", [AUTO], indirect=True)
async def test_AC6_decisions_off_sends_nothing(rt, monkeypatch, path, halted):
    from cli_agent_orchestrator.decisions import egress
    from cli_agent_orchestrator.decisions.settings import load_settings

    rt.env[4]["decisions"] = {"points": {"effort.route": {"state": "on", "decider": "e5_test"}}}
    monkeypatch.setattr(egress, "is_halted", lambda: halted)
    constructed = []
    original = rt.engine.registry._sources["e5_test"]
    rt.engine.registry._sources["e5_test"] = lambda: constructed.append(1) or original()
    rt.engine.settings_loader = lambda: load_settings(environment={})
    result = await invoke(path, rt.name)
    assert result["success"], result
    drain(rt)
    assert len(constructed) == len(rt.decider.requests) == (0 if halted else 1)
    assert len(records(rt, "effort.route")) == (0 if halted else 1)
    assert launched_effort(rt) == ("medium" if halted else "high")
    delete_child()


# AC2: shadow records the answer once, launches the stored effort and never waits.


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("case", [{**AUTO, "state": "shadow"}], indirect=True)
async def test_AC2_shadow_records_once_and_never_applies(rt, path):
    gate = rt.portal.call(asyncio.Event)
    rt.decider.gate = gate
    result = await invoke(path, rt.name)
    assert result["success"], result
    # The launch returned on the stored effort while the decider was still blocked.
    assert launched_effort(rt) == "medium"
    (pending,) = rt.store.list()
    assert pending["decision_status"] == "pending" and pending["answer"] is None
    rt.portal.call(gate.set)
    drain(rt)
    assert asked_points(rt) == [("effort.route",)]
    (record,) = rt.store.list()
    assert record["point"] == "effort.route" and record["target_kind"] == "ephemeral"
    assert record["agent_profile"] == rt.name and record["state"] == "shadow"
    assert record["outcome"] == "shadow" and record["answer"] == "high"
    assert record["applied_value"] is None and record["fallback_value"] == "medium"
    assert [e["record_ids"] for e in decided_events(rt.name)] == [[record["id"]]]
    assert len(finalized(rt.name)) == 1
    delete_child()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("case", [{**AUTO, "state": "shadow"}], indirect=True)
async def test_AC2_shadow_never_applies_an_answer_that_is_already_ready(rt, path):
    # No gate: the decider answers at once, so "apply the shadow answer if ready" would show.
    result = await invoke(path, rt.name)
    assert result["success"], result
    assert launched_effort(rt) == "medium"
    drain(rt)
    (record,) = rt.store.list()
    assert record["outcome"] == "shadow" and record["applied_value"] is None
    assert len(finalized(rt.name)) == 1
    assert database.get_ephemeral_agent(rt.name)["effort"] == "medium"
    delete_child()


# AC3, and Q1 (C): on applies a confident answer, capped into [min_effort, max_effort], to the
# provider for that launch only, through the launch plan (#1068 C, #1083). The row and the
# live profile keep the stored effort; nothing is written for the decision.


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize(
    "case,applied",
    [
        ({**AUTO, "effort_answer": "high"}, "high"),
        ({**AUTO, "effort_answer": "low"}, "low"),
        ({**AUTO, "effort_answer": "high", "policy": {"max_effort": "medium"}}, "medium"),
        ({**AUTO, "effort_answer": "low", "policy": {"min_effort": "medium"}}, "medium"),
    ],
    indirect=["case"],
)
async def test_AC3_on_applies_the_answer_capped_into_bounds(rt, case, applied, path):
    result = await invoke(path, rt.name)
    assert result["success"], result
    assert launched_effort(rt) == applied
    (record,) = rt.store.list()
    assert record["point"] == "effort.route" and record["agent_profile"] == rt.name
    assert record["outcome"] == ("applied" if case["effort_answer"] == applied else "capped")
    assert record["answer"] == case["effort_answer"] and record["applied_value"] == applied
    assert record["launch_status"] == "launched"
    assert [e["record_ids"] for e in decided_events(rt.name)] == [[record["id"]]]
    # The decider saw one point, the redacted description and purpose, never the brief.
    (request,) = rt.decider.requests
    assert request.points == ("effort.route",) and request.facts.profile is None
    # The row and the live profile, verified by its hash, both keep the stored effort,
    # whatever was applied; the command carries the applied one (#1068 C, #1083).
    ((profile, row),) = rt.profiles
    assert row["effort"] == "medium"
    assert hashlib.sha256(profile).hexdigest() == row["profile_sha256"]
    assert frontmatter.loads(profile.decode())["claudeConfig"] == {"effort": "medium"}
    assert [e["event"] for e in audit(rt.name)["events"]][:5] == [
        "created",
        "claimed",
        "finalized",
        "decided",
        "bound",
    ]
    (event,) = finalized(rt.name)
    assert event["effort"] == "medium" and event["profile_sha256"] == row["profile_sha256"]
    delete_child()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        {
            **AUTO,
            "effort_answer": "high",
            "policy": {"allowed_providers": ["claude_code", "codex"]},
            "tiers": {
                provider: {t: "model-" + t for t in ("small", "medium", "large")}
                for provider in ("claude_code", "codex")
            },
        }
    ],
    indirect=True,
)
async def test_AC3_codex_launches_the_applied_effort_as_a_config_override(rt, monkeypatch):
    # The fixture creates for the creator's provider; recreate the spec for Codex.
    from test.services.test_ephemeral_service import SPEC, create

    name = create(rt.env, {**SPEC, "provider": "codex", "effort": "auto"})["name"]
    token = claim(rt.env, name)["claim_id"]
    response = rt.client.post(
        "/sessions/cao-session/terminals",
        params={"provider": "codex", "agent_profile": name, "caller_id": CALLER, "claim_id": token},
        json={"delegation_message": "inspect", "delegation_kind": "handoff"},
    )
    assert response.status_code < 300, response.text
    (args,) = rt.commands
    assert args.count('model_reasoning_effort="high"') == 1
    assert args[args.index('model_reasoning_effort="high"') - 1] == "-c"
    assert not any(arg.startswith("model_reasoning_effort=") and "medium" in arg for arg in args)
    (record,) = rt.store.list()
    assert record["applied_value"] == "high" and record["provider"] == "codex"
    delete_child()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "default"])
@pytest.mark.parametrize("case", [{**AUTO, "effort_answer": "medium"}], indirect=True)
async def test_Q1_an_answer_equal_to_the_stored_effort_rewrites_nothing(rt, path):
    before = live_profile(rt)
    result = await invoke(path, rt.name)
    assert result["success"], result
    assert launched_effort(rt) == "medium"
    (record,) = rt.store.list()
    assert record["outcome"] == "applied" and record["applied_value"] == "medium"
    ((profile, _),) = rt.profiles
    assert len(finalized(rt.name)) == 1 and profile == before
    delete_child()


# AC4: an explicit or omitted effort never reaches a decider, even with effort.route on.


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize(
    "case,effort,model_asked",
    [
        ({"effort": "high"}, "high", False),
        ({"effort": "low", "tier": "small"}, "low", False),
        ({"policy": {"default_effort": "low"}}, "low", False),
        ({}, None, False),
        ({"effort": "high", "tier": "auto"}, "high", True),
        ({"tier": "auto", "policy": {"default_effort": "medium"}}, "medium", True),
    ],
    indirect=["case"],
)
async def test_AC4_explicit_or_omitted_effort_is_never_asked(rt, effort, model_asked, path):
    result = await invoke(path, rt.name)
    assert result["success"], result
    drain(rt)
    assert asked_points(rt) == ([("model.route",)] if model_asked else [])
    assert records(rt, "effort.route") == []
    assert launched_effort(rt) == effort
    delete_child()


# AC5: no request body or query can set the applied effort, at any site.

INJECTED = {
    "effort": "low",
    "launch_plan": {"effort": "low"},
    "claudeConfig": {"effort": "low"},
    "codexConfig": {"model_reasoning_effort": "low"},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("key", sorted(INJECTED))
@pytest.mark.parametrize("where", ["body", "query"])
@pytest.mark.parametrize("site", ["launch", "claim"])
# assign launches through create-terminal, the default handoff through run-step.
@pytest.mark.parametrize("path", ["assign", "default"])
@pytest.mark.parametrize(
    "case,effort",
    [
        ({**AUTO, "state": "off"}, "medium"),
        ({"effort": "medium"}, "medium"),
        ({**AUTO, "effort_answer": "high"}, "high"),
    ],
    indirect=["case"],
)
async def test_AC5_effort_is_not_a_caller_input(rt, monkeypatch, path, site, where, key, effort):
    original = orchestration.requests.post
    suffixes = ("/claim",) if site == "claim" else ("/terminals", "/run-step")
    value = INJECTED[key]

    def inject(url, **kw):
        if url.endswith(suffixes):
            if where == "body":
                kw["json"] = {**(kw.get("json") or {}), key: value}
            else:
                text = value if isinstance(value, str) else json.dumps(value)
                kw["params"] = {**(kw.get("params") or {}), key: text}
        return original(url, **kw)

    monkeypatch.setattr(orchestration.requests, "post", inject)
    result = await invoke(path, rt.name)
    if site == "claim" and where == "body":
        # The claim body is strict: an extra field refuses the launch outright.
        assert not result["success"] and result["rule"] == "invalid_request"
        assert rt.commands == [] and rt.decider.requests == []
        return
    assert result["success"], result
    assert launched_effort(rt) == effort
    delete_child()


# An unusable answer, a timeout or a decider error launches the stored effort.


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "early", "default"])
@pytest.mark.parametrize(
    "case,reason",
    [
        ({**AUTO, "effort_answer": "unsure"}, "unsure"),
        ({**AUTO, "confidence": 0.2}, "low_confidence"),
        ({**AUTO, "effort_answer": "huge"}, "invalid_answer"),
        ({**AUTO, "failure": "no_answer"}, "no_answer"),
        ({**AUTO, "failure": "error"}, "error"),
        ({**AUTO, "failure": "timeout"}, "timeout"),
    ],
    indirect=["case"],
)
async def test_on_falls_back_to_the_stored_effort_and_records_why(rt, reason, path):
    result = await invoke(path, rt.name)
    assert result["success"], result
    assert launched_effort(rt) == "medium"
    (record,) = rt.store.list()
    assert record["outcome"] == "fallback" and record["reason"] == reason
    assert record["fallback_value"] == "medium" and record["fallback_source"] == "policy"
    assert record["applied_value"] is None and record["launch_status"] == "launched"
    assert len(finalized(rt.name)) == 1
    delete_child()


# Q3: both fields auto ask twice, one point per call, model first, with one request id.


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "early", "default"])
@pytest.mark.parametrize(
    "case,model,effort",
    [
        ({**AUTO, "tier": "auto"}, "model-large", "high"),
        ({**AUTO, "tier": "auto", "failures": {"model.route": "timeout"}}, "model-medium", "high"),
        (
            {**AUTO, "tier": "auto", "failures": {"effort.route": "timeout"}},
            "model-large",
            "medium",
        ),
    ],
    indirect=["case"],
)
async def test_Q3_both_fields_are_asked_in_turn(rt, path, model, effort):
    result = await invoke(path, rt.name)
    assert result["success"], result
    assert asked_points(rt) == [("model.route",), ("effort.route",)]
    first, second = rt.decider.requests
    assert first.request_id == second.request_id
    assert (launched_model(rt), launched_effort(rt)) == (model, effort)
    model_record, effort_record = records(rt)
    assert (model_record["point"], effort_record["point"]) == ("model.route", "effort.route")
    # Each record carries the launch's model facts.
    assert model_record["launched_model"] == effort_record["launched_model"] == model
    ids = [model_record["id"], effort_record["id"]]
    assert [e["record_ids"] for e in decided_events(rt.name)] == [ids]
    delete_child()


# A policy the operator changes while the decision runs applies to the effort too.


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "early", "default"])
@pytest.mark.parametrize(
    "case,change,rule,outcome",
    [
        (
            {**AUTO, "effort_answer": "high"},
            {"max_effort": "medium"},
            "policy_changed_since_create:effort_exceeds_policy",
            "applied",
        ),
        (
            {**AUTO, "effort_answer": "low"},
            {"min_effort": "medium"},
            "policy_changed_since_create:effort_below_policy",
            "applied",
        ),
        # A launch that keeps its stored effort is rechecked too: it is now too high.
        (
            {**AUTO, "failure": "timeout"},
            {"max_effort": "low", "default_effort": "low"},
            "policy_changed_since_create:effort_exceeds_policy",
            "fallback",
        ),
        (
            {**AUTO, "effort_answer": "unsure"},
            {"max_effort": "low", "default_effort": "low"},
            "policy_changed_since_create:effort_exceeds_policy",
            "fallback",
        ),
        (
            {**AUTO, "effort_answer": "medium"},
            {"max_effort": "low", "default_effort": "low"},
            "policy_changed_since_create:effort_exceeds_policy",
            "applied",
        ),
    ],
    indirect=["case"],
)
async def test_a_policy_changed_during_the_decision_bounds_the_effort(
    rt, path, change, rule, outcome
):
    rt.decider.during = lambda: operator_edit(rt, change)
    result = await invoke(path, rt.name)
    assert not result["success"] and result["rule"] == rule
    rt.backend.create_window.assert_not_called()
    rt.factory.assert_not_called()
    (record,) = rt.store.list()
    assert record["outcome"] == outcome and record["launch_status"] == "not_launched"
    row = database.get_ephemeral_agent(rt.name)
    assert row["state"] == "gc" and row["gc_reason"] == "policy_changed"
    assert decided_events(rt.name) == [] and len(finalized(rt.name)) == 1
    assert not (rt.env[2] / "ephemeral/live" / (rt.name + ".md")).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "default"])
@pytest.mark.parametrize(
    "case,change,effort",
    [
        ({**AUTO, "effort_answer": "high"}, {"min_effort": "medium"}, "high"),
        ({**AUTO, "effort_answer": "high"}, {"default_effort": "high"}, "high"),
        ({**AUTO, "failure": "timeout"}, {"max_effort": "medium"}, "medium"),
    ],
    indirect=["case"],
)
async def test_a_change_that_keeps_the_effort_in_bounds_launches(rt, path, change, effort):
    rt.decider.during = lambda: operator_edit(rt, change)
    result = await invoke(path, rt.name)
    assert result["success"], result
    assert launched_effort(rt) == effort
    (record,) = rt.store.list()
    assert record["launch_status"] == "launched"
    assert database.get_ephemeral_agent(rt.name)["effort"] == "medium"
    delete_child()


# The answer and the recheck never depend on a written record.


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "default"])
@pytest.mark.parametrize("case", [{**AUTO, "failure": "timeout"}], indirect=True)
async def test_an_effort_only_launch_with_no_plan_is_still_rechecked(rt, path, monkeypatch):
    # No record, no applied effort and no tier: the engine returns no plan at all.
    monkeypatch.setattr(rt.store, "insert", lambda *a, **kw: None)
    rt.decider.during = lambda: operator_edit(rt, {"max_effort": "low", "default_effort": "low"})
    result = await invoke(path, rt.name)
    assert not result["success"]
    assert result["rule"] == "policy_changed_since_create:effort_exceeds_policy"
    rt.factory.assert_not_called()
    row = database.get_ephemeral_agent(rt.name)
    assert row["state"] == "gc" and row["gc_reason"] == "policy_changed"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "default"])
@pytest.mark.parametrize("case", [{**AUTO, "effort_answer": "high"}], indirect=True)
async def test_an_applied_effort_launches_when_its_record_cannot_be_written(rt, path, monkeypatch):
    monkeypatch.setattr(rt.store, "insert", lambda *a, **kw: None)
    result = await invoke(path, rt.name)
    assert result["success"], result
    assert launched_effort(rt) == "high"
    assert rt.store.list() == [] and decided_events(rt.name) == []
    # Nothing is written for the decision, record or no record: one finalized event, the
    # claim's own, and the live profile keeps the stored effort (#1068 C, #1083).
    (event,) = finalized(rt.name)
    assert event["effort"] == "medium"
    delete_child()


# A block broken while the decider runs refuses before the bind the same way for an
# effort-only launch: the recheck validates the whole snapshot before it checks the
# applied effort, so a malformed block never reaches a raw exception (#1068's fix, #1083).

EFFORT_BROKEN_BLOCKS = [*BROKEN_BLOCKS, ({"min_effort": "bogus"}, "min_effort")]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "default"])
@pytest.mark.parametrize("change,key", EFFORT_BROKEN_BLOCKS)
@pytest.mark.parametrize("case", [{**AUTO, "effort_answer": "high"}], indirect=True)
async def test_a_block_broken_during_an_unrecorded_effort_decision_returns_the_name(
    rt, path, monkeypatch, change, key
):
    monkeypatch.setattr(rt.store, "insert", lambda *a, **kw: None)
    rt.decider.during = lambda: operator_edit(rt, change)
    result = await invoke(path, rt.name)
    assert not result["success"] and result["rule"] == "policy_config_error:" + key
    drain(rt)
    assert rt.store.list() == []
    assert_returned_to_pending(rt)


# The remap rule: an effort-only launch runs its tier's current model id.


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "default"])
@pytest.mark.parametrize(
    "case",
    [{**AUTO, "tier": "small"}, {**AUTO, "tier": "small", "state": "off"}],
    indirect=True,
)
async def test_an_effort_only_launch_runs_the_current_id_of_its_tier(rt, path, monkeypatch):
    from cli_agent_orchestrator.api import main

    remap = {"model_tiers": {"claude_code": {**TIERS["claude_code"], "small": "model-next"}}}
    original = main.prepare_launch

    async def remapped(req):
        # The operator remaps the tier after the claim's finalize, before the recheck.
        plan = await original(req)
        operator_edit(rt, remap)
        return plan

    monkeypatch.setattr(main, "prepare_launch", remapped)
    result = await invoke(path, rt.name)
    assert result["success"], result
    assert launched_model(rt) == "model-next"
    delete_child()


# A decision that outlives its claim neither finalizes nor binds.


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "early", "default"])
@pytest.mark.parametrize("case", [{**AUTO, "effort_answer": "high"}], indirect=True)
async def test_a_decision_past_the_lease_neither_finalizes_nor_binds(rt, monkeypatch, path):
    def lapse():
        monkeypatch.setattr(database, "_utcnow", lambda: NOW + timedelta(seconds=61))

    rt.decider.during = lapse
    result = await invoke(path, rt.name)
    assert not result["success"] and result["rule"] == "claim_expired"
    rt.factory.assert_not_called()
    assert database.get_terminal_metadata(CHILD) is None
    assert len(finalized(rt.name)) == 1
    assert frontmatter.loads(live_profile(rt).decode())["claudeConfig"] == {"effort": "medium"}


@pytest.mark.parametrize("site", ["terminal", "step"])
@pytest.mark.parametrize("case", [{**AUTO, "effort_answer": "high"}], indirect=True)
def test_a_replaced_claim_keeps_its_own_profile_and_archive(rt, monkeypatch, site):
    token = claim(rt.env, rt.name)["claim_id"]
    after_reclaim = {}

    def reclaim():
        # During claim A's decision its lease lapses and the owner claims again as B.
        if not after_reclaim:
            monkeypatch.setattr(database, "_utcnow", lambda: NOW + timedelta(seconds=61))
            after_reclaim["token"] = claim(rt.env, rt.name)["claim_id"]
            after_reclaim["profile"] = live_profile(rt)
            after_reclaim["events"] = audit(rt.name)["events"]

    rt.decider.during = reclaim
    response = raw_launch(rt, site, token)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["rule"] == "already_claimed"
    row = database.get_ephemeral_agent(rt.name)
    assert row["state"] == "claimed" and row["claim_id"] == after_reclaim["token"] != token
    assert live_profile(rt) == after_reclaim["profile"]
    events = audit(rt.name)["events"]
    assert [e for e in events if e["event"] == "finalized"] == [
        e for e in after_reclaim["events"] if e["event"] == "finalized"
    ]
    rt.factory.assert_not_called()


# Under C, recheck_decided writes nothing, so a claim replaced right after it returns hits
# the bind's own existing fence exactly as test_a_replaced_claim_keeps_its_own_profile_and_archive
# does: there is no longer a separate "second finalize" race to pin here.


# A keyed retry of a launched name reattaches without a second decision or finalize.


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [{**AUTO, "effort_answer": "high"}], indirect=True)
async def test_a_keyed_early_retry_reattaches_without_deciding(rt):
    async def early():
        return (
            await orchestration._handoff_impl(
                rt.name,
                "inspect",
                working_directory="/workspace",
                wait=False,
                on_terminal_id=lambda _: None,
                idempotency_key="effort-retry",
            )
        ).model_dump()

    first, second = await early(), await early()
    assert first["success"] and second["success"]
    assert first["terminal_id"] == second["terminal_id"]
    drain(rt)
    assert len(rt.decider.requests) == 1 and len(rt.store.list()) == 1
    # Nothing is written for the decision (#1068 C, #1083): one finalized event either way.
    assert len(finalized(rt.name)) == 1 and rt.backend.create_window.call_count == 1
    assert launched_effort(rt) == "high"
    delete_child()


@pytest.mark.parametrize("case", [{**AUTO, "effort_answer": "high"}], indirect=True)
def test_a_raw_keyed_create_replays_without_deciding_or_finalizing(rt):
    token = claim(rt.env, rt.name, idempotency_key="raw-retry")["claim_id"]
    extra = {"idempotency_key": "raw-retry", "defer_init": "true"}
    first = raw_launch(rt, "terminal", token, **extra)
    assert first.status_code == 201, first.text
    second = raw_launch(rt, "terminal", token, **extra)
    assert second.status_code == 201, second.text
    assert second.json()["id"] == first.json()["id"]
    drain(rt)
    assert len(rt.decider.requests) == 1 and len(rt.store.list()) == 1
    # Nothing is written for the decision (#1068 C, #1083): one finalized event either way.
    assert len(finalized(rt.name)) == 1 and rt.backend.create_window.call_count == 1
    delete_child()


# A launch without a claim identifier asks nothing and runs the live profile.


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [{**AUTO, "effort_answer": "high"}], indirect=True)
async def test_a_claimless_launch_of_an_auto_effort_asks_nothing(rt):
    from cli_agent_orchestrator.services import terminal_service

    with record_decision_calls() as calls:
        terminal = await terminal_service.create_terminal(
            "claude_code", rt.name, session_name="cao-session", caller_id=CALLER
        )
    assert terminal.id == CHILD
    assert calls == [] and rt.decider.requests == [] and rt.store.list() == []
    assert launched_effort(rt) == "medium"
    delete_child()


@pytest.mark.parametrize("case", [{**AUTO, "effort_answer": "high"}], indirect=True)
def test_a_claimless_delegation_through_the_route_asks_nothing(rt):
    response = rt.client.post(
        "/sessions/cao-session/terminals",
        params={"provider": "claude_code", "agent_profile": rt.name, "caller_id": CALLER},
        json={"delegation_message": "inspect", "delegation_kind": "handoff"},
    )
    assert response.status_code < 300, response.text
    drain(rt)
    assert rt.decider.requests == [] and rt.store.list() == []
    # No claim, so no finalize: the live file runs as it stands, on the stored effort.
    assert launched_effort(rt) == "medium" and finalized(rt.name) == []
    delete_child()


# The creator's note stays model-only; an effort-only launch gets none.


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "default"])
@pytest.mark.parametrize("case", [{**AUTO, "policy": {"max_effort": "medium"}}], indirect=True)
async def test_an_effort_only_launch_gets_no_note(rt, path):
    result = await invoke(path, rt.name)
    assert result["success"], result
    assert "auto:" not in result["message"]
    (record,) = rt.store.list()
    assert record["outcome"] == "capped"
    delete_child()
