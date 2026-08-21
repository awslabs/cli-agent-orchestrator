"""W3C trace-context propagation helpers.

Used to round-trip the active span context through CAO surfaces that cross a
process or queue boundary (the inbox column, plugin events, future A2A calls).

The functions are no-ops when telemetry is disabled — ``inject_traceparent``
returns ``None`` if there is no active span, and ``extract_traceparent``
returns the implicit (empty) context when ``traceparent`` is ``None``.
"""

from __future__ import annotations

from typing import Optional

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

_TRACEPARENT_KEY = "traceparent"
_TRACESTATE_KEY = "tracestate"

# Use an explicit W3C propagator rather than the global textmap propagator: a
# ``traceparent`` is W3C by definition, and the global propagator is mutable
# (an operator can set ``OTEL_PROPAGATORS=b3``, and other in-process code may
# reconfigure it), which would otherwise make these helpers emit/parse the
# wrong format non-deterministically.
_PROPAGATOR = TraceContextTextMapPropagator()


def inject_traceparent() -> Optional[str]:
    """Return the W3C ``traceparent`` for the current span, or ``None``.

    Returns ``None`` when there is no recording span (i.e. telemetry disabled
    or called outside a span). Callers persist the returned string verbatim;
    do not parse or rewrite it.
    """
    span = trace.get_current_span()
    if not span.get_span_context().is_valid:
        return None
    carrier: dict[str, str] = {}
    _PROPAGATOR.inject(carrier)
    return carrier.get(_TRACEPARENT_KEY)


def extract_traceparent(traceparent: Optional[str], tracestate: Optional[str] = None) -> Context:
    """Return a Context resuming the trace identified by ``traceparent``.

    When ``traceparent`` is ``None`` (no upstream context recorded), returns a
    fresh empty ``Context`` — a new root, **not** the currently-active context.
    Callers typically pass the result to ``opentelemetry.context.attach`` or as
    ``context=`` on a span start call.
    """
    if traceparent is None:
        return Context()
    carrier: dict[str, str] = {_TRACEPARENT_KEY: traceparent}
    if tracestate is not None:
        carrier[_TRACESTATE_KEY] = tracestate
    return _PROPAGATOR.extract(carrier)
