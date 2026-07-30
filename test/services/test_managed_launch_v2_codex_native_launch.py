"""Managed v2 launches Codex as an exact resumed native TUI."""

from __future__ import annotations

import hashlib
import os
import subprocess
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.managed_launch_v2 import (
    PROTOCOL_VERSION_V2,
    ManagedLaunchV2ReserveRequest,
)
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import codex_native_bootstrap as bootstrap
from cli_agent_orchestrator.services import managed_launch_v2 as v2
from cli_agent_orchestrator.services import managed_provider_bridge as bridge
from cli_agent_orchestrator.services import native_attachment, native_pane_input

SESSION = "019fb17d-0c6d-7161-a408-6b1fa61c8f2d"
MODEL = "gpt-5.6-sol"
EFFORT = "xhigh"


@pytest.fixture(autouse=True)
def _companion(tmp_path, monkeypatch):
    monkeypatch.setattr(v2, "COMPANION_DIR", tmp_path / "companion")
    monkeypatch.setattr(bridge, "BRIDGE_ROOT", tmp_path / "bridge")


@pytest.fixture
def worktree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def _request(worktree, tmp_path):
    executable = tmp_path / "codex"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    executable = os.path.realpath(executable)
    return ManagedLaunchV2ReserveRequest(
        protocol_version=PROTOCOL_VERSION_V2,
        reservation_id=str(uuid.uuid4()),
        session_name="cao-test",
        provider="codex",
        agent_profile="reviewer",
        caller_id="deadbeef",
        working_directory=os.path.realpath(worktree),
        trusted_project_root=os.path.realpath(worktree),
        expected_model=MODEL,
        expected_effort=EFFORT,
        provider_executable=executable,
        provider_executable_sha256=hashlib.sha256(open(executable, "rb").read()).hexdigest(),
        obligation_generation="obgen-codex-native",
        task_id="codex-native-test",
        run_id="run-codex-native",
        delivery_id=str(uuid.uuid4()),
        launch_nonce="n" * 40,
        execution_mode="native_tui",
    )


@pytest.mark.asyncio
async def test_launch_resumes_the_bootstrapped_thread_as_the_pane_process(
    isolated_memory_db, worktree, tmp_path, monkeypatch
):
    launched: list[dict[str, Any]] = []
    bootstrap_calls: list[dict[str, Any]] = []

    def mint_session(**kwargs):
        bootstrap_calls.append(kwargs)
        return {
            "schema": bootstrap.BOOTSTRAP_SCHEMA,
            "provider": "codex",
            "native_session_id": SESSION,
            "id_source": "app_server_thread_start",
            "provider_version": "0.146.0",
            "binary_path": kwargs["codex_binary"],
            "binary_sha256": kwargs["binary_sha256"],
            "working_directory": kwargs["working_directory"],
            "model": kwargs["model"],
            "effort": kwargs["effort"],
            "sent_no_turn": True,
            "materialization_method": "thread/name/set",
            "materialization_sent_no_turn": True,
            "rollout_path": os.path.join(
                kwargs["working_directory"], f"rollout-test-{SESSION}.jsonl"
            ),
            "rollout_sha256": "a" * 64,
            "detached_before_launch": True,
            "exit_proof": {"reaped": True, "exit_status": -15},
        }

    async def create_terminal(**kwargs):
        launched.append(kwargs)
        database.create_terminal_v2(
            kwargs["reserved_terminal_id"],
            kwargs["session_name"],
            kwargs.get("window_name") or f"w-{kwargs['reserved_terminal_id']}",
            kwargs["provider"],
            generation=kwargs["terminal_generation"],
            pane_id="%7",
            window_id="@7",
            server_socket_path="/private/tmp/cao-native.sock",
            session_id="$1",
            pane_pid=4242,
        )
        return {"terminal_id": kwargs["reserved_terminal_id"]}

    def observe(pane):
        return {
            "pane_id": "%7",
            "pid": 4242,
            "start_marker": "Thu Jul 30 01:00:00 2026",
            "argv": list(launched[-1]["managed_native_command"]),
            "cwd": pane._record["working_directory"],
        }

    monkeypatch.setattr(bootstrap, "mint_session", mint_session)
    monkeypatch.setattr(
        bridge,
        "provider_version_banner",
        lambda *args, **kwargs: "codex-cli 0.146.0",
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.create_terminal",
        create_terminal,
    )
    monkeypatch.setattr(v2._V2NativePane, "observe", observe)
    monkeypatch.setattr(
        native_pane_input,
        "observe_codex_turn_state",
        lambda *args, **kwargs: TerminalStatus.IDLE,
    )

    record, _ = v2.reserve(_request(worktree, tmp_path))
    result = await v2.launch_reserved(record["reservation_id"])

    assert result["state"] == "launching", result["preflight_failure"]["detail"]
    assert len(bootstrap_calls) == 1
    assert bootstrap_calls[0]["model"] == MODEL
    assert bootstrap_calls[0]["effort"] == EFFORT
    argv = launched[0]["managed_native_command"]
    assert argv[-2:] == ["resume", SESSION]
    assert "--no-alt-screen" in argv
    assert "app-server" not in argv

    state = bridge.read_state(record["reservation_id"])
    receipt = state["readiness"]
    assert receipt["provider_receipt_kind"] == "codex-native-thread-start"
    assert receipt["provider_session_id"] == SESSION
    assert receipt["model_input_ready"] is True
    assert receipt["provider_session_start_proven"] is False

    attachment = native_attachment.get("codex", SESSION)
    assert attachment["state"] == native_attachment.ATTACHED
    assert attachment["owner"]["execution_mode"] == "native_tui"


def test_codex_native_profile_preserves_mcp_env_inheritance_and_timeout(tmp_path):
    args = v2._codex_profile_launch_args(
        record={
            "terminal_id": "term-codex",
            "generation": "gen-codex",
            "working_directory": os.path.realpath(tmp_path),
        },
        request={"expected_model": MODEL, "expected_effort": EFFORT},
        profile_material={
            "profile": SimpleNamespace(
                codexProfile=None,
                codexConfig={},
            ),
            "allowed_tools": ["*"],
            "system_prompt": "",
            "mcp_servers": [
                {
                    "name": "context7",
                    "command": "/usr/bin/env",
                    "args": ["context7"],
                    "env": [],
                    "env_vars": ["HOME", "PATH"],
                    "tool_timeout_sec": 90,
                }
            ],
        },
        tui=True,
    )

    assert 'mcp_servers.context7.env_vars=["HOME", "PATH", "CAO_TERMINAL_ID"]' in args
    assert "mcp_servers.context7.tool_timeout_sec=90.0" in args
