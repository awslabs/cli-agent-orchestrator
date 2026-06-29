"""Tests for server-process lifecycle helpers."""

from unittest.mock import MagicMock, patch

import requests

from cli_agent_orchestrator.utils import server_process as sp

_SP = "cli_agent_orchestrator.utils.server_process"


class TestHealth:
    @patch(f"{_SP}.requests.get")
    def test_health_returns_json_when_ok(self, mock_get):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"status": "ok"}
        mock_get.return_value = resp

        assert sp.health() == {"status": "ok"}
        assert sp.is_server_running() is True

    @patch(f"{_SP}.requests.get", side_effect=requests.exceptions.ConnectionError())
    def test_health_none_on_connection_error(self, _get):
        assert sp.health() is None
        assert sp.is_server_running() is False

    @patch(f"{_SP}.requests.get")
    def test_health_none_on_non_200(self, mock_get):
        mock_get.return_value = MagicMock(status_code=503)
        assert sp.health() is None


class TestPidfile:
    def test_write_read_clear_roundtrip(self, tmp_path):
        pidfile = tmp_path / "server.pid"
        with patch(f"{_SP}.SERVER_PIDFILE", pidfile):
            sp.write_pidfile(1234)
            assert sp.read_pidfile() == 1234
            sp.clear_pidfile()
            assert sp.read_pidfile() is None

    def test_read_pidfile_missing(self, tmp_path):
        with patch(f"{_SP}.SERVER_PIDFILE", tmp_path / "nope.pid"):
            assert sp.read_pidfile() is None

    def test_read_pidfile_garbage(self, tmp_path):
        pidfile = tmp_path / "server.pid"
        pidfile.write_text("not-a-pid")
        with patch(f"{_SP}.SERVER_PIDFILE", pidfile):
            assert sp.read_pidfile() is None


class TestStartServerDetached:
    @patch(f"{_SP}.read_pidfile", return_value=7777)
    @patch(f"{_SP}.is_server_running", return_value=True)
    def test_noop_when_already_running(self, _running, _pid):
        assert sp.start_server_detached() == 7777

    @patch(f"{_SP}.write_pidfile")
    @patch(f"{_SP}.subprocess.Popen")
    @patch(f"{_SP}.is_server_running")
    def test_spawns_and_waits_for_health(self, mock_running, mock_popen, mock_write, tmp_path):
        # Not running at first check, healthy after spawn.
        mock_running.side_effect = [False, True]
        proc = MagicMock(pid=4242)
        proc.poll.return_value = None
        mock_popen.return_value = proc

        with patch(f"{_SP}.LOG_DIR", tmp_path):
            pid = sp.start_server_detached(timeout=5)

        assert pid == 4242
        mock_write.assert_called_once_with(4242)
        # Detached process group requested.
        assert mock_popen.call_args.kwargs["start_new_session"] is True

    @patch(f"{_SP}.write_pidfile")
    @patch(f"{_SP}.subprocess.Popen")
    @patch(f"{_SP}.is_server_running")
    def test_raises_when_child_dies(self, mock_running, mock_popen, _write, tmp_path):
        mock_running.return_value = False
        proc = MagicMock(pid=4242, returncode=1)
        proc.poll.return_value = 1  # exited
        mock_popen.return_value = proc

        with patch(f"{_SP}.LOG_DIR", tmp_path):
            try:
                sp.start_server_detached(timeout=5)
                assert False, "expected RuntimeError"
            except RuntimeError as e:
                assert "exited during startup" in str(e)


class TestStopServer:
    @patch(f"{_SP}.clear_pidfile")
    @patch(f"{_SP}._pid_alive")
    @patch(f"{_SP}.is_server_running", return_value=False)
    @patch(f"{_SP}.os.kill")
    @patch(f"{_SP}.read_pidfile", return_value=4321)
    def test_stop_success(self, _pid, mock_kill, _running, mock_alive, _clear):
        # Alive when SIGTERM is sent, dead on the confirmation loop.
        mock_alive.side_effect = [True, False]
        assert sp.stop_server(timeout=5) is True
        mock_kill.assert_called_once()

    @patch(f"{_SP}.read_pidfile", return_value=None)
    @patch(f"{_SP}.is_server_running", return_value=False)
    def test_stop_no_pidfile_reports_health(self, _running, _pid):
        # No PID known but health already down → treat as stopped.
        assert sp.stop_server(timeout=5) is True


class TestServerErrorHint:
    def test_hint_mentions_logs_command(self):
        with patch(
            "cli_agent_orchestrator.utils.logging.latest_server_log_path", return_value=None
        ):
            hint = sp.server_error_hint()
        assert "cao logs --server" in hint
