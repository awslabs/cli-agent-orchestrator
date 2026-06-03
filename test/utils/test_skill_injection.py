"""Tests for skill injection utilities."""

import json
import logging
import os
from pathlib import Path
from unittest.mock import patch

import frontmatter
import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.utils import skill_injection


def _write_json(path: Path, payload: dict) -> None:
    """Write JSON test data to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _read_json(path: Path) -> dict:
    """Read JSON test data from disk."""
    return json.loads(path.read_text(encoding="utf-8"))


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


class TestRefreshAgentJsonPrompt:
    """Tests for refresh_agent_json_prompt."""

    def test_returns_false_when_json_path_is_missing(self, tmp_path):
        missing_path = tmp_path / "missing.json"
        profile = AgentProfile(name="developer", description="Developer", prompt="Prompt")

        assert skill_injection.refresh_agent_json_prompt(missing_path, profile) is False
        assert not missing_path.exists()

    @patch(
        "cli_agent_orchestrator.utils.skill_injection.compose_agent_prompt",
        return_value="Profile prompt",
    )
    def test_adds_prompt_field_when_missing(self, _mock_prompt, tmp_path):
        json_path = tmp_path / "developer.json"
        _write_json(json_path, {"name": "developer", "description": "Developer"})

        rewritten = skill_injection.refresh_agent_json_prompt(
            json_path, AgentProfile(name="developer", description="Developer")
        )

        assert rewritten is True
        assert _read_json(json_path)["prompt"] == "Profile prompt"

    @patch(
        "cli_agent_orchestrator.utils.skill_injection.compose_agent_prompt",
        return_value="Updated prompt",
    )
    def test_replaces_existing_prompt_field(self, _mock_prompt, tmp_path):
        json_path = tmp_path / "developer.json"
        _write_json(
            json_path,
            {"name": "developer", "description": "Developer", "prompt": "Old prompt"},
        )

        rewritten = skill_injection.refresh_agent_json_prompt(
            json_path, AgentProfile(name="developer", description="Developer")
        )

        assert rewritten is True
        assert _read_json(json_path)["prompt"] == "Updated prompt"

    @patch("cli_agent_orchestrator.utils.skill_injection.compose_agent_prompt", return_value=None)
    def test_removes_prompt_field_when_new_prompt_is_none(self, _mock_prompt, tmp_path):
        json_path = tmp_path / "developer.json"
        _write_json(
            json_path,
            {"name": "developer", "description": "Developer", "prompt": "Old prompt"},
        )

        rewritten = skill_injection.refresh_agent_json_prompt(
            json_path, AgentProfile(name="developer", description="Developer")
        )

        assert rewritten is True
        assert "prompt" not in _read_json(json_path)

    @patch(
        "cli_agent_orchestrator.utils.skill_injection.compose_agent_prompt",
        return_value="Atomic prompt",
    )
    def test_writes_atomically_with_os_replace_and_no_tmp_leak(self, _mock_prompt, tmp_path):
        json_path = tmp_path / "developer.json"
        _write_json(json_path, {"name": "developer", "description": "Developer"})
        temp_path = json_path.with_suffix(".json.tmp")

        with patch(
            "cli_agent_orchestrator.utils.skill_injection.os.replace", wraps=os.replace
        ) as mock_replace:
            rewritten = skill_injection.refresh_agent_json_prompt(
                json_path, AgentProfile(name="developer", description="Developer")
            )

        assert rewritten is True
        mock_replace.assert_called_once_with(temp_path, json_path)
        assert not temp_path.exists()

    @patch(
        "cli_agent_orchestrator.utils.skill_injection.compose_agent_prompt",
        return_value="Stable prompt",
    )
    def test_is_idempotent_for_same_prompt(self, _mock_prompt, tmp_path):
        json_path = tmp_path / "developer.json"
        _write_json(json_path, {"name": "developer", "description": "Developer"})
        profile = AgentProfile(name="developer", description="Developer")

        assert skill_injection.refresh_agent_json_prompt(json_path, profile) is True
        first_bytes = json_path.read_bytes()

        assert skill_injection.refresh_agent_json_prompt(json_path, profile) is True
        second_bytes = json_path.read_bytes()

        assert first_bytes == second_bytes


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

    def test_returns_only_q_path_when_only_q_json_exists(self, tmp_path, monkeypatch):
        q_dir = tmp_path / "q"
        copilot_dir = tmp_path / "copilot"
        q_path = q_dir / "team__developer.json"
        _write_json(q_path, {"name": "team/developer", "description": "Developer"})

        monkeypatch.setattr(skill_injection, "Q_AGENTS_DIR", q_dir)
        monkeypatch.setattr(skill_injection, "COPILOT_AGENTS_DIR", copilot_dir)
        monkeypatch.setattr(
            skill_injection,
            "load_agent_profile",
            lambda name: AgentProfile(
                name="team/developer", description="Developer", prompt="Prompt"
            ),
        )
        monkeypatch.setattr(skill_injection, "build_skill_catalog", lambda: "")

        assert skill_injection.refresh_installed_agent_for_profile("team-developer") == [q_path]

    def test_returns_copilot_path_when_copilot_agent_exists(self, tmp_path, monkeypatch):
        q_dir = tmp_path / "q"
        copilot_dir = tmp_path / "copilot"
        copilot_path = copilot_dir / "team__developer.agent.md"
        _write_agent_md(copilot_path, "team/developer", "Developer", "Old prompt")

        monkeypatch.setattr(skill_injection, "Q_AGENTS_DIR", q_dir)
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

    def test_returns_q_and_copilot_when_both_exist(self, tmp_path, monkeypatch):
        q_dir = tmp_path / "q"
        copilot_dir = tmp_path / "copilot"
        q_path = q_dir / "team__developer.json"
        copilot_path = copilot_dir / "team__developer.agent.md"
        _write_json(q_path, {"name": "team/developer", "description": "Developer"})
        _write_agent_md(copilot_path, "team/developer", "Developer", "Old prompt")

        monkeypatch.setattr(skill_injection, "Q_AGENTS_DIR", q_dir)
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
            q_path,
            copilot_path,
        ]

    def test_returns_empty_list_when_no_installed_agents_exist(self, tmp_path, monkeypatch):
        monkeypatch.setattr(skill_injection, "Q_AGENTS_DIR", tmp_path / "q")
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

    def test_refreshes_q_json_with_cao_managed_resource(self, tmp_path, monkeypatch):
        context_dir = tmp_path / "agent-context"
        q_dir = tmp_path / "q"
        copilot_dir = tmp_path / "copilot"
        managed_path = q_dir / "developer.json"
        context_file = context_dir / "developer.md"
        context_file.parent.mkdir(parents=True, exist_ok=True)
        context_file.write_text("context", encoding="utf-8")
        _write_json(
            managed_path,
            {
                "name": "developer",
                "description": "Developer",
                "resources": [f"file://{context_file}"],
            },
        )

        monkeypatch.setattr(skill_injection, "AGENT_CONTEXT_DIR", context_dir)
        monkeypatch.setattr(skill_injection, "Q_AGENTS_DIR", q_dir)
        monkeypatch.setattr(skill_injection, "COPILOT_AGENTS_DIR", copilot_dir)
        monkeypatch.setattr(
            skill_injection,
            "load_agent_profile",
            lambda name: AgentProfile(name="developer", description="Developer", prompt="Prompt"),
        )
        monkeypatch.setattr(skill_injection, "build_skill_catalog", lambda: "## Available Skills")

        refreshed = skill_injection.refresh_all_cao_managed_agents()

        assert refreshed == [managed_path]
        assert _read_json(managed_path)["prompt"] == "Prompt\n\n## Available Skills"

    def test_refreshes_copilot_agent_with_matching_context_file(self, tmp_path, monkeypatch):
        context_dir = tmp_path / "agent-context"
        q_dir = tmp_path / "q"
        copilot_dir = tmp_path / "copilot"

        # Create context file (marks this as CAO-managed)
        context_file = context_dir / "developer.md"
        context_file.parent.mkdir(parents=True, exist_ok=True)
        context_file.write_text("context", encoding="utf-8")

        # Create Copilot agent
        copilot_path = copilot_dir / "developer.agent.md"
        _write_agent_md(copilot_path, "developer", "Developer", "Old prompt")

        monkeypatch.setattr(skill_injection, "AGENT_CONTEXT_DIR", context_dir)
        monkeypatch.setattr(skill_injection, "Q_AGENTS_DIR", q_dir)
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
        q_dir = tmp_path / "q"
        copilot_dir = tmp_path / "copilot"

        # No context file for this agent — not CAO-managed
        copilot_path = copilot_dir / "external.agent.md"
        _write_agent_md(copilot_path, "external", "External agent", "External prompt")
        original_bytes = copilot_path.read_bytes()

        monkeypatch.setattr(skill_injection, "AGENT_CONTEXT_DIR", context_dir)
        monkeypatch.setattr(skill_injection, "Q_AGENTS_DIR", q_dir)
        monkeypatch.setattr(skill_injection, "COPILOT_AGENTS_DIR", copilot_dir)

        assert skill_injection.refresh_all_cao_managed_agents() == []
        assert copilot_path.read_bytes() == original_bytes

    def test_skips_json_with_only_non_cao_resources(self, tmp_path, monkeypatch):
        context_dir = tmp_path / "agent-context"
        q_dir = tmp_path / "q"
        copilot_dir = tmp_path / "copilot"
        unmanaged_path = q_dir / "developer.json"
        _write_json(
            unmanaged_path,
            {
                "name": "developer",
                "description": "Developer",
                "resources": ["file:///tmp/not-cao.md", "skill://local/skill"],
            },
        )
        original_bytes = unmanaged_path.read_bytes()

        monkeypatch.setattr(skill_injection, "AGENT_CONTEXT_DIR", context_dir)
        monkeypatch.setattr(skill_injection, "Q_AGENTS_DIR", q_dir)
        monkeypatch.setattr(skill_injection, "COPILOT_AGENTS_DIR", copilot_dir)
        monkeypatch.setattr(
            skill_injection,
            "load_agent_profile",
            lambda name: AgentProfile(name="developer", description="Developer", prompt="Prompt"),
        )

        assert skill_injection.refresh_all_cao_managed_agents() == []
        assert unmanaged_path.read_bytes() == original_bytes

    @pytest.mark.parametrize(
        "payload",
        [
            {"name": "developer", "description": "Developer", "resources": []},
            {"name": "developer", "description": "Developer"},
        ],
    )
    def test_skips_json_with_empty_or_missing_resources(self, payload, tmp_path, monkeypatch):
        context_dir = tmp_path / "agent-context"
        q_dir = tmp_path / "q"
        copilot_dir = tmp_path / "copilot"
        json_path = q_dir / "developer.json"
        _write_json(json_path, payload)
        original_bytes = json_path.read_bytes()

        monkeypatch.setattr(skill_injection, "AGENT_CONTEXT_DIR", context_dir)
        monkeypatch.setattr(skill_injection, "Q_AGENTS_DIR", q_dir)
        monkeypatch.setattr(skill_injection, "COPILOT_AGENTS_DIR", copilot_dir)

        assert skill_injection.refresh_all_cao_managed_agents() == []
        assert json_path.read_bytes() == original_bytes

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
        q_dir = tmp_path / "q"
        copilot_dir = tmp_path / "copilot"
        good_context = context_dir / "good.md"
        missing_context = context_dir / "missing.md"
        good_context.parent.mkdir(parents=True, exist_ok=True)
        good_context.write_text("good", encoding="utf-8")
        missing_context.write_text("missing", encoding="utf-8")

        good_path = q_dir / "good.json"
        missing_path = q_dir / "missing.json"
        _write_json(
            good_path,
            {
                "name": "good",
                "description": "Good agent",
                "resources": [f"file://{good_context}"],
            },
        )
        _write_json(
            missing_path,
            {
                "name": "missing",
                "description": "Missing agent",
                "resources": [f"file://{missing_context}"],
            },
        )

        def load_profile(name: str) -> AgentProfile:
            if name == "good":
                return AgentProfile(name="good", description="Good agent", prompt="Prompt")
            raise load_error

        monkeypatch.setattr(skill_injection, "AGENT_CONTEXT_DIR", context_dir)
        monkeypatch.setattr(skill_injection, "Q_AGENTS_DIR", q_dir)
        monkeypatch.setattr(skill_injection, "COPILOT_AGENTS_DIR", copilot_dir)
        monkeypatch.setattr(skill_injection, "load_agent_profile", load_profile)
        monkeypatch.setattr(skill_injection, "build_skill_catalog", lambda: "## Available Skills")

        with caplog.at_level(logging.WARNING):
            refreshed = skill_injection.refresh_all_cao_managed_agents()

        assert refreshed == [good_path]
        assert "source profile could not be loaded" in caplog.text
        assert "missing" in caplog.text
        assert "prompt" not in _read_json(missing_path)

    def test_refreshes_cao_managed_across_q_and_copilot(self, tmp_path, monkeypatch):
        context_dir = tmp_path / "agent-context"
        q_dir = tmp_path / "q"
        copilot_dir = tmp_path / "copilot"

        # Create context files for managed agents
        for name in ("managed-q", "managed-copilot"):
            ctx = context_dir / f"{name}.md"
            ctx.parent.mkdir(parents=True, exist_ok=True)
            ctx.write_text(name, encoding="utf-8")

        managed_q = q_dir / "managed-q.json"
        unmanaged_q = q_dir / "unmanaged-q.json"
        managed_copilot = copilot_dir / "managed-copilot.agent.md"
        unmanaged_copilot = copilot_dir / "unmanaged-copilot.agent.md"

        _write_json(
            managed_q,
            {
                "name": "managed-q",
                "description": "Managed Q",
                "resources": [f"file://{context_dir / 'managed-q.md'}"],
            },
        )
        _write_json(
            unmanaged_q,
            {
                "name": "unmanaged-q",
                "description": "Unmanaged Q",
                "resources": ["file:///tmp/other-q.md"],
            },
        )
        _write_agent_md(managed_copilot, "managed-copilot", "Managed Copilot", "Old prompt")
        _write_agent_md(
            unmanaged_copilot, "unmanaged-copilot", "Unmanaged Copilot", "External prompt"
        )

        def load_profile(name: str) -> AgentProfile:
            return AgentProfile(
                name=name, description=f"{name} description", prompt=f"{name} prompt"
            )

        monkeypatch.setattr(skill_injection, "AGENT_CONTEXT_DIR", context_dir)
        monkeypatch.setattr(skill_injection, "Q_AGENTS_DIR", q_dir)
        monkeypatch.setattr(skill_injection, "COPILOT_AGENTS_DIR", copilot_dir)
        monkeypatch.setattr(skill_injection, "load_agent_profile", load_profile)
        monkeypatch.setattr(skill_injection, "build_skill_catalog", lambda: "## Available Skills")

        refreshed = skill_injection.refresh_all_cao_managed_agents()

        assert refreshed == [managed_q, managed_copilot]
        assert _read_json(managed_q)["prompt"] == "managed-q prompt\n\n## Available Skills"
        assert (
            _read_agent_md_body(managed_copilot) == "managed-copilot prompt\n\n## Available Skills"
        )
        assert "prompt" not in _read_json(unmanaged_q)
