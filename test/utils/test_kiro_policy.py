"""Fail-closed KAS policy compiler tests."""

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.kiro_engine import KiroEngine
from cli_agent_orchestrator.utils.kiro_policy import (
    KiroPolicyError,
    compile_kiro_policy,
)


def profile(**kwargs) -> AgentProfile:
    return AgentProfile(
        name="policy-test",
        description="Policy test",
        engine=KiroEngine.KAS,
        **kwargs,
    )


def test_capability_families_compile_to_canonical_actions() -> None:
    result = compile_kiro_policy(
        profile(
            allowedTools=[
                "fs_read",
                "fs_list",
                "fs_write",
                "execute_bash",
                "web_fetch",
                "@builtin",
                "@scoped/search",
            ],
            mcpServers={"scoped": {"command": "synthetic-mcp"}},
        )
    )

    assert result.visible_tools == (
        "agent_crew",
        "execute_bash",
        "execute_cmd",
        "fs_read",
        "fs_write",
        "glob",
        "grep",
        "mcp::scoped::search",
        "use_aws",
        "use_subagent",
        "web_fetch",
    )
    assert result.deny_rule_count == 0


def test_omitted_policy_uses_restricted_developer_default() -> None:
    result = compile_kiro_policy(
        profile(mcpServers={"cao-mcp-server": {"command": "synthetic-mcp"}})
    )

    assert result.source == "default"
    assert result.unrestricted is False
    assert result.visible_tools != ("*",)


def test_role_policy_is_resolved_without_mcp_auto_grants() -> None:
    result = compile_kiro_policy(
        profile(
            role="supervisor",
            mcpServers={
                "cao-mcp-server": {"command": "synthetic-cao-mcp"},
                "ungranted": {"command": "synthetic-mcp"},
            },
        )
    )

    assert result.source == "role"
    assert all("ungranted" not in tool for tool in result.visible_tools)


def test_explicit_wildcard_is_the_only_unrestricted_input() -> None:
    result = compile_kiro_policy(profile(allowedTools=["*"]))

    assert result.unrestricted is True
    assert result.visible_tools == ("*",)
    assert result.permissions.rules == ['permit(principal, action == Action::"*", resource);']


def test_explicit_deny_overrides_allow_and_wildcard() -> None:
    restricted = compile_kiro_policy(profile(allowedTools=["fs_*"], deniedTools=["fs_write"]))
    unrestricted = compile_kiro_policy(profile(allowedTools=["*"], deniedTools=["web_fetch"]))

    assert "fs_write" not in restricted.visible_tools
    assert "fs_write" in restricted.denied_tools
    assert unrestricted.visible_tools == ("*",)
    assert unrestricted.permissions.rules[-1] == (
        'forbid(principal, action == Action::"web_fetch", resource);'
    )


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"allowedTools": ["unknown"]}, "unknown-capability"),
        ({"allowedTools": ["fs_read", "fs_read"]}, "contradictory-policy"),
        ({"allowedTools": ["*", "fs_read"]}, "contradictory-policy"),
        ({"allowedTools": ["@missing/tool"]}, "unknown-mcp-server"),
        ({"allowedTools": ["@declared/"]}, "unsafe-mcp-grant"),
        ({"allowedTools": ["fs_read"], "tools": ["unsafeAlias"]}, "unknown-capability"),
        (
            {"allowedTools": ["fs_read"], "toolAliases": {"read": "fs_read"}},
            "unsafe-aliases",
        ),
        (
            {
                "allowedTools": ["fs_read"],
                "toolsSettings": {"fs_read": {"allowedPaths": ["/synthetic"]}},
            },
            "unsupported-settings",
        ),
        (
            {"allowedTools": ["fs_read"], "tools": ["fs_write"]},
            "contradictory-policy",
        ),
        (
            {
                "allowedTools": ["@service/tool"],
                "mcpServers": {"service": {"url": "https://unsupported.example"}},
            },
            "unsupported-mcp-field",
        ),
        (
            {
                "allowedTools": ["@service/tool"],
                "mcpServers": {"service": {"command": "", "args": [1]}},
            },
            "malformed-mcp",
        ),
    ],
)
def test_invalid_or_unrepresentable_policy_fails_closed(kwargs: dict, code: str) -> None:
    with pytest.raises(KiroPolicyError) as exc_info:
        compile_kiro_policy(profile(**kwargs))

    assert exc_info.value.diagnostic.code == code


def test_unknown_role_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "cli_agent_orchestrator.utils.kiro_policy._get_role_defaults",
        lambda role: None,
    )

    with pytest.raises(KiroPolicyError, match="unknown-role"):
        compile_kiro_policy(profile(role="not-configured"))
