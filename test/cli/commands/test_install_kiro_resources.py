"""Kiro installation retains the profile's additional context resources."""

import json

import pytest

from cli_agent_orchestrator.cli.commands.install import install


@pytest.mark.parametrize("source_kind", ["file", "name"])
def test_install_kiro_preserves_profile_resources(runner, workspace, tmp_path, source_kind):
    resources = ["file:///project/team notes.md", "file:///project/指南.md", "file://docs/**/*.md"]
    profile = tmp_path / "context-agent.md"
    profile.write_text(
        "---\nname: context-agent\ndescription: Extra context\n"
        f"resources: {json.dumps(resources)}\n---\nUse the project documentation.\n",
        encoding="utf-8",
    )
    if source_kind == "name":
        (workspace["local_store"] / profile.name).write_text(
            profile.read_text(encoding="utf-8"), encoding="utf-8"
        )
    source = str(profile) if source_kind == "file" else profile.stem

    for _ in range(2):
        result = runner.invoke(install, [source, "--provider", "kiro_cli"])
        assert result.exit_code == 0 and "Error:" not in result.output, result.output
        config = json.loads(
            (workspace["kiro_agents_dir"] / "context-agent.json").read_text(encoding="utf-8")
        )
        assert config["resources"][3:] == resources
        assert config["resources"][0] == (f"file://{workspace['context_dir'] / 'context-agent.md'}")
        assert config["resources"][1].startswith("skill://")
        assert config["resources"][1].endswith("/**/SKILL.md")
        assert config["resources"][2].startswith("skill://")
        assert config["resources"][2].endswith("/*/SKILL.md")

    # A subsequent profile must not inherit the previous agent's extra context.
    other = tmp_path / "other-agent.md"
    other.write_text("---\nname: other-agent\ndescription: Other\n---\nHelp.\n", encoding="utf-8")
    result = runner.invoke(install, [str(other), "--provider", "kiro_cli"])
    assert result.exit_code == 0 and "Error:" not in result.output, result.output
    other_config = json.loads(
        (workspace["kiro_agents_dir"] / "other-agent.json").read_text(encoding="utf-8")
    )
    assert len(other_config["resources"]) == 3
    assert other_config["resources"][0] == f"file://{workspace['context_dir'] / 'other-agent.md'}"


@pytest.mark.parametrize("frontmatter", ["", "resources: []\n"])
def test_install_kiro_without_extra_resources_keeps_defaults(
    runner, workspace, tmp_path, frontmatter
):
    profile = tmp_path / "default-agent.md"
    profile.write_text(
        f"---\nname: default-agent\ndescription: Default context\n{frontmatter}---\nHelp.\n",
        encoding="utf-8",
    )
    result = runner.invoke(install, [str(profile), "--provider", "kiro_cli"])
    assert result.exit_code == 0 and "Error:" not in result.output, result.output
    config = json.loads(
        (workspace["kiro_agents_dir"] / "default-agent.json").read_text(encoding="utf-8")
    )
    assert len(config["resources"]) == 3
    assert config["resources"][0] == f"file://{workspace['context_dir'] / 'default-agent.md'}"
