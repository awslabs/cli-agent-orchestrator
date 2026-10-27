"""#1083 (provisional): provider declarations for applying a reasoning effort.

Only providers that map an effort from the launch profile declare ``honors_effort``; every
other provider keeps the conservative ``False`` default. Each declaration is checked against
the provider's own command builder.
"""

import shlex
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider
from cli_agent_orchestrator.providers.codex import CodexProvider
from cli_agent_orchestrator.providers.manager import ProviderManager

DECLARED = {ProviderType.CLAUDE_CODE.value, ProviderType.CODEX.value}


def _profile(**fields) -> AgentProfile:
    return AgentProfile(name="worker", description="Test worker", **fields)


def test_only_providers_that_map_an_effort_declare_it():
    declared = {
        provider.value
        for provider in ProviderType
        if "honors_effort" in ProviderManager.provider_class(provider.value).__dict__
    }
    assert declared == DECLARED


@pytest.mark.parametrize("provider", list(ProviderType), ids=lambda provider: provider.value)
def test_an_undeclared_provider_does_not_honor_effort(provider):
    honored = ProviderManager.provider_class(provider.value).honors_effort("worker", _profile())
    assert honored is (provider.value in DECLARED)


def test_the_base_default_is_false():
    assert BaseProvider.honors_effort("worker", _profile()) is False
    assert BaseProvider.honors_effort(None, None) is False


def _claude_args(profile: AgentProfile) -> list[str]:
    with patch(
        "cli_agent_orchestrator.providers.claude_code.agent_profiles.load_agent_profile",
        return_value=profile,
    ):
        command = ClaudeCodeProvider("tid", "sess", "win", "worker")._build_claude_command()
    return shlex.split(command.split("; ")[-1])


def _codex_args(profile: AgentProfile) -> list[str]:
    with patch(
        "cli_agent_orchestrator.providers.codex.agent_profiles.load_agent_profile",
        return_value=profile,
    ):
        command = CodexProvider("tid", "sess", "win", "worker")._build_codex_command()
    return shlex.split(command.split("; ")[-1])


@pytest.mark.parametrize("native_agent", [None, "native-worker"])
def test_claude_code_honors_effort_exactly_when_its_command_carries_it(native_agent):
    profile = _profile(native_agent=native_agent, claudeConfig={"effort": "high"})
    args = _claude_args(profile)
    carried = "--effort" in args and args[args.index("--effort") + 1] == "high"
    assert ClaudeCodeProvider.honors_effort("worker", profile) is carried
    # The --agent branch drops claudeConfig: only the full profile carries the effort.
    assert carried is (native_agent is None)


def test_codex_honors_effort_and_its_command_carries_it():
    profile = _profile(codexConfig={"model_reasoning_effort": "high"})
    args = _codex_args(profile)
    item = 'model_reasoning_effort="high"'
    assert args.count(item) == 1 and args[args.index(item) - 1] == "-c"
    assert CodexProvider.honors_effort("worker", profile) is True


def test_a_launch_without_a_profile_file_is_judged_like_honors_model():
    # As with honors_model, no profile means no native agent to drop claudeConfig.
    assert ClaudeCodeProvider.honors_effort(None, None) is True
    assert ClaudeCodeProvider.honors_model(None, None, None) is True
