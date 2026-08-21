"""OpenTelemetry GenAI v1.37+ instrumentation for CAO.

Telemetry is opt-in twice over:

* the OTel packages ship as the ``[otel]`` optional extra
  (``pip install cli-agent-orchestrator[otel]``), keeping the base install
  lean, and
* even with the extra installed, the SDK activates only when
  ``OTEL_SDK_DISABLED=false``; otherwise the helpers fall back to OTel's
  no-op tracer and add no measurable overhead.

Without the extra, every helper below degrades to a no-op with the same
signature, so callers never need to guard their imports.
"""

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator, Optional

try:
    from cli_agent_orchestrator.telemetry.context import extract_traceparent, inject_traceparent
    from cli_agent_orchestrator.telemetry.metrics import record_orchestration_dispatch
    from cli_agent_orchestrator.telemetry.otel import init_telemetry, shutdown_telemetry
    from cli_agent_orchestrator.telemetry.spans import (
        chat_span,
        execute_tool_span,
        invoke_agent_span,
    )

    OTEL_AVAILABLE = True
except ImportError as exc:  # opentelemetry not installed (base install, no [otel] extra)
    # Only degrade when the OpenTelemetry stack itself is absent. A failure
    # importing anything else — e.g. a real bug (typo/bad import) in CAO's own
    # telemetry modules — must surface loudly, not be silently masked as a no-op.
    if not (exc.name or "").startswith("opentelemetry"):
        raise
    OTEL_AVAILABLE = False

    _logger = logging.getLogger(__name__)

    def init_telemetry(service_name: str) -> None:
        """No-op: the [otel] extra is not installed.

        An operator who explicitly asked for telemetry gets told why nothing
        is exported, instead of a silent no-op.
        """
        if os.environ.get("OTEL_SDK_DISABLED", "true").lower() == "false":
            _logger.warning(
                "OTEL_SDK_DISABLED=false but the OpenTelemetry packages are not "
                "installed; telemetry stays off. Install the extra: "
                "pip install cli-agent-orchestrator[otel]"
            )

    def shutdown_telemetry() -> None:
        """No-op: the [otel] extra is not installed."""

    def record_orchestration_dispatch(orchestration_type: str) -> None:
        """No-op: no metric instruments without the [otel] extra."""

    def inject_traceparent() -> Optional[str]:
        """No-op: no recording span can exist without the [otel] extra."""
        return None

    def extract_traceparent(  # type: ignore[misc]  # real variant returns opentelemetry Context
        traceparent: Optional[str], tracestate: Optional[str] = None
    ) -> Any:
        """No-op: there is no OTel Context type without the [otel] extra."""
        return None

    @contextmanager
    def invoke_agent_span(
        agent_id: str,
        conversation_id: Optional[str] = None,
        tier: Optional[int] = None,
    ) -> Iterator[Any]:
        yield None

    @contextmanager
    def execute_tool_span(
        tool_name: str,
        conversation_id: Optional[str] = None,
    ) -> Iterator[Any]:
        yield None

    @contextmanager
    def chat_span(
        model: str,
        conversation_id: Optional[str] = None,
    ) -> Iterator[Any]:
        yield None


__all__ = [
    "OTEL_AVAILABLE",
    "chat_span",
    "execute_tool_span",
    "extract_traceparent",
    "init_telemetry",
    "invoke_agent_span",
    "inject_traceparent",
    "record_orchestration_dispatch",
    "shutdown_telemetry",
]
