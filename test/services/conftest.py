"""Deterministic Kiro probe fixture for terminal-service unit lifecycles."""

import pytest

from cli_agent_orchestrator.providers.kiro_capabilities import KiroCapabilities


@pytest.fixture(autouse=True)
def mock_kiro_capability_probe(monkeypatch):
    """Keep service tests independent from a locally installed Kiro wrapper."""

    def probe(_engine, _requested):
        return KiroCapabilities(
            version="2.13.0",
            flags=frozenset(
                {
                    "--agent-engine",
                    "--v3",
                    "--agent",
                    "--model",
                    "--legacy-ui",
                    "--trust-all-tools",
                    "--require-mcp-startup",
                }
            ),
        )

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.probe_kiro_capabilities",
        probe,
    )


@pytest.fixture(autouse=True)
def isolated_terminal_log_dir(tmp_path_factory, monkeypatch):
    """Point terminal_service's TERMINAL_LOG_DIR at a per-test directory.

    create_terminal writes an early ``<tid>.snapshot.json`` and the delete path
    a ``.scrollback`` there, so without this every service test that runs them
    writes into the real log directory. terminal_service binds the constant at
    import (``from ..constants import TERMINAL_LOG_DIR``), so its module
    attribute is what has to be repointed: patching ``constants`` would not
    reach it. Tests that patch it themselves still take precedence.

    The directory comes from ``tmp_path_factory`` rather than ``tmp_path`` so
    tests that assert on their own ``tmp_path`` contents do not see it.
    """
    log_dir = tmp_path_factory.mktemp("terminal-logs")
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.TERMINAL_LOG_DIR", log_dir
    )
    return log_dir
