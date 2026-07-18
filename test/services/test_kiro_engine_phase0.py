"""Fail-closed terminal-service coverage for Kiro Phase 0."""

import subprocess
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.kiro_engine import KiroEngine
from cli_agent_orchestrator.providers.kiro_capabilities import (
    KiroCapabilities,
    KiroCapabilityError,
    KiroPhase0KASError,
    probe_kiro_capabilities,
)
from cli_agent_orchestrator.services.agent_step import run_agent_step
from cli_agent_orchestrator.services.terminal_service import create_terminal

_MODULE = "cli_agent_orchestrator.services.terminal_service"


@pytest.mark.asyncio
async def test_kas_probes_then_rejects_before_backend_or_persistence_allocation():
    """KAS Phase 0 rejection happens before every terminal lifecycle side effect."""
    probe = Mock(
        return_value=KiroCapabilities(version="2.13.0", flags=frozenset({"--v3", "--agent"}))
    )
    profile = AgentProfile(
        name="kas-profile",
        description="KAS profile",
        engine=KiroEngine.KAS,
        allowedTools=["fs_read"],
        toolsSettings={"cao-mcp-server": {"enabled": True}},
    )

    with (
        patch(f"{_MODULE}.load_agent_profile", return_value=profile),
        patch(f"{_MODULE}.get_backend") as backend,
        patch(f"{_MODULE}.db_create_terminal") as db_create,
        patch(f"{_MODULE}.fifo_manager") as fifo,
        patch(f"{_MODULE}.provider_manager") as providers,
    ):
        with pytest.raises(KiroPhase0KASError, match="Cedar"):
            await create_terminal(
                provider="kiro_cli",
                agent_profile="kas-profile",
                new_session=True,
                kiro_capability_probe=probe,
            )

    probe.assert_called_once_with(KiroEngine.KAS, {"profile"})
    backend.return_value.create_session.assert_not_called()
    backend.return_value.create_window.assert_not_called()
    db_create.assert_not_called()
    fifo.create_reader.assert_not_called()
    providers.create_provider.assert_not_called()


@pytest.mark.asyncio
async def test_explicit_and_profile_engines_conflict_before_probe_or_allocation():
    profile = AgentProfile(name="kas-profile", description="KAS profile", engine=KiroEngine.KAS)
    probe = Mock()

    with (
        patch(f"{_MODULE}.load_agent_profile", return_value=profile),
        patch(f"{_MODULE}.get_backend") as backend,
        patch(f"{_MODULE}.db_create_terminal") as db_create,
    ):
        with pytest.raises(ValueError, match="conflict"):
            await create_terminal(
                provider="kiro_cli",
                agent_profile="kas-profile",
                new_session=True,
                engine=KiroEngine.V2,
                kiro_capability_probe=probe,
            )

    probe.assert_not_called()
    backend.return_value.create_session.assert_not_called()
    db_create.assert_not_called()


@pytest.mark.asyncio
async def test_omitted_engine_launches_as_explicitly_pinned_v2():
    """Callers that omit engine still create a Kiro provider pinned to v2."""
    probe = Mock(
        return_value=KiroCapabilities(
            version="2.13.0",
            flags=frozenset({"--agent-engine", "--agent", "--trust-all-tools"}),
        )
    )
    provider = MagicMock()
    provider.initialize = AsyncMock(return_value=True)
    provider.shell_baseline = None

    with (
        patch(
            f"{_MODULE}.load_agent_profile",
            return_value=AgentProfile(name="developer", description="Developer"),
        ),
        patch(f"{_MODULE}.get_backend") as backend,
        patch(f"{_MODULE}.db_create_terminal") as db_create,
        patch(f"{_MODULE}.fifo_manager"),
        patch(f"{_MODULE}.provider_manager") as providers,
        patch(f"{_MODULE}.generate_terminal_id", return_value="test1234"),
        patch(f"{_MODULE}.generate_session_name", return_value="session"),
        patch(f"{_MODULE}.generate_window_name", return_value="developer-window"),
        patch(f"{_MODULE}.get_herdr_inbox_service", return_value=None),
    ):
        backend.return_value.session_exists.return_value = False
        backend.return_value.supports_event_inbox.return_value = True
        providers.create_provider.return_value = provider

        terminal = await create_terminal(
            provider="kiro_cli",
            agent_profile="developer",
            new_session=True,
            kiro_capability_probe=probe,
        )

    assert terminal.engine == KiroEngine.V2
    probe.assert_called_once_with(KiroEngine.V2, {"profile", "trust", "ui"})
    assert providers.create_provider.call_args.kwargs["engine"] == KiroEngine.V2
    assert db_create.call_args.kwargs["engine"] == "v2"


@pytest.mark.asyncio
async def test_non_yolo_v2_missing_legacy_ui_rejects_before_allocation():
    """The optional fallback flag is probed before any v2 lifecycle allocation."""

    def missing_ui_probe(engine: KiroEngine, requested: set[str]) -> KiroCapabilities:
        def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            output = (
                "kiro-cli version 2.13.0"
                if command[-1] == "--version"
                else "--agent-engine v2\n--agent NAME\n--trust-all-tools\n"
            )
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

        return probe_kiro_capabilities(engine, requested, runner=runner)

    probe = Mock(side_effect=missing_ui_probe)
    profile = AgentProfile(name="developer", description="Developer")

    with (
        patch(f"{_MODULE}.load_agent_profile", return_value=profile),
        patch(f"{_MODULE}.get_backend") as backend,
        patch(f"{_MODULE}.db_create_terminal") as db_create,
        patch(f"{_MODULE}.fifo_manager") as fifo,
        patch(f"{_MODULE}.provider_manager") as providers,
    ):
        with pytest.raises(KiroCapabilityError, match="--legacy-ui") as exc_info:
            await create_terminal(
                provider="kiro_cli",
                agent_profile="developer",
                new_session=True,
                kiro_capability_probe=probe,
            )

    assert exc_info.value.kind == "unsupported_capability"
    assert exc_info.value.engine == KiroEngine.V2
    assert exc_info.value.capability == "--legacy-ui"
    probe.assert_called_once_with(KiroEngine.V2, {"profile", "trust", "ui"})
    backend.return_value.create_session.assert_not_called()
    backend.return_value.create_window.assert_not_called()
    db_create.assert_not_called()
    fifo.create_reader.assert_not_called()
    providers.create_provider.assert_not_called()


@pytest.mark.asyncio
async def test_yolo_v2_prose_only_trust_flag_rejects_before_allocation():
    """A prose-only yolo flag cannot authorize terminal lifecycle allocation."""

    def prose_only_trust_probe(engine: KiroEngine, requested: set[str]) -> KiroCapabilities:
        def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            output = (
                "kiro-cli version 2.13.0"
                if command[-1] == "--version"
                else (
                    "--agent-engine v2\n--agent NAME\n--legacy-ui\n"
                    "--trust-all-tools bypasses confirmation when enabled\n"
                )
            )
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

        return probe_kiro_capabilities(engine, requested, runner=runner)

    probe = Mock(side_effect=prose_only_trust_probe)
    profile = AgentProfile(name="developer", description="Developer")

    with (
        patch(f"{_MODULE}.load_agent_profile", return_value=profile),
        patch(f"{_MODULE}.get_backend") as backend,
        patch(f"{_MODULE}.db_create_terminal") as db_create,
        patch(f"{_MODULE}.fifo_manager") as fifo,
        patch(f"{_MODULE}.provider_manager") as providers,
    ):
        with pytest.raises(KiroCapabilityError, match="--trust-all-tools") as exc_info:
            await create_terminal(
                provider="kiro_cli",
                agent_profile="developer",
                allowed_tools=["*"],
                new_session=True,
                kiro_capability_probe=probe,
            )

    assert exc_info.value.kind == "unsupported_capability"
    assert exc_info.value.engine == KiroEngine.V2
    assert exc_info.value.capability == "--trust-all-tools"
    probe.assert_called_once_with(KiroEngine.V2, {"profile", "ui", "trust"})
    backend.return_value.create_session.assert_not_called()
    backend.return_value.create_window.assert_not_called()
    db_create.assert_not_called()
    fifo.create_reader.assert_not_called()
    providers.create_provider.assert_not_called()


@pytest.mark.asyncio
async def test_yolo_v2_required_value_trust_flag_rejects_before_allocation():
    """A yolo launch cannot use a wrapper that requires a trust option value."""

    def required_value_trust_probe(engine: KiroEngine, requested: set[str]) -> KiroCapabilities:
        def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            output = (
                "kiro-cli version 2.13.0"
                if command[-1] == "--version"
                else ("--agent-engine v2\n--agent NAME\n--legacy-ui\n" "--trust-all-tools VALUE\n")
            )
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

        return probe_kiro_capabilities(engine, requested, runner=runner)

    probe = Mock(side_effect=required_value_trust_probe)
    profile = AgentProfile(name="developer", description="Developer")

    with (
        patch(f"{_MODULE}.load_agent_profile", return_value=profile),
        patch(f"{_MODULE}.get_backend") as backend,
        patch(f"{_MODULE}.db_create_terminal") as db_create,
        patch(f"{_MODULE}.fifo_manager") as fifo,
        patch(f"{_MODULE}.provider_manager") as providers,
        patch(f"{_MODULE}.generate_terminal_id") as terminal_id,
    ):
        with pytest.raises(KiroCapabilityError, match="--trust-all-tools") as exc_info:
            await create_terminal(
                provider="kiro_cli",
                agent_profile="developer",
                allowed_tools=["*"],
                new_session=True,
                kiro_capability_probe=probe,
            )

    assert exc_info.value.kind == "unsupported_capability"
    assert exc_info.value.engine == KiroEngine.V2
    assert exc_info.value.capability == "--trust-all-tools"
    probe.assert_called_once_with(KiroEngine.V2, {"profile", "ui", "trust"})
    terminal_id.assert_not_called()
    backend.return_value.create_session.assert_not_called()
    backend.return_value.create_window.assert_not_called()
    db_create.assert_not_called()
    fifo.create_reader.assert_not_called()
    providers.create_provider.assert_not_called()


@pytest.mark.asyncio
async def test_v2_agent_engine_value_exclusion_rejects_before_allocation():
    """A wrapper advertising only v1/v3 must not allocate a v2 lifecycle."""

    def v1_v3_only_probe(engine: KiroEngine, requested: set[str]) -> KiroCapabilities:
        def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            output = (
                "kiro-cli version 2.13.0"
                if command[-1] == "--version"
                else ("--agent-engine v1|v3\n--agent NAME\n" "--legacy-ui\n--trust-all-tools\n")
            )
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

        return probe_kiro_capabilities(engine, requested, runner=runner)

    probe = Mock(side_effect=v1_v3_only_probe)
    profile = AgentProfile(name="developer", description="Developer")

    with (
        patch(f"{_MODULE}.load_agent_profile", return_value=profile),
        patch(f"{_MODULE}.get_backend") as backend,
        patch(f"{_MODULE}.db_create_terminal") as db_create,
        patch(f"{_MODULE}.fifo_manager") as fifo,
        patch(f"{_MODULE}.provider_manager") as providers,
    ):
        with pytest.raises(KiroCapabilityError, match="accept 'v2'") as exc_info:
            await create_terminal(
                provider="kiro_cli",
                agent_profile="developer",
                new_session=True,
                kiro_capability_probe=probe,
            )

    assert exc_info.value.kind == "unsupported_capability"
    assert exc_info.value.engine == KiroEngine.V2
    assert exc_info.value.capability == "--agent-engine=v2"
    probe.assert_called_once_with(KiroEngine.V2, {"profile", "trust", "ui"})
    backend.return_value.create_session.assert_not_called()
    backend.return_value.create_window.assert_not_called()
    db_create.assert_not_called()
    fifo.create_reader.assert_not_called()
    providers.create_provider.assert_not_called()


def test_send_input_rejects_persisted_kas_before_provider_or_pane_access():
    metadata = {
        "id": "persisted-kas",
        "provider": "kiro_cli",
        "engine": "kas",
        "tmux_session": "cao-session",
        "tmux_window": "developer-window",
    }

    with (
        patch(f"{_MODULE}.get_terminal_metadata", return_value=metadata),
        patch(f"{_MODULE}.provider_manager") as providers,
        patch(f"{_MODULE}.get_backend") as backend,
    ):
        with pytest.raises(KiroPhase0KASError, match="Cedar"):
            from cli_agent_orchestrator.services.terminal_service import send_input

            send_input("persisted-kas", "must not be delivered")

    providers.get_provider.assert_not_called()
    backend.return_value.send_keys.assert_not_called()


@pytest.mark.asyncio
async def test_agent_step_reuse_rejects_persisted_kas_before_pane_write():
    metadata = {
        "id": "persisted-kas",
        "provider": "kiro_cli",
        "engine": "kas",
        "tmux_session": "cao-session",
        "tmux_window": "developer-window",
    }

    with (
        patch(f"{_MODULE}.get_terminal_metadata", return_value=metadata),
        patch(f"{_MODULE}.provider_manager") as providers,
        patch(f"{_MODULE}.get_backend") as backend,
    ):
        with pytest.raises(KiroPhase0KASError, match="Cedar"):
            await run_agent_step(
                provider="kiro_cli",
                agent="developer",
                prompt="must not be delivered",
                reuse_terminal_id="persisted-kas",
            )

    providers.get_provider.assert_not_called()
    backend.return_value.send_keys.assert_not_called()
