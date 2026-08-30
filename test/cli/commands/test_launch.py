"""Tests for launch command."""

import os
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.launch import (
    _HEADLESS_TASK_TIMEOUT,
    _READINESS_WAIT_TIMEOUT,
    _is_waiting_on_user,
    _parse_env_pairs,
    launch,
)
from cli_agent_orchestrator.models.terminal import TerminalStatus

# ── Backend auto-detection (issue #308) ──────────────────────────────


def test_launch_syncs_backend_from_server_before_attach():
    """Non-headless launch calls sync_backend_from_server() before get_backend().

    Regression guard for #308: when ``cao-server --terminal herdr`` is used
    without config.json, the CLI must auto-detect the server's backend via
    /health rather than defaulting to tmux.
    """
    runner = CliRunner()
    call_order = []

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.get_backend") as mock_get_backend,
        patch("cli_agent_orchestrator.cli.commands.launch.wait_until_terminal_status") as mock_wait,
        patch("cli_agent_orchestrator.cli.commands.launch.sync_backend_from_server") as mock_sync,
    ):
        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "id": "test-terminal-id",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None
        mock_wait.return_value = True

        def record_sync():
            call_order.append("sync")

        def record_attach(*a, **kw):
            call_order.append("attach")

        mock_sync.side_effect = record_sync
        mock_get_backend.return_value.attach_session.side_effect = record_attach

        result = runner.invoke(launch, ["--agents", "test-agent", "--yolo"])

        assert result.exit_code == 0
        mock_sync.assert_called_once()
        assert call_order == ["sync", "attach"]


def test_launch_headless_does_not_sync_backend():
    """Headless launch skips sync_backend_from_server (no attach needed)."""
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.sync_backend_from_server") as mock_sync,
    ):
        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "id": "test-terminal-id",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None

        result = runner.invoke(launch, ["--agents", "test-agent", "--headless", "--yolo"])

        assert result.exit_code == 0
        mock_sync.assert_not_called()


def test_launch_passes_cwd_by_default():
    """Test that launch command sends current working directory when not explicitly provided."""
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.get_backend") as mock_get_backend,
        patch("cli_agent_orchestrator.cli.commands.launch.wait_until_terminal_status") as mock_wait,
    ):
        mock_get_backend.return_value.attach_session.return_value = None

        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "id": "test-terminal-id",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None
        mock_wait.return_value = True

        result = runner.invoke(launch, ["--agents", "test-agent", "--yolo"])

        assert result.exit_code == 0
        mock_post.assert_called_once()
        params = mock_post.call_args.kwargs["params"]
        assert "working_directory" in params
        assert params["working_directory"] == os.path.realpath(os.getcwd())


def test_launch_passes_explicit_working_directory():
    """Test that --working-directory is passed to the API when provided."""
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.get_backend") as mock_get_backend,
        patch("cli_agent_orchestrator.cli.commands.launch.wait_until_terminal_status") as mock_wait,
    ):
        mock_get_backend.return_value.attach_session.return_value = None

        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "id": "test-terminal-id",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None
        mock_wait.return_value = True

        result = runner.invoke(
            launch,
            [
                "--agents",
                "test-agent",
                "--yolo",
                "--working-directory",
                "/remote/path",
            ],
        )

        assert result.exit_code == 0
        params = mock_post.call_args.kwargs["params"]
        assert params["working_directory"] == "/remote/path"


def test_launch_passes_explicit_kiro_engine():
    """The direct CLI surface forwards engine selection without changing provider selection."""
    runner = CliRunner()

    with patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post:
        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "id": "test-terminal-id",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None

        result = runner.invoke(
            launch, ["--agents", "test-agent", "--engine", "kas", "--headless", "--yolo"]
        )

    assert result.exit_code == 0
    assert mock_post.call_args.kwargs["params"]["engine"] == "kas"


def test_launch_headless_message_sends_to_terminal():
    """Headless+message hands MESSAGE to the server in the create body, then polls.

    Regression guard for caom-7it: the CLI used to create the session and then
    issue a SEPARATE ``POST /terminals/{id}/input``. When provider init outlived
    the client's read timeout on ``POST /sessions``, that second request never
    happened and MESSAGE was silently dropped. There must be exactly ONE POST,
    carrying ``initial_message``.
    """
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.requests.get") as mock_get,
        patch("cli_agent_orchestrator.cli.commands.launch.wait_until_terminal_status") as mock_wait,
    ):
        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "id": "test-terminal-id",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None
        mock_wait.return_value = True

        poll_resp = MagicMock()
        poll_resp.raise_for_status.return_value = None
        poll_resp.json.return_value = {"status": "completed"}

        output_resp = MagicMock()
        output_resp.raise_for_status.return_value = None
        output_resp.json.return_value = {"output": "task done"}

        mock_get.side_effect = [poll_resp, output_resp]

        result = runner.invoke(
            launch,
            [
                "--agents",
                "test-agent",
                "--headless",
                "--yolo",
                "do something",
            ],
        )

        assert result.exit_code == 0
        assert "task done" in result.output
        # ONE POST: create session, with MESSAGE in the body. No follow-up
        # /terminals/{id}/input — the server delivers it off the deferred-init
        # path and re-submits if the TUI swallowed it.
        assert mock_post.call_count == 1
        assert mock_post.call_args.kwargs["json"]["initial_message"] == "do something"
        assert "/input" not in str(mock_post.call_args)
        # No client-side readiness gate either: the create response returns
        # before init finishes (status UNKNOWN), and poll_until_done handles it.
        mock_wait.assert_not_called()


def test_launch_invalid_provider():
    """Test launch with invalid provider."""
    runner = CliRunner()

    result = runner.invoke(launch, ["--agents", "test-agent", "--provider", "invalid-provider"])

    assert result.exit_code != 0
    assert "Invalid provider" in result.output


def test_launch_with_session_name():
    """Test launch with custom session name."""
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.get_backend") as mock_get_backend,
        patch("cli_agent_orchestrator.cli.commands.launch.wait_until_terminal_status") as mock_wait,
    ):
        mock_get_backend.return_value.attach_session.return_value = None
        mock_post.return_value.json.return_value = {
            "session_name": "custom-session",
            "id": "test-terminal-id",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None
        mock_wait.return_value = True

        result = runner.invoke(
            launch, ["--agents", "test-agent", "--session-name", "custom-session", "--yolo"]
        )

        assert result.exit_code == 0

        call_args = mock_post.call_args
        params = call_args.kwargs["params"]
        assert params["session_name"] == "custom-session"


def test_launch_request_exception():
    """Test launch handles RequestException."""
    runner = CliRunner()

    with patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post:
        import requests

        mock_post.side_effect = requests.exceptions.RequestException("Connection refused")

        result = runner.invoke(launch, ["--agents", "test-agent", "--yolo"])

        assert result.exit_code != 0
        assert "Failed to connect to cao-server" in result.output


def test_launch_generic_exception():
    """Test launch handles generic exception."""
    runner = CliRunner()

    with patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post:
        mock_post.side_effect = Exception("Unexpected error")

        result = runner.invoke(launch, ["--agents", "test-agent", "--yolo"])

        assert result.exit_code != 0
        assert "Unexpected error" in result.output


def test_launch_headless_mode():
    """Test launch in headless mode doesn't attach to the backend."""
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.get_backend") as mock_get_backend,
    ):
        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None

        result = runner.invoke(launch, ["--agents", "test-agent", "--headless", "--yolo"])

        assert result.exit_code == 0
        # In headless mode, attach_session should not be called
        mock_get_backend.return_value.attach_session.assert_not_called()


def test_launch_non_headless_waits_for_idle_before_attach():
    """Non-headless launch must wait for IDLE/COMPLETED before attaching.

    Regression guard for #220: attaching before the TUI finishes initializing
    races with input-handler wiring and silently drops keystrokes. The wait
    must be called with the terminal id before attach_session.
    """
    runner = CliRunner()

    call_order = []

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.get_backend") as mock_get_backend,
        patch("cli_agent_orchestrator.cli.commands.launch.wait_until_terminal_status") as mock_wait,
    ):
        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "id": "test-terminal-id",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None

        def record_wait(*a, **kw):
            call_order.append("wait")
            return True

        def record_attach(*a, **kw):
            call_order.append("attach")

        mock_wait.side_effect = record_wait
        mock_get_backend.return_value.attach_session.side_effect = record_attach

        result = runner.invoke(launch, ["--agents", "test-agent", "--yolo"])

        assert result.exit_code == 0
        mock_wait.assert_called_once()
        wait_args = mock_wait.call_args
        assert wait_args.args[0] == "test-terminal-id"
        assert call_order == ["wait", "attach"]


def test_launch_non_headless_attaches_even_if_wait_times_out():
    """Non-headless launch warns but still attaches if the idle wait times out.

    The wait is advisory: orphaning the session (by refusing to attach)
    would be worse than letting the user inspect a slow-initializing session.
    """
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.get_backend") as mock_get_backend,
        patch("cli_agent_orchestrator.cli.commands.launch.wait_until_terminal_status") as mock_wait,
    ):
        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "id": "test-terminal-id",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None
        mock_wait.return_value = False
        mock_get_backend.return_value.attach_session.return_value = None

        result = runner.invoke(launch, ["--agents", "test-agent", "--yolo"])

        assert result.exit_code == 0
        assert "did not reach idle within 120s" in result.output
        mock_get_backend.return_value.attach_session.assert_called_once_with("test-session")


# ── A screen that legitimately needs the operator is settled, not a stall ──
#
# A provider can finish init on a prompt only a human can answer (Codex's
# first-run login menu is the case that forced this). Such a screen never
# becomes IDLE on its own, so a readiness poll that accepts only
# {IDLE, COMPLETED} burned the full _READINESS_WAIT_TIMEOUT and then blamed
# init for a pane that was merely waiting — while telling the operator nothing
# about the sign-in they had to complete.


def test_readiness_poll_accepts_waiting_user_answer():
    """WAITING_USER_ANSWER is in the pre-attach target set.

    This is the assertion that matters: it pins the set itself, so narrowing it
    back to {IDLE, COMPLETED} fails here rather than only showing up as a 120s
    stall nothing in the suite waits around for.
    """
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.get_backend") as mock_get_backend,
        patch("cli_agent_orchestrator.cli.commands.launch.wait_until_terminal_status") as mock_wait,
        patch("cli_agent_orchestrator.cli.commands.launch._is_waiting_on_user") as mock_waiting,
    ):
        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "id": "test-terminal-id",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None
        mock_wait.return_value = True
        mock_waiting.return_value = False
        mock_get_backend.return_value.attach_session.return_value = None

        result = runner.invoke(launch, ["--agents", "test-agent", "--yolo"])

        assert result.exit_code == 0
        target_set = mock_wait.call_args[0][1]
        assert TerminalStatus.WAITING_USER_ANSWER in target_set, (
            "pre-attach poll must treat WAITING_USER_ANSWER as settled, or a "
            f"first-run sign-in stalls for {_READINESS_WAIT_TIMEOUT}s; got {target_set}"
        )
        # The pre-existing members must survive the widening.
        assert {TerminalStatus.IDLE, TerminalStatus.COMPLETED} <= target_set


def test_launch_tells_the_operator_when_the_pane_awaits_an_answer():
    """On a settled WAITING_USER_ANSWER the operator is told to finish it, not warned.

    The "did not reach idle" warning would be actively wrong here: init did
    finish, and the only thing outstanding is the human's answer.
    """
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.get_backend") as mock_get_backend,
        patch("cli_agent_orchestrator.cli.commands.launch.wait_until_terminal_status") as mock_wait,
        patch("cli_agent_orchestrator.cli.commands.launch._is_waiting_on_user") as mock_waiting,
    ):
        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "id": "test-terminal-id",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None
        mock_wait.return_value = True
        mock_waiting.return_value = True
        mock_get_backend.return_value.attach_session.return_value = None

        result = runner.invoke(launch, ["--agents", "test-agent", "--yolo"])

        assert result.exit_code == 0
        assert "waiting for an answer in the pane" in result.output
        assert "did not reach idle" not in result.output
        mock_get_backend.return_value.attach_session.assert_called_once_with("test-session")


def test_launch_stays_quiet_when_the_terminal_settles_idle():
    """The new hint must not fire on the ordinary path."""
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.get_backend") as mock_get_backend,
        patch("cli_agent_orchestrator.cli.commands.launch.wait_until_terminal_status") as mock_wait,
        patch("cli_agent_orchestrator.cli.commands.launch._is_waiting_on_user") as mock_waiting,
    ):
        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "id": "test-terminal-id",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None
        mock_wait.return_value = True
        mock_waiting.return_value = False
        mock_get_backend.return_value.attach_session.return_value = None

        result = runner.invoke(launch, ["--agents", "test-agent", "--yolo"])

        assert result.exit_code == 0
        assert "waiting for an answer" not in result.output
        assert "did not reach idle" not in result.output


def test_is_waiting_on_user_reads_the_live_status():
    """True only for WAITING_USER_ANSWER."""
    with patch("cli_agent_orchestrator.cli.commands.launch.requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": TerminalStatus.WAITING_USER_ANSWER.value
        }
        assert _is_waiting_on_user("t1") is True

        mock_get.return_value.json.return_value = {"status": TerminalStatus.IDLE.value}
        assert _is_waiting_on_user("t1") is False


def test_is_waiting_on_user_swallows_transport_errors():
    """A blip must not turn a successful launch into "Failed to connect to cao-server".

    This helper only decides which advisory line to print, and it runs inside the
    command's ``RequestException`` handler — so letting one escape would report a
    connection failure to a server the poll just finished talking to.
    """
    import requests as _requests

    with patch("cli_agent_orchestrator.cli.commands.launch.requests.get") as mock_get:
        mock_get.side_effect = _requests.exceptions.ConnectionError("boom")
        assert _is_waiting_on_user("t1") is False

    with patch("cli_agent_orchestrator.cli.commands.launch.requests.get") as mock_get:
        mock_get.return_value.status_code = 500
        assert _is_waiting_on_user("t1") is False


@pytest.mark.parametrize("payload", [[], ["a"], None, "waiting_user_answer", 3])
def test_is_waiting_on_user_survives_a_200_that_is_not_a_json_object(payload):
    """A parseable-but-non-object body must not abort the launch.

    ``AttributeError`` from ``[].get`` is no kind of ``RequestException``, so
    without the isinstance check it escapes this function, reaches the command's
    generic handler, and exits 1 *after* ``POST /sessions`` has already created
    the session — orphaning it in tmux, which is the exact outcome the local
    except exists to prevent. Parametrized because each JSON scalar type reaches
    ``.get`` by a different route.
    """
    with patch("cli_agent_orchestrator.cli.commands.launch.requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = payload
        assert _is_waiting_on_user("t1") is False


def test_launch_workspace_confirmation_accepted():
    """Test workspace confirmation is shown for claude_code provider and accepted."""
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.get_backend"),
    ):
        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None

        # Provide 'y' input to accept the confirmation prompt
        result = runner.invoke(
            launch,
            ["--agents", "test-agent", "--provider", "claude_code", "--headless"],
            input="y\n",
        )

        assert result.exit_code == 0
        # New prompt format shows tool summary
        assert "launching on claude_code" in result.output
        assert "Allowed:" in result.output
        assert "Proceed?" in result.output
        mock_post.assert_called_once()


def test_launch_workspace_confirmation_declined():
    """Test workspace confirmation declined cancels launch."""
    runner = CliRunner()

    # Provide 'n' input to decline the confirmation prompt
    result = runner.invoke(
        launch, ["--agents", "test-agent", "--provider", "claude_code"], input="n\n"
    )

    assert result.exit_code != 0
    assert "Launch cancelled by user" in result.output


def test_launch_workspace_confirmation_skipped_with_yolo_flag():
    """Test --yolo flag skips workspace confirmation."""
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.get_backend"),
    ):
        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None

        result = runner.invoke(
            launch, ["--agents", "test-agent", "--provider", "claude_code", "--headless", "--yolo"]
        )

        assert result.exit_code == 0
        # --yolo shows warning but no confirmation prompt
        assert "Proceed?" not in result.output
        assert "WARNING" in result.output
        mock_post.assert_called_once()


def test_launch_workspace_confirmation_for_default_provider():
    """Test that default provider (kiro_cli) also triggers workspace confirmation."""
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.get_backend"),
    ):
        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None

        # Default provider is kiro_cli, which requires workspace confirmation
        result = runner.invoke(launch, ["--agents", "test-agent", "--headless"], input="y\n")

        assert result.exit_code == 0
        assert "launching on kiro_cli" in result.output
        assert "Proceed?" in result.output


def test_launch_yolo_sets_unrestricted_allowed_tools():
    """Test --yolo flag passes allowed_tools=* to the API."""
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.get_backend"),
        patch("cli_agent_orchestrator.cli.commands.launch.wait_until_terminal_status") as mock_wait,
    ):
        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "id": "test-terminal-id",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None
        mock_wait.return_value = True

        result = runner.invoke(launch, ["--agents", "test-agent", "--yolo"])

        assert result.exit_code == 0
        call_args = mock_post.call_args
        params = call_args.kwargs["params"]
        assert params["allowed_tools"] == "*"


def test_launch_allowed_tools_override():
    """Test --allowed-tools CLI flag overrides profile defaults."""
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.get_backend"),
    ):
        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None

        result = runner.invoke(
            launch,
            [
                "--agents",
                "test-agent",
                "--allowed-tools",
                "@cao-mcp-server",
                "--allowed-tools",
                "fs_read",
                "--headless",
            ],
            input="y\n",
        )

        assert result.exit_code == 0
        call_args = mock_post.call_args
        params = call_args.kwargs["params"]
        assert params["allowed_tools"] == "@cao-mcp-server,fs_read"


def test_launch_builtin_profile_resolves_role_defaults():
    """Test that launching a built-in profile resolves role-based allowedTools."""
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.get_backend"),
    ):
        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None

        # code_supervisor is a built-in profile with role=supervisor
        result = runner.invoke(
            launch,
            ["--agents", "code_supervisor", "--headless"],
            input="y\n",
        )

        assert result.exit_code == 0
        call_args = mock_post.call_args
        params = call_args.kwargs["params"]
        # Supervisor should only have MCP server tools
        assert "@cao-mcp-server" in params["allowed_tools"]


def test_launch_headless_message_does_not_gate_delivery_on_client_readiness():
    """Headless+message must not abort just because the CLI never saw IDLE.

    The old flow waited for IDLE client-side and raised "did not become ready"
    before sending — a launch that hit that branch dropped MESSAGE outright even
    though the terminal was alive and would have accepted it moments later. The
    server now owns readiness, so a never-IDLE reading from the CLI's own poll
    helper is irrelevant: MESSAGE is already in the create body.
    """
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.requests.get") as mock_get,
        patch("cli_agent_orchestrator.cli.commands.launch.wait_until_terminal_status") as mock_wait,
    ):
        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "id": "test-terminal-id",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None
        mock_wait.return_value = False

        poll_resp = MagicMock()
        poll_resp.raise_for_status.return_value = None
        poll_resp.json.return_value = {"status": "completed"}
        output_resp = MagicMock()
        output_resp.raise_for_status.return_value = None
        output_resp.json.return_value = {"output": "task done"}
        mock_get.side_effect = [poll_resp, output_resp]

        result = runner.invoke(
            launch,
            [
                "--agents",
                "test-agent",
                "--headless",
                "--yolo",
                "do something",
            ],
        )

        assert result.exit_code == 0
        assert "did not become ready" not in result.output
        assert mock_post.call_args.kwargs["json"]["initial_message"] == "do something"


def test_launch_headless_message_poll_error_status():
    """Test headless+message raises when terminal reaches error status during poll."""
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.requests.get") as mock_get,
        patch("cli_agent_orchestrator.cli.commands.launch.wait_until_terminal_status") as mock_wait,
    ):
        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "id": "test-terminal-id",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None
        mock_wait.return_value = True

        poll_resp = MagicMock()
        poll_resp.raise_for_status.return_value = None
        poll_resp.json.return_value = {"status": "error"}
        mock_get.return_value = poll_resp

        result = runner.invoke(
            launch,
            [
                "--agents",
                "test-agent",
                "--headless",
                "--yolo",
                "do something",
            ],
        )

        assert result.exit_code != 0
        assert "ERROR" in result.output


def test_launch_headless_message_poll_processing_then_completed():
    """Test headless+message poll loop sleeps when status is processing before completing."""
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.requests.get") as mock_get,
        patch("cli_agent_orchestrator.cli.commands.launch.wait_until_terminal_status") as mock_wait,
    ):
        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "id": "test-terminal-id",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None
        mock_wait.return_value = True

        processing_resp = MagicMock()
        processing_resp.raise_for_status.return_value = None
        processing_resp.json.return_value = {"status": "processing"}

        completed_resp = MagicMock()
        completed_resp.raise_for_status.return_value = None
        completed_resp.json.return_value = {"status": "completed"}

        output_resp = MagicMock()
        output_resp.raise_for_status.return_value = None
        output_resp.json.return_value = {"output": "done"}

        mock_get.side_effect = [processing_resp, completed_resp, output_resp]

        result = runner.invoke(
            launch,
            [
                "--agents",
                "test-agent",
                "--headless",
                "--yolo",
                "do something",
            ],
        )

        assert result.exit_code == 0
        assert "done" in result.output


def test_launch_honors_profile_provider_when_flag_not_given():
    """When --provider is not passed, provider is omitted from POST params (server resolves it)."""
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.get_backend"),
        patch(
            "cli_agent_orchestrator.utils.agent_profiles.resolve_provider",
            return_value="claude_code",
        ) as mock_resolve,
    ):
        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None

        result = runner.invoke(
            launch,
            ["--agents", "code_supervisor", "--headless"],
            input="y\n",
        )

        assert result.exit_code == 0
        mock_resolve.assert_called_once()
        params = mock_post.call_args.kwargs["params"]
        # provider is NOT sent — server-side resolution handles it
        assert "provider" not in params


def test_launch_yolo_still_resolves_profile_provider():
    """--yolo must not swallow ``provider:`` in the agent profile frontmatter.

    Regression guard for #239: ``resolve_provider`` previously lived inside
    the ``else`` branch of the permission-resolution conditional and never
    fired when ``--yolo`` took the first branch, breaking heterogeneous-
    panelist workflows where each agent profile pins a specific CLI.
    """
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.get_backend"),
        patch("cli_agent_orchestrator.cli.commands.launch.wait_until_terminal_status") as mock_wait,
        patch(
            "cli_agent_orchestrator.utils.agent_profiles.resolve_provider",
            return_value="claude_code",
        ) as mock_resolve,
    ):
        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "id": "test-terminal-id",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None
        mock_wait.return_value = True

        result = runner.invoke(launch, ["--agents", "codex_panelist", "--yolo"])

        assert result.exit_code == 0
        # Provider resolution must run even on the --yolo branch.
        mock_resolve.assert_called_once_with("codex_panelist", "kiro_cli")
        # The kiro_cli-specific yolo warning text must NOT appear, because
        # the profile's provider ("claude_code") was honoured.
        assert "consent dialog will be auto-answered" not in result.output


def test_launch_allowed_tools_still_resolves_profile_provider():
    """--allowed-tools must also not swallow ``provider:`` in the profile."""
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.get_backend"),
        patch(
            "cli_agent_orchestrator.utils.agent_profiles.resolve_provider",
            return_value="copilot_cli",
        ) as mock_resolve,
    ):
        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None

        result = runner.invoke(
            launch,
            [
                "--agents",
                "copilot_panelist",
                "--allowed-tools",
                "fs_read",
                "--headless",
            ],
            input="y\n",
        )

        assert result.exit_code == 0
        mock_resolve.assert_called_once_with("copilot_panelist", "kiro_cli")
        # Local prompts must reflect the resolved provider, not the default.
        assert "launching on copilot_cli" in result.output


def test_launch_explicit_provider_skips_profile_resolution():
    """An explicit --provider flag wins over the profile's provider field.

    ``resolve_provider`` should not be invoked at all when the operator names
    a provider on the command line.
    """
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.get_backend"),
        patch("cli_agent_orchestrator.cli.commands.launch.wait_until_terminal_status") as mock_wait,
        patch("cli_agent_orchestrator.utils.agent_profiles.resolve_provider") as mock_resolve,
    ):
        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "id": "test-terminal-id",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None
        mock_wait.return_value = True

        result = runner.invoke(
            launch,
            ["--agents", "codex_panelist", "--yolo", "--provider", "claude_code"],
        )

        assert result.exit_code == 0
        mock_resolve.assert_not_called()
        # Explicit provider IS sent to the API in this path.
        params = mock_post.call_args.kwargs["params"]
        assert params["provider"] == "claude_code"


def test_launch_yolo_falls_back_to_default_when_profile_lacks_provider():
    """When the profile has no ``provider`` key, ``--yolo`` falls back to
    DEFAULT_PROVIDER. ``resolve_provider`` handles this by returning the
    fallback it was given, so the trailing local fallback is unnecessary."""
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.get_backend"),
        patch("cli_agent_orchestrator.cli.commands.launch.wait_until_terminal_status") as mock_wait,
        patch(
            "cli_agent_orchestrator.utils.agent_profiles.resolve_provider",
            return_value="kiro_cli",  # fallback returned because profile has no provider
        ),
    ):
        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "id": "test-terminal-id",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None
        mock_wait.return_value = True

        result = runner.invoke(launch, ["--agents", "test-agent", "--yolo"])

        assert result.exit_code == 0
        # The kiro_cli-specific yolo warning IS expected here because the
        # profile didn't override and the fallback is kiro_cli.
        assert "consent dialog will be auto-answered" in result.output


# ── --env forwarded env vars (issue #248) ────────────────────────────


class TestParseEnvPairs:
    """``_parse_env_pairs`` validates --env at the CLI boundary so a value
    that would be silently dropped server-side is rejected up front."""

    def test_valid_pairs_parsed(self):
        assert _parse_env_pairs(["FOO=bar", "AWS_REGION=us-west-2"]) == {
            "FOO": "bar",
            "AWS_REGION": "us-west-2",
        }

    def test_value_with_equals_preserved(self):
        # Split on first '=' only — values may legitimately contain '='.
        assert _parse_env_pairs(["URL=https://x?a=1&b=2"]) == {"URL": "https://x?a=1&b=2"}

    def test_empty_value_allowed(self):
        assert _parse_env_pairs(["EMPTY="]) == {"EMPTY": ""}

    @pytest.mark.parametrize("bad", ["NO_EQUALS", "", "=value_with_no_key"])
    def test_invalid_format_rejected(self, bad):
        import click as _click

        with pytest.raises(_click.ClickException):
            _parse_env_pairs([bad])

    @pytest.mark.parametrize("key", ["1FOO", "FÖÖ", "BAD-KEY", "WITH SPACE"])
    def test_bad_key_rejected(self, key):
        import click as _click

        with pytest.raises(_click.ClickException, match="--env key must match"):
            _parse_env_pairs([f"{key}=x"])

    @pytest.mark.parametrize("blocked", ["CLAUDE_SECRET=x", "CODEX_TOKEN=x", "__MISE_X=y"])
    def test_blocked_prefix_rejected(self, blocked):
        import click as _click

        with pytest.raises(_click.ClickException, match="blocked prefix"):
            _parse_env_pairs([blocked])

    def test_allowlisted_claude_auth_var_passes(self):
        # The 6 CLAUDE_CODE_USE_* / SKIP_* auth vars are explicitly allowed
        # through despite matching the CLAUDE prefix — same allowlist as
        # TmuxClient inherits at the server boundary.
        assert _parse_env_pairs(["CLAUDE_CODE_USE_BEDROCK=1"]) == {"CLAUDE_CODE_USE_BEDROCK": "1"}

    def test_value_at_cap_rejected(self):
        import click as _click

        with pytest.raises(_click.ClickException, match="exceeds 2048 bytes"):
            _parse_env_pairs(["BIG=" + ("x" * 2048)])

    def test_value_under_cap_accepted(self):
        assert _parse_env_pairs(["SMALL=" + ("x" * 2047)]) == {"SMALL": "x" * 2047}


def test_launch_forwards_env_in_json_body_not_url():
    """``--env`` values travel in the request body so secrets do not leak
    into cao-server's HTTP access log. Issue #248."""
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.get_backend"),
        patch("cli_agent_orchestrator.cli.commands.launch.wait_until_terminal_status") as mock_wait,
    ):
        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "id": "test-terminal-id",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None
        mock_wait.return_value = True

        result = runner.invoke(
            launch,
            [
                "--agents",
                "test-agent",
                "--yolo",
                "--env",
                "MNEMOSYNE_DIR=/root/mnemosyne",
                "--env",
                "ISAAC_CHANNEL=room:engineering",
            ],
        )

        assert result.exit_code == 0
        kwargs = mock_post.call_args.kwargs
        # env vars must NOT appear in query params
        assert "env_vars" not in kwargs["params"]
        assert "MNEMOSYNE_DIR" not in kwargs["params"]
        # env vars travel under the embedded ``env_vars`` body key
        assert kwargs["json"] == {
            "env_vars": {
                "MNEMOSYNE_DIR": "/root/mnemosyne",
                "ISAAC_CHANNEL": "room:engineering",
            }
        }


def test_launch_without_env_omits_request_body():
    """A launch with no --env must not send a JSON body — preserves
    backward compatibility with callers that ignore an unexpected body."""
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.get_backend"),
        patch("cli_agent_orchestrator.cli.commands.launch.wait_until_terminal_status") as mock_wait,
    ):
        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "id": "test-terminal-id",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None
        mock_wait.return_value = True

        result = runner.invoke(launch, ["--agents", "test-agent", "--yolo"])

        assert result.exit_code == 0
        assert "json" not in mock_post.call_args.kwargs


def test_launch_rejects_blocked_env_prefix_before_calling_api():
    """A blocked --env prefix must fail at the CLI boundary, before any
    POST is issued — operator gets an actionable error instead of a
    silent server-side drop."""
    runner = CliRunner()

    with patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post:
        result = runner.invoke(
            launch,
            ["--agents", "test-agent", "--yolo", "--env", "CLAUDE_SESSION_ID=abc"],
        )

        assert result.exit_code != 0
        assert "blocked prefix" in result.output
        mock_post.assert_not_called()


def test_launch_omp_requires_workspace_confirmation():
    runner = CliRunner()
    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.get_backend"),
    ):
        mock_post.return_value.json.return_value = {
            "session_name": "test-session",
            "name": "test-terminal",
        }
        mock_post.return_value.raise_for_status.return_value = None

        result = runner.invoke(
            launch,
            ["--agents", "test-agent", "--provider", "omp", "--headless"],
            input="y\n",
        )

    assert result.exit_code == 0
    assert "launching on omp" in result.output
    assert "Proceed?" in result.output
    mock_post.assert_called_once()


def test_grok_cli_requires_workspace_access_confirmation():
    from cli_agent_orchestrator.cli.commands.launch import (
        PROVIDERS_REQUIRING_WORKSPACE_ACCESS,
    )

    assert "grok_cli" in PROVIDERS_REQUIRING_WORKSPACE_ACCESS


def test_minimax_code_requires_workspace_access_confirmation():
    from cli_agent_orchestrator.cli.commands.launch import (
        PROVIDERS_REQUIRING_WORKSPACE_ACCESS,
    )

    assert "mcode" in PROVIDERS_REQUIRING_WORKSPACE_ACCESS


# ── Server-side initial_message delivery (caom-7it) ────────────────────


def _create_session_response():
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "session_name": "test-session",
        "id": "test-terminal-id",
        "name": "test-terminal",
    }
    return resp


def test_launch_create_call_keeps_mcp_request_timeout():
    """``POST /sessions`` stays on the ordinary ``mcp_request_timeout``.

    Widening this instead of moving delivery server-side was the wrong fix: it
    raises requests' CONNECT timeout as well (``timeout=`` is a single scalar,
    so a dead server takes minutes to report), and it only helps callers that
    happen to be this CLI. With ``initial_message`` in the body the server
    returns before init even starts, so no widening is needed at all.
    """
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.requests.get") as mock_get,
        patch("cli_agent_orchestrator.cli.commands.launch.get_server_settings") as mock_settings,
    ):
        mock_settings.return_value = {"mcp_request_timeout": 30}
        mock_post.return_value = _create_session_response()

        poll_resp = MagicMock()
        poll_resp.raise_for_status.return_value = None
        poll_resp.json.return_value = {"status": "completed"}
        output_resp = MagicMock()
        output_resp.raise_for_status.return_value = None
        output_resp.json.return_value = {"output": "task done"}
        mock_get.side_effect = [poll_resp, output_resp]

        result = runner.invoke(
            launch, ["--agents", "test-agent", "--headless", "--yolo", "do something"]
        )

        assert result.exit_code == 0
        assert mock_post.call_args.kwargs["timeout"] == 30
        # The /output read is an ordinary request too.
        assert mock_get.call_args_list[-1].kwargs["timeout"] == 30


def test_launch_headless_message_travels_in_body_not_query_string():
    """MESSAGE must not land in the URL: prompts are large (414 risk) and are
    routinely captured verbatim in HTTP access logs and traces. Same reason
    ``--env`` values were moved into the body in issue #248."""
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.requests.get") as mock_get,
    ):
        mock_post.return_value = _create_session_response()

        poll_resp = MagicMock()
        poll_resp.raise_for_status.return_value = None
        poll_resp.json.return_value = {"status": "completed"}
        output_resp = MagicMock()
        output_resp.raise_for_status.return_value = None
        output_resp.json.return_value = {"output": ""}
        mock_get.side_effect = [poll_resp, output_resp]

        result = runner.invoke(
            launch, ["--agents", "test-agent", "--headless", "--yolo", "secret prompt"]
        )

        assert result.exit_code == 0
        assert mock_post.call_args.kwargs["json"]["initial_message"] == "secret prompt"
        assert "message" not in mock_post.call_args.kwargs["params"]
        assert "secret prompt" not in str(mock_post.call_args.kwargs["params"])


def test_launch_headless_message_shares_body_with_forwarded_env():
    """``initial_message`` and ``env_vars`` are both body fields on
    ``CreateSessionBody`` — passing both must not drop either."""
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.requests.get") as mock_get,
    ):
        mock_post.return_value = _create_session_response()

        poll_resp = MagicMock()
        poll_resp.raise_for_status.return_value = None
        poll_resp.json.return_value = {"status": "completed"}
        output_resp = MagicMock()
        output_resp.raise_for_status.return_value = None
        output_resp.json.return_value = {"output": ""}
        mock_get.side_effect = [poll_resp, output_resp]

        result = runner.invoke(
            launch,
            [
                "--agents",
                "test-agent",
                "--headless",
                "--yolo",
                "--env",
                "AWS_PROFILE=dev",
                "do something",
            ],
        )

        assert result.exit_code == 0
        body = mock_post.call_args.kwargs["json"]
        assert body["initial_message"] == "do something"
        assert body["env_vars"] == {"AWS_PROFILE": "dev"}


def test_launch_async_returns_without_sending_input():
    """``--async`` reports and returns. It must not fall back to a client-side
    send, and it must not wait for IDLE first — the server delivers MESSAGE
    once init completes."""
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.requests.get") as mock_get,
        patch("cli_agent_orchestrator.cli.commands.launch.wait_until_terminal_status") as mock_wait,
        patch("cli_agent_orchestrator.cli.commands.launch.poll_until_done") as mock_poll,
    ):
        mock_post.return_value = _create_session_response()

        result = runner.invoke(
            launch,
            ["--agents", "test-agent", "--headless", "--async", "--yolo", "do something"],
        )

        assert result.exit_code == 0
        assert "Running in background" in result.output
        assert mock_post.call_count == 1
        assert mock_post.call_args.kwargs["json"]["initial_message"] == "do something"
        mock_wait.assert_not_called()
        mock_poll.assert_not_called()
        mock_get.assert_not_called()


def test_launch_headless_no_message_omits_initial_message():
    """A headless launch with no MESSAGE must not put the terminal on the
    deferred-init path — it has nothing to deliver, and deferring would report
    UNKNOWN instead of IDLE to a caller that expects a ready terminal."""
    runner = CliRunner()

    with patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post:
        mock_post.return_value = _create_session_response()

        result = runner.invoke(launch, ["--agents", "test-agent", "--headless", "--yolo"])

        assert result.exit_code == 0
        assert "json" not in mock_post.call_args.kwargs


def test_launch_non_headless_does_not_defer_init():
    """The attach path never sent MESSAGE and must not start now.

    Deferring init here would leave the pre-attach readiness poll racing the
    agent's first turn: the terminal reports UNKNOWN until init finishes and
    then goes straight to PROCESSING, so the wait for IDLE would usually expire
    and print a spurious "did not reach idle" warning before attaching.
    """
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.get_backend"),
        patch("cli_agent_orchestrator.cli.commands.launch.sync_backend_from_server"),
        patch("cli_agent_orchestrator.cli.commands.launch.wait_until_terminal_status") as mock_wait,
    ):
        mock_post.return_value = _create_session_response()
        mock_wait.return_value = True

        result = runner.invoke(launch, ["--agents", "test-agent", "--yolo", "do something"])

        assert result.exit_code == 0
        assert "json" not in mock_post.call_args.kwargs
        assert mock_post.call_count == 1


def test_launch_headless_poll_budget_covers_server_side_init():
    """The wait budget must cover init as well as the task.

    Init used to happen in a separate readiness poll BEFORE the client-side
    send; it now runs inside this wait, so charging the task's own 300s for it
    would silently shrink how long an agent gets to finish.
    """
    runner = CliRunner()

    with (
        patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post,
        patch("cli_agent_orchestrator.cli.commands.launch.requests.get") as mock_get,
        patch("cli_agent_orchestrator.cli.commands.launch.poll_until_done") as mock_poll,
    ):
        mock_post.return_value = _create_session_response()
        output_resp = MagicMock()
        output_resp.raise_for_status.return_value = None
        output_resp.json.return_value = {"output": "task done"}
        mock_get.return_value = output_resp

        result = runner.invoke(
            launch, ["--agents", "test-agent", "--headless", "--yolo", "do something"]
        )

        assert result.exit_code == 0
        mock_poll.assert_called_once_with(
            "test-terminal-id", timeout=_READINESS_WAIT_TIMEOUT + _HEADLESS_TASK_TIMEOUT
        )
