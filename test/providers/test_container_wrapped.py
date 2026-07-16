"""Integration tests for the container-wrapped provider features (Tasks 1-4).

The per-method unit tests live elsewhere (test_base_provider.py,
test_provider_init_timeout.py, test_startup_prompt_idle_gap.py,
test_claude_code_unit.py). These tests instead exercise the four features as they
COMBINE on a real provider (ClaudeCodeProvider) through its public surface —
``get_status()``, ``_build_claude_command()``, ``initialize()`` and the
startup-prompt handler — in the container-wrapped scenario a wrapped
``podman``/``docker exec`` launch produces:

  Task 1: herdr "unknown" agent_status surfaces as native None, which always
          falls through to a LIVE buffer read (BaseProvider._resolve_buffer)
          rather than a dispatch-timing guess -- restoring fail-fast ERROR
          detection and a real COMPLETED path for wrapped agents.
  Task 2: host->guest path translation of the temp prompt/MCP files reaches the
          emitted CLI command.
  Task 3: idle-gap startup-prompt handling keeps polling for late dialogs under
          a hard outer cap.
  Task 4: a per-profile ``provider_init_timeout`` override governs every init wait
          and the startup-prompt handler's outer cap.

All backends and subprocesses are mocked — no Docker/Podman/tmux required.
"""

import shlex
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.agent_profile import (
    AgentProfile,
    ContainerConfig,
    ContainerPathMap,
)
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

# time.time() lives in base.py; pin it here to make the staleness window deterministic.
_BASE_TIME = "cli_agent_orchestrator.providers.base.time.time"
# The backend singleton every provider resolves via backends.registry.get_backend().
_BACKEND = "cli_agent_orchestrator.backends.registry._backend"


def test_path_translation_unit():
    """Task 2: _translate_path picks the longest matching host prefix and otherwise
    passes the path through unchanged.

    One profile carries two overlapping maps so a single call exercises all three
    behaviors the container path layer relies on.
    """
    provider = ClaudeCodeProvider("t1", "sess", "win")
    profile = AgentProfile(
        name="c",
        description="d",
        container=ContainerConfig(
            path_maps=[
                ContainerPathMap(host="/host/work", guest="/guest/work"),
                ContainerPathMap(host="/host/work/sub", guest="/deep"),
            ]
        ),
    )

    # Longest-prefix-wins: /host/work/sub is longer than /host/work, so it governs.
    assert provider._translate_path("/host/work/sub/file.txt", profile) == "/deep/file.txt"
    # A path under only the shorter prefix maps via that shorter prefix.
    assert provider._translate_path("/host/work/other.txt", profile) == "/guest/work/other.txt"
    # No mapping matches -> passthrough (unmapped host paths are never rewritten).
    assert provider._translate_path("/unmapped/x.txt", profile) == "/unmapped/x.txt"


@patch("cli_agent_orchestrator.providers.claude_code.load_agent_profile")
def test_build_command_with_container_profile(mock_load, tmp_path):
    """Task 2: with a ContainerConfig, the built command carries GUEST paths.

    The temp prompt-file and MCP-config paths (rooted at CAO_HOME_DIR) must be
    translated to the container's guest paths in the emitted
    --append-system-prompt-file and --mcp-config arguments — the containerized
    CLI cannot see the host paths.
    """
    mock_load.return_value = AgentProfile(
        name="test-agent",
        description="d",
        system_prompt="You are a container agent.",
        mcpServers={"test-mcp": {"command": "echo", "args": []}},
        container=ContainerConfig(
            path_maps=[ContainerPathMap(host=str(tmp_path), guest="/app/config")]
        ),
    )

    provider = ClaudeCodeProvider("test-container", "sess", "win", "test-agent")
    # tmp_path is the host prefix; files are written under it and auto-cleaned by pytest.
    with patch("cli_agent_orchestrator.providers.claude_code.CAO_HOME_DIR", tmp_path):
        command = provider._build_claude_command()

    args = shlex.split(command)
    prompt_arg = args[args.index("--append-system-prompt-file") + 1]
    mcp_arg = args[args.index("--mcp-config") + 1]

    assert prompt_arg == "/app/config/tmp/test-container.prompt"
    assert mcp_arg == "/app/config/tmp/test-container.mcp.json"
    # The host prefix must not leak into either arg — translation actually happened.
    assert str(tmp_path) not in prompt_arg
    assert str(tmp_path) not in mcp_arg


@patch(_BACKEND)
def test_status_during_init_native_none_reads_live_buffer(mock_backend):
    """Task 1: dispatched + native None -> live get_history() read, not a clock guess.

    A wrapped exec hides the agent CLI from herdr, so get_native_status() is
    None. The shared resolver no longer infers PROCESSING from elapsed dispatch
    time; it falls through to BaseProvider._resolve_buffer(), which reads the
    backend's live pane content on herdr (supports_event_inbox=True). Claude
    Code's own buffer-parsing then sees a real spinner line and reports
    PROCESSING because the agent is genuinely still working, not because a
    timer says so.
    """
    mock_backend.get_native_status.return_value = None
    mock_backend.supports_event_inbox.return_value = True
    mock_backend.get_history.return_value = "✻ Orbiting…"

    provider = ClaudeCodeProvider("t1", "sess", "win")
    provider.mark_input_received()  # real dispatch hook: sets _task_dispatched=True
    assert provider._task_dispatched is True

    assert provider.get_status("") == TerminalStatus.PROCESSING


@patch(_BACKEND)
def test_status_wedged_pane_reports_unknown_not_error_from_a_clock(mock_backend):
    """Task 1: a genuinely idle-content-free wedged pane must not become ERROR by clock alone.

    Regresses must-fix 2: the removed staleness cap flipped ANY unresolved
    native status to ERROR purely from elapsed wall-clock time since dispatch,
    even for a pane that never showed error text -- which meant a long-running
    but healthy turn on a wrapped agent could never reach COMPLETED and would
    hard-fail. With native=None now always falling through, a pane with no
    recognizable content reports Claude Code's own empty-buffer default
    (UNKNOWN) regardless of how long ago dispatch happened -- it does NOT
    become ERROR just because a lot of time passed.
    """
    mock_backend.get_native_status.return_value = None
    mock_backend.supports_event_inbox.return_value = True
    mock_backend.get_history.return_value = ""  # pane alive, but no content yet

    provider = ClaudeCodeProvider("t1", "sess", "win")
    provider.mark_input_received()
    provider._last_dispatch_time = 1000.0

    with patch(_BASE_TIME, return_value=1000.0 + 300.0):  # would have been "stale" pre-fix
        result = provider.get_status("")

    assert result == TerminalStatus.UNKNOWN


@patch(_BACKEND)
def test_status_dead_launch_reports_unknown_not_false_idle(mock_backend):
    """Task 1: fail-fast is restored -- a dead launch no longer reports false-success IDLE.

    Pre-#400, herdr 'unknown' mapped straight to ERROR, so wait_until_status()
    kept polling a dead pane until a real TimeoutError. #400 regressed this: a
    dead launch (no task ever dispatched, so _task_dispatched=False) reported
    optimistic IDLE immediately -- init "succeeded" and pasted the task into a
    dead pane. This test proves the rework restores fail-fast: the live read
    sees plain shell output with no Claude Code prompt/response markers at
    all, so Claude Code's own buffer parsing reports UNKNOWN. UNKNOWN is not in
    {IDLE, COMPLETED}, so init's wait_until_status keeps polling toward a real
    TimeoutError instead of falsely declaring success.
    """
    mock_backend.get_native_status.return_value = None
    mock_backend.supports_event_inbox.return_value = True
    # A dead/exited launch: the shell prompt is back, no Claude Code TUI ever
    # rendered (no ❯/>, no ⏺/●, no separator) -- genuinely unrecognizable.
    mock_backend.get_history.return_value = "bash: claude: command not found\n$ "

    provider = ClaudeCodeProvider("t1", "sess", "win")
    # No task dispatched -- this is the pre-dispatch init-wait scenario, where
    # the #400 regression's guess (IDLE) was most damaging (false init success).
    assert provider._task_dispatched is False

    result = provider.get_status("")

    assert result == TerminalStatus.UNKNOWN
    assert result not in (TerminalStatus.IDLE, TerminalStatus.COMPLETED)


@patch("cli_agent_orchestrator.providers.claude_code.time")
@patch(_BACKEND)
def test_idle_timeout_prompt_handler(mock_backend, mock_time):
    """Tasks 3 + 4: the idle gap keeps polling for a LATE dialog inside the outer cap.

    A cold containerized start renders dialogs late and in sequence. The bypass
    prompt at t=18s resets the idle timer; the trust prompt at t=35s is within the
    20s idle gap of that reset (35-18=17<20) AND within the per-profile 180s outer
    cap, so it is still handled. Under a fixed 20s-from-start window the loop would
    have exited at t=20s and never answered the trust prompt.

    idle_gap/outer_timeout are passed explicitly — the exact values initialize()
    forwards from the per-profile provider_init_timeout — so no settings mock is
    needed and the Task 3<->Task 4 wiring is what is under test.
    """
    mock_time.sleep = MagicMock()
    mock_time.monotonic.side_effect = [
        0.0,  # outer_deadline = 0 + 180 (per-profile init timeout)
        0.0,  # last_prompt_time = 0
        18.0,  # iter1: gap 18<20 and 18<180 -> bypass handled, timer reset
        18.0,  # last_prompt_time reset to 18
        35.0,  # iter2: gap 35-18=17<20 and 35<180 -> trust handled -> return
    ]
    mock_backend.get_history.side_effect = [
        "WARNING: Bypass Permissions\n1. No\n2. Yes, I accept\n",
        "Yes, I trust this folder",
    ]

    provider = ClaudeCodeProvider("t1", "sess", "win")
    provider._handle_startup_prompts(idle_gap=20.0, outer_timeout=180.0)

    # Bypass: Down arrow (send_keys) + Enter (send_special_key). Trust: Enter.
    assert mock_backend.send_keys.call_count == 1
    assert mock_backend.send_special_key.call_count == 2


@pytest.mark.asyncio
@patch.object(ClaudeCodeProvider, "_ensure_skip_bypass_prompt_setting")
@patch.object(ClaudeCodeProvider, "_build_claude_command", return_value="claude")
@patch("cli_agent_orchestrator.providers.claude_code.load_agent_profile")
@patch("cli_agent_orchestrator.providers.claude_code.wait_for_shell")
@patch("cli_agent_orchestrator.providers.claude_code.wait_until_status")
@patch(_BACKEND)
async def test_wrapped_provider_lifecycle(
    mock_backend, mock_wait_status, mock_wait_shell, mock_load, mock_build, mock_ensure
):
    """End-to-end: launch -> prompts handled -> PROCESSING -> IDLE (all mocked).

    Container-on-tmux: the wrapped CLI is invisible to native status, so
    get_native_status() is always None and status comes from the buffer. The
    container profile also carries a 180s provider_init_timeout that must flow to
    every init wait.
    """
    mock_load.return_value = AgentProfile(name="c", description="d", provider_init_timeout=180)
    mock_wait_shell.return_value = True
    mock_wait_status.return_value = True
    # A workspace-trust dialog is showing at startup (handled by the prompt handler).
    mock_backend.get_history.return_value = "Yes, I trust this folder"
    # Wrapped exec -> native status is always unresolved; status is buffer-driven.
    mock_backend.get_native_status.return_value = None

    provider = ClaudeCodeProvider("test-container-life", "sess", "win", "c")

    # 1) Launch succeeds and the per-profile 180s cap flows to every init wait.
    result = await provider.initialize()
    assert result is True
    assert provider._initialized is True
    assert mock_wait_shell.call_args.kwargs["timeout"] == 180
    assert mock_wait_status.call_args.kwargs["timeout"] == 180

    # 2) The startup trust dialog was answered with Enter during initialize().
    mock_backend.send_special_key.assert_called_with("sess", "win", "Enter")

    # 3) Working turn: a live spinner in the buffer -> PROCESSING (native None -> buffer path).
    assert provider.get_status("✻ Orbiting…") == TerminalStatus.PROCESSING

    # 4) Turn finished: the ready prompt -> IDLE.
    assert provider.get_status("❯ ") == TerminalStatus.IDLE
