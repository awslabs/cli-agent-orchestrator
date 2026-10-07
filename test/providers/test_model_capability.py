"""Provider declarations for per-launch model support."""

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.kiro_engine import KiroEngine
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.providers.manager import ProviderManager


def _profile(*, model: str | None = None, native_agent: str | None = None) -> AgentProfile:
    return AgentProfile(
        name="worker",
        description="Test worker",
        model=model,
        native_agent=native_agent,
    )


def test_every_provider_explicitly_declares_model_support() -> None:
    """Adding a provider without a capability declaration must fail this test."""
    for provider in ProviderType:
        provider_class = ProviderManager.provider_class(provider.value)
        assert "honors_model" in provider_class.__dict__, provider.value


@pytest.mark.parametrize(
    ("provider", "agent_profile", "profile", "requested_model", "expected"),
    [
        (ProviderType.CLAUDE_CODE, "worker", _profile(), "model-x", True),
        (ProviderType.CLAUDE_CODE, None, None, "model-x", True),
        (
            ProviderType.CLAUDE_CODE,
            "worker",
            _profile(native_agent="native-worker"),
            "model-x",
            False,
        ),
        (ProviderType.KIRO_CLI, "worker", _profile(), "model-x", True),
        (ProviderType.CODEX, "worker", _profile(), "model-x", True),
        (ProviderType.KIMI_CLI, "worker", _profile(), "model-x", True),
        (ProviderType.OPENCODE_CLI, "worker", _profile(), "model-x", True),
        (ProviderType.OMP, "worker", _profile(), "model-x", True),
        (ProviderType.HERMES, "worker", _profile(), "model-x", True),
        (ProviderType.GROK_CLI, "worker", _profile(), "model-x", True),
        (ProviderType.MINIMAX_CODE, "worker", _profile(), "model-x", True),
        (ProviderType.COPILOT_CLI, "worker", _profile(), "model-x", True),
        (ProviderType.COPILOT_CLI, "", _profile(), "model-x", False),
        (ProviderType.COPILOT_CLI, None, None, "model-x", False),
        (ProviderType.CURSOR_CLI, "worker", _profile(), "model-x", True),
        (ProviderType.CURSOR_CLI, "worker", None, "model-x", True),
        (
            ProviderType.CURSOR_CLI,
            "worker",
            _profile(model="profile-model"),
            "model-x",
            False,
        ),
        (
            ProviderType.CURSOR_CLI,
            "worker",
            _profile(model="profile-model"),
            None,
            False,
        ),
        (
            ProviderType.CURSOR_CLI,
            "worker",
            _profile(model="model-x"),
            "model-x",
            True,
        ),
        (ProviderType.ANTIGRAVITY_CLI, "worker", _profile(), "model-x", True),
        (ProviderType.ANTIGRAVITY_CLI, "worker", None, "model-x", True),
        (
            ProviderType.ANTIGRAVITY_CLI,
            "worker",
            _profile(model="profile-model"),
            "model-x",
            False,
        ),
        (
            ProviderType.ANTIGRAVITY_CLI,
            "worker",
            _profile(model="profile-model"),
            None,
            False,
        ),
        (
            ProviderType.ANTIGRAVITY_CLI,
            "worker",
            _profile(model="model-x"),
            "model-x",
            True,
        ),
        (ProviderType.DEVIN_CLI, "worker", _profile(), "model-x", True),
        (ProviderType.MOCK_CLI, "worker", _profile(), "model-x", False),
    ],
    ids=lambda value: value.value if isinstance(value, ProviderType) else None,
)
def test_provider_model_support_matches_launch_behavior(
    provider: ProviderType,
    agent_profile: str | None,
    profile: AgentProfile | None,
    requested_model: str | None,
    expected: bool,
) -> None:
    provider_class = ProviderManager.provider_class(provider.value)

    assert (
        provider_class.honors_model(
            agent_profile=agent_profile,
            profile=profile,
            requested_model=requested_model,
        )
        is expected
    )


def test_provider_class_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError, match="Unknown provider type"):
        ProviderManager.provider_class("unknown")


@pytest.mark.parametrize("provider", list(ProviderType), ids=lambda provider: provider.value)
def test_provider_class_matches_create_provider_dispatch(provider: ProviderType) -> None:
    manager = ProviderManager()
    with (
        patch(
            "cli_agent_orchestrator.providers.manager.resolve_kiro_engine",
            return_value=KiroEngine.V2,
        ),
        patch(
            "cli_agent_orchestrator.backends.registry.get_backend",
            return_value=MagicMock(),
        ),
    ):
        created = manager.create_provider(
            provider.value,
            terminal_id=f"test-{provider.value}",
            tmux_session="test-session",
            tmux_window="test-window",
            agent_profile="worker",
        )

    assert type(created) is ProviderManager.provider_class(provider.value)
