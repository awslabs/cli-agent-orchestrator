"""Shared guards for the ``cao`` CLI test suite."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _never_touch_the_real_plugin_store(tmp_path_factory, monkeypatch):
    """Point the DEFAULT ``InstalledPluginStore`` at a scratch tree.

    CLI code legitimately constructs ``InstalledPluginStore()`` with no arguments
    -- the real store IS its target in production. In a test that means the
    operator's real ``~/.aws/cli-agent-orchestrator/agent-plugins``, and the
    resulting write is silent: an inert file appears and nothing fails.

    Added after the lifecycle lock (review 4 item 1 on #584) created
    ``.state/.lifecycle.lock`` in a live home during a test run.
    """
    root = tmp_path_factory.mktemp("default-plugin-store")
    monkeypatch.setattr(
        "cli_agent_orchestrator.agent_plugins.store.AGENT_PLUGINS_DIR", root / "agent-plugins"
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.agent_plugins.store.AGENT_PLUGIN_DATA_DIR",
        root / "agent-plugin-data",
    )
