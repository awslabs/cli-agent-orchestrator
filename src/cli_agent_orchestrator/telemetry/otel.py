"""OpenTelemetry SDK initialization for CAO.

Telemetry is **opt-in**. The SDK is installed only when ``OTEL_SDK_DISABLED`` is
explicitly set to ``"false"``. Any other value (including unset) leaves the
default no-op tracer in place, which is the behaviour the existing test suite
expects.

When enabled, the SDK is configured with:
  * a ``Resource`` carrying ``service.name=cao`` and the package version,
  * a ``BatchSpanProcessor`` exporting via OTLP/gRPC,
  * a short shutdown timeout so FastAPI's lifespan teardown does not hang
    when the collector is unreachable.

Standard OTel environment variables are honoured:
  * ``OTEL_EXPORTER_OTLP_ENDPOINT`` (default ``http://localhost:4317``)
  * ``OTEL_EXPORTER_OTLP_HEADERS`` (e.g. ``authorization=Bearer ...``)
"""

from __future__ import annotations

import logging
import os
from importlib import metadata
from typing import Optional

logger = logging.getLogger(__name__)

# Module-level handle so shutdown_telemetry() can flush the same provider that
# init_telemetry() installed. None when telemetry is disabled.
_provider: Optional[object] = None
_meter_provider: Optional[object] = None
_SHUTDOWN_TIMEOUT_MS = 2_000


def _telemetry_enabled() -> bool:
    """Return True only when OTEL_SDK_DISABLED is explicitly 'false'."""
    return os.environ.get("OTEL_SDK_DISABLED", "true").lower() == "false"


def _package_version() -> str:
    try:
        return metadata.version("cli-agent-orchestrator")
    except metadata.PackageNotFoundError:
        return "0.0.0"


def init_telemetry(service_name: str) -> None:
    """Install the global OTel TracerProvider when telemetry is enabled.

    No-op when ``OTEL_SDK_DISABLED`` is not explicitly ``"false"``. Safe to call
    multiple times — only the first activation installs a provider.
    """
    global _provider
    if not _telemetry_enabled():
        logger.debug("OTel telemetry disabled (OTEL_SDK_DISABLED is not 'false')")
        return
    if _provider is not None:
        return

    # Imports are deferred so the OTel packages do not have to be loaded when
    # telemetry is off — keeps cold start fast and keeps test isolation simple.
    from opentelemetry import trace

    # The [otel] extra ships api+sdk+exporter together, but opentelemetry-api
    # can also arrive transitively via another package — in which case this
    # module imports fine while the SDK is still absent. Degrade to a logged
    # no-op rather than raising at enable-time.
    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning(
            "OTEL_SDK_DISABLED=false but the OpenTelemetry SDK is not installed; "
            "telemetry stays off. Install the extra: pip install "
            "cli-agent-orchestrator[otel]"
        )
        return

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": _package_version(),
        }
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    _provider = provider

    # Metrics: install a MeterProvider that exports over the same OTLP
    # endpoint via PeriodicExportingMetricReader. Instrumented call sites emit
    # counters/gauges through the global meter; when telemetry is off, the
    # no-op meter swallows them.
    global _meter_provider
    try:
        from opentelemetry import metrics
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

        reader = PeriodicExportingMetricReader(OTLPMetricExporter())
        meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
        metrics.set_meter_provider(meter_provider)
        _meter_provider = meter_provider
    except Exception:  # pragma: no cover - SDK extras may not be installed
        # Metrics are opt-in; missing the OTLP metrics extra shouldn't
        # break tracing. Operators that want metrics install the extra.
        logger.info("OTel metrics SDK unavailable; tracer-only mode")

    logger.info("OTel telemetry enabled (service=%s)", service_name)


def shutdown_telemetry() -> None:
    """Flush and shut down the installed TracerProvider + MeterProvider.

    Bounded by ``_SHUTDOWN_TIMEOUT_MS`` to avoid hanging FastAPI's graceful
    shutdown when the collector is unreachable.
    """
    global _provider, _meter_provider
    for handle_attr in (_provider, _meter_provider):
        if handle_attr is None:
            continue
        try:
            flush = getattr(handle_attr, "force_flush", None)
            if callable(flush):
                flush(timeout_millis=_SHUTDOWN_TIMEOUT_MS)
            shutdown = getattr(handle_attr, "shutdown", None)
            if callable(shutdown):
                shutdown()
        except Exception:  # pragma: no cover - defensive, shutdown must not raise
            logger.exception("Error while shutting down OTel provider")
    _provider = None
    _meter_provider = None
