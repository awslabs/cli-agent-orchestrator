"""Tests for the install service."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import frontmatter
import pytest
import requests  # type: ignore[import-untyped]

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.services.install_service import InstallResult, install_agent
from cli_agent_orchestrator.utils.skill_injection import refresh_agent_json_prompt


def _profile_text(*, name: str, include_prompt: bool = True) -> str:
    """Build a profile fixture with env placeholders in prompt and MCP config."""
    prompt_lines = "Fallback prompt\n" if include_prompt else ""
    return (
        "---\n"
        f"name: {name}\n"
        "description: Test agent\n"
        "role: developer\n"
        "mcpServers:\n"
        "  service:\n"
        "    command: service-mcp\n"
        "    env:\n"
        "      API_TOKEN: ${API_TOKEN}\n"
        "      BASE_URL: ${BASE_URL}\n"
        f"prompt: |\n  {prompt_lines}"
        "---\n"
        "Use the service at ${BASE_URL} with token ${API_TOKEN}.\n"
    )


@pytest.fixture
def install_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    """Patch install-related filesystem paths into a temp workspace."""
    local_store_dir = tmp_path / "agent-store"
    context_dir = tmp_path / "agent-context"
    kiro_dir = tmp_path / "kiro"
    q_dir = tmp_path / "q"
    copilot_dir = tmp_path / "copilot"
    provider_dir = tmp_path / "provider"
    extra_dir = tmp_path / "extra"
    env_file = tmp_path / ".env"

    for path in (
        local_store_dir,
        context_dir,
        kiro_dir,
        q_dir,
        copilot_dir,
        provider_dir,
        extra_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.install_service.LOCAL_AGENT_STORE_DIR",
        local_store_dir,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR",
        local_store_dir,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.install_service.AGENT_CONTEXT_DIR",
        context_dir,
    )
    monkeypatch.setattr("cli_agent_orchestrator.services.install_service.KIRO_AGENTS_DIR", kiro_dir)
    monkeypatch.setattr("cli_agent_orchestrator.services.install_service.Q_AGENTS_DIR", q_dir)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.install_service.COPILOT_AGENTS_DIR",
        copilot_dir,
    )
    monkeypatch.setattr("cli_agent_orchestrator.utils.env.CAO_ENV_FILE", env_file)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_agent_dirs",
        lambda: {"kiro_cli": str(provider_dir)},
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs",
        lambda: [str(extra_dir)],
    )

    return {
        "local_store_dir": local_store_dir,
        "context_dir": context_dir,
        "kiro_dir": kiro_dir,
        "q_dir": q_dir,
        "copilot_dir": copilot_dir,
        "provider_dir": provider_dir,
        "extra_dir": extra_dir,
        "env_file": env_file,
    }


class TestInstallAgent:
    """Tests for install_service.install_agent."""

    def test_install_from_name_uses_provider_dirs_and_writes_context_only(
        self, install_paths: dict[str, Path]
    ) -> None:
        """Bare profile names should resolve from configured provider directories."""
        provider_profile = install_paths["provider_dir"] / "service-agent" / "agent.md"
        provider_profile.parent.mkdir(parents=True, exist_ok=True)
        provider_profile.write_text(_profile_text(name="service-agent"), encoding="utf-8")

        result = install_agent("service-agent", "claude_code")

        assert result.success is True
        assert result.agent_name == "service-agent"
        assert result.agent_file is None
        assert result.unresolved_vars == ["API_TOKEN", "BASE_URL"]
        context_text = (install_paths["context_dir"] / "service-agent.md").read_text(
            encoding="utf-8"
        )
        assert "${API_TOKEN}" in context_text
        assert "${BASE_URL}" in context_text

    def test_install_from_url_downloads_and_writes_q_config(
        self, install_paths: dict[str, Path]
    ) -> None:
        """URL sources should be downloaded into the local store and installed for Q CLI."""
        mock_response = MagicMock()
        mock_response.text = _profile_text(name="downloaded-agent")
        mock_response.raise_for_status.return_value = None

        with patch(
            "cli_agent_orchestrator.services.install_service.requests.get",
            return_value=mock_response,
        ) as mock_get:
            result = install_agent(
                "https://example.com/downloaded-agent.md",
                "q_cli",
                {"API_TOKEN": "secret-token"},
            )

        assert result.success is True
        assert result.agent_name == "downloaded-agent"
        assert result.unresolved_vars == ["BASE_URL"]
        mock_get.assert_called_once_with("https://example.com/downloaded-agent.md")
        assert (install_paths["local_store_dir"] / "downloaded-agent.md").exists()

        q_config = json.loads((install_paths["q_dir"] / "downloaded-agent.json").read_text())
        assert q_config["mcpServers"]["service"]["env"]["API_TOKEN"] == "secret-token"
        assert q_config["mcpServers"]["service"]["env"]["BASE_URL"] == "${BASE_URL}"

    def test_install_from_path_copies_profile_and_writes_copilot_config(
        self, install_paths: dict[str, Path], tmp_path: Path
    ) -> None:
        """File path sources should be copied to local store and converted for Copilot."""
        source_profile = tmp_path / "copilot-agent.md"
        source_profile.write_text(_profile_text(name="copilot-agent"), encoding="utf-8")

        result = install_agent(str(source_profile), "copilot_cli", {"API_TOKEN": "secret-token"})

        assert result.success is True
        assert (install_paths["local_store_dir"] / "copilot-agent.md").exists()
        agent_file = install_paths["copilot_dir"] / "copilot-agent.agent.md"
        assert agent_file.exists()
        post = frontmatter.loads(agent_file.read_text(encoding="utf-8"))
        assert post.metadata["name"] == "copilot-agent"
        assert post.metadata["description"] == "Test agent"
        assert "secret-token" in post.content

    def test_install_from_builtin_writes_kiro_config(
        self, install_paths: dict[str, Path], tmp_path: Path
    ) -> None:
        """Built-in profiles should install correctly for Kiro CLI."""
        built_in_dir = tmp_path / "builtin-agent-store"
        built_in_dir.mkdir()
        (built_in_dir / "developer.md").write_text(
            _profile_text(name="developer"), encoding="utf-8"
        )

        with patch(
            "cli_agent_orchestrator.services.install_service.resources.files",
            return_value=built_in_dir,
        ):
            result = install_agent("developer", "kiro_cli", {"API_TOKEN": "secret-token"})

        assert result.success is True
        kiro_config = json.loads((install_paths["kiro_dir"] / "developer.json").read_text())
        assert kiro_config["name"] == "developer"
        assert kiro_config["mcpServers"]["service"]["env"]["API_TOKEN"] == "secret-token"

    def test_install_sets_env_vars_before_profile_loading(
        self, install_paths: dict[str, Path]
    ) -> None:
        """Env vars should be persisted before profile parsing begins."""
        local_profile = install_paths["local_store_dir"] / "developer.md"
        local_profile.write_text(_profile_text(name="developer"), encoding="utf-8")

        call_order: list[str] = []

        def track_set_env_var(key: str, value: str) -> None:
            call_order.append(f"set:{key}")

        def track_parse_agent_profile_text(resolved_text: str, profile_name: str):
            call_order.append(f"parse:{profile_name}")
            from cli_agent_orchestrator.utils.agent_profiles import parse_agent_profile_text

            return parse_agent_profile_text(resolved_text, profile_name)

        with (
            patch(
                "cli_agent_orchestrator.services.install_service.set_env_var",
                side_effect=track_set_env_var,
            ),
            patch(
                "cli_agent_orchestrator.services.install_service.parse_agent_profile_text",
                side_effect=track_parse_agent_profile_text,
            ),
        ):
            result = install_agent("developer", "claude_code", {"API_TOKEN": "secret-token"})

        assert result.success is True
        assert call_order == ["set:API_TOKEN", "parse:developer"]

    def test_install_returns_failure_for_invalid_source(
        self, install_paths: dict[str, Path]
    ) -> None:
        """Missing sources should be returned as structured failures."""
        result = install_agent("missing-agent", "kiro_cli")

        assert result == InstallResult(
            success=False, message="Agent profile not found: missing-agent"
        )

    def test_install_returns_failure_for_download_errors(
        self, install_paths: dict[str, Path]
    ) -> None:
        """Request failures should be returned as structured download errors."""
        with patch(
            "cli_agent_orchestrator.services.install_service.requests.get",
            side_effect=requests.RequestException("boom"),
        ):
            result = install_agent("https://example.com/missing-agent.md", "q_cli")

        assert result.success is False
        assert result.message == "Failed to download agent: boom"

    def test_install_returns_failure_when_copilot_prompt_missing(
        self, install_paths: dict[str, Path], tmp_path: Path
    ) -> None:
        """Copilot installs should fail when both system_prompt and prompt are empty."""
        source_profile = tmp_path / "empty-copilot.md"
        source_profile.write_text(
            "---\nname: empty-copilot\ndescription: Test agent\nprompt: '   '\n---\n   \n",
            encoding="utf-8",
        )

        result = install_agent(str(source_profile), "copilot_cli")

        assert result.success is False
        assert "has no usable prompt content for Copilot" in result.message

    def test_install_rejects_url_without_md_suffix(self, install_paths: dict[str, Path]) -> None:
        """URL sources must point at a .md file."""
        mock_response = MagicMock()
        mock_response.text = "not a profile"
        mock_response.raise_for_status.return_value = None

        with patch(
            "cli_agent_orchestrator.services.install_service.requests.get",
            return_value=mock_response,
        ):
            result = install_agent("https://example.com/agent.txt", "kiro_cli")

        assert result.success is False
        assert result.message == "Failed to install agent: URL must point to a .md file"

    def test_install_rejects_file_path_without_md_suffix(
        self, install_paths: dict[str, Path], tmp_path: Path
    ) -> None:
        """File path sources must end in .md."""
        source_file = tmp_path / "agent.txt"
        source_file.write_text("not a profile", encoding="utf-8")

        result = install_agent(str(source_file), "kiro_cli")

        assert result.success is False
        assert result.message == "Failed to install agent: File must be a .md file"


def _create_skill(folder: Path, name: str, description: str, body: str = "# Skill\n\nBody") -> None:
    """Create a skill folder with SKILL.md for catalog-baking tests."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        "---\n" f"name: {name}\n" f"description: {description}\n" "---\n\n" f"{body}\n"
    )


class TestInstallSkillCatalogBaking:
    """Tests for skill catalog injection during install_agent."""

    @pytest.fixture
    def install_workspace(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
        """Patch install and skills paths into a temp workspace."""
        local_store_dir = tmp_path / "agent-store"
        context_dir = tmp_path / "agent-context"
        kiro_dir = tmp_path / "kiro"
        q_dir = tmp_path / "q"
        skills_dir = tmp_path / "skills"

        for d in (local_store_dir, context_dir, kiro_dir, q_dir, skills_dir):
            d.mkdir()

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.install_service.LOCAL_AGENT_STORE_DIR",
            local_store_dir,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR",
            local_store_dir,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.install_service.AGENT_CONTEXT_DIR", context_dir
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.install_service.KIRO_AGENTS_DIR", kiro_dir
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.install_service.Q_AGENTS_DIR", q_dir
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.install_service.SKILLS_DIR", skills_dir
        )
        monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", skills_dir)
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_agent_dirs", lambda: {}
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", lambda: []
        )

        return {
            "local_store_dir": local_store_dir,
            "context_dir": context_dir,
            "kiro_dir": kiro_dir,
            "q_dir": q_dir,
            "skills_dir": skills_dir,
        }

    @staticmethod
    def _write_profile(profile_path: Path, frontmatter_body: str, system_prompt: str) -> None:
        profile_path.write_text(
            f"---\n{frontmatter_body}---\n{system_prompt}\n", encoding="utf-8"
        )

    def test_install_kiro_uses_skill_resources_not_baked_prompt(
        self, install_workspace: dict
    ) -> None:
        """Kiro installs should use skill:// glob in resources instead of baking catalog."""
        _create_skill(
            install_workspace["skills_dir"] / "python-testing",
            "python-testing",
            "Pytest conventions",
        )
        self._write_profile(
            install_workspace["local_store_dir"] / "test-agent.md",
            "name: test-agent\ndescription: Test agent\nprompt: Build things\n",
            "System prompt",
        )

        result = install_agent("test-agent", "kiro_cli")

        assert result.success is True
        agent_json = json.loads(
            (install_workspace["kiro_dir"] / "test-agent.json").read_text()
        )
        assert agent_json["prompt"] == "Build things"
        assert "Available Skills" not in agent_json["prompt"]
        skill_resources = [r for r in agent_json["resources"] if r.startswith("skill://")]
        assert len(skill_resources) == 1
        assert skill_resources[0].endswith("/**/SKILL.md")

    def test_install_q_bakes_catalog_into_prompt(self, install_workspace: dict) -> None:
        """Q installs should bake the global skill catalog into the JSON prompt."""
        _create_skill(
            install_workspace["skills_dir"] / "python-testing",
            "python-testing",
            "Pytest conventions",
        )
        self._write_profile(
            install_workspace["local_store_dir"] / "test-agent.md",
            "name: test-agent\ndescription: Test agent\nprompt: Build things\n",
            "System prompt",
        )

        result = install_agent("test-agent", "q_cli")

        assert result.success is True
        agent_json = json.loads(
            (install_workspace["q_dir"] / "test-agent.json").read_text()
        )
        assert agent_json["prompt"].startswith("Build things\n\n## Available Skills")
        assert "python-testing" in agent_json["prompt"]

    def test_install_kiro_omits_prompt_field_when_profile_prompt_is_empty(
        self, install_workspace: dict
    ) -> None:
        """Empty profile prompt should omit prompt field; skill:// glob still in resources."""
        self._write_profile(
            install_workspace["local_store_dir"] / "test-agent.md",
            "name: test-agent\ndescription: Test agent\n",
            "System prompt",
        )

        result = install_agent("test-agent", "kiro_cli")

        assert result.success is True
        agent_path = install_workspace["kiro_dir"] / "test-agent.json"
        agent_json = json.loads(agent_path.read_text())
        assert "prompt" not in agent_json
        skill_resources = [r for r in agent_json["resources"] if r.startswith("skill://")]
        assert len(skill_resources) == 1

    def test_install_non_ascii_prompt_round_trips_through_refresh_without_byte_drift(
        self, install_workspace: dict
    ) -> None:
        """Non-ASCII prompt content should survive install and refresh with byte-identical JSON."""
        _create_skill(
            install_workspace["skills_dir"] / "unicode-skill",
            "unicode-skill",
            "Unicode skill",
        )
        self._write_profile(
            install_workspace["local_store_dir"] / "unicode-agent.md",
            "name: unicode-agent\ndescription: Test agent\nprompt: こんにちは 🚀\n",
            "System prompt",
        )

        result = install_agent("unicode-agent", "q_cli")

        assert result.success is True
        agent_path = install_workspace["q_dir"] / "unicode-agent.json"
        before_refresh = agent_path.read_bytes()

        refreshed = refresh_agent_json_prompt(
            agent_path,
            AgentProfile(name="unicode-agent", description="Test agent", prompt="こんにちは 🚀"),
        )

        assert refreshed is True
        assert agent_path.read_bytes() == before_refresh
