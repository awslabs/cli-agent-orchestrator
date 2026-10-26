"""Conformance and unchanged results through the real delegation entry points."""

from test.fixtures.decision_conformance import ConfidentDecider, conformance_cases, run_case
from test.fixtures.delegation_conformance import DelegationAdapter

import pytest

ENTRYPOINTS = ("assign", "remote_assign", "handoff", "handoff_early", "elastic")
NEW_SESSION_HANDOFFS = ("handoff_early_new_session", "handoff_reuse_new_session")


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ENTRYPOINTS)
@pytest.mark.parametrize("case", conformance_cases(), ids=lambda case: case.name)
async def test_delegation_conformance(case, entrypoint, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CAO_HOME_DIR", str(tmp_path / "cao"))
    await run_case(case, lambda: DelegationAdapter(tmp_path, monkeypatch, entrypoint))


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", (*ENTRYPOINTS, *NEW_SESSION_HANDOFFS))
async def test_empty_task_launches_with_points_off(entrypoint, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CAO_HOME_DIR", str(tmp_path / "cao"))
    adapter = DelegationAdapter(tmp_path, monkeypatch, entrypoint)
    adapter.configure(
        states={"model.route": "off"},
        deciders={"model.route": ConfidentDecider("small", 0.91)},
        model_tiers={"codex": {"small": "model-x"}},
    )
    observed = await adapter.delegate(message="")
    assert observed.launched, observed.error
    await adapter.engine.runner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["assign", "handoff", "elastic"])
async def test_decision_state_does_not_change_the_caller_visible_result(
    entrypoint, tmp_path, monkeypatch
):
    """Decision routing must be invisible to the caller: the same task gets the
    same result whether the point is off, shadow, driven by an explicit model,
    or bypassed by an excluded profile. This is the regression the old
    byte-for-byte golden text guarded: enabling/disabling decisions must not
    change what assign/handoff/elastic hand back. Comparing the four modes to
    each other (instead of to stored text) still catches that regression
    without pinning wording that is free to change for unrelated reasons.
    """
    from cli_agent_orchestrator.constants import DECISION_RESPONSE_HEADER

    decider = ConfidentDecider("small", 0.91)
    results = {}
    for mode in ("off", "shadow", "explicit", "excluded"):
        adapter = DelegationAdapter(tmp_path, monkeypatch, entrypoint)
        adapter.exclude = mode == "excluded"
        adapter.configure(
            states={
                "model.route": "off" if mode == "off" else ("shadow" if mode == "shadow" else "on")
            },
            deciders={"model.route": decider},
            model_tiers={"codex": {"small": "model-x"}},
        )
        await adapter.delegate(
            message="fixture task", model="explicit" if mode == "explicit" else None
        )
        result = (
            adapter.result
            if isinstance(adapter.result, dict)
            else adapter.result.model_dump(mode="json")
        )
        results[mode] = result
        leaked = set(result) & _decision_only_fields()
        assert not leaked, f"{entrypoint} [{mode}] returns decision field(s) {sorted(leaked)}"
        assert not [
            r for r in adapter.responses if DECISION_RESPONSE_HEADER in r.headers
        ], f"{entrypoint} [{mode}] sent a decision note"
        await adapter.engine.runner.drain()
        await adapter.engine.runner.close()

    off_result = results["off"]
    assert off_result["success"] is True
    assert off_result.get("terminal_id")
    for mode, result in results.items():
        assert (
            result == off_result
        ), f"{entrypoint}: {mode} result differs from off: {result} != {off_result}"


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", (*ENTRYPOINTS, "handoff_reuse", *NEW_SESSION_HANDOFFS))
async def test_applied_note_reaches_every_result(entrypoint, tmp_path, monkeypatch):
    adapter = DelegationAdapter(tmp_path, monkeypatch, entrypoint)
    fake = ConfidentDecider("small", 0.91)
    adapter.configure(
        states={"model.route": "on"},
        deciders={"model.route": fake},
        model_tiers={adapter.provider: {"small": "model-x"}},
    )
    await adapter.delegate(message="fixture task")
    message = (
        adapter.result["message"] if isinstance(adapter.result, dict) else adapter.result.message
    )
    assert message.endswith("; ran on model-x (auto: small 0.91)")
    assert len(adapter.store.list()) == 1
    expected_kind = "assign" if entrypoint in ("assign", "remote_assign", "elastic") else "handoff"
    assert adapter.store.list()[0]["kind"] == fake.requests[0].facts.kind == expected_kind
    if adapter.new_session:
        assert any(url.endswith("/sessions") for url, _ in adapter.posts)
    await adapter.engine.runner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", (*ENTRYPOINTS, "handoff_reuse"))
@pytest.mark.parametrize(
    "model,encoded",
    [
        ("with space", "with%20space"),
        ("modèle", "mod%C3%A8le"),
        ("model\r\nvalue", "model%0D%0Avalue"),
    ],
)
async def test_model_encoding_is_preserved_verbatim_in_result(
    entrypoint, model, encoded, tmp_path, monkeypatch
):
    adapter = DelegationAdapter(tmp_path, monkeypatch, entrypoint)
    adapter.configure(
        states={"model.route": "on"},
        deciders={"model.route": ConfidentDecider("small", 0.91)},
        model_tiers={"codex": {"small": model}},
    )
    await adapter.delegate(message="fixture task")
    message = (
        adapter.result["message"] if isinstance(adapter.result, dict) else adapter.result.message
    )
    assert message.endswith(f"; ran on {encoded} (auto: small 0.91)")
    assert "\r" not in message and "\n" not in message
    await adapter.engine.runner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", (*ENTRYPOINTS, "handoff_reuse"))
@pytest.mark.parametrize(
    "reason", ["unsure", "timeout", "decider_unavailable", "not_honored", "provider_default"]
)
async def test_fallback_note_vouches_only_for_bound_model(
    entrypoint, reason, tmp_path, monkeypatch
):
    from test.fixtures.decision_conformance import TimeoutDecider, UnsureDecider

    adapter = DelegationAdapter(tmp_path, monkeypatch, entrypoint)
    fake = TimeoutDecider() if reason == "timeout" else UnsureDecider()
    if reason == "provider_default":
        adapter.profile_model = None
    elif reason == "not_honored":
        adapter.provider = "mock_cli"
        adapter.honored = False
    adapter.configure(
        states={"model.route": "on"},
        deciders={} if reason == "decider_unavailable" else {"model.route": fake},
        model_tiers={adapter.provider: {"small": "model-x"}},
        on_timeout_ms=50,
    )
    await adapter.delegate(message="fixture task")
    message = (
        adapter.result["message"] if isinstance(adapter.result, dict) else adapter.result.message
    )
    model = "provider default" if reason in ("provider_default", "not_honored") else "model-y"
    expected = "unsure" if reason == "provider_default" else reason
    assert message.endswith(f"; ran on {model} (auto: fallback, {expected})")
    await adapter.engine.runner.close()


@pytest.mark.asyncio
async def test_all_paths_hash_only_the_same_unshaped_redacted_message(tmp_path, monkeypatch):
    from cli_agent_orchestrator.decisions.hashing import KeyCache
    from cli_agent_orchestrator.models.agent_profile import AgentProfile
    from cli_agent_orchestrator.services.secret_gate import redact_secrets

    text = "sentinel task 文\n" + "ghp_" + "a" * 36
    description = "description-sentinel " + "ghp_" + "z" * 36
    hashes, sizes = [], []
    for entrypoint, provider in [(e, "codex") for e in (*ENTRYPOINTS, "handoff_reuse")] + [
        (e, "claude_code") for e in (*NEW_SESSION_HANDOFFS, "handoff")
    ]:
        adapter = DelegationAdapter(tmp_path, monkeypatch, entrypoint)
        adapter.provider = provider
        fake = ConfidentDecider("small", 0.91)
        adapter.configure(
            states={"model.route": "on"},
            deciders={"model.route": fake},
            model_tiers={provider: {"small": "model-x"}},
        )
        adapter.store._key = KeyCache(tmp_path / "common-hash.key")
        adapter.engine.profile_loader = lambda name: AgentProfile(
            name=name, description=description, model="model-y"
        )
        await adapter.delegate(message=text, purpose="purpose-sentinel")
        assert fake.requests and fake.requests[0].message == redact_secrets(text)[0]
        assert fake.requests[0].profile_description == redact_secrets(description)[0]
        assert fake.requests[0].purpose is None
        for marker in (
            "a1b2c3d4",
            "[Assigned by terminal",
            "[Elastic worker lifecycle",
            "[CAO Handoff]",
        ):
            assert marker not in fake.requests[0].message
        row = adapter.store.list()[0]
        assert "sentinel task" not in str(row) and "description-sentinel" not in str(row)
        assert all("purpose" not in (kwargs.get("json") or {}) for url, kwargs in adapter.posts)
        hashes.append(row["message_hash"])
        sizes.append(row["message_bytes"])
        await adapter.engine.runner.close()
    assert len(set(hashes)) == 1
    assert sizes == [len(redact_secrets(text)[0].encode("utf-8"))] * len(sizes)


@pytest.mark.asyncio
async def test_only_declared_missing_capabilities_skip(tmp_path, monkeypatch):
    unsupported = [case for case in conformance_cases() if case.requires]
    assert DelegationAdapter.supports == frozenset()
    for case in unsupported:
        assert case.requires <= {"policy", "field_states"}
        with pytest.raises(pytest.skip.Exception) as skipped:
            await run_case(case, lambda: DelegationAdapter(tmp_path, monkeypatch))
        assert str(skipped.value) == "adapter lacks " + ", ".join(sorted(case.requires))


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", (*ENTRYPOINTS, "handoff_reuse"))
async def test_real_capability_reprobe_is_reflected_in_tool_result(
    entrypoint, tmp_path, monkeypatch
):
    adapter = DelegationAdapter(tmp_path, monkeypatch, entrypoint)
    adapter.provider = "kiro_cli"
    adapter.reprobe = True
    adapter.configure(
        states={"model.route": "on"},
        deciders={"model.route": ConfidentDecider("small", 0.91)},
        model_tiers={"kiro_cli": {"small": "model-x"}},
    )
    await adapter.delegate(message="fixture task")
    message = (
        adapter.result["message"] if isinstance(adapter.result, dict) else adapter.result.message
    )
    assert adapter.probe.call_count == 2
    assert message.endswith("; ran on model-y (auto: fallback, not_honored)")
    row = adapter.store.list()[0]
    assert row["launched_model"] == "model-y" and row["candidate_model"] == "model-x"
    assert row["outcome"] == "fallback" and row["reason"] == "not_honored"
    assert adapter.spans[0]["outcome"] == "fallback" and adapter.spans[0]["reason"] == "not_honored"
    await adapter.engine.runner.close()


# Decision-record column names (clients/database.py
# DecisionRecordModel) that are also used elsewhere on the MCP surface for
# something unrelated to decisions: agent_profile (params of assign,
# assign_elastic, handoff, list_outcomes, report_outcome), answer
# (answer_user_prompt param), provider (params of assign_elastic and
# create_ephemeral_agent), state (workflow_list param) and terminal_id
# (params plus HandoffResult). These are excluded because flagging them
# would fail this test today for reasons that have nothing to do with a
# decision leaking onto the public surface.
_GENERIC_DECISION_RECORD_FIELDS = frozenset(
    {"agent_profile", "answer", "provider", "state", "terminal_id"}
)


def _decision_only_fields():
    from cli_agent_orchestrator.clients.database import DecisionRecordModel

    return frozenset(
        c
        for c in DecisionRecordModel.__table__.columns.keys()
        if c not in _GENERIC_DECISION_RECORD_FIELDS
    )


def test_no_mcp_tool_param_or_output_model_has_a_decision_field():
    """No `@mcp.tool` parameter or result model may expose a decision-record
    field (candidate_model, launched_model, applied_value, decider, ...): the
    decision platform is meant to stay an internal routing concern, invisible
    on the public MCP surface. This replaces a byte-for-byte hash of
    `server.py`/`models.py`, which broke on any unrelated docstring edit or
    new tool and didn't explain what changed; this check instead fails only
    when a decision field actually appears on a tool param or result model.
    """
    import ast
    import asyncio
    import inspect

    from fastmcp import FastMCP
    from pydantic import BaseModel

    from cli_agent_orchestrator.mcp_server import models as result_models
    from cli_agent_orchestrator.mcp_server import server as cao_mcp

    decision_fields = _decision_only_fields()

    tools = asyncio.run(cao_mcp.mcp._list_tools())
    assert tools, "expected at least one registered MCP tool"
    scratch = FastMCP("scratch")
    cao_mcp._register_ephemeral_tool(scratch, True)
    tools += asyncio.run(scratch._list_tools())
    for tool in tools:
        params = set(tool.parameters.get("properties", {}))
        leaked = params & decision_fields
        assert not leaked, f"{tool.name} takes decision field(s) {sorted(leaked)}"
        output_props = set((tool.output_schema or {}).get("properties", {}))
        leaked = output_props & decision_fields
        assert not leaked, f"{tool.name} returns decision field(s) {sorted(leaked)}"

    for name, obj in vars(result_models).items():
        if isinstance(obj, type) and issubclass(obj, BaseModel) and obj is not BaseModel:
            leaked = set(obj.model_fields) & decision_fields
            assert not leaked, f"{name} has decision field(s) {sorted(leaked)}"

    for node in ast.walk(ast.parse(inspect.getsource(cao_mcp))):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
            isinstance(d, ast.Call) and getattr(d.func, "attr", None) == "tool"
            for d in node.decorator_list
        ):
            names = {a.arg for a in node.args.args + node.args.kwonlyargs}
            leaked = names & decision_fields
            assert not leaked, f"{node.name} (any branch) takes decision field(s) {sorted(leaked)}"


def test_decision_response_header_name_is_shared():
    # The header name is a wire-format value callers key off of -- an exact
    # pin is the contract here, not brittleness.
    from cli_agent_orchestrator.api import main as api
    from cli_agent_orchestrator.utils import orchestration

    assert (
        api.DECISION_RESPONSE_HEADER == orchestration.DECISION_RESPONSE_HEADER == "X-CAO-Decision"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["remote_assign", "elastic"])
async def test_remote_provider_is_resolved_only_on_receiving_node(
    entrypoint, tmp_path, monkeypatch
):
    from unittest.mock import Mock

    from cli_agent_orchestrator.services import session_service

    adapter = DelegationAdapter(tmp_path, monkeypatch, entrypoint)
    adapter.configure(
        states={"model.route": "on"},
        deciders={"model.route": ConfidentDecider("small", 0.91)},
        model_tiers={"codex": {"small": "model-x"}},
    )
    resolve = Mock(return_value="codex")
    monkeypatch.setattr(adapter.api, "resolve_provider", resolve)
    monkeypatch.setattr(
        session_service,
        "resolve_provider",
        Mock(side_effect=AssertionError("provider resolved twice")),
    )
    await adapter.delegate(message="fixture task")
    resolve.assert_called_once_with("developer", fallback_provider="kiro_cli")
    create = next(kwargs for url, kwargs in adapter.posts if url.endswith("/sessions"))
    assert "provider" not in create["params"]
    assert adapter.creates[0]["provider"] == adapter.store.list()[0]["provider"] == "codex"
    assert adapter.launched_model == "model-x"
    await adapter.engine.runner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ENTRYPOINTS)
async def test_invalid_shadow_answer_is_invisible_to_delegation(entrypoint, tmp_path, monkeypatch):
    from test.fixtures.decision_conformance import OutOfSetDecider

    adapter = DelegationAdapter(tmp_path, monkeypatch, entrypoint)
    adapter.configure(
        states={"model.route": "shadow"},
        deciders={"model.route": OutOfSetDecider()},
        model_tiers={"codex": {"small": "model-x"}},
    )
    await adapter.delegate(message="fixture task")
    await adapter.engine.runner.drain()
    row = adapter.store.list()[0]
    assert adapter.launched_model == "model-y" and row["reason"] == "invalid_answer"
    assert all(
        row[name] is None
        for name in (
            "answer",
            "probabilities",
            "candidate_value",
            "candidate_model",
            "applied_value",
        )
    )
    message = (
        adapter.result["message"] if isinstance(adapter.result, dict) else adapter.result.message
    )
    assert "auto:" not in message
    await adapter.engine.runner.close()


def test_no_decision_note_leaves_the_message_unchanged():
    from cli_agent_orchestrator.utils import orchestration

    assert orchestration._append_decision_note("task text", []) == "task text"
