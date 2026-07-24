"""KAS profile rendering, artifact identity, and atomicity tests."""

import json
from pathlib import Path

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.kiro_engine import KiroEngine
from cli_agent_orchestrator.services.kiro_profiles import (
    atomic_write_text,
    kiro_artifact_path,
    render_kiro_kas,
)


@pytest.mark.parametrize(
    ("profile", "expected_tools", "expected_allow", "expected_deny"),
    [
        (
            AgentProfile(
                name="restricted",
                description="Restricted",
                engine=KiroEngine.KAS,
                allowedTools=["fs_read", "fs_list"],
            ),
            ["fs_read", "glob", "grep"],
            3,
            7,
        ),
        (
            AgentProfile(
                name="unrestricted",
                description="Unrestricted",
                engine=KiroEngine.KAS,
                allowedTools=["*"],
            ),
            ["*"],
            1,
            0,
        ),
        (
            AgentProfile(
                name="model-resource",
                description="Model and resource",
                engine=KiroEngine.KAS,
                allowedTools=["fs_read"],
                model="synthetic-model",
                hooks={"agentSpawn": [{"command": "synthetic-hook"}]},
                resources=["file:///synthetic/model-resource/model.md"],
            ),
            ["fs_read"],
            1,
            9,
        ),
        (
            AgentProfile(
                name="mcp-scoped",
                description="MCP scoped",
                engine=KiroEngine.KAS,
                allowedTools=["@search/query"],
                mcpServers={"search": {"command": "synthetic-mcp", "args": ["serve"]}},
            ),
            ["mcp::search::query"],
            1,
            10,
        ),
        (
            AgentProfile(
                name="explicit-deny",
                description="Explicit deny",
                engine=KiroEngine.KAS,
                allowedTools=["fs_*"],
                deniedTools=["fs_write"],
            ),
            ["fs_read", "glob", "grep"],
            3,
            7,
        ),
    ],
)
def test_kas_complete_json_goldens(
    profile: AgentProfile,
    expected_tools: list[str],
    expected_allow: int,
    expected_deny: int,
) -> None:
    resources = [
        f"file:///synthetic/{profile.name}/context.md",
        "skill:///synthetic/skills/**/SKILL.md",
    ]
    rendered, policy = render_kiro_kas(profile, resources, profile.mcpServers)
    expected_resources = resources + (profile.resources or [])
    value = json.loads(rendered)

    assert value == {
        "name": profile.name,
        "description": profile.description,
        "tools": expected_tools,
        "permissions": {
            "rules": list(policy.permissions.rules),
            "includePowers": False,
            "excludedTools": list(policy.denied_tools),
        },
        "resources": expected_resources,
        **({"mcpServers": profile.mcpServers} if profile.mcpServers else {}),
        **({"hooks": profile.hooks} if profile.hooks else {}),
        **({"model": profile.model} if profile.model else {}),
    }
    assert policy.allow_rule_count == expected_allow
    assert policy.deny_rule_count == expected_deny


def test_kas_rendering_is_deterministic() -> None:
    profile = AgentProfile(
        name="deterministic",
        description="Deterministic",
        allowedTools=["web_fetch", "fs_read"],
    )

    first, _ = render_kiro_kas(profile, [], None)
    second, _ = render_kiro_kas(profile, [], None)

    assert first == second


def test_v2_and_kas_artifact_paths_are_distinct_and_sanitized(tmp_path: Path) -> None:
    assert kiro_artifact_path(tmp_path, "safe-name", KiroEngine.V2) == (tmp_path / "safe-name.json")
    assert kiro_artifact_path(tmp_path, "safe-name", KiroEngine.KAS) == (
        tmp_path / "safe-name.kas.json"
    )
    with pytest.raises(ValueError):
        kiro_artifact_path(tmp_path, "../escape", KiroEngine.KAS)


def test_atomic_write_failure_leaves_existing_artifact_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "agent.kas.json"
    destination.write_text("original", encoding="utf-8")

    def fail_replace(source: Path, target: Path) -> None:
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.kiro_profiles.os.replace",
        fail_replace,
    )

    with pytest.raises(OSError, match="synthetic replace failure"):
        atomic_write_text(destination, "replacement")

    assert destination.read_text(encoding="utf-8") == "original"
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.parametrize(
    ("profile_kwargs", "code"),
    [
        ({"resources": ["https://unsupported.example/resource"]}, "unknown-resource"),
        ({"resources": ["file:///same", "file:///same"]}, "contradictory-resource"),
        ({"hooks": {"bad": object()}}, "serialization-error"),
    ],
)
def test_resource_and_serialization_errors_fail_before_writing(
    profile_kwargs: dict, code: str
) -> None:
    profile = AgentProfile(
        name="blocked",
        description="Blocked",
        allowedTools=["fs_read"],
        **profile_kwargs,
    )

    with pytest.raises(ValueError, match=code):
        render_kiro_kas(profile, [], None)
