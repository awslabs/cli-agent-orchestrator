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

    def test_add_parses_client_file_and_posts_fields(self, remote_env, tmp_path):
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
        assert body["script"] == "check.py"
        assert body["prompt_template"].strip() == "Do the nightly things."

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
