"""Conformance and unchanged results through the real delegation entry points."""

import json
from test.fixtures.decision_conformance import ConfidentDecider, conformance_cases, run_case
from test.fixtures.delegation_conformance import DelegationAdapter
from test.fixtures.delegation_goldens import GOLDEN_RESULTS

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
@pytest.mark.parametrize(
    "entrypoint,key", [("assign", "assign"), ("handoff", "handoff"), ("elastic", "assign_elastic")]
)
@pytest.mark.parametrize("mode", ["off", "shadow", "explicit", "excluded"])
async def test_unchanged_results_equal_captured_goldens(
    entrypoint, key, mode, tmp_path, monkeypatch
):
    adapter = DelegationAdapter(tmp_path, monkeypatch, entrypoint)
    adapter.exclude = mode == "excluded"
    decider = ConfidentDecider("small", 0.91)
    adapter.configure(
        states={
            "model.route": "off" if mode == "off" else ("shadow" if mode == "shadow" else "on")
        },
        deciders={"model.route": decider},
        model_tiers={"codex": {"small": "model-x"}},
    )
    await adapter.delegate(message="fixture task", model="explicit" if mode == "explicit" else None)
    result = (
        adapter.result
        if isinstance(adapter.result, dict)
        else adapter.result.model_dump(mode="json")
    )
    assert json.dumps(result) == json.dumps(GOLDEN_RESULTS[key])
    await adapter.engine.runner.drain()
    await adapter.engine.runner.close()


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


def test_agent_tool_definitions_and_models_are_unchanged():
    import hashlib
    from pathlib import Path
    from test.fixtures.delegation_goldens import TOOL_DEFINITIONS_SHA256, TOOL_MODELS_SHA256
    from test.fixtures.tool_definitions import definition_fingerprint

    root = Path(__file__).resolve().parents[2]
    assert (
        definition_fingerprint(
            (root / "src/cli_agent_orchestrator/mcp_server/server.py").read_text()
        )
        == TOOL_DEFINITIONS_SHA256
    )
    assert (
        hashlib.sha256(
            (root / "src/cli_agent_orchestrator/mcp_server/models.py").read_bytes()
        ).hexdigest()
        == TOOL_MODELS_SHA256
    )
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
