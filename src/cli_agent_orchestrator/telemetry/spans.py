"""Span helpers wrapping the OTel GenAI v1.37+ semantic conventions.

Each helper is a context manager that starts a span with the right operation
name and required attributes, so call sites stay terse and consistent. When
telemetry is disabled the underlying tracer is the OTel no-op tracer and these
helpers contribute zero overhead beyond a couple of attribute sets.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Optional

from opentelemetry import trace
from opentelemetry.trace import Span

from cli_agent_orchestrator.telemetry import semconv

_TRACER_NAME = "cli_agent_orchestrator"


def _tracer() -> trace.Tracer:
    """Resolve the tracer at call time.

    The global ``TracerProvider`` is installed by ``init_telemetry`` in the app
    lifespan — *after* this module is imported. Caching a tracer at import time
    would bind it to the pre-init (no-op) provider, so spans opened after init
    would never be recorded. Fetching per call always reflects the installed
    provider (and is cheap — the SDK returns a cached tracer).
    """

    return trace.get_tracer(_TRACER_NAME)


@contextmanager
def invoke_agent_span(
    agent_id: str,
    conversation_id: Optional[str] = None,
    tier: Optional[int] = None,
) -> Iterator[Span]:
    """Open an ``invoke_agent`` span for the named agent."""
    name = f"invoke_agent {agent_id}"
    with _tracer().start_as_current_span(name) as span:
        span.set_attribute(semconv.GEN_AI_OPERATION_NAME, semconv.OPERATION_INVOKE_AGENT)
        span.set_attribute(semconv.GEN_AI_AGENT_ID, agent_id)
        if conversation_id is not None:
            span.set_attribute(semconv.GEN_AI_CONVERSATION_ID, conversation_id)
        if tier is not None:
            span.set_attribute(semconv.CAO_TIER, tier)
        yield span


@contextmanager
def execute_tool_span(
    tool_name: str,
    conversation_id: Optional[str] = None,
) -> Iterator[Span]:
    """Open an ``execute_tool`` span for the named tool."""
    name = f"execute_tool {tool_name}"
    with _tracer().start_as_current_span(name) as span:
        span.set_attribute(semconv.GEN_AI_OPERATION_NAME, semconv.OPERATION_EXECUTE_TOOL)
        if conversation_id is not None:
            span.set_attribute(semconv.GEN_AI_CONVERSATION_ID, conversation_id)
        yield span


@contextmanager
def chat_span(
    model: str,
    conversation_id: Optional[str] = None,
) -> Iterator[Span]:
    """Open a ``chat`` span for a model invocation."""
    name = f"chat {model}"
    with _tracer().start_as_current_span(name) as span:
        span.set_attribute(semconv.GEN_AI_OPERATION_NAME, semconv.OPERATION_CHAT)
        span.set_attribute(semconv.GEN_AI_REQUEST_MODEL, model)
        if conversation_id is not None:
            span.set_attribute(semconv.GEN_AI_CONVERSATION_ID, conversation_id)
        yield span
