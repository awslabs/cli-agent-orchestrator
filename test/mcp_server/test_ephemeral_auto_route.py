"""#1068: model.route for a spec that declared model_tier auto, through the real routes.

The claim, bind and provider construction are real; only the backend and the provider
process are mocked, as in test_ephemeral_launch_handlers.
"""

import asyncio
import logging
from dataclasses import replace
from datetime import timedelta
from test.mcp_server.test_ephemeral_launch_handlers import (  # noqa: F401
    CHILD,
    _create_store,
    invoke,
    launch_http,
    record_decision_calls,
    release_runtime,
    runtime_store,
)
from test.services.test_ephemeral_claim import NOW, audit, claim
from test.services.test_ephemeral_service import CALLER, SPEC, create
from types import SimpleNamespace

import pytest
from anyio.from_thread import start_blocking_portal
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.decisions.engine import DecisionEngine, DelegationRequest
from cli_agent_orchestrator.decisions.policy import DecisionInputError
from cli_agent_orchestrator.decisions.registry import DeciderRegistry
from cli_agent_orchestrator.decisions.settings import DecisionSettings, PointSettings
from cli_agent_orchestrator.decisions.shadow import ShadowRunner
from cli_agent_orchestrator.decisions.store import DecisionStore
from cli_agent_orchestrator.decisions.targets import TargetKind
from cli_agent_orchestrator.decisions.types import DecisionAnswer, PointState
from cli_agent_orchestrator.services.secret_gate import redact_secrets
from cli_agent_orchestrator.utils import ephemeral_capabilities as caps
from cli_agent_orchestrator.utils import orchestration

TIERS = {"claude_code": {t: "model-" + t for t in ("small", "medium", "large")}}
BOUNDS = {"min_tier": "small", "default_tier": "medium", "max_tier": "large"}
DESCRIPTION = "Reviewer of synthetic logs."
BRIEF = "CREATOR_BRIEF_IS_NEVER_SENT"
PATHS = ["assign", "early", "default", "nowait"]


@pytest.fixture
def case(request):
    return getattr(request, "param", {})


@pytest.fixture
def create_store(_create_store, case):
    config = _create_store[4]
    config["model_tiers"] = case.get("tiers", TIERS)
    config["ephemeral"].update(BOUNDS if case.get("tier") == "auto" else {})
    config["ephemeral"].update(case.get("policy", {}))
    return _create_store


@pytest.fixture
def claimed_store(create_store, case, monkeypatch):
    monkeypatch.setattr(database, "_utcnow", lambda: NOW)
    spec = {**SPEC, "description": DESCRIPTION, "brief": BRIEF}
    if case.get("tier"):
        spec["model_tier"] = case["tier"]
    name = create(create_store, spec)["name"]
    tokens = iter(range(1, 100))
    monkeypatch.setattr(create_store[0].secrets, "token_hex", lambda n: f"{next(tokens):0{n * 2}x}")
    return create_store, name


class Decider:
    name = "e5_test"
    version = "1"
    points = frozenset({"model.route"})

    def __init__(self, answer, confidence, failure):
        self.answer, self.confidence, self.failure = answer, confidence, failure
        self.requests = []
        self.during = None
        self.gate = None

    async def decide(self, request, config):
        self.requests.append(request)
        if self.during is not None:
            self.during()
        if self.gate is not None:
            await self.gate.wait()
        if self.failure == "error":
            raise ValueError("synthetic decider failure")
        if self.failure == "timeout":
            await asyncio.sleep(1)
        if self.failure == "no_answer":
            return None
        return {"model.route": DecisionAnswer(self.answer, {self.answer: self.confidence})}


@pytest.fixture
def e5(launch_http, case, monkeypatch, tmp_path):
    from cli_agent_orchestrator.api import main
    from cli_agent_orchestrator.services import terminal_service

    # Bind and provider construction stay real; the fake provider's deferred start is not.
    monkeypatch.setattr(terminal_service, "_schedule_deferred_init", lambda *a, **kw: None)
    env, name, backend, factory = launch_http[0]
    decider = Decider(
        case.get("answer", "large"), case.get("confidence", 0.91), case.get("failure")
    )
    # A file of its own, so each thread gets its own connection. On the shared in-memory
    # StaticPool connection, another thread's session close rolls back the shadow write
    # before it commits, and nothing writes it again.
    decisions_db = create_engine(
        "sqlite:///" + str(tmp_path / "decisions.db"),
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    database.DecisionRecordModel.__table__.create(decisions_db)
    store = DecisionStore(
        sessionmaker(bind=decisions_db), env[2] / "decision-hash.key", emit=lambda _: None
    )
    runner = ShadowRunner(store)
    settings = DecisionSettings(
        points={"model.route": PointSettings(PointState(case.get("state", "on")), decider.name)},
        model_tiers=case.get("tiers", TIERS),
        on_timeout_ms=50 if case.get("failure") == "timeout" else 1000,
    )
    engine = DecisionEngine(
        DeciderRegistry({decider.name: lambda: decider}),
        store,
        runner,
        settings_loader=lambda: settings,
        honors_model=lambda *a: True,
    )
    monkeypatch.setattr(main.app.state, "decision_engine", engine, raising=False)
    # A persistent portal keeps shadow work on the request loop without app lifespan.
    with start_blocking_portal() as portal:
        launch_http[1].portal = portal
        yield SimpleNamespace(
            env=env,
            name=name,
            backend=backend,
            factory=factory,
            decider=decider,
            store=store,
            runner=runner,
            portal=portal,
            engine=engine,
            calls=launch_http[2],
            client=launch_http[1],
        )
        portal.call(runner.drain)
        portal.call(runner.close)
        launch_http[1].portal = None
    decisions_db.dispose()


def drain(rt):
    rt.portal.call(rt.runner.drain)


def launched_model(rt):
    (model,) = [c.kwargs["model"] for c in rt.factory.call_args_list if "model" in c.kwargs]
    return model


def decided_events(name):
    return [e for e in audit(name)["events"] if e["event"] == "decided"]


def delete_child():
    if database.get_terminal_metadata(CHILD) is not None:
        assert orchestration._delete_terminal_impl(CHILD)["success"]


def raw_launch(rt, site, token, caller=CALLER, **extra):
    if site == "terminal":
        return rt.client.post(
            "/sessions/cao-session/terminals",
            params={
                "provider": "claude_code",
                "agent_profile": rt.name,
                "caller_id": caller,
                "claim_id": token,
                **extra,
            },
            json={"delegation_message": "inspect", "delegation_kind": "handoff"},
        )
    return rt.client.post(
        "/terminals/run-step",
        json={
            "provider": "claude_code",
            "agent": rt.name,
            "prompt": "inspect",
            "caller_id": caller,
            "claim_id": token,
            "orchestration_type": "handoff",
            "delegation_message": "inspect",
            "teardown": False,
            **extra,
        },
    )


# AC1, AC10: off, and a halted platform, ask nothing and run the default tier.


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("case", [{"tier": "auto", "state": "off"}], indirect=True)
async def test_AC1_off_asks_nothing_and_runs_the_default_tier(e5, path):
    result = await invoke(path, e5.name)
    assert result["success"], result
    drain(e5)
    assert e5.decider.requests == [] and e5.store.list() == []
    assert launched_model(e5) == "model-medium"
    assert decided_events(e5.name) == []
    delete_child()


@pytest.mark.asyncio
@pytest.mark.parametrize("halted", [True, False])
@pytest.mark.parametrize("path", ["assign", "default"])
@pytest.mark.parametrize("case", [{"tier": "auto"}], indirect=True)
async def test_AC10_decisions_off_sends_nothing(e5, monkeypatch, path, halted):
    from cli_agent_orchestrator.decisions import egress
    from cli_agent_orchestrator.decisions.settings import load_settings

    e5.env[4]["decisions"] = {"points": {"model.route": {"state": "on", "decider": "e5_test"}}}
    monkeypatch.setattr(egress, "is_halted", lambda: halted)
    e5.engine.settings_loader = lambda: load_settings(environment={})
    result = await invoke(path, e5.name)
    assert result["success"], result
    drain(e5)
    # The control (not halted) proves the same settings do reach the decider.
    assert len(e5.decider.requests) == (0 if halted else 1)
    assert len(e5.store.list()) == (0 if halted else 1)
    assert launched_model(e5) == ("model-medium" if halted else "model-large")
    delete_child()


# AC2: shadow records the answer once, launches the fallback and never waits for it.


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("case", [{"tier": "auto", "state": "shadow"}], indirect=True)
async def test_AC2_shadow_records_once_and_never_applies(e5, path):
    gate = e5.portal.call(asyncio.Event)
    e5.decider.gate = gate
    result = await invoke(path, e5.name)
    assert result["success"], result
    # The launch returned on the fallback while the decider was still blocked.
    assert launched_model(e5) == "model-medium"
    (pending,) = e5.store.list()
    assert pending["decision_status"] == "pending" and pending["answer"] is None
    e5.portal.call(gate.set)
    drain(e5)
    assert len(e5.decider.requests) == 1
    (record,) = e5.store.list()
    assert record["agent_profile"] == e5.name and record["state"] == "shadow"
    assert record["outcome"] == "shadow" and record["answer"] == "large"
    assert record["applied_value"] is None and record["candidate_value"] == "large"
    assert record["launched_model"] == "model-medium"
    assert [e["record_ids"] for e in decided_events(e5.name)] == [[record["id"]]]
    delete_child()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("case", [{"tier": "auto", "state": "shadow"}], indirect=True)
async def test_AC2_shadow_never_applies_an_answer_that_is_already_ready(e5, path):
    # No gate: the decider answers at once, so "apply the shadow answer if ready" would show.
    result = await invoke(path, e5.name)
    assert result["success"], result
    assert launched_model(e5) == "model-medium"
    drain(e5)
    (record,) = e5.store.list()
    assert record["outcome"] == "shadow" and record["answer"] == "large"
    assert record["applied_value"] is None and record["launched_model"] == "model-medium"
    delete_child()


# AC3: on applies a confident answer, capped into [min_tier, max_tier].


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize(
    "case,applied",
    [
        ({"tier": "auto", "answer": "large"}, "large"),
        ({"tier": "auto", "answer": "small"}, "small"),
        ({"tier": "auto", "answer": "large", "policy": {"max_tier": "medium"}}, "medium"),
        (
            {"tier": "auto", "answer": "small", "policy": {"min_tier": "medium"}},
            "medium",
        ),
    ],
    indirect=["case"],
)
async def test_AC3_on_applies_the_answer_capped_into_bounds(e5, case, applied, path):
    result = await invoke(path, e5.name)
    assert result["success"], result
    assert launched_model(e5) == "model-" + applied
    (record,) = e5.store.list()
    assert record["agent_profile"] == e5.name
    assert record["outcome"] == ("applied" if case["answer"] == applied else "capped")
    assert record["answer"] == case["answer"] and record["applied_value"] == applied
    assert record["launched_model"] == "model-" + applied and record["launch_status"] == "launched"
    assert [e["record_ids"] for e in decided_events(e5.name)] == [[record["id"]]]
    # The decider saw the redacted description and purpose, never the brief or a profile.
    (request,) = e5.decider.requests
    assert request.facts.profile is None and request.purpose == SPEC["purpose"]
    assert request.profile_description == redact_secrets(DESCRIPTION)[0]
    assert BRIEF not in repr(request)
    # The stored profile and row keep the fallback; the model applies through the plan.
    assert database.get_ephemeral_agent(e5.name)["model_tier"] == "medium"
    delete_child()


# AC4: an unusable answer, a timeout or a decider error falls back without failing.


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "early", "default"])
@pytest.mark.parametrize(
    "case,reason",
    [
        ({"tier": "auto", "answer": "unsure"}, "unsure"),
        ({"tier": "auto", "confidence": 0.2}, "low_confidence"),
        ({"tier": "auto", "answer": "huge"}, "invalid_answer"),
        ({"tier": "auto", "failure": "no_answer"}, "no_answer"),
        ({"tier": "auto", "failure": "error"}, "error"),
        ({"tier": "auto", "failure": "timeout"}, "timeout"),
    ],
    indirect=["case"],
)
async def test_AC4_on_falls_back_and_records_why(e5, reason, path):
    result = await invoke(path, e5.name)
    assert result["success"], result
    assert launched_model(e5) == "model-medium"
    (record,) = e5.store.list()
    assert record["outcome"] == "fallback" and record["reason"] == reason
    assert record["fallback_value"] == "medium" and record["fallback_source"] == "policy"
    assert record["launch_status"] == "launched"
    delete_child()


# AC5: an explicit or omitted tier never reaches a decider, even with model.route on.


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize(
    "case,model",
    [
        ({"tier": "large"}, "model-large"),
        ({"tier": "small", "policy": BOUNDS}, "model-small"),
        ({"policy": {"default_tier": "small"}}, "model-small"),
        ({"policy": BOUNDS}, "model-medium"),
        ({}, None),
    ],
    indirect=["case"],
)
async def test_AC5_explicit_or_omitted_tier_asks_nothing(e5, model, path):
    with record_decision_calls() as calls:
        result = await invoke(path, e5.name)
    assert result["success"], result
    drain(e5)
    assert calls == [] and e5.decider.requests == [] and e5.store.list() == []
    assert launched_model(e5) == model
    assert decided_events(e5.name) == []
    delete_child()


# AC6: a per-call model refuses before the claim.


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("case", [{"tier": "auto"}], indirect=True)
async def test_AC6_per_call_model_refuses_before_the_claim(e5, path):
    result = await invoke(path, e5.name, model="caller-model")
    assert result["rule"] == "model_override_not_allowed"
    assert e5.calls == []
    assert database.get_ephemeral_agent(e5.name)["state"] == "pending"
    assert e5.decider.requests == [] and e5.store.list() == []


@pytest.mark.parametrize("site", ["terminal", "step"])
@pytest.mark.parametrize("case", [{"tier": "auto"}], indirect=True)
def test_AC6_raw_per_call_model_after_a_claim_asks_nothing(e5, site):
    token = claim(e5.env, e5.name)["claim_id"]
    response = raw_launch(e5, site, token, model="caller-model")
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["rule"] == "model_override_not_allowed"
    assert e5.decider.requests == [] and e5.store.list() == []
    row = database.get_ephemeral_agent(e5.name)
    assert row["state"] == "pending" and row["claim_id"] is None


# AC7: a bound removed after create refuses the claim and returns the name to pending.


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", ["min_tier", "max_tier"])
@pytest.mark.parametrize("path", ["assign", "early", "default"])
@pytest.mark.parametrize("case", [{"tier": "auto"}], indirect=True)
async def test_AC7_bound_removed_after_create_returns_the_name(e5, path, bound):
    e5.env[4]["ephemeral"][bound] = None
    result = await invoke(path, e5.name)
    assert not result["success"] and result["rule"] == "policy_config_error:" + bound
    assert "ephemeral." + bound in result["message"]
    row = database.get_ephemeral_agent(e5.name)
    assert row["state"] == "pending" and row["claim_id"] is None
    assert e5.decider.requests == [] and e5.store.list() == []
    e5.backend.create_window.assert_not_called()


# AC8: a keyed retry of a launched name reattaches without a second decision.


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [{"tier": "auto"}], indirect=True)
async def test_AC8_keyed_early_retry_reattaches_without_deciding(e5):
    async def early():
        return (
            await orchestration._handoff_impl(
                e5.name,
                "inspect",
                working_directory="/workspace",
                wait=False,
                on_terminal_id=lambda _: None,
                idempotency_key="e5-retry",
            )
        ).model_dump()

    first, second = await early(), await early()
    assert first["success"] and second["success"]
    assert first["terminal_id"] == second["terminal_id"]
    drain(e5)
    assert len(e5.decider.requests) == 1 and len(e5.store.list()) == 1
    assert e5.backend.create_window.call_count == 1
    delete_child()


@pytest.mark.parametrize("case", [{"tier": "auto"}], indirect=True)
def test_AC8_raw_keyed_create_replays_without_deciding(e5):
    token = claim(e5.env, e5.name, idempotency_key="raw-retry")["claim_id"]
    extra = {"idempotency_key": "raw-retry", "defer_init": "true"}
    first = raw_launch(e5, "terminal", token, **extra)
    assert first.status_code == 201, first.text
    second = raw_launch(e5, "terminal", token, **extra)
    assert second.status_code == 201, second.text
    assert second.json()["id"] == first.json()["id"]
    drain(e5)
    assert len(e5.decider.requests) == 1 and len(e5.store.list()) == 1
    assert e5.backend.create_window.call_count == 1
    delete_child()


# AC9: a decision that outlives the claim lease cannot bind.


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "early", "default"])
@pytest.mark.parametrize("case", [{"tier": "auto"}], indirect=True)
async def test_AC9_a_decision_past_the_lease_cannot_bind(e5, monkeypatch, path):
    def lapse():
        monkeypatch.setattr(database, "_utcnow", lambda: NOW + timedelta(seconds=61))

    e5.decider.during = lapse
    result = await invoke(path, e5.name)
    assert not result["success"] and result["rule"] == "claim_expired"
    e5.backend.create_window.assert_not_called()
    e5.factory.assert_not_called()
    assert database.get_terminal_metadata(CHILD) is None
    (record,) = e5.store.list()
    # The bind refused inside the launch, so the record is a failed, unbound launch.
    assert record["launch_status"] == "launch_failed" and record["terminal_id"] is None
    assert database.get_ephemeral_agent(e5.name)["state"] == "pending"


@pytest.mark.parametrize("site", ["terminal", "step"])
@pytest.mark.parametrize("case", [{"tier": "auto"}], indirect=True)
def test_AC9_a_lapsed_claims_decision_is_never_archived_for_the_next_claim(e5, monkeypatch, site):
    token = claim(e5.env, e5.name)["claim_id"]
    reclaimed = []

    def reclaim():
        # During claim A's decision its lease lapses and the owner claims again as B.
        if not reclaimed:
            monkeypatch.setattr(database, "_utcnow", lambda: NOW + timedelta(seconds=61))
            reclaimed.append(claim(e5.env, e5.name)["claim_id"])

    e5.decider.during = reclaim
    response = raw_launch(e5, site, token)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["rule"] == "already_claimed"
    row = database.get_ephemeral_agent(e5.name)
    assert row["state"] == "claimed" and row["claim_id"] == reclaimed[0] != token
    assert decided_events(e5.name) == []
    e5.factory.assert_not_called()
    # B's own launch (through the terminal route) then records exactly B's decision.
    response = raw_launch(e5, "terminal", reclaimed[0])
    assert response.status_code < 300, response.text
    _, own = sorted(record["id"] for record in e5.store.list())
    assert [e["record_ids"] for e in decided_events(e5.name)] == [[own]]
    delete_child()


# The policy the operator changes while a decision runs applies before the bind.

ONLY_SMALL = {"min_tier": "small", "default_tier": "small", "max_tier": "small"}


def operator_edit(rt, change):
    """Replace the table the way a settings save does, then apply the block keys."""
    tiers = change.get("model_tiers")
    if tiers is not None:
        rt.env[4]["model_tiers"] = tiers
    rt.env[4]["ephemeral"].update({k: v for k, v in change.items() if k != "model_tiers"})


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "early", "default"])
@pytest.mark.parametrize(
    "case,change,rule,state,outcome",
    [
        (
            {"tier": "auto", "answer": "large"},
            {"max_tier": "medium", "default_tier": "medium"},
            "policy_changed_since_create:tier_exceeds_policy",
            "gc",
            "applied",
        ),
        (
            {"tier": "auto", "answer": "small"},
            {"min_tier": "medium"},
            "policy_changed_since_create:tier_below_policy",
            "gc",
            "applied",
        ),
        (
            {"tier": "auto", "answer": "large"},
            {"min_tier": None},
            "policy_config_error:min_tier",
            "pending",
            "applied",
        ),
        # A launch that keeps its fallback is rechecked too: the stored tier is now too high.
        (
            {"tier": "auto", "failure": "timeout"},
            ONLY_SMALL,
            "policy_changed_since_create:tier_exceeds_policy",
            "gc",
            "fallback",
        ),
        (
            {"tier": "auto", "answer": "unsure"},
            ONLY_SMALL,
            "policy_changed_since_create:tier_exceeds_policy",
            "gc",
            "fallback",
        ),
        (
            {"tier": "auto", "answer": "medium"},
            ONLY_SMALL,
            "policy_changed_since_create:tier_exceeds_policy",
            "gc",
            "applied",
        ),
    ],
    indirect=["case"],
)
async def test_a_policy_changed_during_the_decision_applies_before_the_bind(
    e5, path, change, rule, state, outcome
):
    e5.decider.during = lambda: operator_edit(e5, change)
    result = await invoke(path, e5.name)
    assert not result["success"] and result["rule"] == rule
    e5.backend.create_window.assert_not_called()
    e5.factory.assert_not_called()
    (record,) = e5.store.list()
    assert record["outcome"] == outcome and record["launch_status"] == "not_launched"
    row = database.get_ephemeral_agent(e5.name)
    assert row["state"] == state
    if state == "gc":
        assert row["gc_reason"] == "policy_changed"
    else:
        assert row["claim_id"] is None
    assert decided_events(e5.name) == []


# A block broken while the decider runs refuses as at claim, even when the launch has no
# applied record and its tier must be recovered from the plan's model.

BROKEN_BLOCKS = [
    ({"min_tier": "bogus"}, "min_tier"),
    ({"min_tier": 3}, "min_tier"),
    ({"max_tier": "bogus"}, "max_tier"),
    ({"default_tier": "bogus"}, "default_tier"),
    ({"allowed_providers": 5}, "allowed_providers"),
    ({"allowed_providers": None}, "allowed_providers"),
    ({"allowed_providers": "claude_code"}, "allowed_providers"),
    ({"max_effort": "bogus"}, "max_effort"),
]


def assert_returned_to_pending(rt):
    row = database.get_ephemeral_agent(rt.name)
    assert row["state"] == "pending" and row["claim_id"] is None
    assert decided_events(rt.name) == []
    rt.backend.create_window.assert_not_called()
    rt.factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "early", "default"])
@pytest.mark.parametrize("change,key", BROKEN_BLOCKS)
@pytest.mark.parametrize(
    "case",
    [{"tier": "auto", "answer": "unsure"}, {"tier": "auto", "failure": "timeout"}],
    indirect=True,
)
async def test_a_block_broken_during_a_fallback_decision_returns_the_name(e5, path, change, key):
    e5.decider.during = lambda: operator_edit(e5, change)
    result = await invoke(path, e5.name)
    assert not result["success"] and result["rule"] == "policy_config_error:" + key
    assert "ephemeral." + key in result["message"]
    (record,) = e5.store.list()
    assert record["outcome"] == "fallback" and record["launch_status"] == "not_launched"
    assert_returned_to_pending(e5)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "default"])
@pytest.mark.parametrize("change,key", BROKEN_BLOCKS)
@pytest.mark.parametrize("case", [{"tier": "auto", "answer": "large"}], indirect=True)
async def test_a_block_broken_during_an_unrecorded_decision_returns_the_name(
    e5, path, monkeypatch, change, key
):
    monkeypatch.setattr(e5.store, "insert", lambda *a, **kw: None)
    e5.decider.during = lambda: operator_edit(e5, change)
    result = await invoke(path, e5.name)
    assert not result["success"] and result["rule"] == "policy_config_error:" + key
    drain(e5)
    assert e5.store.list() == []
    assert_returned_to_pending(e5)


# A record that was written but can't be read back takes the unwritten record's path: the
# recheck reverse-maps the plan's model, and the warning carries no record content.


def make_records_unreadable(rt, monkeypatch):
    def get(record_id):
        raise OperationalError("SELECT decision_records", {"id": record_id}, Exception("locked"))

    monkeypatch.setattr(rt.store, "get", get)


def assert_read_failure_logged_without_content(caplog, record):
    (warning,) = [r for r in caplog.records if "record read failed" in r.getMessage()]
    assert warning.levelno == logging.WARNING and warning.exc_info is None
    text = logging.Formatter().format(warning)
    assert str(record["id"]) not in text
    for value in record.values():
        if isinstance(value, str) and len(value) >= 4:
            assert value not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "default"])
@pytest.mark.parametrize("case", [{"tier": "auto", "answer": "large"}], indirect=True)
async def test_an_applied_answer_launches_when_its_record_cannot_be_read(
    e5, path, monkeypatch, caplog
):
    make_records_unreadable(e5, monkeypatch)
    with caplog.at_level(logging.WARNING):
        result = await invoke(path, e5.name)
    assert result["success"], result
    assert launched_model(e5) == "model-large"
    drain(e5)
    (record,) = e5.store.list()
    assert record["outcome"] == "applied" and record["launch_status"] == "launched"
    assert_read_failure_logged_without_content(caplog, record)
    delete_child()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "default"])
@pytest.mark.parametrize("case", [{"tier": "auto", "answer": "large"}], indirect=True)
async def test_an_unreadable_record_still_rechecks_the_launched_tier(e5, path, monkeypatch, caplog):
    make_records_unreadable(e5, monkeypatch)
    e5.decider.during = lambda: operator_edit(e5, {"max_tier": "medium", "default_tier": "medium"})
    with caplog.at_level(logging.WARNING):
        result = await invoke(path, e5.name)
    assert not result["success"]
    assert result["rule"] == "policy_changed_since_create:tier_exceeds_policy"
    e5.factory.assert_not_called()
    row = database.get_ephemeral_agent(e5.name)
    assert row["state"] == "gc" and row["gc_reason"] == "policy_changed"
    (record,) = e5.store.list()
    assert_read_failure_logged_without_content(caplog, record)


@pytest.mark.parametrize("site", ["terminal", "step"])
@pytest.mark.parametrize("change,key", BROKEN_BLOCKS)
@pytest.mark.parametrize("case", [{"tier": "auto", "answer": "unsure"}], indirect=True)
def test_a_block_broken_during_the_decision_is_a_logged_policy_refusal(
    e5, site, change, key, caplog
):
    rule = "policy_config_error:" + key
    token = claim(e5.env, e5.name)["claim_id"]
    e5.decider.during = lambda: operator_edit(e5, change)
    with caplog.at_level("WARNING"):
        response = raw_launch(e5, site, token)
    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["kind"] == "ephemeral_policy" and detail["rule"] == rule
    assert "ephemeral." + key in detail["message"]
    assert audit(e5.name)["refusals"][rule]["count"] == 1
    assert any("ephemeral refusal rule=" + rule + " " in r.getMessage() for r in caplog.records)
    assert_returned_to_pending(e5)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "default"])
@pytest.mark.parametrize("unrecorded", [False, True])
@pytest.mark.parametrize(
    "case",
    [{"tier": "auto", "answer": "unsure"}, {"tier": "auto", "answer": "large"}],
    indirect=True,
)
async def test_the_bind_recheck_reads_one_settings_snapshot(e5, path, monkeypatch, unrecorded):
    # The tier recovered for an unrecorded launch and the policy it is checked against come
    # from one snapshot, so no second read can disagree with the one the recheck validated.
    service = e5.env[0]
    real, reads = service.read_settings, []

    def read_settings():
        reads.append(1)
        return real()

    monkeypatch.setattr(service, "read_settings", read_settings)
    if unrecorded:
        monkeypatch.setattr(e5.store, "insert", lambda *a, **kw: None)
    # The edit lands after decision_inputs' read, so only the pre-bind reads are counted.
    e5.decider.during = lambda: (reads.clear(), operator_edit(e5, {"min_tier": "bogus"}))
    result = await invoke(path, e5.name)
    assert not result["success"] and result["rule"] == "policy_config_error:min_tier"
    assert len(reads) == 1
    drain(e5)
    assert_returned_to_pending(e5)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "early", "default"])
@pytest.mark.parametrize("case", [{"tier": "auto", "answer": "large"}], indirect=True)
async def test_sweeps_during_the_decision_leave_the_collection_to_the_recheck(e5, path):
    """#1079's periodic sweep never touches the in-lease claim a decision is running for."""
    from cli_agent_orchestrator.services import ephemeral_service

    counts = []

    def during():
        counts.append(ephemeral_service.sweep(startup=False))
        operator_edit(e5, {"max_tier": "medium", "default_tier": "medium"})
        counts.append(ephemeral_service.sweep(startup=False))

    e5.decider.during = during
    result = await invoke(path, e5.name)
    assert not result["success"]
    assert result["rule"] == "policy_changed_since_create:tier_exceeds_policy"
    assert counts == [{}, {}]
    e5.factory.assert_not_called()
    row = database.get_ephemeral_agent(e5.name)
    assert row["state"] == "gc" and row["gc_reason"] == "policy_changed"
    released = [e for e in audit(e5.name)["events"] if e["event"] == "released"]
    assert [e["gc_reason"] for e in released] == ["policy_changed"]
    # Once collected, a later pass finds nothing to collect, retry or release.
    assert ephemeral_service.sweep(startup=False) == {}
    assert [e for e in audit(e5.name)["events"] if e["event"] == "released"] == released


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "default"])
@pytest.mark.parametrize(
    "case,change,tier,model",
    [
        ({"tier": "auto", "answer": "large"}, {"min_tier": "medium"}, "large", "model-large"),
        ({"tier": "auto", "answer": "large"}, {"default_tier": "large"}, "large", "model-large"),
        # A remap tightens nothing: the launched tier runs on its current model id.
        (
            {"tier": "auto", "answer": "large"},
            {"model_tiers": {"claude_code": {**TIERS["claude_code"], "large": "model-next"}}},
            "large",
            "model-next",
        ),
        (
            {"tier": "auto", "failure": "timeout"},
            {"model_tiers": {"claude_code": {**TIERS["claude_code"], "medium": "model-next"}}},
            "medium",
            "model-next",
        ),
    ],
    indirect=["case"],
)
async def test_an_answer_still_within_the_current_policy_launches(e5, path, change, tier, model):
    e5.decider.during = lambda: operator_edit(e5, change)
    result = await invoke(path, e5.name)
    assert result["success"], result
    assert launched_model(e5) == model
    (record,) = e5.store.list()
    assert record["launch_status"] == "launched" and record["launched_model"] == model
    assert (record["applied_value"] or record["fallback_value"]) == tier
    # The row keeps the fallback; the model applies through the plan.
    assert database.get_ephemeral_agent(e5.name)["model_tier"] == "medium"
    assert [e["record_ids"] for e in decided_events(e5.name)] == [[record["id"]]]
    delete_child()


# An answer still applies when its decision record could not be written, the same
# way an installed target already applies candidate_model regardless of the record.


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "default"])
@pytest.mark.parametrize("case", [{"tier": "auto", "answer": "large"}], indirect=True)
async def test_an_applied_answer_still_launches_when_its_record_cannot_be_written(
    e5, path, monkeypatch
):
    monkeypatch.setattr(e5.store, "insert", lambda *a, **kw: None)
    result = await invoke(path, e5.name)
    assert result["success"], result
    assert launched_model(e5) == "model-large"
    drain(e5)
    assert len(e5.decider.requests) == 1
    assert e5.store.list() == []
    assert decided_events(e5.name) == []
    delete_child()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "default"])
@pytest.mark.parametrize(
    "case",
    [
        {
            "tier": "auto",
            "answer": "large",
            "tiers": {
                "claude_code": {
                    "small": "model-shared",
                    "medium": "model-medium",
                    "large": "model-shared",
                }
            },
            "policy": {"min_tier": "medium"},
        }
    ],
    indirect=True,
)
async def test_an_unrecorded_answer_whose_model_is_shared_by_two_tiers_launches_the_in_bounds_one(
    e5, path, monkeypatch
):
    # "small" and "large" both map to model-shared; with no record to say which tier was
    # decided, the reverse map must not collect the name just because "small" (below the
    # floor) happens to come first in the table — "large" is in bounds, so it launches.
    monkeypatch.setattr(e5.store, "insert", lambda *a, **kw: None)
    result = await invoke(path, e5.name)
    assert result["success"], result
    assert launched_model(e5) == "model-shared"
    drain(e5)
    assert e5.store.list() == []
    delete_child()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "default"])
@pytest.mark.parametrize("case", [{"tier": "auto", "answer": "large"}], indirect=True)
async def test_an_unrecorded_answer_whose_model_is_remapped_runs_the_fallback(
    e5, path, monkeypatch
):
    # The decided tier's old model id maps to nothing in the current table, and there is no
    # record to say which tier was decided: there is nothing safe to reverse-map.
    monkeypatch.setattr(e5.store, "insert", lambda *a, **kw: None)
    e5.decider.during = lambda: operator_edit(
        e5, {"model_tiers": {"claude_code": {**TIERS["claude_code"], "large": "model-next"}}}
    )
    result = await invoke(path, e5.name)
    assert result["success"], result
    assert launched_model(e5) == "model-medium"
    drain(e5)
    assert e5.store.list() == []
    delete_child()


# The recheck acts only for the live claim it was asked for.


@pytest.mark.parametrize("case", [{"tier": "auto", "answer": "large"}], indirect=True)
def test_the_recheck_leaves_a_claim_replaced_during_the_decision_alone(e5, monkeypatch):
    token = claim(e5.env, e5.name)["claim_id"]
    reclaimed = []

    def reclaim_and_tighten():
        # Claim A lapses, the owner claims again as B, and the operator lowers the ceiling.
        if not reclaimed:
            monkeypatch.setattr(database, "_utcnow", lambda: NOW + timedelta(seconds=61))
            reclaimed.append(claim(e5.env, e5.name)["claim_id"])
            operator_edit(e5, {"max_tier": "medium", "default_tier": "medium"})

    e5.decider.during = reclaim_and_tighten
    response = raw_launch(e5, "terminal", token)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["rule"] == "already_claimed"
    row = database.get_ephemeral_agent(e5.name)
    assert row["state"] == "claimed" and row["claim_id"] == reclaimed[0] != token
    assert "policy_changed_since_create:tier_exceeds_policy" not in audit(e5.name).get(
        "refusals", {}
    )
    e5.factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "default"])
@pytest.mark.parametrize("case", [{"tier": "auto", "answer": "large"}], indirect=True)
async def test_the_recheck_leaves_a_lapsed_claim_to_the_bind(e5, monkeypatch, path):
    def lapse_and_tighten():
        monkeypatch.setattr(database, "_utcnow", lambda: NOW + timedelta(seconds=61))
        operator_edit(e5, {"max_tier": "medium", "default_tier": "medium"})

    e5.decider.during = lapse_and_tighten
    result = await invoke(path, e5.name)
    assert not result["success"] and result["rule"] == "claim_expired"
    e5.factory.assert_not_called()
    # The stored tier is still within the bounds, so the name stays reusable.
    row = database.get_ephemeral_agent(e5.name)
    assert row["state"] == "pending" and row["claim_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "default"])
@pytest.mark.parametrize("case", [{"tier": "auto", "answer": "large"}], indirect=True)
async def test_disabling_ephemeral_agents_during_the_decision_stops_the_launch(e5, path):
    e5.decider.during = lambda: operator_edit(e5, {"enabled": False})
    result = await invoke(path, e5.name)
    assert not result["success"] and result["rule"] == "ephemeral_disabled"
    e5.backend.create_window.assert_not_called()
    e5.factory.assert_not_called()
    (record,) = e5.store.list()
    assert record["launch_status"] == "not_launched"
    row = database.get_ephemeral_agent(e5.name)
    assert row["state"] == "pending" and row["claim_id"] is None


# AC11: no request body or query can set the launch plan.


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["body", "query"])
@pytest.mark.parametrize("path", ["assign", "default"])
@pytest.mark.parametrize(
    "case", [{"tier": "auto", "state": "off"}, {"tier": "medium"}], indirect=True
)
async def test_AC11_launch_plan_is_not_a_caller_input(e5, monkeypatch, path, where):
    original = orchestration.requests.post

    def inject(url, **kw):
        if url.endswith(("/terminals", "/run-step")):
            if where == "body":
                kw.setdefault("json", {})["launch_plan"] = {"model": "untrusted-model"}
            else:
                kw.setdefault("params", {})["launch_plan"] = '{"model":"untrusted-model"}'
        return original(url, **kw)

    monkeypatch.setattr(orchestration.requests, "post", inject)
    result = await invoke(path, e5.name)
    assert result["success"], result
    assert launched_model(e5) == "model-medium"
    delete_child()


# The opt-in and the claim it requires.


@pytest.mark.parametrize("site", ["terminal", "step"])
@pytest.mark.parametrize("case", [{"tier": "auto"}], indirect=True)
def test_a_foreign_caller_triggers_no_decision(e5, site):
    token = claim(e5.env, e5.name)["claim_id"]
    database.create_terminal(
        "ffffffff", "cao-session", "foreign", "claude_code", allowed_tools=["*"]
    )
    response = raw_launch(e5, site, token, caller="ffffffff")
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["rule"] == "not_owner"
    drain(e5)
    assert e5.decider.requests == [] and e5.store.list() == []
    e5.factory.assert_not_called()


@pytest.mark.parametrize("case", [{"tier": "auto"}], indirect=True)
def test_decision_inputs_need_a_live_matching_claim(e5, monkeypatch):
    service = e5.env[0]
    token = claim(e5.env, e5.name)["claim_id"]
    for args in (
        ("f" * 32, CALLER, "claude_code"),
        (token, "ffffffff", "claude_code"),
        (token, CALLER, "codex"),
    ):
        assert service.decision_inputs(e5.name, *args) is None
    inputs = service.decision_inputs(e5.name, token, CALLER, "claude_code")
    assert inputs.field_states == {"model.route": "auto"}
    assert (inputs.purpose, inputs.description) == (SPEC["purpose"], DESCRIPTION)
    assert inputs.policy.allowed_providers == frozenset({"claude_code"})
    assert (inputs.policy.min_tier, inputs.policy.max_tier) == ("small", "large")
    assert inputs.policy.default_tier == "medium"
    monkeypatch.setattr(database, "_utcnow", lambda: NOW + timedelta(seconds=61))
    assert service.decision_inputs(e5.name, token, CALLER, "claude_code") is None


@pytest.mark.parametrize("case", [{"tier": "auto"}], indirect=True)
def test_the_fallback_is_the_tier_resolved_at_create(e5):
    # M4: a default changed after create does not re-resolve a pending name's fallback.
    e5.env[4]["ephemeral"]["default_tier"] = "large"
    token = claim(e5.env, e5.name)["claim_id"]
    inputs = e5.env[0].decision_inputs(e5.name, token, CALLER, "claude_code")
    assert inputs.policy.default_tier == "medium"


@pytest.mark.parametrize("site", ["terminal", "step"])
@pytest.mark.parametrize("case", [{"tier": "auto"}], indirect=True)
def test_a_policy_tightened_after_the_claim_collects_without_asking(e5, site):
    token = claim(e5.env, e5.name)["claim_id"]
    e5.env[4]["ephemeral"].update(default_tier="small", max_tier="small")
    response = raw_launch(e5, site, token)
    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["rule"] == "policy_changed_since_create:tier_exceeds_policy"
    row = database.get_ephemeral_agent(e5.name)
    assert row["state"] == "gc" and row["gc_reason"] == "policy_changed"
    assert e5.decider.requests == [] and e5.store.list() == []
    e5.factory.assert_not_called()


# #1074: the provider-capability rules apply at both decision rechecks, as at claim.

PROVIDER_RULES = [
    "policy_changed_since_create:provider_unsupported",
    "policy_changed_since_create:tool_unsupported_on_provider:fs_read",
]


def drop_provider_support(rt, monkeypatch, rule):
    """Make the table refuse the stored claude_code launch, as a CAO upgrade could."""
    row = caps.CAPABILITIES["claude_code"]
    if rule.endswith(":provider_unsupported"):
        # validate_block reads the same table: the operator has already removed the provider.
        tiers = {**TIERS, "codex": {t: "codex-" + t for t in ("small", "medium", "large")}}
        operator_edit(rt, {"allowed_providers": ["codex"], "model_tiers": tiers})
        monkeypatch.setitem(caps.CAPABILITIES, "claude_code", caps.capability_for("kiro_cli"))
    else:
        narrowed = replace(row, tool_atoms=row.tool_atoms - {"fs_read"})
        monkeypatch.setitem(caps.CAPABILITIES, "claude_code", narrowed)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "early", "default"])
@pytest.mark.parametrize("rule", PROVIDER_RULES)
@pytest.mark.parametrize(
    "case,outcome",
    [
        ({"tier": "auto", "answer": "large"}, "applied"),
        ({"tier": "auto", "answer": "unsure"}, "fallback"),
    ],
    indirect=["case"],
)
async def test_a_provider_dropped_during_the_decision_collects_before_the_bind(
    e5, path, rule, outcome, monkeypatch
):
    e5.decider.during = lambda: drop_provider_support(e5, monkeypatch, rule)
    result = await invoke(path, e5.name)
    assert not result["success"] and result["rule"] == rule
    e5.backend.create_window.assert_not_called()
    e5.factory.assert_not_called()
    (record,) = e5.store.list()
    assert record["outcome"] == outcome and record["launch_status"] == "not_launched"
    row = database.get_ephemeral_agent(e5.name)
    assert row["state"] == "gc" and row["gc_reason"] == "policy_changed"
    assert decided_events(e5.name) == []


@pytest.mark.parametrize("site", ["claim", "terminal", "step"])
@pytest.mark.parametrize("rule", PROVIDER_RULES)
@pytest.mark.parametrize("case", [{"tier": "auto"}], indirect=True)
def test_a_provider_dropped_before_the_decision_collects_without_asking(
    e5, site, rule, monkeypatch
):
    if site == "claim":
        drop_provider_support(e5, monkeypatch, rule)
        with pytest.raises(e5.env[0].EphemeralPolicyError) as err:
            claim(e5.env, e5.name)
        assert err.value.rule == rule
    else:
        token = claim(e5.env, e5.name)["claim_id"]
        drop_provider_support(e5, monkeypatch, rule)
        response = raw_launch(e5, site, token)
        assert response.status_code == 400, response.text
        assert response.json()["detail"]["rule"] == rule
    row = database.get_ephemeral_agent(e5.name)
    assert row["state"] == "gc" and row["gc_reason"] == "policy_changed"
    assert e5.decider.requests == [] and e5.store.list() == []
    e5.factory.assert_not_called()


@pytest.mark.parametrize(
    "reason,rule,state",
    [
        ("above_ceiling", "policy_changed_since_create:tier_exceeds_policy", "gc"),
        ("below_floor", "policy_changed_since_create:tier_below_policy", "gc"),
        ("explicit_unmapped", "policy_changed_since_create:tier_unmapped", "gc"),
        ("provider_not_allowed", "policy_changed_since_create:provider_not_allowed", "gc"),
        ("policy_invalid", "policy_config_error:model_tiers", "pending"),
        ("default_unmapped", "policy_config_error:model_tiers", "pending"),
        ("model_override_not_allowed", "model_override_not_allowed", "pending"),
        ("something_new", "unexpected_failure", "pending"),
        (None, "unexpected_failure", "pending"),
    ],
)
@pytest.mark.parametrize("case", [{"tier": "auto"}], indirect=True)
def test_M1_engine_refusals_map_onto_ephemeral_rules(e5, monkeypatch, reason, rule, state):
    from cli_agent_orchestrator.api import main

    async def refuse(req):
        error = DecisionInputError("private-engine-text")
        error.reason = reason
        raise error

    monkeypatch.setattr(main, "prepare_launch", refuse)
    token = claim(e5.env, e5.name)["claim_id"]
    response = raw_launch(e5, "terminal", token)
    detail = response.json()["detail"]
    assert detail["rule"] == rule and "private-engine-text" not in detail["message"]
    assert response.status_code == (500 if rule == "unexpected_failure" else 400)
    row = database.get_ephemeral_agent(e5.name)
    assert row["state"] == state
    if state == "pending":
        assert row["claim_id"] is None
    else:
        assert row["gc_reason"] == "policy_changed"
    e5.factory.assert_not_called()


def test_M1_reason_map_covers_every_engine_reason():
    from cli_agent_orchestrator.decisions.policy import POLICY_REJECTION_REASONS
    from cli_agent_orchestrator.services import ephemeral_service as service

    mapped = set(service._DECISION_TIGHTENED) | set(service._DECISION_REVERTED)
    assert mapped == POLICY_REJECTION_REASONS
    assert not set(service._DECISION_TIGHTENED) & set(service._DECISION_REVERTED)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [RuntimeError("synthetic"), asyncio.CancelledError()])
@pytest.mark.parametrize("case", [{"tier": "auto"}], indirect=True)
async def test_a_failure_before_the_bind_ends_only_this_claim(e5, monkeypatch, fault):
    from cli_agent_orchestrator.api import main

    async def explode(req):
        raise fault

    monkeypatch.setattr(main, "prepare_launch", explode)
    token = claim(e5.env, e5.name)["claim_id"]
    req = DelegationRequest(
        "assign", "claude_code", e5.name, None, "inspect", target_kind=TargetKind.EPHEMERAL
    )
    with pytest.raises(type(fault)):
        await main._prepare_delegation(req, token, CALLER)
    row = database.get_ephemeral_agent(e5.name)
    assert row["state"] == "pending" and row["claim_id"] is None
    e5.factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [{"tier": "auto"}], indirect=True)
async def test_an_audit_failure_never_controls_the_launch(e5, monkeypatch):
    service = e5.env[0]
    original = service._audit_update

    def fail_decided(row, event=None, *args, **kwargs):
        if event == "decided":
            raise OSError("synthetic archive failure")
        return original(row, event, *args, **kwargs)

    monkeypatch.setattr(service, "_audit_update", fail_decided)
    result = await invoke("assign", e5.name)
    assert result["success"], result
    assert launched_model(e5) == "model-large"
    (record,) = e5.store.list()
    assert record["launch_status"] == "launched"
    delete_child()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "default"])
@pytest.mark.parametrize(
    "case,note",
    [
        ({"tier": "auto", "state": "shadow"}, None),
        (
            {"tier": "auto", "policy": {"max_tier": "medium"}},
            "ran on model-medium (auto: large 0.91, capped to medium)",
        ),
    ],
    indirect=["case"],
)
async def test_the_creator_sees_the_on_mode_note_only(e5, path, note):
    result = await invoke(path, e5.name)
    assert result["success"], result
    if note is None:
        assert "auto:" not in result["message"]
    else:
        assert note in result["message"]
    delete_child()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [{"tier": "auto"}], indirect=True)
async def test_a_claimless_launch_of_an_auto_spec_asks_nothing(e5):
    from cli_agent_orchestrator.services import terminal_service

    with record_decision_calls() as calls:
        terminal = await terminal_service.create_terminal(
            "claude_code", e5.name, session_name="cao-session", caller_id=CALLER
        )
    assert terminal.id == CHILD
    assert calls == [] and e5.decider.requests == [] and e5.store.list() == []
    assert launched_model(e5) == "model-medium"
    delete_child()
