import asyncio
from dataclasses import replace
from test.decisions.test_engine import setup_engine

import pytest


@pytest.fixture
def export(setup_engine, monkeypatch):
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from cli_agent_orchestrator.decisions import telemetry

    reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader])
    spans = InMemorySpanExporter()
    trace_provider = TracerProvider()
    trace_provider.add_span_processor(SimpleSpanProcessor(spans))
    monkeypatch.setattr(
        telemetry.trace, "get_tracer", lambda *args: trace_provider.get_tracer("test")
    )
    monkeypatch.setattr(
        telemetry.metrics, "get_meter", lambda *args: meter_provider.get_meter("test")
    )
    setup_engine[3].emit = telemetry.emit_record
    yield spans, reader
    trace_provider.shutdown()
    meter_provider.shutdown()


def request_count(reader):
    data = reader.get_metrics_data()
    if data is None:
        return 0
    return sum(
        point.value
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == "cao.decision.requests"
        for point in metric.data.data_points
    )


@pytest.mark.asyncio
async def test_rejected_and_double_bind_export_once(setup_engine, export):
    from cli_agent_orchestrator.decisions.policy import DecisionInputError, PolicyBounds

    engine, req, decider, store, events, settings = setup_engine
    spans, reader = export
    with pytest.raises(DecisionInputError):
        await engine.prepare_launch(
            replace(
                req,
                field_states={"model.route": "large"},
                policy=PolicyBounds(default_tier="small", max_tier="medium"),
            )
        )
    assert len(spans.get_finished_spans()) == 1
    assert spans.get_finished_spans()[0].attributes["cao.decision.reason"] == "above_ceiling"
    assert request_count(reader) == 1
    plan = await engine.prepare_launch(req)
    engine.bind_launch(plan, launch_status="not_launched")
    engine.bind_launch(plan, launch_status="not_launched")
    assert len(spans.get_finished_spans()) == 2 and request_count(reader) == 2


@pytest.mark.asyncio
async def test_two_points_deferred_failure_does_not_export_again(setup_engine, export):
    engine, req, decider, store, events, settings = setup_engine
    spans, reader = export
    plan = await engine.prepare_launch(
        replace(req, field_states={"effort.route": "auto", "model.route": "auto"})
    )
    assert not spans.get_finished_spans()
    engine.bind_launch(plan, terminal_id="worker", model="model-x", honored=True)
    assert len(spans.get_finished_spans()) == 2 and request_count(reader) == 2
    engine.bind_launch(plan, launch_status="launch_failed")
    assert len(spans.get_finished_spans()) == 2 and request_count(reader) == 2
    attributes = spans.get_finished_spans()[0].attributes
    allowed = {
        "cao.decision." + name
        for name in (
            "point",
            "state",
            "decider",
            "decider_version",
            "option",
            "applied_option",
            "confidence",
            "outcome",
            "reason",
            "latency_ms",
            "model_honored",
        )
    } | {"gen_ai.request.model"}
    assert set(attributes) <= allowed
    assert "message_hash" not in str(attributes) and "sentinel-body" not in str(attributes)


@pytest.mark.asyncio
async def test_shadow_task_and_bind_export_once(setup_engine, export):
    from cli_agent_orchestrator.decisions.settings import PointSettings
    from cli_agent_orchestrator.decisions.types import PointState

    engine, req, decider, store, events, settings = setup_engine
    spans, reader = export
    engine.settings_loader = lambda: replace(
        settings, points={"model.route": PointSettings(PointState.SHADOW, decider.name)}
    )
    plan = await engine.prepare_launch(req)
    engine.bind_launch(plan, terminal_id="worker", model="model-y", honored=True)
    await engine.runner.drain()
    engine.bind_launch(plan, launch_status="not_launched")
    assert len(spans.get_finished_spans()) == 1 and request_count(reader) == 1
    assert spans.get_finished_spans()[0].attributes["cao.decision.outcome"] == "shadow"


@pytest.mark.asyncio
async def test_missing_default_exports_only_rejected_model(setup_engine, export):
    from cli_agent_orchestrator.decisions.policy import DecisionInputError, PolicyBounds
    from cli_agent_orchestrator.decisions.targets import TargetKind

    engine, req, decider, store, events, settings = setup_engine
    spans, reader = export
    engine.settings_loader = lambda: replace(settings, model_tiers={})
    with pytest.raises(DecisionInputError):
        await engine.prepare_launch(
            replace(
                req,
                target_kind=TargetKind.EPHEMERAL,
                field_states={"effort.route": "auto", "model.route": "auto"},
                policy=PolicyBounds(default_tier="small"),
            )
        )
    assert len(spans.get_finished_spans()) == 1 and request_count(reader) == 1
    attributes = spans.get_finished_spans()[0].attributes
    assert attributes["cao.decision.point"] == "model.route"
    assert (
        attributes["cao.decision.outcome"] == "rejected"
        and attributes["cao.decision.reason"] == "default_unmapped"
    )


@pytest.mark.asyncio
async def test_all_off_never_loads_or_exports(setup_engine, export):
    from unittest.mock import Mock

    from cli_agent_orchestrator.decisions.settings import PointSettings

    engine, req, decider, store, events, settings = setup_engine
    spans, reader = export
    engine.settings_loader = lambda: replace(
        settings, points={point: PointSettings() for point in settings.points}
    )
    engine.registry.get = Mock(side_effect=AssertionError("decider loaded"))
    assert await engine.prepare_launch(req) is None
    assert not engine.registry.get.called and not spans.get_finished_spans()
    assert request_count(reader) == 0 and store.list() == []


@pytest.mark.asyncio
async def test_startup_sweep_exports_each_transition_once(setup_engine, export):
    from test.decisions.test_checkpoint import row_values

    engine, req, decider, store, events, settings = setup_engine
    spans, reader = export
    store.insert(row_values(), "on")
    store.insert(row_values("shadow", "pending"), "shadow")
    store.sweep()
    assert len(spans.get_finished_spans()) == 2 and request_count(reader) == 2
    store.sweep()
    assert len(spans.get_finished_spans()) == 2 and request_count(reader) == 2
