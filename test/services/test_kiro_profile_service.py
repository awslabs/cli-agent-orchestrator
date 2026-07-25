"""U6: the Kiro profile facade — delegation, policy surfacing, no v2 drift.

Traces to FR-105, ADR-004, NFR-102/103/105, BR-U6-1..10.
"""

import json
from pathlib import Path

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.kiro_engine import KiroEngine
from cli_agent_orchestrator.services import kiro_profile_service
from cli_agent_orchestrator.services.kiro_profile_service import (
    InstallOutcome,
    RenderedProfile,
    install_profile,
    lint_profile,
    render_profile,
)
from cli_agent_orchestrator.services.kiro_profiles import render_kiro_v2
from cli_agent_orchestrator.utils.kiro_policy import CompiledKiroPolicy, KiroPolicyError

_SENTINEL_PROMPT = "SENTINEL-PROMPT-do-not-leak-9f2a"
_SENTINEL_TOKEN = "SENTINEL-MCP-TOKEN-do-not-leak-4b7c"


def _kas_profile(**kwargs) -> AgentProfile:
    base = {
        "name": "facade-kas",
        "description": "Facade KAS",
        "engine": KiroEngine.KAS,
        "allowedTools": ["fs_read"],
    }
    base.update(kwargs)
    return AgentProfile(**base)


def _v2_profile(**kwargs) -> AgentProfile:
    base = {
        "name": "facade-v2",
        "description": "Facade v2",
        "engine": KiroEngine.V2,
        "tools": ["*"],
        "allowedTools": ["fs_read"],
    }
    base.update(kwargs)
    return AgentProfile(**base)


def test_v2_render_through_the_facade_is_byte_identical_to_direct_render() -> None:
    """BR-U6-2/NFR-105: byte-identity is inherited, not re-achieved."""
    profile = _v2_profile(
        model="fixture-model",
        mcpServers={"service": {"command": "service-mcp", "env": {"API_TOKEN": _SENTINEL_TOKEN}}},
    )
    resources = ["file:///fixture/context.md"]

    direct = render_kiro_v2(profile, ["fs_read"], resources, profile.mcpServers)
    through_facade = render_profile(
        profile,
        engine=KiroEngine.V2,
        resources=resources,
        mcp_servers=profile.mcpServers,
        allowed_tools=["fs_read"],
    )

    assert through_facade.text == direct
    assert through_facade.engine == KiroEngine.V2
    assert through_facade.policy is None


def test_render_profile_returns_the_real_compiled_policy() -> None:
    """BR-U6-3/FR-105: the policy is surfaced, not discarded."""
    rendered = render_profile(_kas_profile(), engine=KiroEngine.KAS, resources=[], mcp_servers=None)
    assert isinstance(rendered, RenderedProfile)
    assert isinstance(rendered.policy, CompiledKiroPolicy)
    assert rendered.policy.visible_tools == ("fs_read",)
    assert rendered.policy.allow_rule_count == 1
    assert json.loads(rendered.text)["tools"] == ["fs_read"]


def test_install_outcome_carries_the_policy_and_both_paths(tmp_path: Path) -> None:
    """BR-U6-3: install-time assertions on CompiledKiroPolicy are possible."""
    outcome = install_profile(
        _kas_profile(), directory=tmp_path, engine=KiroEngine.KAS, resources=[], mcp_servers=None
    )
    assert isinstance(outcome, InstallOutcome)
    assert outcome.artifact_path == tmp_path / "facade-kas.kas.json"
    assert outcome.summary_path == tmp_path / "facade-kas.kas.summary.json"
    assert isinstance(outcome.policy, CompiledKiroPolicy)
    assert outcome.artifact_path.exists()
    assert outcome.summary_path.exists()


def test_v2_install_writes_no_sidecar(tmp_path: Path) -> None:
    """BR-U6-7/SEC-U7-8: v2 has no compiled Cedar policy to summarise."""
    outcome = install_profile(
        _v2_profile(),
        directory=tmp_path,
        engine=KiroEngine.V2,
        resources=[],
        mcp_servers=None,
        allowed_tools=["fs_read"],
    )
    assert outcome.summary_path is None
    assert outcome.policy is None
    assert outcome.artifact_path == tmp_path / "facade-v2.json"
    assert list(tmp_path.glob("*.summary.json")) == []


def test_untranslatable_profile_writes_nothing_and_leaves_prior_artifact_intact(
    tmp_path: Path,
) -> None:
    """BR-U6-4/BR-U6-5/BR-U6-6: render precedes any write; the error propagates."""
    existing = tmp_path / "facade-kas.kas.json"
    existing.write_text('{"pre": "existing"}', encoding="utf-8")
    original = existing.read_bytes()

    profile = _kas_profile(toolAliases={"ls": "fs_list"})

    with pytest.raises(KiroPolicyError) as exc_info:
        install_profile(
            profile, directory=tmp_path, engine=KiroEngine.KAS, resources=[], mcp_servers=None
        )

    assert exc_info.value.diagnostic.code == "unsafe-aliases"
    assert existing.read_bytes() == original
    assert not (tmp_path / "facade-kas.kas.summary.json").exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_escape_profile_name_still_raises_via_kiro_artifact_path(tmp_path: Path) -> None:
    """BR-U6-8: the facade never composes a path; the traversal defense is reused."""
    with pytest.raises(ValueError, match=r"\[A-Za-z0-9_-\]"):
        install_profile(
            _kas_profile(),
            directory=tmp_path,
            engine=KiroEngine.KAS,
            resources=[],
            mcp_servers=None,
            artifact_name="../escape",
        )
    assert list(tmp_path.iterdir()) == []


def test_lint_profile_writes_no_file_and_leaks_nothing(tmp_path: Path) -> None:
    """BR-U6-9/NFR-104: read-only and redaction-safe."""
    profile = _kas_profile(
        prompt=_SENTINEL_PROMPT,
        mcpServers={"service": {"command": "service-mcp", "env": {"API_TOKEN": _SENTINEL_TOKEN}}},
        allowedTools=["fs_read", "@service/query"],
    )

    result = lint_profile(profile, artifact_directory=tmp_path)
    serialised = json.dumps(result.model_dump(mode="json"))

    assert list(tmp_path.iterdir()) == []
    assert result.generation_safe is True
    assert _SENTINEL_PROMPT not in serialised
    assert _SENTINEL_TOKEN not in serialised
    assert "permit(" not in serialised
    assert "forbid(" not in serialised


def test_lint_profile_is_a_passthrough_to_lint_kiro_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BR-U6-1: delegation — the facade adds no lint logic of its own."""
    seen: list[AgentProfile] = []
    sentinel = object()

    def fake_lint(profile, *args, **kwargs):
        seen.append(profile)
        return sentinel

    monkeypatch.setattr(kiro_profile_service, "lint_kiro_profile", fake_lint)
    profile = _kas_profile()

    assert lint_profile(profile) is sentinel
    assert seen == [profile]


def test_render_profile_delegates_to_the_existing_renderers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BR-U6-1: each arm forwards to the tested function, not a fork of it."""
    calls: list[str] = []

    monkeypatch.setattr(
        kiro_profile_service,
        "render_kiro_kas",
        lambda *a, **k: (calls.append("kas") or "kas-text", "kas-policy"),
    )
    monkeypatch.setattr(
        kiro_profile_service,
        "render_kiro_v2",
        lambda *a, **k: (calls.append("v2") or "v2-text"),
    )

    kas = render_profile(_kas_profile(), engine=KiroEngine.KAS, resources=[], mcp_servers=None)
    v2 = render_profile(_v2_profile(), engine=KiroEngine.V2, resources=[], mcp_servers=None)

    assert calls == ["kas", "v2"]
    assert kas.text == "kas-text" and kas.policy == "kas-policy"
    assert v2.text == "v2-text" and v2.policy is None
