"""Coverage for the OpenTelemetry failure-isolation branches in the app lifespan.

``init_telemetry`` / ``shutdown_telemetry`` are called unconditionally during the
FastAPI ``lifespan`` (safe: no-ops unless ``OTEL_SDK_DISABLED=false``) and are
wrapped in ``try/except`` so a telemetry backend hiccup never blocks server boot
or shutdown. Ordinary TestClient tests never trip these handlers because the
no-op telemetry cannot raise, so this module forces each to raise and drives the
lifespan to prove the errors are swallowed (the app still boots and shuts down).
"""

from __future__ import annotations

import logging

from fastapi.testclient import TestClient

import cli_agent_orchestrator.api.main as main


def test_otel_init_failure_is_isolated(monkeypatch, caplog):
    """A raising ``init_telemetry`` is caught during startup; boot continues."""

    def _boom(_service_name):
        raise RuntimeError("otel init boom")

    monkeypatch.setattr(main, "init_telemetry", _boom)

    with caplog.at_level(logging.WARNING):
        # Entering the context runs lifespan startup (which calls init_telemetry).
        with TestClient(main.app, base_url="http://localhost") as client:
            # App still serves despite the telemetry init failure.
            assert client.get("/health").status_code == 200

    assert any("OTel telemetry init failed" in r.message for r in caplog.records)


def test_otel_shutdown_failure_is_isolated(monkeypatch, caplog):
    """A raising ``shutdown_telemetry`` is caught during shutdown; teardown continues."""

    def _boom():
        raise RuntimeError("otel shutdown boom")

    monkeypatch.setattr(main, "shutdown_telemetry", _boom)

    with caplog.at_level(logging.WARNING):
        # Exiting the context runs lifespan shutdown (which calls shutdown_telemetry).
        with TestClient(main.app, base_url="http://localhost") as client:
            assert client.get("/health").status_code == 200

    assert any("Error shutting down OTel telemetry" in r.message for r in caplog.records)
