"""Tests for skill injection utilities."""

import logging
import os
from pathlib import Path
from unittest.mock import patch

import frontmatter
import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.utils import skill_injection


def _write_agent_md(path: Path, name: str, description: str, body: str) -> None:
    """Write a Copilot .agent.md file with frontmatter."""
    path.parent.mkdir(parents=True, exist_ok=True)
    post = frontmatter.Post(body, name=name, description=description)
    path.write_text(frontmatter.dumps(post), encoding="utf-8")


def _read_agent_md_body(path: Path) -> str:
    """Read the body content of a Copilot .agent.md file."""
    post = frontmatter.load(path)
    return post.content


class TestComposeAgentPrompt:
    """Tests for compose_agent_prompt."""

    @pytest.mark.parametrize("prompt", [None, "", "   ", "\n"])
    @patch("cli_agent_orchestrator.utils.skill_injection.build_skill_catalog", return_value="")
    def test_returns_none_when_prompt_and_catalog_are_empty(self, _mock_catalog, prompt):
        profile = AgentProfile(name="developer", description="Developer", prompt=prompt)

        assert skill_injection.compose_agent_prompt(profile) is None

    @patch("cli_agent_orchestrator.utils.skill_injection.build_skill_catalog", return_value="")
    def test_returns_profile_prompt_when_only_prompt_is_set(self, _mock_catalog):
        profile = AgentProfile(name="developer", description="Developer", prompt="Profile prompt")

        assert skill_injection.compose_agent_prompt(profile) == "Profile prompt"

    @patch(
        "cli_agent_orchestrator.utils.skill_injection.build_skill_catalog",
        return_value="## Available Skills\n\n- **python-testing**: Pytest conventions",
    )
    def test_returns_catalog_when_only_skills_exist(self, _mock_catalog):
        profile = AgentProfile(name="developer", description="Developer")

        assert skill_injection.compose_agent_prompt(profile) == (
            "## Available Skills\n\n- **python-testing**: Pytest conventions"
        )

    @patch(
        "cli_agent_orchestrator.utils.skill_injection.build_skill_catalog",
        return_value="## Available Skills\n\n- **python-testing**: Pytest conventions",
    )
    def test_joins_prompt_and_catalog_with_blank_line(self, _mock_catalog):
        profile = AgentProfile(name="developer", description="Developer", prompt="Profile prompt")

        assert skill_injection.compose_agent_prompt(profile) == (
            "Profile prompt\n\n## Available Skills\n\n- **python-testing**: Pytest conventions"
        )

    @patch("cli_agent_orchestrator.utils.skill_injection.build_skill_catalog", return_value="")
    def test_uses_base_prompt_when_provided(self, _mock_catalog):
        profile = AgentProfile(
            name="developer", description="Developer", prompt="Should be ignored"
        )

        result = skill_injection.compose_agent_prompt(profile, base_prompt="Custom base")
        assert result == "Custom base"

    @patch(
        "cli_agent_orchestrator.utils.skill_injection.build_skill_catalog",
        return_value="## Available Skills",
    )
    def test_joins_base_prompt_and_catalog(self, _mock_catalog):
        profile = AgentProfile(name="developer", description="Developer", prompt="Ignored")

        result = skill_injection.compose_agent_prompt(profile, base_prompt="Custom base")
        assert result == "Custom base\n\n## Available Skills"

    @patch("cli_agent_orchestrator.utils.skill_injection.build_skill_catalog", return_value="")
    def test_base_prompt_empty_string_returns_none(self, _mock_catalog):
        profile = AgentProfile(name="developer", description="Developer", prompt="Has content")

        assert skill_injection.compose_agent_prompt(profile, base_prompt="   ") is None


class TestRefreshAgentMdPrompt:
    """Tests for refresh_agent_md_prompt (Copilot .agent.md files)."""

    def test_returns_false_when_md_path_is_missing(self, tmp_path):
        missing_path = tmp_path / "missing.agent.md"
        profile = AgentProfile(name="developer", description="Developer", prompt="Prompt")

        assert skill_injection.refresh_agent_md_prompt(missing_path, profile) is False
        assert not missing_path.exists()

    @patch(
        "cli_agent_orchestrator.utils.skill_injection.build_skill_catalog",
        return_value="## Available Skills",
    )
    def test_rewrites_body_preserving_frontmatter(self, _mock_catalog, tmp_path):
        md_path = tmp_path / "developer.agent.md"
        _write_agent_md(md_path, "developer", "Developer agent", "Old prompt body")

        profile = AgentProfile(
            name="developer", description="Developer", system_prompt="New system prompt"
        )

        assert skill_injection.refresh_agent_md_prompt(md_path, profile) is True

        post = frontmatter.load(md_path)
        assert post.metadata["name"] == "developer"
        assert post.metadata["description"] == "Developer agent"
        assert post.content == "New system prompt\n\n## Available Skills"

    @patch("cli_agent_orchestrator.utils.skill_injection.build_skill_catalog", return_value="")
    def test_uses_system_prompt_over_profile_prompt(self, _mock_catalog, tmp_path):
        md_path = tmp_path / "developer.agent.md"
        _write_agent_md(md_path, "developer", "Developer", "Old body")

        profile = AgentProfile(
            name="developer",
            description="Developer",
            system_prompt="System prompt wins",
            prompt="Fallback prompt",
        )

        skill_injection.refresh_agent_md_prompt(md_path, profile)
        assert _read_agent_md_body(md_path) == "System prompt wins"

    @patch("cli_agent_orchestrator.utils.skill_injection.build_skill_catalog", return_value="")
    def test_falls_back_to_profile_prompt_when_no_system_prompt(self, _mock_catalog, tmp_path):
        md_path = tmp_path / "developer.agent.md"
        _write_agent_md(md_path, "developer", "Developer", "Old body")

        profile = AgentProfile(name="developer", description="Developer", prompt="Fallback prompt")

        skill_injection.refresh_agent_md_prompt(md_path, profile)
        assert _read_agent_md_body(md_path) == "Fallback prompt"

    @patch(
        "cli_agent_orchestrator.utils.skill_injection.build_skill_catalog",
        return_value="## Skills",
    )
    def test_writes_atomically_with_os_replace(self, _mock_catalog, tmp_path):
        md_path = tmp_path / "developer.agent.md"
        _write_agent_md(md_path, "developer", "Developer", "Body")
        temp_path = md_path.with_suffix(".md.tmp")

        profile = AgentProfile(name="developer", description="Developer", prompt="Prompt")

        with patch(
            "cli_agent_orchestrator.utils.skill_injection.os.replace", wraps=os.replace
        ) as mock_replace:
            skill_injection.refresh_agent_md_prompt(md_path, profile)

        mock_replace.assert_called_once_with(temp_path, md_path)
        assert not temp_path.exists()

    @patch(
        "cli_agent_orchestrator.utils.skill_injection.build_skill_catalog",
        return_value="## Skills",
    )
    def test_is_idempotent_for_same_prompt(self, _mock_catalog, tmp_path):
        md_path = tmp_path / "developer.agent.md"
        _write_agent_md(md_path, "developer", "Developer", "Body")
        profile = AgentProfile(name="developer", description="Developer", prompt="Prompt")

        skill_injection.refresh_agent_md_prompt(md_path, profile)
        first_bytes = md_path.read_bytes()

        skill_injection.refresh_agent_md_prompt(md_path, profile)
        second_bytes = md_path.read_bytes()

        assert first_bytes == second_bytes


class TestRefreshInstalledAgentForProfile:
    """Tests for refresh_installed_agent_for_profile."""

    def test_returns_copilot_path_when_copilot_agent_exists(self, tmp_path, monkeypatch):
        copilot_dir = tmp_path / "copilot"
        copilot_path = copilot_dir / "team__developer.agent.md"
        _write_agent_md(copilot_path, "team/developer", "Developer", "Old prompt")

        monkeypatch.setattr(skill_injection, "COPILOT_AGENTS_DIR", copilot_dir)
        monkeypatch.setattr(
            skill_injection,
            "load_agent_profile",
            lambda name: AgentProfile(
                name="team/developer", description="Developer", prompt="Prompt"
            ),
        )
        monkeypatch.setattr(skill_injection, "build_skill_catalog", lambda: "")

        assert skill_injection.refresh_installed_agent_for_profile("team-developer") == [
            copilot_path
        ]

    def test_returns_empty_list_when_no_installed_agents_exist(self, tmp_path, monkeypatch):
        monkeypatch.setattr(skill_injection, "COPILOT_AGENTS_DIR", tmp_path / "copilot")
        monkeypatch.setattr(
            skill_injection,
            "load_agent_profile",
            lambda name: AgentProfile(
                name="team/developer", description="Developer", prompt="Prompt"
            ),
        )
        monkeypatch.setattr(skill_injection, "build_skill_catalog", lambda: "")

        assert skill_injection.refresh_installed_agent_for_profile("team-developer") == []


class TestRefreshAllCaoManagedAgents:
    """Tests for refresh_all_cao_managed_agents."""

    def test_refreshes_copilot_agent_with_matching_context_file(self, tmp_path, monkeypatch):
        context_dir = tmp_path / "agent-context"
        copilot_dir = tmp_path / "copilot"

        # Create context file (marks this as CAO-managed)
        context_file = context_dir / "developer.md"
        context_file.parent.mkdir(parents=True, exist_ok=True)
        context_file.write_text("context", encoding="utf-8")

        # Create Copilot agent
        copilot_path = copilot_dir / "developer.agent.md"
        _write_agent_md(copilot_path, "developer", "Developer", "Old prompt")

        monkeypatch.setattr(skill_injection, "AGENT_CONTEXT_DIR", context_dir)
        monkeypatch.setattr(skill_injection, "COPILOT_AGENTS_DIR", copilot_dir)
        monkeypatch.setattr(
            skill_injection,
            "load_agent_profile",
            lambda name: AgentProfile(name="developer", description="Developer", prompt="Prompt"),
        )
        monkeypatch.setattr(skill_injection, "build_skill_catalog", lambda: "## Available Skills")

        refreshed = skill_injection.refresh_all_cao_managed_agents()

        assert refreshed == [copilot_path]
        assert _read_agent_md_body(copilot_path) == "Prompt\n\n## Available Skills"

    def test_skips_copilot_agent_without_context_file(self, tmp_path, monkeypatch):
        context_dir = tmp_path / "agent-context"
        context_dir.mkdir(parents=True, exist_ok=True)
        copilot_dir = tmp_path / "copilot"

        # No context file for this agent — not CAO-managed
        copilot_path = copilot_dir / "external.agent.md"
        _write_agent_md(copilot_path, "external", "External agent", "External prompt")
        original_bytes = copilot_path.read_bytes()

        monkeypatch.setattr(skill_injection, "AGENT_CONTEXT_DIR", context_dir)
        monkeypatch.setattr(skill_injection, "COPILOT_AGENTS_DIR", copilot_dir)

        assert skill_injection.refresh_all_cao_managed_agents() == []
        assert copilot_path.read_bytes() == original_bytes

    @pytest.mark.parametrize(
        "load_error",
        [
            FileNotFoundError("profile missing"),
            RuntimeError("profile missing"),
            ValueError("invalid profile name"),
        ],
    )
    def test_logs_warning_and_continues_when_source_profile_load_fails(
        self, tmp_path, monkeypatch, caplog, load_error
    ):
        context_dir = tmp_path / "agent-context"
        copilot_dir = tmp_path / "copilot"
        good_context = context_dir / "good.md"
        missing_context = context_dir / "missing.md"
        good_context.parent.mkdir(parents=True, exist_ok=True)
        good_context.write_text("good", encoding="utf-8")
        missing_context.write_text("missing", encoding="utf-8")

        good_path = copilot_dir / "good.agent.md"
        missing_path = copilot_dir / "missing.agent.md"
        _write_agent_md(good_path, "good", "Good agent", "Old prompt")
        _write_agent_md(missing_path, "missing", "Missing agent", "Old prompt")
        missing_original = missing_path.read_bytes()

        def load_profile(name: str) -> AgentProfile:
            if name == "good":
                return AgentProfile(name="good", description="Good agent", prompt="Prompt")
            raise load_error

        monkeypatch.setattr(skill_injection, "AGENT_CONTEXT_DIR", context_dir)
        monkeypatch.setattr(skill_injection, "COPILOT_AGENTS_DIR", copilot_dir)
        monkeypatch.setattr(skill_injection, "load_agent_profile", load_profile)
        monkeypatch.setattr(skill_injection, "build_skill_catalog", lambda: "## Available Skills")

        with caplog.at_level(logging.WARNING):
            refreshed = skill_injection.refresh_all_cao_managed_agents()

        assert refreshed == [good_path]
        assert "source profile could not be loaded" in caplog.text
        assert "missing" in caplog.text
        assert missing_path.read_bytes() == missing_original

    def test_refreshes_only_cao_managed_copilot_agents(self, tmp_path, monkeypatch):
        context_dir = tmp_path / "agent-context"
        copilot_dir = tmp_path / "copilot"

        # Create context file for the managed agent only
        ctx = context_dir / "managed-copilot.md"
        ctx.parent.mkdir(parents=True, exist_ok=True)
        ctx.write_text("managed-copilot", encoding="utf-8")

        managed_copilot = copilot_dir / "managed-copilot.agent.md"
        unmanaged_copilot = copilot_dir / "unmanaged-copilot.agent.md"
        _write_agent_md(managed_copilot, "managed-copilot", "Managed Copilot", "Old prompt")
        _write_agent_md(
            unmanaged_copilot, "unmanaged-copilot", "Unmanaged Copilot", "External prompt"
        )
        unmanaged_original = unmanaged_copilot.read_bytes()

        def load_profile(name: str) -> AgentProfile:
            return AgentProfile(
                name=name, description=f"{name} description", prompt=f"{name} prompt"
            )

        monkeypatch.setattr(skill_injection, "AGENT_CONTEXT_DIR", context_dir)
        monkeypatch.setattr(skill_injection, "COPILOT_AGENTS_DIR", copilot_dir)
        monkeypatch.setattr(skill_injection, "load_agent_profile", load_profile)
        monkeypatch.setattr(skill_injection, "build_skill_catalog", lambda: "## Available Skills")

        refreshed = skill_injection.refresh_all_cao_managed_agents()

        assert refreshed == [managed_copilot]
        assert (
            _read_agent_md_body(managed_copilot) == "managed-copilot prompt\n\n## Available Skills"
        )
        assert unmanaged_copilot.read_bytes() == unmanaged_original
