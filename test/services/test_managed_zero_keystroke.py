"""P1-4/P1-9 (spec §20.2d(3)/(7), §20.2e): managed zero-keystroke bridge
creation and the minimal allowlisted provider environment.

Proves the managed bridge is started as the explicit process/argv at window
creation with ZERO send_keys/special-key/generic-input calls, that a shell in
the pane fails closed, that backends without atomic process creation fail
closed, and that the bridge/provider environment rejects protected control
variables and never inherits ambient PATH/control state.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from cli_agent_orchestrator.services import terminal_service
from cli_agent_orchestrator.services.managed_provider_bridge import (
    BridgeError,
    _assert_bridge_environment,
    _provider_env,
)

SESSION = "cao-test"
TERMINAL_ID = "abcd1234"
GENERATION = "11111111-2222-3333-4444-555555555555"
BRIDGE_ARGV = [
    "/usr/bin/python3",
    "-I",
    "-m",
    "cli_agent_orchestrator.services.managed_provider_bridge",
    "--reservation-id",
    "r1",
]


class FakeBackend:
    def __init__(self, *, pane_command="python3", event_inbox=True):
        self.calls = []
        self.pane_command = pane_command
        self.event_inbox = event_inbox

    def session_exists(self, name):
        return True

    def create_window_with_argv(
        self, session, window, terminal_id, argv, working_directory=None, extra_env=None
    ):
        self.calls.append(
            (
                "create_window_with_argv",
                session,
                window,
                terminal_id,
                list(argv),
                working_directory,
                extra_env,
            )
        )
        return window

    def create_window(self, *args, **kwargs):
        self.calls.append(("create_window", args, kwargs))
        return args[1]

    def supports_event_inbox(self):
        return self.event_inbox

    def window_identity(self, session, window):
        return {"pane_id": "%9", "window_id": "@9"}

    def get_pane_current_command(self, session, window):
        return self.pane_command

    def kill_window(self, session, window):
        self.calls.append(("kill_window", session, window))
        return True

    def pipe_pane(self, *args, **kwargs):
        self.calls.append(("pipe_pane", args))

    def stop_pipe_pane(self, *args, **kwargs):
        self.calls.append(("stop_pipe_pane", args))

    def send_keys(self, *args, **kwargs):
        self.calls.append(("send_keys", args, kwargs))

    def send_special_key(self, *args, **kwargs):
        self.calls.append(("send_special_key", args, kwargs))


def _patch_common(monkeypatch, backend):
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(terminal_service, "db_create_terminal", lambda *a, **k: {"id": a[0]})
    monkeypatch.setattr(terminal_service, "get_terminal_metadata", lambda *a, **k: None)
    monkeypatch.setattr(
        terminal_service,
        "fifo_manager",
        SimpleNamespace(create_reader=lambda *a, **k: None, stop_reader=lambda *a: None),
    )
    monkeypatch.setattr(
        terminal_service,
        "status_monitor",
        SimpleNamespace(clear_terminal=lambda *a: None),
    )
    monkeypatch.setattr(
        terminal_service,
        "provider_manager",
        SimpleNamespace(cleanup_provider=lambda *a: None),
    )
    # The managed path must never consult operator session env (P1-9).
    monkeypatch.setattr(
        terminal_service,
        "get_session_env",
        lambda name: pytest.fail(f"get_session_env consulted for managed launch: {name}"),
    )


@pytest.mark.asyncio
async def test_managed_bridge_is_explicit_argv_with_zero_keystrokes(monkeypatch):
    backend = FakeBackend(event_inbox=False)  # pipe-pane backend: hardest case
    _patch_common(monkeypatch, backend)

    terminal = await terminal_service.create_terminal(
        provider="codex",
        agent_profile="reviewer-sol-max",
        session_name=SESSION,
        new_session=False,
        working_directory="/tmp",
        registry=None,
        reserved_terminal_id=TERMINAL_ID,
        terminal_generation=GENERATION,
        preserve_on_init_failure=True,
        managed_native_command=BRIDGE_ARGV,
    )

    assert terminal.id == TERMINAL_ID
    kinds = [call[0] for call in backend.calls]
    assert "create_window_with_argv" in kinds
    assert "create_window" not in kinds
    assert "send_keys" not in kinds
    assert "send_special_key" not in kinds  # no Enter nudge, no answers
    call = next(c for c in backend.calls if c[0] == "create_window_with_argv")
    assert call[4] == BRIDGE_ARGV  # the exact bridge argv at window creation
    assert call[6] == {}  # minimal managed window env, no session-env merge


@pytest.mark.asyncio
async def test_managed_native_window_receives_only_its_explicit_environment(
    monkeypatch,
):
    backend = FakeBackend(event_inbox=False)
    _patch_common(monkeypatch, backend)

    await terminal_service.create_terminal(
        provider="kimi_cli",
        agent_profile="reviewer",
        session_name=SESSION,
        new_session=False,
        working_directory="/tmp",
        registry=None,
        env_vars={
            "PATH": "/usr/bin:/bin",
            "KIMI_CODE_HOME": "/tmp/private-kimi-home",
        },
        reserved_terminal_id=TERMINAL_ID,
        terminal_generation=GENERATION,
        preserve_on_init_failure=True,
        managed_native_command=["/opt/homebrew/bin/kimi", "--auto"],
        native_status_source=True,
    )

    call = next(c for c in backend.calls if c[0] == "create_window_with_argv")
    assert call[6] == {
        "PATH": "/usr/bin:/bin",
        "KIMI_CODE_HOME": "/tmp/private-kimi-home",
    }


@pytest.mark.asyncio
async def test_shell_in_pane_fails_closed_and_kills_window(monkeypatch):
    backend = FakeBackend(pane_command="zsh")
    _patch_common(monkeypatch, backend)

    with pytest.raises(RuntimeError, match="did not start the bridge"):
        await terminal_service.create_terminal(
            provider="codex",
            agent_profile="reviewer-sol-max",
            session_name=SESSION,
            new_session=False,
            working_directory="/tmp",
            registry=None,
            reserved_terminal_id=TERMINAL_ID,
            terminal_generation=GENERATION,
            preserve_on_init_failure=True,
            managed_native_command=BRIDGE_ARGV,
        )
    assert ("kill_window", SESSION, f"managed-{TERMINAL_ID}-{GENERATION}") in [
        tuple(c[:3]) for c in backend.calls if c[0] == "kill_window"
    ] or any(c[0] == "kill_window" for c in backend.calls)
    assert not [c for c in backend.calls if c[0] in {"send_keys", "send_special_key"}]


@pytest.mark.asyncio
async def test_backend_without_atomic_creation_fails_closed(monkeypatch):
    from cli_agent_orchestrator.backends.base import (
        TerminalBackend,
        TerminalBackendError,
    )

    class LegacyBackend(TerminalBackend):
        """Implements only the abstract surface; atomic argv creation is the
        un-overridden base default, which must fail closed."""

        def create_session(self, *a, **k): ...
        def session_exists(self, session_name):
            return True

        def list_sessions(self):
            return []

        def kill_session(self, session_name):
            return True

        def create_window(self, *a, **k): ...
        def kill_window(self, session_name, window_name):
            return True

        def window_exists(self, session_name, window_name):
            return True

        def send_keys(self, *a, **k): ...
        def send_special_key(self, *a, **k): ...
        def get_history(self, *a, **k):
            return ""

        def get_pane_working_directory(self, *a, **k):
            return None

        def get_pane_current_command(self, *a, **k):
            return None

        def attach_session(self, session_name): ...
        def prepare_web_attach(self, *a, **k):
            return []

        def pipe_pane(self, *a, **k): ...
        def stop_pipe_pane(self, *a, **k): ...

    backend = FakeBackend()
    _patch_common(monkeypatch, backend)
    monkeypatch.setattr(terminal_service, "get_backend", lambda: LegacyBackend())
    with pytest.raises(TerminalBackendError, match="atomic process window creation"):
        await terminal_service.create_terminal(
            provider="codex",
            agent_profile="reviewer-sol-max",
            session_name=SESSION,
            new_session=False,
            working_directory="/tmp",
            registry=None,
            reserved_terminal_id=TERMINAL_ID,
            terminal_generation=GENERATION,
            preserve_on_init_failure=True,
            managed_native_command=BRIDGE_ARGV,
        )


@pytest.mark.asyncio
async def test_managed_new_session_rejected(monkeypatch):
    backend = FakeBackend()
    _patch_common(monkeypatch, backend)
    monkeypatch.setattr(backend, "session_exists", lambda name: False)
    with pytest.raises(ValueError, match="existing session"):
        await terminal_service.create_terminal(
            provider="codex",
            agent_profile="reviewer-sol-max",
            session_name=SESSION,
            new_session=True,
            working_directory="/tmp",
            registry=None,
            reserved_terminal_id=TERMINAL_ID,
            terminal_generation=GENERATION,
            managed_native_command=BRIDGE_ARGV,
        )


class TmuxArgvTest:
    pass


def test_tmux_client_builds_direct_argv_no_shell(monkeypatch, tmp_path):
    from cli_agent_orchestrator.clients.tmux import TmuxClient

    client = TmuxClient.__new__(TmuxClient)  # bypass libtmux server contact
    runs = []

    class Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.tmux.subprocess.run",
        lambda cmd, **kwargs: runs.append(cmd) or Proc(),
    )
    name = client.create_window_with_argv(
        SESSION,
        "managed-window",
        TERMINAL_ID,
        BRIDGE_ARGV,
        str(tmp_path),
        extra_env={},
    )
    assert name == "managed-window"
    cmd = runs[0]
    sep = cmd.index("--")
    assert cmd[sep + 1 :] == BRIDGE_ARGV  # direct argv, never a shell string
    assert "-e" in cmd
    assert any(item == f"CAO_TERMINAL_ID={TERMINAL_ID}" for item in cmd)


def test_tmux_client_rejects_relative_executable(tmp_path):
    from cli_agent_orchestrator.clients.tmux import TmuxClient

    client = TmuxClient.__new__(TmuxClient)
    with pytest.raises(ValueError):
        client.create_window_with_argv(
            SESSION,
            "w",
            TERMINAL_ID,
            ["python3", "-m", "x"],
            str(tmp_path),
        )


# -- P1-9: minimal allowlisted provider environment ---------------------------


def test_provider_env_is_minimal_and_rejects_ambient_control_state(monkeypatch):
    monkeypatch.setenv("HOME", "/home/test")
    monkeypatch.setenv("PATH", "/sneaky/bin:/usr/bin:/bin")
    monkeypatch.setenv("CAO_TERMINAL_ID", "worker1")
    monkeypatch.setenv("CAO_WORKFLOW_RUN_ID", "run-1")
    monkeypatch.setenv("CONDUCT_SKIP_QUOTA_PREFLIGHT", "1")
    monkeypatch.setenv("KIMI_MODEL_THINKING_EFFORT", "low")
    env = _provider_env()
    assert env["HOME"] == "/home/test"
    assert env["PATH"] == "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"
    assert "CAO_TERMINAL_ID" not in env
    assert "CAO_WORKFLOW_RUN_ID" not in env
    assert "CONDUCT_SKIP_QUOTA_PREFLIGHT" not in env
    assert "KIMI_MODEL_THINKING_EFFORT" not in env
    # bridge-set route control applies via overrides only
    env = _provider_env({"KIMI_MODEL_THINKING_EFFORT": "max"})
    assert env["KIMI_MODEL_THINKING_EFFORT"] == "max"


def test_bridge_environment_rejects_protected_variables(monkeypatch):
    monkeypatch.delenv("CONDUCT_SKIP_QUOTA_PREFLIGHT", raising=False)
    monkeypatch.delenv("CHECK_AI_QUOTA_SCRIPT", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    for name in list(os.environ):
        if name.startswith(("CONDUCT_", "CHECK_AI_QUOTA", "CODEX_")):
            monkeypatch.delenv(name, raising=False)
    _assert_bridge_environment()  # clean: no raise
    monkeypatch.setenv("CONDUCT_SKIP_QUOTA_PREFLIGHT", "1")
    with pytest.raises(BridgeError):
        _assert_bridge_environment()
    monkeypatch.delenv("CONDUCT_SKIP_QUOTA_PREFLIGHT")
    monkeypatch.setenv("CODEX_HOME", "/elsewhere")
    with pytest.raises(BridgeError):
        _assert_bridge_environment()
