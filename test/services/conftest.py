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
def cleanup_test_snapshots():
    """Remove early-snapshot files created by create_terminal during tests.

    Mirrors the temp-file cleanup pattern in test_claude_code_unit.py: service
    tests run create_terminal with mock ids (test1234, ...) and the early
    snapshot writes to the real TERMINAL_LOG_DIR.
    """
    yield
    from cli_agent_orchestrator.constants import TERMINAL_LOG_DIR

    if TERMINAL_LOG_DIR.exists():
        for f in TERMINAL_LOG_DIR.glob("test*.snapshot.json"):
            f.unlink(missing_ok=True)
