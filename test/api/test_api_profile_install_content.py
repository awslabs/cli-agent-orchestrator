"""POST /agents/profiles/install with ``content``: the web UI's install-from-file.

The browser reads the ``.md`` file and sends its text, so the server never
sees a path. These tests pin that the endpoint forwards the text as the
CLI's ``profile_content`` import, that ``source`` must then be a bare name,
and that a real install writes the store copy only when it is accepted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.services import install_service
from cli_agent_orchestrator.services.install_service import InstallResult

PROFILE = "---\nname: file_agent\ndescription: Installed from a file\n---\nDo the task.\n"


@pytest.fixture()
def install_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """Redirect every install destination into tmp_path.

    A compact copy of ``test/cli/commands/conftest.py::workspace`` (fixtures
    are not shared across test packages here): the local store, the shared
    context dir that discovery scans, and the provider agent dirs.
    """
    local_store = tmp_path / "agent-store"
    context_dir = tmp_path / "agent-context"
    local_store.mkdir(parents=True)
    context_dir.mkdir(parents=True)
    for target in (
        "cli_agent_orchestrator.services.profile_store.LOCAL_AGENT_STORE_DIR",
        "cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR",
    ):
        monkeypatch.setattr(target, local_store)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.install_service.AGENT_CONTEXT_DIR", context_dir
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.install_service.KIRO_AGENTS_DIR",
        tmp_path / "kiro" / "agents",
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.install_service.OPENCODE_AGENTS_DIR",
        tmp_path / "opencode_cli" / "agents",
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.install_service.COPILOT_AGENTS_DIR",
        tmp_path / "copilot" / "agents",
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.utils.opencode_config.OPENCODE_CONFIG_FILE",
        tmp_path / "opencode_cli" / "opencode.json",
    )
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
    monkeypatch.setattr(install_service, "load_env_vars", lambda: {})
    return {"local_store": local_store, "context_dir": context_dir}


class TestInstallWithContentForwarding:
    def test_content_is_forwarded_as_profile_content(self, client) -> None:
        ok = InstallResult(success=True, message="ok", agent_name="file_agent")
        with patch("cli_agent_orchestrator.api.main.install_agent", return_value=ok) as mock:
            response = client.post(
                "/agents/profiles/install",
                json={"source": "file_agent", "provider": "claude_code", "content": PROFILE},
            )

        assert response.status_code == 200
        mock.assert_called_once_with(
            source="file_agent",
            provider="claude_code",
            env_vars=None,
            profile_content=PROFILE,
        )

    def test_without_content_the_call_is_unchanged(self, client) -> None:
        """No ``profile_content`` kwarg at all, so name and URL installs keep their path."""
        ok = InstallResult(success=True, message="ok", agent_name="developer")
        with patch("cli_agent_orchestrator.api.main.install_agent", return_value=ok) as mock:
            client.post("/agents/profiles/install", json={"source": "developer"})

        mock.assert_called_once_with(source="developer", provider=None, env_vars=None)

    def test_oversize_content_is_rejected_before_the_service(self, client) -> None:
        with patch("cli_agent_orchestrator.api.main.install_agent") as mock:
            response = client.post(
                "/agents/profiles/install",
                json={"source": "big", "content": "x" * 262_145},
            )

        assert response.status_code == 422
        mock.assert_not_called()


class TestInstallWithContentEndToEnd:
    def test_installs_and_stores_the_submitted_text(self, client, install_dirs) -> None:
        response = client.post(
            "/agents/profiles/install",
            json={"source": "file_agent", "provider": "claude_code", "content": PROFILE},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["success"] is True
        assert body["agent_name"] == "file_agent"
        assert body["provider"] == "claude_code"
        stored = (install_dirs["local_store"] / "file_agent.md").read_text(encoding="utf-8")
        assert "Do the task." in stored
        assert (install_dirs["context_dir"] / "file_agent.md").is_file()

    @pytest.mark.parametrize(
        "source",
        ["https://example.com/file_agent.md", "../file_agent", "dir/file_agent", "file_agent.md"],
    )
    def test_non_name_source_is_refused_and_nothing_is_written(
        self, client, install_dirs, source
    ) -> None:
        """With ``content`` set, ``source`` is only ever a store name, never a URL or path."""
        response = client.post(
            "/agents/profiles/install",
            json={"source": source, "content": PROFILE},
        )

        assert response.status_code == 400
        assert "Invalid profile name" in response.json()["detail"]
        assert list(install_dirs["local_store"].iterdir()) == []
        assert list(install_dirs["context_dir"].iterdir()) == []

    def test_refused_import_leaves_the_stored_profile_untouched(self, client, install_dirs) -> None:
        """An unparseable upload must not replace the existing store copy."""
        existing = install_dirs["local_store"] / "file_agent.md"
        existing.write_text(PROFILE, encoding="utf-8")
        before = existing.read_bytes()

        response = client.post(
            "/agents/profiles/install",
            json={"source": "file_agent", "content": "---\nname: [unclosed\n---\nbody\n"},
        )

        assert response.status_code == 400
        assert existing.read_bytes() == before
