"""Preserve request documentation and best-effort delegation behavior."""

from test.decisions.test_engine import (  # noqa: F401  # pytest fixture, injected by name into the tests below
    setup_engine,
)
from test.decisions.test_launch_binding import (  # noqa: F401  # pytest fixture, injected by name into the tests below
    terminal_path,
)

from test.decisions.test_wiring import (  # isort: skip  (per-name noqa below; see the comment)
    handler_path,  # noqa: F401  # pytest fixture, injected by name
    post_delegation,
)
from unittest.mock import Mock

import pytest


def test_request_descriptions_match_original_openapi():
    from test.fixtures.request_descriptions import REQUEST_DESCRIPTIONS

    from cli_agent_orchestrator.api import main as api

    schemas = api.app.openapi()["components"]["schemas"]
    for name, expected in REQUEST_DESCRIPTIONS.items():
        assert schemas[name]["description"] == expected


def test_failed_unlaunched_bind_keeps_successful_response(handler_path, monkeypatch, caplog):
    api, client, engine, decider, store, events, calls = handler_path
    monkeypatch.setattr(api, "bind_launch", Mock(side_effect=RuntimeError("store failure")))
    response = post_delegation(client, "terminal")
    assert response.status_code == 201
    assert store.list()[0]["launch_status"] == "launched"
    assert "Decision launch cleanup bind failed" in caplog.text


@pytest.mark.asyncio
async def test_failed_service_bind_keeps_launched_terminal(terminal_path, monkeypatch, caplog):
    from cli_agent_orchestrator.decisions import engine as module

    service, engine, req, provider, backend, providers, store, events = terminal_path
    plan = await engine.prepare_launch(req)
    monkeypatch.setattr(module, "bind_launch", Mock(side_effect=RuntimeError("store failure")))
    terminal = await service.create_terminal(
        "codex", "developer", new_session=True, launch_plan=plan
    )
    assert terminal.model == "model-x"
    assert "Decision launch bind failed" in caplog.text


@pytest.mark.asyncio
async def test_failed_response_header_read_keeps_assignment(tmp_path, monkeypatch, caplog):
    from test.fixtures.decision_conformance import ConfidentDecider
    from test.fixtures.delegation_conformance import DelegationAdapter

    adapter = DelegationAdapter(tmp_path, monkeypatch)
    adapter.configure(
        states={"model.route": "on"},
        deciders={"model.route": ConfidentDecider("small", 0.91)},
        model_tiers={"codex": {"small": "model-x"}},
    )
    post = adapter.post

    class UnreadableHeader:
        def get(self, name):
            raise RuntimeError("header failure")

    class Response:
        headers = UnreadableHeader()

        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

    def intercepted(url, **kwargs):
        return Response(post(url, **kwargs))

    from cli_agent_orchestrator.utils import orchestration

    monkeypatch.setattr(orchestration.requests, "post", intercepted)
    observation = await adapter.delegate(message="task")
    assert observation.launched and adapter.result["success"]
    assert "auto:" not in adapter.result["message"]
    assert "Decision response header could not be read" in caplog.text
    await adapter.engine.runner.close()


def test_new_session_handoff_without_delivery_is_routed(handler_path):
    api, client, engine, decider, store, events, calls = handler_path
    response = client.post(
        "/sessions",
        params={"provider": "codex", "agent_profile": "developer"},
        json={"delegation_message": "task", "delegation_kind": "handoff"},
    )
    assert response.status_code == 201, response.text
    assert calls[0]["initial_message"] is None and calls[0]["launch_plan"].model == "model-x"
    assert store.list()[0]["kind"] == decider.requests[0].facts.kind == "handoff"
    assert response.headers["X-CAO-Decision"] == "ran on model-x (auto: small 0.91)"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    [
        "above_ceiling",
        "explicit_unmapped",
        "provider_not_allowed",
        "model_override_not_allowed",
        "default_unmapped",
        "policy_invalid",
    ],
)
async def test_rejected_record_reason_belongs_to_exported_set(setup_engine, reason):
    from dataclasses import replace

    from cli_agent_orchestrator.decisions.policy import (
        POLICY_REJECTION_REASONS,
        DecisionInputError,
        PolicyBounds,
    )
    from cli_agent_orchestrator.decisions.targets import TargetKind

    engine, req, decider, store, events, settings = setup_engine
    request = replace(req, field_states={"model.route": "auto"})
    if reason == "above_ceiling":
        request = replace(
            request,
            field_states={"model.route": "large"},
            policy=PolicyBounds(default_tier="small", max_tier="medium"),
        )
    elif reason == "explicit_unmapped":
        engine.settings_loader = lambda: replace(settings, model_tiers={})
        request = replace(request, field_states={"model.route": "small"})
    elif reason == "provider_not_allowed":
        request = replace(
            request, policy=PolicyBounds(allowed_providers=frozenset({"claude_code"}))
        )
    elif reason == "model_override_not_allowed":
        request = replace(request, target_kind=TargetKind.EPHEMERAL, model="model-x")
    elif reason == "default_unmapped":
        engine.settings_loader = lambda: replace(settings, model_tiers={})
        request = replace(
            request, target_kind=TargetKind.EPHEMERAL, policy=PolicyBounds(default_tier="small")
        )
    else:
        request = replace(request, policy=PolicyBounds(max_tier="medium"))
    with pytest.raises(DecisionInputError) as raised:
        await engine.prepare_launch(request)
    rows = store.list()
    assert rows and all(
        row["outcome"] == "rejected" and row["reason"] in POLICY_REJECTION_REASONS for row in rows
    )
    assert rows[0]["reason"] == raised.value.reason == reason


def test_policy_rejection_set_matches_all_error_subclasses():
    import importlib
    import pkgutil

    import cli_agent_orchestrator.decisions as package
    from cli_agent_orchestrator.decisions.policy import (
        POLICY_REJECTION_REASONS,
        DecisionInputError,
        UnmappedTierError,
    )

    for module in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
        importlib.import_module(module.name)

    # This constructor dispatches to concrete errors with their own reasons.
    dispatchers = {UnmappedTierError: ("explicit_unmapped", "default_unmapped")}
    pending = [DecisionInputError]
    visited = set()
    reasons = set()
    while pending:
        cls = pending.pop()
        if cls in visited:
            continue
        visited.add(cls)
        pending.extend(cls.__subclasses__())
        if cls is DecisionInputError:
            assert cls.reason is None
            continue
        if cls in dispatchers:
            assert "reason" not in UnmappedTierError.__dict__
            for reason in dispatchers[cls]:
                error = cls("model.route", "codex", "small", reason)
                assert isinstance(error, cls) and type(error) is not cls
                assert issubclass(type(error), cls) and error.reason == reason
                assert error.reason in POLICY_REJECTION_REASONS
                reasons.add(error.reason)
            with pytest.raises(ValueError, match="unsupported unmapped-tier reason"):
                cls("model.route", "codex", "small", "unknown")
        else:
            assert cls.reason is not None, cls.__name__
            assert cls.reason in POLICY_REJECTION_REASONS, cls.__name__
            reasons.add(cls.reason)
    assert set(dispatchers) <= visited
    assert reasons == POLICY_REJECTION_REASONS
    assert POLICY_REJECTION_REASONS == frozenset(
        {
            "above_ceiling",
            "explicit_unmapped",
            "provider_not_allowed",
            "model_override_not_allowed",
            "default_unmapped",
            "policy_invalid",
        }
    )


@pytest.mark.asyncio
async def test_missing_allowed_provider_is_rejected_before_decider(setup_engine):
    from dataclasses import replace

    from cli_agent_orchestrator.decisions.policy import PolicyBounds, PolicyConfigError

    engine, req, decider, store, events, settings = setup_engine
    engine.settings_loader = lambda: replace(
        settings, model_tiers={"claude_code": {"small": "model-c"}}
    )
    request = replace(
        req,
        provider="claude_code",
        policy=PolicyBounds(
            default_tier="small", allowed_providers=frozenset({"claude_code", "codex"})
        ),
    )
    with pytest.raises(
        PolicyConfigError, match=r"^model_tiers\.codex\.small is not mapped \(required by policy\)$"
    ):
        await engine.prepare_launch(request)
    assert decider.calls == 0
    row = store.list()[0]
    assert (
        row["state"] == "on" and row["outcome"] == "rejected" and row["reason"] == "policy_invalid"
    )
    assert row["launch_status"] == "not_launched" and row["terminal_id"] is None


@pytest.mark.parametrize("provider_order", [("codex", "claude_code"), ("claude_code", "codex")])
def test_missing_allowed_providers_are_reported_in_sorted_order(provider_order):
    from cli_agent_orchestrator.decisions.policy import PolicyBounds, PolicyConfigError

    class OrderedProviders(frozenset):
        """Keep the supplied iteration order instead of hash-dependent order."""

        def __new__(cls, values):
            order = tuple(values)
            instance = super().__new__(cls, order)
            instance._order = order
            return instance

        def __iter__(self):
            return iter(self._order)

    providers = OrderedProviders(provider_order)
    policy = PolicyBounds(default_tier="small", allowed_providers=providers)
    assert policy.allowed_providers is providers
    assert tuple(policy.allowed_providers) == provider_order
    with pytest.raises(
        PolicyConfigError,
        match=r"^model_tiers\.claude_code\.small is not mapped \(required by policy\)$",
    ) as raised:
        policy.validate({})
    assert raised.value.reason == "policy_invalid"


def test_not_honored_bind_changes_only_the_on_model_decision(setup_engine):
    from test.decisions.test_checkpoint import row_values

    _, _, _, store, events, _ = setup_engine
    records = [
        store.insert(
            {
                **row_values(state),
                "point": point,
                "outcome": "applied",
                "applied_value": "small",
                "launch_status": "pending",
            },
            "message",
        )
        for point, state in (
            ("model.route", "on"),
            ("effort.route", "on"),
            ("model.route", "shadow"),
        )
    ]
    store.bind(
        tuple(records),
        launch_status="launched",
        terminal_id="worker",
        launched_model="model-y",
        model_honored=False,
        not_honored=True,
    )
    rows = {row["id"]: row for row in store.list()}
    assert (rows[records[0]]["outcome"], rows[records[0]]["reason"]) == ("fallback", "not_honored")
    assert rows[records[0]]["applied_value"] is None
    for record in records[1:]:
        assert (rows[record]["outcome"], rows[record]["applied_value"]) == ("applied", "small")
        assert rows[record]["reason"] is None and rows[record]["launch_status"] == "launched"


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ValueError("bad frontmatter"), RuntimeError("load failed")])
async def test_malformed_installed_profile_surfaces_the_loader_error(setup_engine, error):
    engine, req, decider, store, events, settings = setup_engine

    def load(_):
        raise error

    engine.profile_loader = load
    # The create path catches only FileNotFoundError around the same loader, so it fails alike.
    with pytest.raises(type(error), match=str(error)):
        await engine.prepare_launch(req)
    assert decider.calls == 0 and store.list() == [] and events == []


@pytest.mark.asyncio
async def test_insecure_key_still_launches_without_records(setup_engine, tmp_path, monkeypatch):
    import os
    from unittest.mock import Mock

    from cli_agent_orchestrator.decisions.hashing import KeyCache

    engine, req, decider, store, events, settings = setup_engine
    path = tmp_path / "loose.key"
    path.write_bytes(b"k" * 32)
    path.chmod(0o644)
    monkeypatch.setattr(os, "fchmod", Mock(side_effect=PermissionError("not owner")))
    store._key = KeyCache(path)
    plan = await engine.prepare_launch(req)
    assert plan is not None and plan.model == "model-x" and decider.calls == 1
    assert plan.record_ids == () and store.list() == []
