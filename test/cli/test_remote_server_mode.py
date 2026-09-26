"""Shared-server CLI mode (#745).

With CAO_API_BASE_URL set, `cao schedule` and `cao memory` operate on the
server's state over HTTP (never a client-local database), operations that
need the server's filesystem/tmux fail explicitly, and local mode stays
byte-for-byte unchanged (no env var -> no HTTP, local services called).
"""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.memory import memory
from cli_agent_orchestrator.cli.commands.schedule import schedule
from cli_agent_orchestrator.utils import remote_server

REMOTE = "http://cao-server.example:9889"


@pytest.fixture()
def remote_env(monkeypatch):
    monkeypatch.setenv("CAO_API_BASE_URL", REMOTE)


@pytest.fixture()
def local_env(monkeypatch):
    monkeypatch.delenv("CAO_API_BASE_URL", raising=False)


def _response(json_body=None, status=200, content=b""):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = json_body
    resp.content = content
    return resp


class TestRemoteSelection:
    def test_unset_is_local(self, local_env):
        assert remote_server.remote_base_url() is None
        assert not remote_server.is_remote_server()

    def test_set_is_remote_and_normalized(self, monkeypatch):
        monkeypatch.setenv("CAO_API_BASE_URL", REMOTE + "/")
        assert remote_server.remote_base_url() == REMOTE

    def test_require_local_passes_locally(self, local_env):
        remote_server.require_local("anything")  # no raise

    def test_require_local_fails_remotely(self, remote_env):
        import click

        with pytest.raises(click.ClickException, match="not supported against a shared server"):
            remote_server.require_local("cao memory repair")


class TestScheduleRemote:
    def test_list_goes_over_http(self, remote_env):
        flows = [
            {
                "name": "nightly",
                "schedule": "0 2 * * *",
                "agent_profile": "developer",
                "last_run": None,
                "next_run": "2026-09-21T02:00:00",
                "enabled": True,
            }
        ]
        with patch.object(remote_server.requests, "request", return_value=_response(flows)) as req:
            result = CliRunner().invoke(schedule, ["list"])
        assert result.exit_code == 0, result.output
        assert "nightly" in result.output
        method, url = req.call_args[0]
        assert (method, url) == ("get", f"{REMOTE}/flows")

    def test_add_uploads_the_pre_script_contents(self, remote_env, tmp_path):
        """A relative pre-script resolves against the flow file HERE, and its
        contents are uploaded — a path would be meaningless on the server, which
        never sees this checkout (guojing1217 on #802)."""
        (tmp_path / "check.py").write_text("#!/usr/bin/env python3\nprint('{\"execute\": true}')\n")
        flow_file = tmp_path / "nightly.md"
        flow_file.write_text(
            "---\n"
            "name: nightly\n"
            'schedule: "0 2 * * *"\n'
            "agent_profile: developer\n"
            "engine: v2\n"
            "script: check.py\n"
            "---\n"
            "Do the nightly things.\n"
        )
        created = {
            "name": "nightly",
            "schedule": "0 2 * * *",
            "agent_profile": "developer",
            "next_run": "2026-09-21T02:00:00",
        }
        with patch.object(
            remote_server.requests, "request", return_value=_response(created)
        ) as req:
            result = CliRunner().invoke(schedule, ["add", str(flow_file)])
        assert result.exit_code == 0, result.output
        body = req.call_args.kwargs["json"]
        # Engine and pre-script preserved, never silently dropped (#745).
        assert body["engine"] == "v2"
        assert "script" not in body  # a path is not sent; the contents are
        assert "print(" in body["script_body"]
        assert body["prompt_template"].strip() == "Do the nightly things."

    def test_add_uploads_an_absolute_pre_scripts_contents_too(self, remote_env, tmp_path):
        script = tmp_path / "abs_check.py"
        script.write_text("#!/usr/bin/env python3\nprint('ok')\n")
        flow_file = tmp_path / "nightly.md"
        flow_file.write_text(
            "---\n"
            "name: nightly\n"
            'schedule: "0 2 * * *"\n'
            "agent_profile: developer\n"
            f"script: {script}\n"
            "---\n"
            "Do the nightly things.\n"
        )
        created = {
            "name": "nightly",
            "schedule": "0 2 * * *",
            "agent_profile": "developer",
            "next_run": "2026-09-21T02:00:00",
        }
        with patch.object(
            remote_server.requests, "request", return_value=_response(created)
        ) as req:
            result = CliRunner().invoke(schedule, ["add", str(flow_file)])
        assert result.exit_code == 0, result.output
        assert "print('ok')" in req.call_args.kwargs["json"]["script_body"]

    def test_add_reports_a_missing_pre_script_before_registering(self, remote_env, tmp_path):
        """Reading the file here surfaces a missing script while a human watches,
        instead of deferring the error to a scheduled run minutes later."""
        flow_file = tmp_path / "nightly.md"
        flow_file.write_text(
            "---\n"
            "name: nightly\n"
            'schedule: "0 2 * * *"\n'
            "agent_profile: developer\n"
            "script: nonexistent.py\n"
            "---\n"
            "Do the nightly things.\n"
        )
        with patch.object(remote_server.requests, "request") as req:
            result = CliRunner().invoke(schedule, ["add", str(flow_file)])
        assert result.exit_code != 0
        assert "not found" in result.output
        req.assert_not_called()

    def test_add_without_a_pre_script_is_unaffected(self, remote_env, tmp_path):
        flow_file = tmp_path / "nightly.md"
        flow_file.write_text(
            "---\n"
            "name: nightly\n"
            'schedule: "0 2 * * *"\n'
            "agent_profile: developer\n"
            "---\n"
            "Do the nightly things.\n"
        )
        created = {
            "name": "nightly",
            "schedule": "0 2 * * *",
            "agent_profile": "developer",
            "next_run": "2026-09-21T02:00:00",
        }
        with patch.object(
            remote_server.requests, "request", return_value=_response(created)
        ) as req:
            result = CliRunner().invoke(schedule, ["add", str(flow_file)])
        assert result.exit_code == 0, result.output
        assert "script" not in req.call_args.kwargs["json"]

    def test_run_posts_and_reports(self, remote_env):
        with patch.object(
            remote_server.requests, "request", return_value=_response({"executed": True})
        ) as req:
            result = CliRunner().invoke(schedule, ["run", "nightly"])
        assert result.exit_code == 0, result.output
        assert "executed successfully" in result.output
        assert req.call_args[0] == ("post", f"{REMOTE}/flows/nightly/run")

    def test_local_mode_untouched(self, local_env):
        # No CAO_API_BASE_URL: the existing local service path runs, no HTTP.
        with (
            patch("cli_agent_orchestrator.cli.commands.schedule.flow_service") as svc,
            patch("cli_agent_orchestrator.cli.commands.schedule.init_db") as init,
            patch.object(remote_server.requests, "request") as req,
        ):
            svc.list_flows.return_value = []
            result = CliRunner().invoke(schedule, ["list"])
        assert result.exit_code == 0, result.output
        init.assert_called_once()
        svc.list_flows.assert_called_once()
        req.assert_not_called()


class TestMemoryRemote:
    def test_list_goes_over_http(self, remote_env):
        rows = [
            {
                "key": "deploy-runbook",
                "scope": "global",
                "scope_id": None,
                "memory_type": "reference",
                "tags": "ops",
                "created_at": "2026-09-01T10:00:00",
                "updated_at": "2026-09-19T10:00:00",
            }
        ]
        with patch.object(remote_server.requests, "request", return_value=_response(rows)) as req:
            result = CliRunner().invoke(memory, ["list"])
        assert result.exit_code == 0, result.output
        assert "deploy-runbook" in result.output
        assert req.call_args[0] == ("get", f"{REMOTE}/memory")

    def test_delete_requires_no_local_db(self, remote_env):
        with patch.object(
            remote_server.requests, "request", return_value=_response({"success": True})
        ) as req:
            result = CliRunner().invoke(
                memory,
                ["delete", "deploy-runbook", "--scope", "global", "--yes"],
            )
        assert result.exit_code == 0, result.output
        method, url = req.call_args[0]
        assert (method, url) == ("delete", f"{REMOTE}/memory/deploy-runbook")

    def test_repair_is_explicitly_unsupported(self, remote_env):
        result = CliRunner().invoke(memory, ["repair"])
        assert result.exit_code != 0
        assert "not supported against a shared server" in result.output

    def test_import_is_explicitly_unsupported(self, remote_env, tmp_path):
        result = CliRunner().invoke(memory, ["import", str(tmp_path), "--scope", "global"])
        assert result.exit_code != 0
        assert "not supported against a shared server" in result.output

    def test_http_error_surfaces_detail(self, remote_env):
        resp = _response({"detail": "Memory 'nope' not found"}, status=404)
        with patch.object(remote_server.requests, "request", return_value=resp):
            result = CliRunner().invoke(memory, ["show", "nope"])
        assert result.exit_code != 0
        assert "not found" in result.output


class TestLaunchRemote:
    def test_interactive_remote_launch_attaches_via_relay(self, remote_env):
        """No client-local tmux: interactive launch against a shared server
        goes through the server's WS attach relay (#745/#776)."""
        from cli_agent_orchestrator.cli.commands import launch as launch_mod

        created = MagicMock(status_code=200)
        created.json.return_value = {
            "id": "def67890",
            "name": "developer-def6",
            "session_name": "cao-abc",
        }
        with (
            patch.object(launch_mod.requests, "post", return_value=created),
            patch.object(launch_mod, "_attach_via_relay") as attach,
            patch.object(launch_mod, "get_backend") as backend,
        ):
            result = CliRunner().invoke(
                launch_mod.launch,
                ["--agents", "developer", "--provider", "mock_cli", "--yolo"],
            )
        assert result.exit_code == 0, result.output
        attach.assert_called_once()
        backend.return_value.attach_session.assert_not_called()

    def test_restore_is_explicit_error(self, remote_env):
        from cli_agent_orchestrator.cli.commands.terminal import terminal

        result = CliRunner().invoke(terminal, ["restore", "abcd1234"])
        assert result.exit_code != 0
        assert "not supported against a shared server" in result.output

    def test_interactive_runtime_launch_attaches_via_relay(self, remote_env):
        from cli_agent_orchestrator.cli.commands import launch as launch_mod

        created = MagicMock(status_code=200)
        created.json.return_value = {
            "id": "def67890",
            "name": "developer-def6",
            "session_name": "cao-abc",
        }
        with (
            patch.object(launch_mod.requests, "post", return_value=created) as post,
            patch.object(launch_mod, "_attach_via_relay") as attach,
        ):
            result = CliRunner().invoke(
                launch_mod.launch,
                [
                    "--agents",
                    "developer",
                    "--provider",
                    "mock_cli",
                    "--runtime",
                    "cao-worker-x",
                    "--yolo",
                ],
            )
        assert result.exit_code == 0, result.output
        assert post.call_args.args[0].endswith("/runtimes/cao-worker-x/terminals")
        attach.assert_called_once()

    def test_runtime_launch_posts_to_the_runtime_path(self, remote_env):
        from cli_agent_orchestrator.cli.commands import launch as launch_mod

        created = MagicMock(status_code=200)
        created.json.return_value = {
            "id": "def67890",
            "name": "developer-def6",
            "session_name": "cao-abc",
        }
        with patch.object(launch_mod.requests, "post", return_value=created) as post:
            result = CliRunner().invoke(
                launch_mod.launch,
                [
                    "--agents",
                    "developer",
                    "--provider",
                    "mock_cli",
                    "--headless",
                    "--runtime",
                    "cao-worker-x",
                    "--yolo",
                ],
            )
        assert result.exit_code == 0, result.output
        url = post.call_args.args[0]
        assert url == f"{REMOTE}/runtimes/cao-worker-x/terminals"
        body = post.call_args.kwargs["json"]
        assert body == {"provider": "mock_cli", "agent_profile": "developer"}
        assert "runtime: cao-worker-x" in result.output

    def test_every_launch_request_carries_the_bearer_token(self, remote_env, monkeypatch):
        """A shared server with auth configured refuses an unauthenticated POST.

        `cao launch` addressed the server through the import-time local
        `API_BASE_URL` and sent no Authorization header, so against a shared
        server it either talked to whatever listens on this machine's port or was
        rejected as anonymous (Copilot review on #802, finding 1).
        """
        from cli_agent_orchestrator.cli.commands import launch as launch_mod

        monkeypatch.setenv("CAO_API_TOKEN", "bearer-value")
        created = MagicMock(status_code=200)
        created.json.return_value = {
            "id": "def67890",
            "name": "developer-def6",
            "session_name": "cao-abc",
        }
        with patch.object(launch_mod.requests, "post", return_value=created) as post:
            result = CliRunner().invoke(
                launch_mod.launch,
                [
                    "--agents",
                    "developer",
                    "--provider",
                    "mock_cli",
                    "--headless",
                    "--runtime",
                    "cao-worker-x",
                    "--yolo",
                ],
            )
        assert result.exit_code == 0, result.output
        assert post.call_args.args[0].startswith(REMOTE)
        assert post.call_args.kwargs["headers"] == {"Authorization": "Bearer bearer-value"}

    def test_a_session_create_is_addressed_and_authorized_too(self, remote_env, monkeypatch):
        from cli_agent_orchestrator.cli.commands import launch as launch_mod

        monkeypatch.setenv("CAO_API_TOKEN", "bearer-value")
        created = MagicMock(status_code=200)
        created.json.return_value = {
            "id": "def67890",
            "name": "developer-def6",
            "session_name": "cao-abc",
        }
        with (
            patch.object(launch_mod.requests, "post", return_value=created) as post,
            patch.object(launch_mod, "_attach_via_relay"),
        ):
            result = CliRunner().invoke(
                launch_mod.launch,
                ["--agents", "developer", "--provider", "mock_cli", "--yolo"],
            )
        assert result.exit_code == 0, result.output
        assert post.call_args.args[0] == f"{REMOTE}/sessions"
        assert post.call_args.kwargs["headers"] == {"Authorization": "Bearer bearer-value"}

    def test_no_token_configured_sends_no_header(self, remote_env, monkeypatch):
        """Local and unauthenticated installs are unchanged."""
        from cli_agent_orchestrator.cli.commands import launch as launch_mod

        monkeypatch.delenv("CAO_API_TOKEN", raising=False)
        created = MagicMock(status_code=200)
        created.json.return_value = {
            "id": "def67890",
            "name": "developer-def6",
            "session_name": "cao-abc",
        }
        with (
            patch.object(launch_mod.requests, "post", return_value=created) as post,
            patch.object(launch_mod, "_attach_via_relay"),
        ):
            result = CliRunner().invoke(
                launch_mod.launch,
                ["--agents", "developer", "--provider", "mock_cli", "--yolo"],
            )
        assert result.exit_code == 0, result.output
        assert post.call_args.kwargs["headers"] == {}

    def test_the_attach_relay_is_given_the_server_and_the_token(self, remote_env, monkeypatch):
        """A server with AUTH0_* set closes the WS handshake without a token.

        The terminal would already exist and the attach be refused, which reads
        as a broken launch.
        """
        from cli_agent_orchestrator.cli.commands import launch as launch_mod

        monkeypatch.setenv("CAO_API_TOKEN", "bearer-value")
        with (
            patch.object(launch_mod, "wait_until_terminal_status", return_value=True),
            patch("cli_agent_orchestrator.utils.remote_attach.attach_remote_terminal") as attach,
        ):
            launch_mod._attach_via_relay({"id": "def67890", "name": "developer-def6"})
        assert attach.call_args.args == ("def67890", REMOTE)
        assert attach.call_args.kwargs == {"token": "bearer-value"}

    def test_headless_drive_addresses_the_shared_server(self, remote_env, monkeypatch):
        from cli_agent_orchestrator.cli.commands import launch as launch_mod

        monkeypatch.setenv("CAO_API_TOKEN", "bearer-value")
        out = MagicMock(status_code=200)
        out.json.return_value = {"output": "done"}
        with (
            patch.object(launch_mod, "wait_until_terminal_status", return_value=True),
            patch.object(launch_mod, "poll_until_done"),
            patch.object(launch_mod.time, "sleep"),
            patch.object(launch_mod.requests, "post", return_value=out) as post,
            patch.object(launch_mod.requests, "get", return_value=out) as get,
        ):
            launch_mod._drive_headless_message(
                {"id": "def67890", "name": "developer-def6"}, "go", is_async=False
            )
        assert post.call_args.args[0] == f"{REMOTE}/terminals/def67890/input"
        assert get.call_args.args[0] == f"{REMOTE}/terminals/def67890/output"
        for call in (post, get):
            assert call.call_args.kwargs["headers"] == {"Authorization": "Bearer bearer-value"}

    def test_runtime_launch_refuses_untravelable_overrides(self, remote_env):
        from cli_agent_orchestrator.cli.commands.launch import launch

        result = CliRunner().invoke(
            launch,
            [
                "--agents",
                "developer",
                "--headless",
                "--runtime",
                "cao-worker-x",
                "--allowed-tools",
                "fs_read",
                "--auto-approve",
            ],
        )
        assert result.exit_code != 0
        assert "cannot travel to a runtime launch" in result.output
