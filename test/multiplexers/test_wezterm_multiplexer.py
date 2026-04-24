"""Unit tests for WezTermMultiplexer — all subprocess calls mocked via runner injection."""

from __future__ import annotations

import subprocess
from typing import Optional
from unittest.mock import MagicMock, call, patch

import pytest

from cli_agent_orchestrator.multiplexers.base import LaunchSpec
from cli_agent_orchestrator.multiplexers.wezterm import WezTermMultiplexer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_result(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    result: subprocess.CompletedProcess[str] = subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=""
    )
    return result


def _spawn_result(pane_id: str = "42") -> subprocess.CompletedProcess[str]:
    return _make_result(stdout=f"{pane_id}\n")


def _runner_factory(responses: dict[str, subprocess.CompletedProcess[str]]):
    """Return a runner that matches on the first unique fragment of argv."""

    def runner(argv, env=None):
        key = " ".join(str(a) for a in argv)
        for fragment, result in responses.items():
            if fragment in key:
                return result
        return _make_result()

    return runner


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def wez(tmp_path):
    """WezTermMultiplexer with a no-op runner and patched working-dir validation."""
    mux = WezTermMultiplexer(runner=lambda argv, env=None: _spawn_result(), wezterm_bin="wezterm")
    with patch.object(
        mux,
        "_resolve_and_validate_working_directory",
        return_value=str(tmp_path),
    ):
        yield mux, str(tmp_path)


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr("cli_agent_orchestrator.multiplexers.wezterm.time.sleep", lambda *_: None)


# ---------------------------------------------------------------------------
# create_session
# ---------------------------------------------------------------------------


class TestCreateSession:
    def test_argv_contains_new_window_cwd_and_terminal_id(self, tmp_path):
        calls: list[list[str]] = []

        def runner(argv, env=None):
            calls.append(list(argv))
            return _spawn_result("17")

        mux = WezTermMultiplexer(runner=runner, wezterm_bin="wezterm")
        with patch.object(
            mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)
        ):
            mux.create_session("ses", "win", "tid-abc", str(tmp_path))

        assert len(calls) == 1
        argv = calls[0]
        assert "wezterm" == argv[0]
        assert "cli" in argv
        assert "spawn" in argv
        assert "--new-window" in argv
        assert "--cwd" in argv
        assert str(tmp_path) in argv
        assert "--set-environment" in argv
        cad_idx = argv.index("--set-environment")
        # find the CAO_TERMINAL_ID entry — may not be directly after --set-environment
        # when multiple --set-environment entries exist; check all pairs
        env_pairs = []
        for i, tok in enumerate(argv):
            if tok == "--set-environment" and i + 1 < len(argv):
                env_pairs.append(argv[i + 1])
        assert any("CAO_TERMINAL_ID=tid-abc" == pair for pair in env_pairs)

    def test_parses_pane_id_from_stdout(self, tmp_path):
        mux = WezTermMultiplexer(runner=lambda argv, env=None: _spawn_result("99"), wezterm_bin="wezterm")
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            result = mux.create_session("ses", "win", "tid", str(tmp_path))
        assert result == "win"

    def test_pane_id_stored_in_registry(self, tmp_path):
        mux = WezTermMultiplexer(runner=lambda argv, env=None: _spawn_result("77"), wezterm_bin="wezterm")
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            mux.create_session("ses", "win", "tid", str(tmp_path))
        assert mux._sessions["ses"]["win"]["pane_id"] == "77"

    def test_launch_spec_argv_appended_after_double_dash(self, tmp_path):
        calls: list[list[str]] = []

        def runner(argv, env=None):
            calls.append(list(argv))
            return _spawn_result("5")

        mux = WezTermMultiplexer(runner=runner, wezterm_bin="wezterm")
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            mux.create_session(
                "ses",
                "win",
                "tid",
                str(tmp_path),
                launch_spec=LaunchSpec(argv=["codex.cmd", "--yolo"]),
            )

        argv = calls[0]
        dd_idx = argv.index("--")
        assert argv[dd_idx + 1] == "codex.cmd"
        assert argv[dd_idx + 2] == "--yolo"

    def test_launch_spec_env_adds_set_environment(self, tmp_path):
        calls: list[list[str]] = []

        def runner(argv, env=None):
            calls.append(list(argv))
            return _spawn_result("5")

        mux = WezTermMultiplexer(runner=runner, wezterm_bin="wezterm")
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            mux.create_session(
                "ses",
                "win",
                "tid",
                str(tmp_path),
                launch_spec=LaunchSpec(env={"FOO": "bar"}),
            )

        argv = calls[0]
        env_pairs = []
        for i, tok in enumerate(argv):
            if tok == "--set-environment" and i + 1 < len(argv):
                env_pairs.append(argv[i + 1])
        assert "FOO=bar" in env_pairs

    def test_raises_runtime_error_when_stdout_has_no_pane_id(self, tmp_path):
        mux = WezTermMultiplexer(
            runner=lambda argv, env=None: _make_result(stdout="not a number"),
            wezterm_bin="wezterm",
        )
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            with pytest.raises(RuntimeError, match="no pane id"):
                mux.create_session("ses", "win", "tid", str(tmp_path))

    def test_raises_runtime_error_when_stdout_is_empty(self, tmp_path):
        mux = WezTermMultiplexer(
            runner=lambda argv, env=None: _make_result(stdout=""),
            wezterm_bin="wezterm",
        )
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            with pytest.raises(RuntimeError, match="no pane id"):
                mux.create_session("ses", "win", "tid", str(tmp_path))


# ---------------------------------------------------------------------------
# create_window
# ---------------------------------------------------------------------------


class TestCreateWindow:
    def test_create_window_stores_pane_in_existing_session(self, tmp_path):
        mux = WezTermMultiplexer(runner=lambda argv, env=None: _spawn_result("55"), wezterm_bin="wezterm")
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            mux.create_session("ses", "win1", "tid1", str(tmp_path))
            mux._sessions["ses"]["win1"]["pane_id"] = "55"
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            mux.create_window("ses", "win2", "tid2", str(tmp_path))
        assert "win2" in mux._sessions["ses"]
        assert mux._sessions["ses"]["win2"]["pane_id"] == "55"

    def test_create_window_uses_new_window_flag(self, tmp_path):
        calls: list[list[str]] = []

        def runner(argv, env=None):
            calls.append(list(argv))
            return _spawn_result("10")

        mux = WezTermMultiplexer(runner=runner, wezterm_bin="wezterm")
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            mux.create_window("ses", "win", "tid", str(tmp_path))

        assert "--new-window" in calls[0]


# ---------------------------------------------------------------------------
# _paste_text
# ---------------------------------------------------------------------------


class TestPasteText:
    def test_sends_send_text_with_pane_id_and_text(self, tmp_path, no_sleep):
        calls: list[list[str]] = []

        def runner(argv, env=None):
            calls.append(list(argv))
            return _spawn_result("11")

        mux = WezTermMultiplexer(runner=runner, wezterm_bin="wezterm")
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            mux.create_session("ses", "win", "tid", str(tmp_path))
        calls.clear()

        mux._paste_text("ses", "win", "hello world")

        assert len(calls) == 1
        argv = calls[0]
        assert argv[:3] == ["wezterm", "cli", "send-text"]
        assert "--pane-id" in argv
        pane_idx = argv.index("--pane-id")
        assert argv[pane_idx + 1] == "11"
        assert "--" in argv
        dd_idx = argv.index("--")
        assert argv[dd_idx + 1] == "hello world"
        assert "--no-paste" not in argv

    def test_raises_when_pane_not_found(self):
        mux = WezTermMultiplexer(runner=lambda argv, env=None: _spawn_result(), wezterm_bin="wezterm")
        with pytest.raises(RuntimeError, match="not found"):
            mux._paste_text("missing_ses", "missing_win", "text")


# ---------------------------------------------------------------------------
# _submit_input
# ---------------------------------------------------------------------------


class TestSubmitInput:
    def test_submit_once_sends_carriage_return_with_no_paste(self, tmp_path, no_sleep):
        calls: list[list[str]] = []

        def runner(argv, env=None):
            calls.append(list(argv))
            return _spawn_result("22")

        mux = WezTermMultiplexer(runner=runner, wezterm_bin="wezterm")
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            mux.create_session("ses", "win", "tid", str(tmp_path))
        calls.clear()

        mux._submit_input("ses", "win", enter_count=1)

        assert len(calls) == 1
        argv = calls[0]
        assert argv[:3] == ["wezterm", "cli", "send-text"]
        assert "--pane-id" in argv
        assert "--no-paste" in argv
        dd_idx = argv.index("--")
        assert argv[dd_idx + 1] == "\r"

    def test_submit_once_sleeps_300ms(self, tmp_path):
        sleep_calls: list[float] = []

        def runner(argv, env=None):
            return _spawn_result("22")

        mux = WezTermMultiplexer(runner=runner, wezterm_bin="wezterm")
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            mux.create_session("ses", "win", "tid", str(tmp_path))

        with patch("cli_agent_orchestrator.multiplexers.wezterm.time.sleep", side_effect=lambda d: sleep_calls.append(d)):
            mux._submit_input("ses", "win", enter_count=1)

        assert sleep_calls[0] == pytest.approx(0.3)

    def test_submit_three_times_produces_three_enter_calls(self, tmp_path, no_sleep):
        calls: list[list[str]] = []

        def runner(argv, env=None):
            calls.append(list(argv))
            return _spawn_result("33")

        mux = WezTermMultiplexer(runner=runner, wezterm_bin="wezterm")
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            mux.create_session("ses", "win", "tid", str(tmp_path))
        calls.clear()

        mux._submit_input("ses", "win", enter_count=3)

        enter_calls = [a for a in calls if "--no-paste" in a and "\r" in a]
        assert len(enter_calls) == 3

    def test_submit_three_times_sleeps_300ms_then_500ms_between(self, tmp_path):
        sleep_calls: list[float] = []

        mux = WezTermMultiplexer(runner=lambda argv, env=None: _spawn_result("33"), wezterm_bin="wezterm")
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            mux.create_session("ses", "win", "tid", str(tmp_path))

        with patch("cli_agent_orchestrator.multiplexers.wezterm.time.sleep", side_effect=lambda d: sleep_calls.append(d)):
            mux._submit_input("ses", "win", enter_count=3)

        assert sleep_calls[0] == pytest.approx(0.3)
        assert sleep_calls[1] == pytest.approx(0.5)
        assert sleep_calls[2] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# send_keys (inherited default)
# ---------------------------------------------------------------------------


class TestSendKeys:
    def test_send_keys_calls_paste_then_submit(self, tmp_path, no_sleep):
        method_calls: list[str] = []
        mux = WezTermMultiplexer(runner=lambda argv, env=None: _spawn_result("44"), wezterm_bin="wezterm")
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            mux.create_session("ses", "win", "tid", str(tmp_path))

        original_paste = mux._paste_text
        original_submit = mux._submit_input

        def record_paste(s, w, t):
            method_calls.append("paste")
            original_paste(s, w, t)

        def record_submit(s, w, enter_count=1):
            method_calls.append("submit")
            original_submit(s, w, enter_count=enter_count)

        mux._paste_text = record_paste  # type: ignore[method-assign]
        mux._submit_input = record_submit  # type: ignore[method-assign]

        mux.send_keys("ses", "win", "text", enter_count=2)

        assert method_calls == ["paste", "submit"]


# ---------------------------------------------------------------------------
# send_special_key
# ---------------------------------------------------------------------------


class TestSendSpecialKey:
    def test_enter_maps_to_carriage_return_no_paste(self, tmp_path):
        calls: list[list[str]] = []

        mux = WezTermMultiplexer(runner=lambda argv, env=None: (calls.append(list(argv)) or _spawn_result("50")), wezterm_bin="wezterm")  # type: ignore[return-value]

        def runner(argv, env=None):
            calls.append(list(argv))
            return _spawn_result("50")

        mux = WezTermMultiplexer(runner=runner, wezterm_bin="wezterm")
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            mux.create_session("ses", "win", "tid", str(tmp_path))
        calls.clear()

        mux.send_special_key("ses", "win", "Enter")

        assert len(calls) == 1
        argv = calls[0]
        assert "--no-paste" in argv
        dd_idx = argv.index("--")
        assert argv[dd_idx + 1] == "\r"

    def test_literal_true_sends_raw_bytes_no_paste(self, tmp_path):
        calls: list[list[str]] = []

        def runner(argv, env=None):
            calls.append(list(argv))
            return _spawn_result("51")

        mux = WezTermMultiplexer(runner=runner, wezterm_bin="wezterm")
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            mux.create_session("ses", "win", "tid", str(tmp_path))
        calls.clear()

        mux.send_special_key("ses", "win", "\x1b[B", literal=True)

        argv = calls[0]
        assert "--no-paste" in argv
        dd_idx = argv.index("--")
        assert argv[dd_idx + 1] == "\x1b[B"

    def test_tab_maps_to_tab_character(self, tmp_path):
        calls: list[list[str]] = []

        def runner(argv, env=None):
            calls.append(list(argv))
            return _spawn_result("52")

        mux = WezTermMultiplexer(runner=runner, wezterm_bin="wezterm")
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            mux.create_session("ses", "win", "tid", str(tmp_path))
        calls.clear()

        mux.send_special_key("ses", "win", "Tab")

        argv = calls[0]
        dd_idx = argv.index("--")
        assert argv[dd_idx + 1] == "\t"

    def test_up_arrow_maps_to_vt_sequence(self, tmp_path):
        calls: list[list[str]] = []

        def runner(argv, env=None):
            calls.append(list(argv))
            return _spawn_result("53")

        mux = WezTermMultiplexer(runner=runner, wezterm_bin="wezterm")
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            mux.create_session("ses", "win", "tid", str(tmp_path))
        calls.clear()

        mux.send_special_key("ses", "win", "Up")

        argv = calls[0]
        dd_idx = argv.index("--")
        assert argv[dd_idx + 1] == "\x1b[A"


# ---------------------------------------------------------------------------
# get_history
# ---------------------------------------------------------------------------


class TestGetHistory:
    def test_calls_get_text_without_escapes(self, tmp_path):
        calls: list[list[str]] = []

        def runner(argv, env=None):
            calls.append(list(argv))
            if "spawn" in argv:
                return _spawn_result("60")
            return _make_result(stdout="output line\n")

        mux = WezTermMultiplexer(runner=runner, wezterm_bin="wezterm")
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            mux.create_session("ses", "win", "tid", str(tmp_path))
        calls.clear()

        result = mux.get_history("ses", "win")

        assert len(calls) == 1
        argv = calls[0]
        assert argv[:3] == ["wezterm", "cli", "get-text"]
        assert "--pane-id" in argv
        assert "--escapes" not in argv
        assert result == "output line\n"

    def test_tail_lines_returns_last_n_lines(self, tmp_path):
        content = "\n".join(f"line{i}" for i in range(10))

        def runner(argv, env=None):
            if "spawn" in argv:
                return _spawn_result("61")
            return _make_result(stdout=content)

        mux = WezTermMultiplexer(runner=runner, wezterm_bin="wezterm")
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            mux.create_session("ses", "win", "tid", str(tmp_path))

        result = mux.get_history("ses", "win", tail_lines=5)

        lines = result.splitlines()
        assert len(lines) == 5
        assert lines[0] == "line5"
        assert lines[-1] == "line9"

    def test_raises_when_pane_not_found(self):
        mux = WezTermMultiplexer(runner=lambda argv, env=None: _spawn_result(), wezterm_bin="wezterm")
        with pytest.raises(RuntimeError, match="not found"):
            mux.get_history("missing_ses", "missing_win")


# ---------------------------------------------------------------------------
# kill_session / kill_window
# ---------------------------------------------------------------------------


class TestKillSession:
    def test_removes_session_from_registry_and_kills_panes(self, tmp_path):
        calls: list[list[str]] = []

        def runner(argv, env=None):
            calls.append(list(argv))
            if "spawn" in argv:
                return _spawn_result("70")
            return _make_result()

        mux = WezTermMultiplexer(runner=runner, wezterm_bin="wezterm")
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            mux.create_session("ses", "win", "tid", str(tmp_path))
        calls.clear()

        result = mux.kill_session("ses")

        assert result is True
        assert "ses" not in mux._sessions
        kill_calls = [a for a in calls if "kill-pane" in a]
        assert len(kill_calls) >= 1
        kill_argv = kill_calls[0]
        assert "--pane-id" in kill_argv
        pane_idx = kill_argv.index("--pane-id")
        assert kill_argv[pane_idx + 1] == "70"

    def test_returns_false_for_nonexistent_session(self):
        mux = WezTermMultiplexer(runner=lambda argv, env=None: _spawn_result(), wezterm_bin="wezterm")
        result = mux.kill_session("nonexistent")
        assert result is False


class TestKillWindow:
    def test_removes_window_from_registry(self, tmp_path):
        calls: list[list[str]] = []

        def runner(argv, env=None):
            calls.append(list(argv))
            if "spawn" in argv:
                return _spawn_result("80")
            return _make_result()

        mux = WezTermMultiplexer(runner=runner, wezterm_bin="wezterm")
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            mux.create_session("ses", "win", "tid", str(tmp_path))
        calls.clear()

        result = mux.kill_window("ses", "win")

        assert result is True
        assert "win" not in mux._sessions.get("ses", {})

    def test_returns_false_for_nonexistent_window(self, tmp_path):
        calls: list[list[str]] = []

        def runner(argv, env=None):
            calls.append(list(argv))
            return _spawn_result("81")

        mux = WezTermMultiplexer(runner=runner, wezterm_bin="wezterm")
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            mux.create_session("ses", "win", "tid", str(tmp_path))

        result = mux.kill_window("ses", "no_such_win")
        assert result is False


# ---------------------------------------------------------------------------
# session_exists
# ---------------------------------------------------------------------------


class TestSessionExists:
    def test_returns_true_for_registered_session(self, tmp_path):
        mux = WezTermMultiplexer(runner=lambda argv, env=None: _spawn_result("90"), wezterm_bin="wezterm")
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            mux.create_session("ses", "win", "tid", str(tmp_path))
        assert mux.session_exists("ses") is True

    def test_returns_false_for_unknown_session(self):
        mux = WezTermMultiplexer(runner=lambda argv, env=None: _spawn_result(), wezterm_bin="wezterm")
        assert mux.session_exists("nope") is False


# ---------------------------------------------------------------------------
# list_sessions
# ---------------------------------------------------------------------------


class TestListSessions:
    def test_returns_registered_session(self, tmp_path):
        mux = WezTermMultiplexer(runner=lambda argv, env=None: _spawn_result("91"), wezterm_bin="wezterm")
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            mux.create_session("my-ses", "win", "tid", str(tmp_path))

        sessions = mux.list_sessions()
        names = [s["name"] for s in sessions]
        assert "my-ses" in names

    def test_returns_empty_when_no_sessions(self):
        mux = WezTermMultiplexer(runner=lambda argv, env=None: _spawn_result(), wezterm_bin="wezterm")
        assert mux.list_sessions() == []


# ---------------------------------------------------------------------------
# get_pane_working_directory
# ---------------------------------------------------------------------------


class TestGetPaneWorkingDirectory:
    def test_returns_none_for_unknown_window(self):
        mux = WezTermMultiplexer(runner=lambda argv, env=None: _spawn_result(), wezterm_bin="wezterm")
        assert mux.get_pane_working_directory("ses", "win") is None


# ---------------------------------------------------------------------------
# pipe_pane / stop_pipe_pane — Task 7 stubs
# ---------------------------------------------------------------------------


class TestPipePaneNotImplemented:
    def test_pipe_pane_raises_not_implemented(self, tmp_path):
        mux = WezTermMultiplexer(runner=lambda argv, env=None: _spawn_result("100"), wezterm_bin="wezterm")
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            mux.create_session("ses", "win", "tid", str(tmp_path))
        with pytest.raises(NotImplementedError, match="Task 7"):
            mux.pipe_pane("ses", "win", "/tmp/log.txt")

    def test_stop_pipe_pane_raises_not_implemented(self, tmp_path):
        mux = WezTermMultiplexer(runner=lambda argv, env=None: _spawn_result("101"), wezterm_bin="wezterm")
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            mux.create_session("ses", "win", "tid", str(tmp_path))
        with pytest.raises(NotImplementedError, match="Task 7"):
            mux.stop_pipe_pane("ses", "win")
