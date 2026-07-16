"""Tests for the OTel SDK initialization gate.

Telemetry must be opt-in: ``init_telemetry`` should be a no-op unless
``OTEL_SDK_DISABLED`` is explicitly ``"false"``. This protects the existing
1571-test suite from accidentally booting an exporter (and potentially
blocking on a missing collector) just because the OTel packages happen to be
importable.

The "enabled" path is verified via mocks rather than actually installing a
global TracerProvider, because OTel only allows the global provider to be set
once per process and we don't want this test to interfere with other test
modules that install their own.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.telemetry import init_telemetry
from cli_agent_orchestrator.telemetry import otel as otel_module
from cli_agent_orchestrator.telemetry import shutdown_telemetry


@pytest.fixture(autouse=True)
def _reset_provider():
    """Ensure each test starts with no installed provider."""
    otel_module._provider = None
    yield
    otel_module._provider = None


class TestTelemetryDisabledByDefault:
    """The default behaviour must be no SDK installation, no exporter created."""

    def test_unset_env_is_noop(self):
        env = os.environ.copy()
        env.pop("OTEL_SDK_DISABLED", None)
        with patch.dict("os.environ", env, clear=True):
            init_telemetry("cao")
        assert otel_module._provider is None

    @pytest.mark.parametrize("value", ["true", "TRUE", "1", "", "yes"])
    def test_anything_other_than_explicit_false_is_noop(self, value: str):
        with patch.dict("os.environ", {"OTEL_SDK_DISABLED": value}):
            init_telemetry("cao")
        assert otel_module._provider is None

    def test_shutdown_with_no_provider_is_noop(self):
        # Must not raise even when init was never called.
        shutdown_telemetry()
        assert otel_module._provider is None


class TestTelemetryEnabledExplicitly:
    """``OTEL_SDK_DISABLED=false`` installs a provider exactly once.

    These tests intercept the OTel global setter so they don't pollute the
    process-wide TracerProvider that other test modules rely on.
    """

    def test_enabled_installs_provider(self):
        with (
            patch.dict("os.environ", {"OTEL_SDK_DISABLED": "false"}),
            patch("opentelemetry.trace.set_tracer_provider") as mock_set,
        ):
            init_telemetry("cao")
            assert mock_set.called
            assert otel_module._provider is not None

    def test_double_init_does_not_replace_provider(self):
        with (
            patch.dict("os.environ", {"OTEL_SDK_DISABLED": "false"}),
            patch("opentelemetry.trace.set_tracer_provider") as mock_set,
        ):
            init_telemetry("cao")
            first = otel_module._provider
            init_telemetry("cao")
            assert otel_module._provider is first
            # Provider was set once on first init, not again on second.
            assert mock_set.call_count == 1

    def test_shutdown_clears_module_handle(self):
        with (
            patch.dict("os.environ", {"OTEL_SDK_DISABLED": "false"}),
            patch("opentelemetry.trace.set_tracer_provider"),
        ):
            init_telemetry("cao")
            assert otel_module._provider is not None
            shutdown_telemetry()
            assert otel_module._provider is None


class TestPackageVersionFallback:
    def test_falls_back_to_zero_when_package_missing(self):
        from importlib import metadata

        with patch.object(
            otel_module.metadata, "version", side_effect=metadata.PackageNotFoundError
        ):
            assert otel_module._package_version() == "0.0.0"


class TestInitDegradesWhenSdkMissing:
    def test_enabled_but_sdk_import_fails_is_logged_noop(self, monkeypatch):
        import sys

        monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
        otel_module._provider = None
        # Force the deferred SDK/exporter import to fail while opentelemetry-api
        # stays importable — exercises the degrade-to-logged-no-op branch.
        with patch.dict(
            sys.modules,
            {"opentelemetry.exporter.otlp.proto.grpc.trace_exporter": None},
        ):
            init_telemetry("cao-test")  # must not raise
        assert otel_module._provider is None  # no provider installed on degrade
