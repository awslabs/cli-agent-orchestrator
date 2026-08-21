"""Tests for the W3C trace-context inject/extract helpers."""

from __future__ import annotations

import pytest

pytest.importorskip("opentelemetry.sdk")

from opentelemetry import trace
from opentelemetry.context import attach, detach

from cli_agent_orchestrator.telemetry import extract_traceparent, inject_traceparent


class TestInject:
    def test_returns_none_when_no_active_span(self):
        # No span started; default span has invalid context.
        assert inject_traceparent() is None

    def test_returns_traceparent_inside_span(self, exporter):
        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("root"):
            tp = inject_traceparent()
        assert tp is not None
        assert tp.startswith("00-")  # W3C version 00


class TestExtract:
    def test_none_returns_empty_context(self):
        ctx = extract_traceparent(None)
        # Extracting None should not give us a remote-span context.
        span = trace.get_current_span(ctx)
        assert not span.get_span_context().is_valid

    def test_round_trip_preserves_trace_id(self, exporter):
        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("upstream") as upstream:
            tp = inject_traceparent()
            upstream_trace_id = upstream.get_span_context().trace_id

        ctx = extract_traceparent(tp)
        token = attach(ctx)
        try:
            with tracer.start_as_current_span("downstream") as downstream:
                assert downstream.get_span_context().trace_id == upstream_trace_id
        finally:
            detach(token)


class TestExtractWithTracestate:
    def test_tracestate_is_included_in_carrier(self):
        tp = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
        ctx = extract_traceparent(tp, tracestate="vendor=abc")
        span = trace.get_current_span(ctx)
        assert span.get_span_context().is_valid
