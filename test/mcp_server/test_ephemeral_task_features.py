"""#1199: task features and the local match, through the real claim, recheck, bind and launch.

The harness is #1174's (test_ephemeral_provider_route.py): the claim, the decision, the recheck,
the bind and the provider commands are real; only the backend and the provider process are
mocked. On today's capability table Claude Code is hard and Codex advisory, so no set of the
two validates. As #1174 does, these cases substitute Codex's row with the contained launch's,
sandboxed and without web_fetch, so a match can move a helper between them.
"""

from dataclasses import replace
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
from test.mcp_server.test_ephemeral_provider_route import (  # noqa: F401
    BOTH_TIERS,
    C,
    bound,
    claimed_store,
    create_store,
    flag,
    pr,
    ran,
    records,
)
from test.services.test_ephemeral_claim import claim
from test.services.test_ephemeral_service import CALLER

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.decisions.catalog import load_catalog
from cli_agent_orchestrator.decisions.settings import PointSettings
from cli_agent_orchestrator.decisions.types import FEATURE_POINTS, PointState

CATALOG = {
    "version": 1,
    "long_context_tokens": 500000,
    "models": {
        "claude_code": {
            "claude_code-medium": {"context_tokens": 200000, "cost": "medium"},
            "claude_code-large": {"context_tokens": 200000, "cost": "high"},
        },
        "codex": {
            "codex-medium": {"context_tokens": 200000, "cost": "low", "good_for": ["read_only"]},
            "codex-large": {
                "context_tokens": 2000000,
                "cost": "medium",
                "good_for": ["read_only"],
            },
        },
    },
}
ANSWERS = {
    "task.long_context": "yes",
    "task.deep_reasoning": "no",
    "task.read_only": "yes",
    "task.needs_network": "no",
}


@pytest.fixture
def tf(pr, case):
    """pr, with the feature points declared and answered, and provider.route bound to match."""
    pr.decider.points = pr.decider.points | set(FEATURE_POINTS)
    pr.decider.answers.update(ANSWERS, **case.get("feature_answers", {}))
    pr.decider.answers["model.route"] = case.get("answer", "medium")
    base = pr.engine.settings_loader()
    points = {
        **base.points,
        "provider.route": PointSettings(PointState(case.get("provider_state", "on")), "match"),
        "task.features": PointSettings(
            PointState(case.get("features_state", "on")), pr.decider.name
        ),
    }
    pr.settings = replace(
        base,
        points=points,
        model_tiers=BOTH_TIERS,
        match_catalog=load_catalog({"decisions": {"match": CATALOG}}, BOTH_TIERS),
    )
    pr.engine.settings_loader = lambda: pr.settings
    return pr


def asked(rt):
    return [request.points for request in rt.decider.requests]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "default"])
@pytest.mark.parametrize("case", [C(tier="auto")], indirect=True)
async def test_a_matched_helper_launches_on_the_matched_cli_and_tier(tf, path):
    result = await invoke(path, tf.name)
    assert result["success"], result
    assert ran(tf) == ["codex"]
    (args,) = tf.commands
    assert flag(args, "--model") == [BOTH_TIERS["codex"]["large"]]
    assert sorted(asked(tf)) == sorted([FEATURE_POINTS, ("model.route",)])
    (route,) = records(tf, "provider.route")
    assert (route["decider"], route["applied_value"], route["match_pick"]) == (
        "match",
        "codex",
        "codex/large",
    )
    assert route["launch_status"] == "launched"
    (model,) = records(tf, "model.route")
    assert (model["answer"], model["applied_value"], model["match_from"]) == (
        "medium",
        "large",
        "medium",
    )
    for point in FEATURE_POINTS:
        (row,) = records(tf, point)
        assert row["launch_status"] == "launched"
    (event,) = bound(tf.name)
    assert event["provider"] == "codex"
    if path == "assign":
        assert "matched long_context+read_only -> codex large" in result["message"]
    delete_child()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "default"])
@pytest.mark.parametrize("case", [C(tier="auto")], indirect=True)
async def test_AC13_recheck_refuses_a_matched_cli_removed_from_allowed_providers(tf, path):
    tf.decider.during = lambda: operator_edit(tf, {"allowed_providers": ["claude_code"]})
    result = await invoke(path, tf.name)
    assert not result["success"]
    assert result["rule"] == "policy_changed_since_create:provider_not_allowed", result
    assert tf.commands == []
    row = database.get_ephemeral_agent(tf.name)
    assert row["state"] == "gc" and row["gc_reason"] == "policy_changed"
    assert {record["launch_status"] for record in records(tf)} == {"not_launched"}


@pytest.mark.asyncio
@pytest.mark.parametrize("halted", [True, False])
@pytest.mark.parametrize("case", [C(tier="auto")], indirect=True)
async def test_AC14_decisions_off_asks_nothing_and_runs_the_fallback(tf, monkeypatch, halted):
    from cli_agent_orchestrator.decisions import egress
    from cli_agent_orchestrator.decisions.settings import load_settings

    tf.env[4]["decisions"] = {
        "points": {
            "provider.route": {"state": "on", "decider": "match"},
            "model.route": {"state": "on", "decider": "e5_test"},
            "task.features": {"state": "on", "decider": "e5_test"},
        },
        "match": CATALOG,
    }
    monkeypatch.setattr(egress, "is_halted", lambda: halted)
    tf.engine.settings_loader = lambda: load_settings(environment={})
    result = await invoke("assign", tf.name)
    assert result["success"], result
    if halted:
        assert tf.decider.requests == []
        assert records(tf) == []
        assert ran(tf) == ["claude"]
        (args,) = tf.commands
        assert flag(args, "--model") == [BOTH_TIERS["claude_code"]["medium"]]
    else:
        assert sorted(asked(tf)) == sorted([FEATURE_POINTS, ("model.route",)])
        assert ran(tf) == ["codex"]
    delete_child()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "default"])
@pytest.mark.parametrize("case", [C(tier="auto", features_state="shadow")], indirect=True)
async def test_AC15_shadow_launches_as_off_and_records_the_would_be_match(tf, path):
    result = await invoke(path, tf.name)
    assert result["success"], result
    drain(tf)
    assert ran(tf) == ["claude"]
    (args,) = tf.commands
    assert flag(args, "--model") == [BOTH_TIERS["claude_code"]["medium"]]
    (route,) = records(tf, "provider.route")
    assert (route["outcome"], route["reason"], route["applied_value"]) == (
        "fallback",
        "no_match",
        None,
    )
    assert (route["match_rule"], route["match_pick"]) == ("covered", "codex/large")
    (model,) = records(tf, "model.route")
    assert (model["applied_value"], model["match_from"], model["match_pick"]) == (
        "medium",
        "medium",
        "codex/large",
    )
    for point in FEATURE_POINTS:
        (row,) = records(tf, point)
        assert (row["state"], row["outcome"], row["decision_status"]) == (
            "shadow",
            "shadow",
            "done",
        )
    delete_child()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        C(
            tier="auto",
            feature_answers={
                "task.long_context": "no",
                "task.read_only": "no",
                "task.needs_network": "yes",
            },
        )
    ],
    indirect=True,
)
async def test_AC18_needs_network_without_web_fetch_adds_only_the_hint(tf):
    granted = database.get_ephemeral_agent(tf.name)["effective_tools"]
    assert "web_fetch" not in granted
    result = await invoke("assign", tf.name)
    assert result["success"], result
    assert result["message"].endswith(
        "; the task may need network access, and this helper has no web_fetch"
    )
    # Tools, options and the launch are unchanged: the fallback CLI at the size answer.
    assert ran(tf) == ["claude"]
    (args,) = tf.commands
    assert flag(args, "--model") == [BOTH_TIERS["claude_code"]["medium"]]
    (route,) = records(tf, "provider.route")
    assert route["options"] == ["claude_code", "codex"] and route["match_rule"] == "none_needed"
    assert database.get_ephemeral_agent(tf.name)["effective_tools"] == granted
    delete_child()


@pytest.mark.parametrize("tools", [["fs_read"], ["fs_read", "web_fetch"]])
@pytest.mark.parametrize("case", [C(tier="auto", codex="sandboxed")], indirect=True)
def test_decision_inputs_carry_the_stored_tier_and_the_network_grant(tf, tools, monkeypatch):
    service = tf.env[0]
    token = claim(tf.env, tf.name)["claim_id"]
    row = database.get_ephemeral_agent(tf.name)
    assert row["state"] == "claimed"
    monkeypatch.setattr(
        service.database,
        "get_ephemeral_agent",
        lambda name: {**row, "effective_tools": tools} if name == tf.name else None,
    )
    inputs = service.decision_inputs(tf.name, token, CALLER, "claude_code")
    assert inputs.launch_tier == row["model_tier"] == "medium"
    assert inputs.network_granted is ("web_fetch" in tools)
