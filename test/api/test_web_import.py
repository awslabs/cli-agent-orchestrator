"""Test that web.py uses beads_real module."""


def test_web_imports_beads_real():
    """Verify web.py imports BeadsClient from beads_real, not beads."""
    from cli_agent_orchestrator.api import web
    assert web.BeadsClient.__module__ == "cli_agent_orchestrator.clients.beads_real"


def test_web_imports_task_from_beads_real():
    """Verify web.py imports Task from beads_real."""
    from cli_agent_orchestrator.api import web
    assert web.Task.__module__ == "cli_agent_orchestrator.clients.beads_real"
