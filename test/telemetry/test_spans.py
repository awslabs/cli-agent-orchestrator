"""Tests for the GenAI semconv span helpers.

Verifies the helpers set the right operation name and attributes. The shared
``exporter`` fixture lives in ``conftest.py`` so all telemetry tests share a
single TracerProvider (OTel only allows one per process).
"""

from __future__ import annotations

import pytest

pytest.importorskip("opentelemetry.sdk")

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from cli_agent_orchestrator.telemetry import (
    chat_span,
    execute_tool_span,
    invoke_agent_span,
    semconv,
)


def _spans(exporter: InMemorySpanExporter):
    return list(exporter.get_finished_spans())


class TestInvokeAgentSpan:
    def test_emits_invoke_agent_with_required_attributes(self, exporter):
        with invoke_agent_span("cao-mayor", conversation_id="conv-1", tier=1):
            pass
        finished = _spans(exporter)
        assert len(finished) == 1
        s = finished[0]
        assert s.name == "invoke_agent cao-mayor"
        assert s.attributes[semconv.GEN_AI_OPERATION_NAME] == semconv.OPERATION_INVOKE_AGENT
        assert s.attributes[semconv.GEN_AI_AGENT_ID] == "cao-mayor"
        assert s.attributes[semconv.GEN_AI_CONVERSATION_ID] == "conv-1"
        assert s.attributes[semconv.CAO_TIER] == 1


class TestExecuteToolSpan:
    def test_emits_execute_tool(self, exporter):
        with execute_tool_span("delegate", conversation_id="conv-2"):
            pass
        finished = _spans(exporter)
        assert len(finished) == 1
        s = finished[0]
        assert s.name == "execute_tool delegate"
        assert s.attributes[semconv.GEN_AI_OPERATION_NAME] == semconv.OPERATION_EXECUTE_TOOL
        assert s.attributes[semconv.GEN_AI_CONVERSATION_ID] == "conv-2"


class TestChatSpan:
    def test_emits_chat_with_request_model(self, exporter):
        with chat_span("claude-opus-4-7"):
            pass
        finished = _spans(exporter)
        assert len(finished) == 1
        s = finished[0]
        assert s.name == "chat claude-opus-4-7"
        assert s.attributes[semconv.GEN_AI_OPERATION_NAME] == semconv.OPERATION_CHAT
        assert s.attributes[semconv.GEN_AI_REQUEST_MODEL] == "claude-opus-4-7"


class TestNoopWhenTelemetryDisabled:
    def test_helpers_do_not_raise_with_default_tracer(self):
        # Even with the test provider installed at module scope, this just
        # verifies the helpers compose cleanly. The "no-op" path is exercised
        # implicitly by the rest of the test suite which never installs a
        # provider — so this case is mostly a structural smoke test.
        with invoke_agent_span("cao-mayor"):
            with execute_tool_span("noop"):
                with chat_span("model"):
                    pass


class TestChatSpanConversationId:
    def test_chat_span_sets_conversation_id(self, exporter):
        with chat_span("gpt-4o", conversation_id="conv-9"):
            pass
        assert any(
            s.attributes.get(semconv.GEN_AI_CONVERSATION_ID) == "conv-9" for s in _spans(exporter)
        )
