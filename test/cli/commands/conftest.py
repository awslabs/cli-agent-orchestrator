"""Shared fixtures for the ``cao install`` CLI tests.

``workspace`` redirects every install destination -- local store, shared context
directory, and the OpenCode, Kiro and Copilot agent directories -- into
``tmp_path`` while keeping the default ``cao_installed`` mapping, so discovery,
the ownership guard and the context writer all agree on one directory exactly
as in production. The plain helpers that drive installs against it live in
``install_helpers.py``; keeping fixtures here and helpers there means no test
module imports another test module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest
from click.testing import CliRunner


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """Redirect install paths to tmp while keeping the default cao_installed mapping.

    Crucially ``get_agent_dirs`` returns ``{"cao_installed": <context_dir>}`` —
    the same directory ``_write_context_file`` writes to — so discovery scans
    the installed copies exactly as it does in production. ``AGENT_CONTEXT_DIR``
    is pointed at that same dir so the provenance read and the context write
    agree.
    """
    local_store = tmp_path / "agent-store"
    context_dir = tmp_path / "agent-context"
    opencode_agents = tmp_path / "opencode_cli" / "agents"
    opencode_config = tmp_path / "opencode_cli" / "opencode.json"
    kiro_agents = tmp_path / "kiro" / "agents"
    copilot_agents = tmp_path / "copilot" / "agents"

    local_store.mkdir(parents=True)
    context_dir.mkdir(parents=True)

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.profile_store.LOCAL_AGENT_STORE_DIR", local_store
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR", local_store
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.install_service.AGENT_CONTEXT_DIR", context_dir
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.install_service.OPENCODE_AGENTS_DIR", opencode_agents
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.install_service.KIRO_AGENTS_DIR", kiro_agents
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.install_service.COPILOT_AGENTS_DIR", copilot_agents
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.utils.opencode_config.OPENCODE_CONFIG_FILE", opencode_config
    )
    # DEFAULT mapping preserved (NOT {}): cao_installed points at the context dir.
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_agent_dirs",
        lambda: {"cao_installed": str(context_dir)},
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", lambda: []
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_disabled_agent_dirs", lambda: []
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.install_service.ensure_skills_symlink", lambda: None
    )

    return {
        "local_store": local_store,
        "context_dir": context_dir,
        "agents_dir": opencode_agents,
        "config_file": opencode_config,
        "kiro_agents_dir": kiro_agents,
        "copilot_agents_dir": copilot_agents,
    }
