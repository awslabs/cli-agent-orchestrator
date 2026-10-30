"""Kiro v3 engine selection across CAO's surfaces, with v3 still refused (#1167 Phase 1).

``v3`` is the canonical engine value and ``kas`` an input alias stored as
``v3`` (design D1). Every point that refused ``kas`` now refuses v3 with one
``KiroV3UnavailableError`` before any allocation, ``cao launch`` shows the
server's refusal instead of "Failed to connect", and the wiki compiler's Kiro
call pins ``--agent-engine v2`` (D16). The names, messages and the wiki pin are
provisional choices of #1167.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
import requests
from click.testing import CliRunner

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.kiro_engine import KiroEngine
from cli_agent_orchestrator.providers.kiro_capabilities import (
    KiroCapabilities,
    KiroV3UnavailableError,
)
from cli_agent_orchestrator.services import terminal_service

_TS = "cli_agent_orchestrator.services.terminal_service"
_STEP = "cli_agent_orchestrator.services.agent_step"

V3_SPELLINGS = ["v3", "kas", KiroEngine.V3]

pytestmark = pytest.mark.usefixtures("isolated_memory_db")


# ---------------------------------------------------------------------------
# CLI: choices, forwarding, and the server's refusal body (build step 2)
# ---------------------------------------------------------------------------


def _ok_post(mock_post):
    mock_post.return_value.json.return_value = {
        "session_name": "test-session",
        "id": "test-terminal-id",
        "name": "test-terminal",
    }
    mock_post.return_value.raise_for_status.return_value = None


@pytest.mark.parametrize("spelling", ["v2", "v3", "kas"])
def test_cao_launch_accepts_each_engine_choice(spelling):
    from cli_agent_orchestrator.cli.commands.launch import launch

    with patch("cli_agent_orchestrator.cli.commands.launch.api_http.post") as mock_post:
        _ok_post(mock_post)
        result = CliRunner().invoke(
            launch, ["--agents", "a", "--engine", spelling, "--headless", "--yolo"]
        )

    assert result.exit_code == 0, result.output
    assert mock_post.call_args.kwargs["params"]["engine"] == spelling


def test_cao_launch_rejects_an_unknown_engine_before_any_request():
    from cli_agent_orchestrator.cli.commands.launch import launch

    with patch("cli_agent_orchestrator.cli.commands.launch.api_http.post") as mock_post:
        result = CliRunner().invoke(
            launch, ["--agents", "a", "--engine", "v4", "--headless", "--yolo"]
        )

    assert result.exit_code == 2
    mock_post.assert_not_called()


def _http_response(status: int, payload, url: str = "http://127.0.0.1:9889/sessions"):
    response = requests.Response()
    response.status_code = status
    response.reason = {400: "Bad Request", 422: "Unprocessable Entity"}.get(status, "Error")
    response.url = url
    response._content = json.dumps(payload).encode("utf-8")
    return response


def test_cao_launch_shows_the_servers_400_detail_not_a_connection_failure():
    """The 400 body reaches the operator (``Q8-3b`` showed "Failed to connect")."""
    from cli_agent_orchestrator.cli.commands.launch import launch

    detail = str(KiroV3UnavailableError())
    with patch(
        "cli_agent_orchestrator.cli.commands.launch.api_http.post",
        return_value=_http_response(400, {"detail": detail}),
    ):
        result = CliRunner().invoke(
            launch, ["--agents", "a", "--engine", "kas", "--headless", "--yolo"]
        )

    assert result.exit_code == 1
    assert "Failed to connect" not in result.output
    assert "refused the launch (400)" in result.output
    assert "not available yet" in result.output


def test_cao_launch_falls_back_to_the_http_error_for_a_non_string_detail():
    from cli_agent_orchestrator.cli.commands.launch import launch

    payload = {"detail": [{"loc": ["query", "engine"], "msg": "Input should be 'v2' or 'v3'"}]}
    with patch(
        "cli_agent_orchestrator.cli.commands.launch.api_http.post",
        return_value=_http_response(422, payload),
    ):
        result = CliRunner().invoke(launch, ["--agents", "a", "--headless", "--yolo"])

    assert result.exit_code == 1
    assert "refused the launch (422)" in result.output
    assert "422 Client Error" in result.output
    assert "Failed to connect" not in result.output


def test_cao_launch_still_reports_a_real_connection_failure():
    from cli_agent_orchestrator.cli.commands.launch import launch

    with patch(
        "cli_agent_orchestrator.cli.commands.launch.api_http.post",
        side_effect=requests.exceptions.ConnectionError("connection refused"),
    ):
        result = CliRunner().invoke(launch, ["--agents", "a", "--headless", "--yolo"])

    assert result.exit_code == 1
    assert "Failed to connect to cao-server" in result.output


# ---------------------------------------------------------------------------
# Idempotency fingerprints: ``kas`` and ``v3`` are one request
# ---------------------------------------------------------------------------


def _fingerprint(**overrides):
    args = dict(
        provider="kiro_cli",
        agent_profile="developer",
        session_name="cao-session",
        working_directory=None,
        caller_id=None,
        model=None,
        use_worktree=False,
        engine=None,
        allowed_tools=None,
        env_vars=None,
        resume_session_id=None,
        initial_message=None,
        initial_message_orchestration_type=None,
    )
    args.update(overrides)
    return terminal_service._request_fingerprint(**args)


def test_create_fingerprint_is_one_key_for_every_v3_spelling():
    keys = {_fingerprint(engine=spelling) for spelling in V3_SPELLINGS}
    assert len(keys) == 1
    assert keys != {_fingerprint(engine="v2")}
    assert keys != {_fingerprint(engine=None)}
    # Pure: an invalid value is hashed as itself, not rejected or mapped.
    assert _fingerprint(engine="bogus") not in keys


@pytest.mark.parametrize("spelling", V3_SPELLINGS)
def test_step_call_fingerprint_carries_the_canonical_engine(spelling):
    """``run_agent_step`` hands ``compute`` the canonical value for every spelling."""
    from cli_agent_orchestrator.models.terminal import TerminalStatus
    from cli_agent_orchestrator.services.agent_step import run_agent_step

    seen = []

    def _capture(fields):
        seen.append(fields.engine)
        return "0" * 64

    created = Mock()
    created.id = "abc12345"
    with (
        patch(f"{_STEP}.compute", side_effect=_capture),
        patch(f"{_STEP}.terminal_service.create_terminal", new=AsyncMock(return_value=created)),
        patch(f"{_STEP}.terminal_service.send_input", return_value=True),
        patch(f"{_STEP}.terminal_service.delete_terminal", return_value=True),
        patch(f"{_STEP}.terminal_service.get_output", return_value="the answer"),
        patch(f"{_STEP}.terminal_service.exit_terminal_cli", return_value=None),
        patch(f"{_STEP}.terminal_service.get_working_directory", return_value=None),
        patch(f"{_STEP}.wait_until_status", new=AsyncMock(return_value=True)),
        patch(f"{_STEP}.status_monitor.get_status", return_value=TerminalStatus.COMPLETED),
    ):
        result = asyncio.run(run_agent_step("kiro_cli", "dev", "x", engine=spelling))

    assert result.status == TerminalStatus.COMPLETED
    assert seen == ["v3"]


# ---------------------------------------------------------------------------
# Refusal points: v3 refuses after the probe and before any allocation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", V3_SPELLINGS)
async def test_explicit_v3_probes_then_refuses_before_allocation(spelling):
    probe = Mock(return_value=KiroCapabilities(version="2.35.1", flags=frozenset({"--v3"})))
    profile = AgentProfile(name="developer", description="d")

    with (
        patch(f"{_TS}.agent_profiles.load_agent_profile", return_value=profile),
        patch(f"{_TS}.get_backend") as backend,
        patch(f"{_TS}.db_create_terminal") as db_create,
        patch(f"{_TS}.fifo_manager") as fifo,
        patch(f"{_TS}.provider_manager") as providers,
    ):
        with pytest.raises(KiroV3UnavailableError, match="not available yet"):
            await terminal_service.create_terminal(
                provider="kiro_cli",
                agent_profile="developer",
                new_session=True,
                engine=spelling,
                kiro_capability_probe=probe,
            )

    probe.assert_called_once_with(KiroEngine.V3, {"profile", "trust", "trust_workspace"})
    backend.return_value.create_session.assert_not_called()
    backend.return_value.create_window.assert_not_called()
    db_create.assert_not_called()
    fifo.create_reader.assert_not_called()
    providers.create_provider.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("profile_engine", ["v3", "kas"])
async def test_profile_v3_refuses_and_agrees_with_an_explicit_alias(profile_engine):
    probe = Mock(return_value=KiroCapabilities(version="2.35.1", flags=frozenset({"--v3"})))
    profile = AgentProfile(name="developer", description="d", engine=profile_engine)

    with (
        patch(f"{_TS}.agent_profiles.load_agent_profile", return_value=profile),
        patch(f"{_TS}.get_backend") as backend,
        patch(f"{_TS}.db_create_terminal") as db_create,
    ):
        # ``kas`` explicit + ``v3`` profile (and vice versa) is agreement, not a conflict.
        explicit = "kas" if profile_engine == "v3" else "v3"
        with pytest.raises(KiroV3UnavailableError):
            await terminal_service.create_terminal(
                provider="kiro_cli",
                agent_profile="developer",
                new_session=True,
                engine=explicit,
                kiro_capability_probe=probe,
            )

    backend.return_value.create_window.assert_not_called()
    db_create.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", ["v3", "kas"])
async def test_reuse_with_a_v3_request_refuses_before_any_send(spelling):
    from cli_agent_orchestrator.services.agent_step import _validate_reused_terminal

    with patch(
        f"{_STEP}.terminal_service.get_terminal_metadata",
        return_value={"id": "reuse99", "provider": "kiro_cli", "engine": "v2"},
    ):
        with pytest.raises(KiroV3UnavailableError):
            await _validate_reused_terminal("reuse99", "kiro_cli", spelling)


@pytest.mark.asyncio
async def test_reuse_mismatch_message_is_unchanged_and_canonical():
    from cli_agent_orchestrator.services.agent_step import _validate_reused_terminal

    with patch(
        f"{_STEP}.terminal_service.get_terminal_metadata",
        return_value={"id": "reuse99", "provider": "kiro_cli", "engine": "v3"},
    ):
        with pytest.raises(ValueError) as excinfo:
            await _validate_reused_terminal("reuse99", "kiro_cli", "v2")

    assert str(excinfo.value) == (
        "Kiro engine mismatch for reused terminal 'reuse99': requested 'v2', persisted 'v3'"
    )


def test_provider_construction_refuses_v3_through_the_manager():
    from cli_agent_orchestrator.providers.manager import ProviderManager

    with patch("cli_agent_orchestrator.providers.manager.KiroCliProvider") as provider_class:
        with pytest.raises(KiroV3UnavailableError):
            ProviderManager().create_provider(
                "kiro_cli", "t1", "s1", "w1", "developer", engine=KiroEngine.V3
            )
    provider_class.assert_not_called()


def test_a_cached_v3_provider_is_never_handed_out():
    from cli_agent_orchestrator.providers.kiro_cli import KiroCliProvider
    from cli_agent_orchestrator.providers.manager import ProviderManager

    manager = ProviderManager()
    manager._providers["t1"] = KiroCliProvider("t1", "s1", "w1", "developer", engine=KiroEngine.V3)
    with pytest.raises(KiroV3UnavailableError):
        manager.get_provider("t1")


def test_direct_provider_initialize_refuses_v3():
    from cli_agent_orchestrator.providers.kiro_cli import KiroCliProvider

    provider = KiroCliProvider("t1", "s1", "w1", "developer", engine=KiroEngine.V3)
    with patch("cli_agent_orchestrator.providers.kiro_cli.get_backend") as backend:
        with pytest.raises(KiroV3UnavailableError):
            asyncio.run(provider.initialize())
    backend.return_value.send_keys.assert_not_called()


# ---------------------------------------------------------------------------
# Wiki compiler (D16, G14): the Kiro call pins v2; a nonzero exit is llm_error
# ---------------------------------------------------------------------------


def test_wiki_compiler_kiro_argv_pins_agent_engine_v2():
    from cli_agent_orchestrator.services import wiki_compiler

    argv = wiki_compiler._CLI_BACKENDS["kiro_cli"].build_argv("PROMPT", None)
    assert argv[:2] == ["kiro-cli", "chat"]
    index = argv.index("--agent-engine")
    assert argv[index + 1] == "v2"
    assert argv[-1] == "PROMPT"
    assert "--v3" not in argv


def test_wiki_compiler_kiro_nonzero_exit_still_falls_back_as_llm_error(tmp_path, monkeypatch):
    """A fake ``kiro-cli`` on PATH records its argv and exits 3; no real Kiro runs."""
    from cli_agent_orchestrator.services import wiki_compiler

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_file = tmp_path / "argv.bin"
    fake = bin_dir / "kiro-cli"
    fake.write_text(
        "#!/bin/sh\n"
        f"for a in \"$@\"; do printf '%s\\0' \"$a\"; done > '{argv_file}'\n"
        "exit 3\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin")

    result = asyncio.run(
        wiki_compiler.compile(
            "<!-- id: t -->\n# t\n\nold fact.\n",
            "new fact.",
            topic_key="t",
            timeout_s=10.0,
            provider_hint="kiro_cli",
        )
    )

    assert result.used_llm is False
    assert result.fallback_reason == "llm_error"
    recorded = Path(argv_file).read_bytes().split(b"\0")
    assert recorded[:3] == [b"chat", b"--agent-engine", b"v2"]
