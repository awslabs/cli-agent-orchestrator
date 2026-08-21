"""GenAI-style metric instruments for CAO orchestration (opt-in).

Like the span helpers, these are inert unless the ``[otel]`` extra is installed
and the SDK is activated. The meter and its instruments are resolved lazily at
call time so they bind to the ``MeterProvider`` installed by ``init_telemetry``
in the app lifespan — never a pre-init no-op provider (same lesson as the span
helpers).
"""

from __future__ import annotations

from typing import Optional

from opentelemetry import metrics
from opentelemetry.metrics import Counter

from cli_agent_orchestrator.telemetry import semconv

_METER_NAME = "cli_agent_orchestrator"
_dispatch_counter: Optional[Counter] = None


def _dispatch_counter_instrument() -> Counter:
    """Lazily create (once) the orchestration-dispatch counter."""

    global _dispatch_counter
    if _dispatch_counter is None:
        _dispatch_counter = metrics.get_meter(_METER_NAME).create_counter(
            "cao.orchestration.dispatches",
            unit="1",
            description=(
                "Count of inter-agent orchestration dispatches "
                "(send_message / handoff / assign), by orchestration type."
            ),
        )
    return _dispatch_counter


def record_orchestration_dispatch(orchestration_type: str) -> None:
    """Increment the orchestration-dispatch counter (no-op when telemetry off)."""

    _dispatch_counter_instrument().add(1, {semconv.CAO_ORCHESTRATION_TYPE: orchestration_type})
