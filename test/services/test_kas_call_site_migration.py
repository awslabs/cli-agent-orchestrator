"""U8: all seven former raise sites route through the central guard.

Traces to FR-101 (AC-101.a/b/c), FR-104, ADR-005/008, BR-U8-1..10.

Never launches a real provider or terminal: every backend, database, and provider
seam is patched, so the assertions are about admission control and residue only.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from cli_agent_orchestrator import constants
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.kiro_engine import KiroEngine
from cli_agent_orchestrator.models.kiro_launch import KiroLaunchRefusedError
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.providers.kiro_capabilities import KiroCapabilities
from cli_agent_orchestrator.providers.manager import ProviderManager
from cli_agent_orchestrator.services.agent_step import run_agent_step
from cli_agent_orchestrator.services.terminal_service import create_terminal, send_input

_TERMINAL_SERVICE = "cli_agent_orchestrator.services.terminal_service"
_MANAGER = "cli_agent_orchestrator.providers.manager"
_AGENT_STEP = "cli_agent_orchestrator.services.agent_step"

_KAS_METADATA = {
    "id": "kas-terminal",
    "provider": "kiro_cli",
    "engine": "kas",
    "tmux_session": "cao-session",
    "tmux_window": "developer-window",
    "agent_profile": "developer",
}


@pytest.fixture(autouse=True)
def flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-101.c baseline: the opt-in off is the state every existing user is on."""
    monkeypatch.setattr(constants, "ENABLE_KAS_LAUNCH", False)


def _kas_profile() -> AgentProfile:
    return AgentProfile(
        name="kas-profile",
        description="KAS profile",
        engine=KiroEngine.KAS,
        allowedTools=["fs_read"],
    )


# ---------------------------------------------------------------------------
# AC-101.c — per-site refusal with the flag off (7 sites)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_site1_create_terminal_refuses_with_zero_residue() -> None:
    """Site 1: refusal leaves no window, database row, FIFO, or provider."""
    probe = Mock(return_value=KiroCapabilities(version="3.0.0", flags=frozenset({"--v3"})))

    with (
        patch(f"{_TERMINAL_SERVICE}.load_agent_profile", return_value=_kas_profile()),
        patch(f"{_TERMINAL_SERVICE}.get_backend") as backend,
        patch(f"{_TERMINAL_SERVICE}.db_create_terminal") as db_create,
        patch(f"{_TERMINAL_SERVICE}.fifo_manager") as fifo,
        patch(f"{_TERMINAL_SERVICE}.provider_manager") as providers,
        patch(f"{_TERMINAL_SERVICE}.generate_terminal_id") as terminal_id,
    ):
        with pytest.raises(KiroLaunchRefusedError) as exc_info:
            await create_terminal(
                provider="kiro_cli",
                agent_profile="kas-profile",
                new_session=True,
                kiro_capability_probe=probe,
            )

    assert exc_info.value.code == "launch-not-enabled"
    terminal_id.assert_not_called()
    backend.return_value.create_session.assert_not_called()
    backend.return_value.create_window.assert_not_called()
    db_create.assert_not_called()
    fifo.create_reader.assert_not_called()
    providers.create_provider.assert_not_called()


def test_site2_create_provider_refuses_before_constructing_the_provider() -> None:
    manager = ProviderManager()

    with patch(f"{_MANAGER}.KiroCliProvider") as provider_class:
        with pytest.raises(KiroLaunchRefusedError) as exc_info:
            manager.create_provider(
                ProviderType.KIRO_CLI.value,
                "t-site2",
                "s1",
                "w1",
                agent_profile="developer",
                engine=KiroEngine.KAS,
            )

    assert exc_info.value.code == "launch-not-enabled"
    provider_class.assert_not_called()
    assert "t-site2" not in manager._providers


@pytest.mark.asyncio
async def test_site3_provider_initialize_refuses_before_starting_a_shell() -> None:
    from cli_agent_orchestrator.providers.kiro_cli import KiroCliProvider

    provider = KiroCliProvider("t-site3", "s1", "w1", "developer", engine=KiroEngine.KAS)

    with (
        patch("cli_agent_orchestrator.providers.kiro_cli.wait_for_shell") as wait,
        patch("cli_agent_orchestrator.providers.kiro_cli.get_backend") as backend,
    ):
        with pytest.raises(KiroLaunchRefusedError) as exc_info:
            await provider.initialize()

    assert exc_info.value.code == "launch-not-enabled"
    wait.assert_not_called()
    backend.return_value.send_keys.assert_not_called()


def test_site4_get_provider_cached_refuses_without_returning_the_provider() -> None:
    from cli_agent_orchestrator.providers.kiro_cli import KiroCliProvider

    manager = ProviderManager()
    cached = KiroCliProvider("t-site4", "s1", "w1", "developer", engine=KiroEngine.KAS)
    manager._providers["t-site4"] = cached

    with pytest.raises(KiroLaunchRefusedError) as exc_info:
        manager.get_provider("t-site4")

    assert exc_info.value.code == "launch-not-enabled"


def test_site5_get_provider_on_demand_refuses_before_reconstruction() -> None:
    manager = ProviderManager()

    with (
        patch(f"{_MANAGER}.get_terminal_metadata", return_value=dict(_KAS_METADATA)),
        patch(f"{_MANAGER}.KiroCliProvider") as provider_class,
    ):
        with pytest.raises(KiroLaunchRefusedError) as exc_info:
            manager.get_provider("kas-terminal")

    assert exc_info.value.code == "launch-not-enabled"
    provider_class.assert_not_called()
    assert "kas-terminal" not in manager._providers


def test_site6_send_input_refuses_before_provider_or_pane_access() -> None:
    with (
        patch(f"{_TERMINAL_SERVICE}.get_terminal_metadata", return_value=dict(_KAS_METADATA)),
        patch(f"{_TERMINAL_SERVICE}.provider_manager") as providers,
        patch(f"{_TERMINAL_SERVICE}.get_backend") as backend,
    ):
        with pytest.raises(KiroLaunchRefusedError) as exc_info:
            send_input("kas-terminal", "must not be delivered")

    assert exc_info.value.code == "launch-not-enabled"
    providers.get_provider.assert_not_called()
    backend.return_value.send_keys.assert_not_called()


@pytest.mark.asyncio
async def test_site7_agent_step_reuse_refuses_before_any_pane_write() -> None:
    with (
        patch(f"{_TERMINAL_SERVICE}.get_terminal_metadata", return_value=dict(_KAS_METADATA)),
        patch(f"{_TERMINAL_SERVICE}.provider_manager") as providers,
        patch(f"{_TERMINAL_SERVICE}.get_backend") as backend,
    ):
        with pytest.raises(KiroLaunchRefusedError) as exc_info:
            await run_agent_step(
                provider="kiro_cli",
                agent="developer",
                prompt="must not be delivered",
                reuse_terminal_id="kas-terminal",
            )

    assert exc_info.value.code == "launch-not-enabled"
    providers.get_provider.assert_not_called()
    backend.return_value.send_keys.assert_not_called()


# ---------------------------------------------------------------------------
# Mode assertions — only site 1 is lint-gated (BR-U8-5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_terminal_is_lint_gated_and_refuses_an_untranslatable_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Site 1 passes the profile, so an untranslatable one is refused on merit."""
    monkeypatch.setattr(constants, "ENABLE_KAS_LAUNCH", True)
    probe = Mock(return_value=KiroCapabilities(version="3.0.0", flags=frozenset({"--v3"})))
    profile = AgentProfile(
        name="kas-bad",
        description="Untranslatable",
        engine=KiroEngine.KAS,
        allowedTools=["fs_read"],
        toolsSettings={"fs_read": {"allowedPaths": ["/synthetic"]}},
    )

    with (
        patch(f"{_TERMINAL_SERVICE}.load_agent_profile", return_value=profile),
        patch(f"{_TERMINAL_SERVICE}.get_backend") as backend,
        patch(f"{_TERMINAL_SERVICE}.db_create_terminal") as db_create,
        patch(f"{_TERMINAL_SERVICE}.fifo_manager"),
        patch(f"{_TERMINAL_SERVICE}.provider_manager"),
    ):
        with pytest.raises(KiroLaunchRefusedError) as exc_info:
            await create_terminal(
                provider="kiro_cli",
                agent_profile="kas-bad",
                new_session=True,
                kiro_capability_probe=probe,
            )

    assert exc_info.value.code == "profile-untranslatable"
    assert exc_info.value.profile_field == "toolsSettings"
    backend.return_value.create_session.assert_not_called()
    db_create.assert_not_called()


def test_send_input_performs_no_policy_compile(monkeypatch: pytest.MonkeyPatch) -> None:
    """BR-U8-5: the per-message hot path is flag-only — no compile, ever.

    Asserted with the flag ON so the flag-off early return cannot be what makes
    this pass.
    """
    monkeypatch.setattr(constants, "ENABLE_KAS_LAUNCH", True)
    compiles: list[object] = []

    def tracked_compile(profile):  # pragma: no cover - must never be reached
        compiles.append(profile)
        raise AssertionError("send_input must not compile a policy")

    monkeypatch.setattr(
        "cli_agent_orchestrator.utils.kiro_policy.compile_kiro_policy", tracked_compile
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.utils.kiro_launch_guard.compile_kiro_policy", tracked_compile
    )

    # Stop the call immediately after the guard by making the very next step
    # (provider lookup) raise. The guard has already run by then, so a compile
    # would have been recorded if the site were lint-gated.
    with (
        patch(f"{_TERMINAL_SERVICE}.get_terminal_metadata", return_value=dict(_KAS_METADATA)),
        patch(f"{_TERMINAL_SERVICE}.provider_manager") as providers,
        patch(f"{_TERMINAL_SERVICE}.get_backend") as backend,
    ):
        providers.get_provider.side_effect = RuntimeError("stop-after-guard")
        with pytest.raises(RuntimeError, match="stop-after-guard"):
            send_input("kas-terminal", "delivered")

    assert compiles == []
    providers.get_provider.assert_called_once_with("kas-terminal")
    backend.return_value.send_keys.assert_not_called()


@pytest.mark.parametrize(
    "engine_value",
    [KiroEngine.V2, None],
)
@pytest.mark.asyncio
async def test_non_kas_creation_is_unaffected_by_the_guard(engine_value) -> None:
    """BR-U8-10/NFR-105: v2 paths behave exactly as before."""
    probe = Mock(
        return_value=KiroCapabilities(
            version="2.13.0",
            flags=frozenset({"--agent-engine", "--agent", "--legacy-ui", "--trust-all-tools"}),
            agent_engines=frozenset({"v2"}),
        )
    )
    provider = MagicMock()
    provider.initialize = AsyncMock(return_value=True)
    provider.shell_baseline = None

    with (
        patch(
            f"{_TERMINAL_SERVICE}.load_agent_profile",
            return_value=AgentProfile(name="developer", description="Developer"),
        ),
        patch(f"{_TERMINAL_SERVICE}.get_backend") as backend,
        patch(f"{_TERMINAL_SERVICE}.db_create_terminal"),
        patch(f"{_TERMINAL_SERVICE}.fifo_manager"),
        patch(f"{_TERMINAL_SERVICE}.provider_manager") as providers,
        patch(f"{_TERMINAL_SERVICE}.generate_terminal_id", return_value="v2term01"),
        patch(f"{_TERMINAL_SERVICE}.generate_session_name", return_value="session"),
        patch(f"{_TERMINAL_SERVICE}.generate_window_name", return_value="developer-window"),
        patch(f"{_TERMINAL_SERVICE}.get_herdr_inbox_service", return_value=None),
    ):
        backend.return_value.session_exists.return_value = False
        backend.return_value.supports_event_inbox.return_value = True
        providers.create_provider.return_value = provider

        terminal = await create_terminal(
            provider="kiro_cli",
            agent_profile="developer",
            new_session=True,
            engine=engine_value,
            kiro_capability_probe=probe,
        )

    assert terminal.engine == KiroEngine.V2


# ---------------------------------------------------------------------------
# Rendering (ADR-005) — code + field surfaced; str(exc) enough for a generic
# handler
# ---------------------------------------------------------------------------


def test_refusal_is_renderable_by_a_generic_handler() -> None:
    """BR-U8-8: an un-updated log/print handler still emits a message."""
    exc = KiroLaunchRefusedError(
        code="profile-untranslatable",
        message="Profile 'x' cannot be translated (unsupported-settings).",
        profile_field="toolsSettings",
    )
    assert str(exc) == "Profile 'x' cannot be translated (unsupported-settings)."
    assert isinstance(exc, ValueError)


def test_cli_renders_the_code_and_field_from_a_structured_refusal() -> None:
    """BR-U8-7: the operator sees the code and, when derivable, the field."""
    import click

    from cli_agent_orchestrator.cli.commands.launch import _raise_launch_refusal

    response = Mock()
    response.status_code = 403
    response.json.return_value = {
        "detail": "Profile 'kas-bad' cannot be translated (unsupported-settings).",
        "code": "profile-untranslatable",
        "profile_field": "toolsSettings",
        "engine": "kas",
    }

    with pytest.raises(click.ClickException) as exc_info:
        _raise_launch_refusal(response)

    rendered = exc_info.value.message
    assert "profile-untranslatable" in rendered
    assert "toolsSettings" in rendered
    assert "cannot be translated" in rendered


def test_cli_omits_the_field_line_when_attribution_is_unavailable() -> None:
    """ADR-009: `profile_field` is legitimately None; nothing prints "None"."""
    import click

    from cli_agent_orchestrator.cli.commands.launch import _raise_launch_refusal

    response = Mock()
    response.status_code = 403
    response.json.return_value = {
        "detail": "Kiro engine 'kas' launch is not enabled.",
        "code": "launch-not-enabled",
        "profile_field": None,
        "engine": "kas",
    }

    with pytest.raises(click.ClickException) as exc_info:
        _raise_launch_refusal(response)

    rendered = exc_info.value.message
    assert "launch-not-enabled" in rendered
    assert "None" not in rendered
    assert "field:" not in rendered


@pytest.mark.parametrize(
    ("status_code", "body"),
    [
        (200, {"session_name": "s", "name": "t"}),
        (403, {"detail": "forbidden for an unrelated reason"}),
        (404, {"detail": "not found"}),
    ],
)
def test_cli_renderer_ignores_responses_that_are_not_structured_refusals(
    status_code: int, body: dict
) -> None:
    from cli_agent_orchestrator.cli.commands.launch import _raise_launch_refusal

    response = Mock()
    response.status_code = status_code
    response.json.return_value = body

    assert _raise_launch_refusal(response) is None


def test_api_serialises_the_refusal_as_json_with_stable_keys() -> None:
    """ADR-005: API consumers branch on `code`, not on prose."""
    from cli_agent_orchestrator.api.main import _render_kas_launch_refusal

    exc = KiroLaunchRefusedError(
        code="profile-untranslatable",
        message="Profile 'kas-bad' cannot be translated (unsupported-settings).",
        profile_field="toolsSettings",
    )
    response = asyncio.run(_render_kas_launch_refusal(Mock(), exc))

    assert response.status_code == 403
    payload = response.body.decode("utf-8")
    assert '"code":"profile-untranslatable"' in payload
    assert '"profile_field":"toolsSettings"' in payload
    assert '"engine":"kas"' in payload
