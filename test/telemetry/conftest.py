"""Shared pytest fixtures for the telemetry tests.

OTel only allows the global ``TracerProvider`` to be installed once per
process, so the provider and its in-memory exporter are created once at session
scope and shared across all telemetry test modules.

The OpenTelemetry SDK ships as the optional ``[otel]`` extra, so the SDK imports
live *inside* the fixture: importing this conftest — and therefore collecting
the telemetry test package — must not require the SDK. SDK-dependent test
modules guard themselves with ``pytest.importorskip`` and only then request
these fixtures.
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="session")
def telemetry_exporter():
    """Install a real ``TracerProvider`` once for the test session.

    Idempotent: if another test installed a provider first, we attach our span
    processor to that provider rather than trying to replace it.
    """
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    current = trace.get_tracer_provider()
    if isinstance(current, TracerProvider):
        # Provider already installed — just add our processor.
        current.add_span_processor(SimpleSpanProcessor(exporter))
    else:
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
    # The span helpers resolve the tracer at call time, so no cache refresh is
    # needed here — installing the provider above is sufficient.
    return exporter


@pytest.fixture
def exporter(telemetry_exporter):
    """Per-test exporter handle — clears finished spans on entry."""
    telemetry_exporter.clear()
    return telemetry_exporter
