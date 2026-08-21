"""F4: the send-message orchestration seam is really instrumented.

Proves the telemetry helpers are wired into a live service path (not just
defined): driving ``terminal_service.send_input`` through the dispatch branch
records a GenAI ``execute_tool`` span AND stamps the outgoing plugin event with
a W3C ``traceparent`` captured inside that span.

Run in a subprocess with a pristine OpenTelemetry global state. The global
``TracerProvider`` is process-global and set-once, and other telemetry tests
install/shut down providers in-process, so an in-process assertion on a
recorded span is inherently flaky under the shared test runner. A clean
subprocess makes it deterministic — the same isolation pattern this package
already uses for the no-extra fallback probes.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

pytest.importorskip("opentelemetry.sdk")

_SEAM_PROBE = """
from unittest.mock import MagicMock

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

exporter = InMemorySpanExporter()
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(exporter))
trace.set_tracer_provider(provider)

from cli_agent_orchestrator.models.inbox import OrchestrationType
from cli_agent_orchestrator.services import terminal_service

captured = {}


def _capture_dispatch(registry, event_type, event):
    captured["event_type"] = event_type
    captured["event"] = event


provider_stub = MagicMock()
provider_stub.blocks_orchestrated_input_while_waiting_user_answer = False
provider_stub.paste_enter_count = 1
provider_stub.paste_submit_delay = 0.3

terminal_service.get_terminal_metadata = lambda tid: {"tmux_session": "cao-s", "tmux_window": "w"}
terminal_service.provider_manager.get_provider = lambda tid: provider_stub
terminal_service.inject_memory_context = lambda msg, tid: msg
terminal_service.update_last_active = lambda tid: None
terminal_service.status_monitor = MagicMock()
terminal_service.get_backend = lambda: MagicMock()
terminal_service.dispatch_plugin_event = _capture_dispatch

ok = terminal_service.send_input(
    "term1234",
    "hello worker",
    registry=MagicMock(),
    sender_id="supervisor",
    orchestration_type=OrchestrationType.HANDOFF,
)
assert ok is True, "send_input returned False"

event = captured["event"]
assert captured["event_type"] == "post_send_message"
assert event.traceparent is not None, "traceparent not propagated into the plugin event"
assert event.traceparent.startswith("00-"), event.traceparent

names = [s.name for s in exporter.get_finished_spans()]
assert any("send_message:handoff" in n for n in names), names

print("OK")
"""


def test_send_message_seam_records_span_and_propagates_traceparent() -> None:
    proc = subprocess.run(
        [sys.executable, "-c", _SEAM_PROBE],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"stderr:\n{proc.stderr}\nstdout:\n{proc.stdout}"
    assert "OK" in proc.stdout
