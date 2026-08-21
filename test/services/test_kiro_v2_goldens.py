"""Exact v2 Kiro profile rendering goldens for the Phase 0 migration."""

import json
from pathlib import Path

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.kiro_agent import KiroAgentConfig
from cli_agent_orchestrator.models.kiro_engine import KiroEngine


@pytest.mark.parametrize(
    ("name", "description", "profile_kwargs", "expected_extra"),
    [
        (
            "v2-restricted",
            "V2 restricted golden",
            {
                "role": "supervisor",
                "tools": ["*"],
                "allowedTools": ["@cao-mcp-server"],
                "toolsSettings": {"cao-mcp-server": {"enabled": True}},
                "mcpServers": {"cao-mcp-server": {"command": "fixture-cao-mcp", "args": ["serve"]}},
            },
            {
                "allowedTools": ["@cao-mcp-server"],
                "toolsSettings": {"cao-mcp-server": {"enabled": True}},
                "mcpServers": {"cao-mcp-server": {"command": "fixture-cao-mcp", "args": ["serve"]}},
            },
        ),
        (
            "v2-unrestricted",
            "V2 unrestricted golden",
            {"tools": ["*"], "allowedTools": ["*"]},
            {"allowedTools": ["*"]},
        ),
        (
            "v2-model-pinned",
            "V2 model-pinned golden",
            {
                "tools": ["*"],
                "allowedTools": ["fs_read"],
                "model": "fixture-model",
                "resources": [
                    "file:///fixtures/v2-model-pinned/context.md",
                    "skill:///fixtures/v2-model-pinned/skills/**/SKILL.md",
                ],
            },
            {
                "allowedTools": ["fs_read"],
                "model": "fixture-model",
                "resources": [
                    "file:///fixtures/v2-model-pinned/context.md",
                    "skill:///fixtures/v2-model-pinned/skills/**/SKILL.md",
                ],
            },
        ),
    ],
)
def test_v2_profile_rendering_matches_complete_golden(
    tmp_path: Path, name: str, description: str, profile_kwargs: dict, expected_extra: dict
):
    """Every golden writes its destination and compares the entire JSON value."""
    profile = AgentProfile(
        name=name,
        description=description,
        engine=KiroEngine.V2,
        **profile_kwargs,
    )
    resources = profile.resources or []
    config = KiroAgentConfig(
        name=profile.name,
        description=profile.description,
        tools=profile.tools or ["*"],
        allowedTools=profile.allowedTools or [],
        resources=resources,
        mcpServers=profile.mcpServers,
        toolsSettings=profile.toolsSettings,
        model=profile.model,
    )
    destination = tmp_path / f"{name}.json"
    destination.write_text(config.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")

    rendered = json.loads(destination.read_text(encoding="utf-8"))
    expected = {
        "name": name,
        "description": description,
        "tools": ["*"],
        "allowedTools": expected_extra["allowedTools"],
        "useLegacyMcpJson": False,
        "resources": expected_extra.get("resources", []),
        **{
            key: value
            for key, value in expected_extra.items()
            if key not in {"allowedTools", "resources"}
        },
    }
    assert rendered == expected
