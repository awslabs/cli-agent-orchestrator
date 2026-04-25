"""Unit tests for WezTermMultiplexer — all subprocess calls mocked via runner injection."""

from __future__ import annotations

import subprocess
import threading
from collections import deque
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.multiplexers.base import LaunchSpec
from cli_agent_orchestrator.multiplexers.wezterm import WezTermMultiplexer


def _make_result(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[],
        returncode=returncode,
        stdout=stdout,
        stderr="",
    )


def _spawn_result(pane_id: str = "42") -> subprocess.CompletedProcess[str]:
    return _make_result(stdout=f"{pane_id}\n")


class FakeRunner:
    def __init__(self, pane_id: str = "42") -> None:
        self.pane_id = pane_id
        self.get_text_queue: deque[str | RuntimeError] = deque()
        self.calls: list[list[str]] = []
        self._condition = threading.Condition()

    def __call__(self, argv, env=None):
        del env
        call = [str(part) for part in argv]
        with self._condition:
            self.calls.append(call)

        if "spawn" in call:
            return _spawn_result(self.pane_id)
        if "get-text" in call:
            with self._condition:
                if self.get_text_queue:
                    item = self.get_text_queue.popleft()
                    self._condition.notify_all()
                else:
                    item = ""
            if isinstance(item, RuntimeError):
                raise item
            return _make_result(stdout=item)
        if "kill-pane" in call or "send-text" in call:
            return _make_result()
        return _make_result()

    def queue_responses(self, responses: list[str | RuntimeError]) -> None:
        with self._condition:
            self.get_text_queue.extend(responses)
            self._condition.notify_all()

    def wait_for_queue_drain(self, timeout: float = 1.0) -> bool:
        def drained() -> bool:
            return not self.get_text_queue

        with self._condition:
            return self._condition.wait_for(drained, timeout=timeout)

    def pending_get_text(self) -> int:
        with self._condition:
            return len(self.get_text_queue)


@pytest.fixture
def fake_runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def multiplexer(tmp_path, fake_runner: FakeRunner):
    mux = WezTermMultiplexer(
        runner=fake_runner,
        wezterm_bin="wezterm",
        poll_interval=0.001,
        clock_sleep=lambda *_: None,
    )
    with patch.object(
        mux,
        "_resolve_and_validate_working_directory",
        return_value=str(tmp_path),
    ):
        mux.create_session("sess", "win", "tid", str(tmp_path))
    yield mux
    for key in list(mux._pollers):
        mux.stop_pipe_pane(*key)


class TestCreateSession:
    def test_argv_contains_new_window_cwd_and_terminal_id(self, tmp_path):
        calls: list[list[str]] = []

        def runner(argv, env=None):
            del env
            calls.append(list(argv))
            return _spawn_result("17")

        mux = WezTermMultiplexer(runner=runner, wezterm_bin="wezterm")
        with patch.object(
            mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)
        ):
            mux.create_session("ses", "win", "tid-abc", str(tmp_path))

        argv = calls[0]
        assert argv[0] == "wezterm"
        assert "cli" in argv
        assert "spawn" in argv
        assert "--new-window" in argv
        assert "--cwd" in argv
        assert str(tmp_path) in argv
        env_pairs = [
            argv[i + 1]
            for i, token in enumerate(argv)
            if token == "--set-environment" and i + 1 < len(argv)
        ]
        assert "CAO_TERMINAL_ID=tid-abc" in env_pairs

    def test_parses_pane_id_from_stdout(self, tmp_path):
        mux = WezTermMultiplexer(runner=lambda argv, env=None: _spawn_result("99"), wezterm_bin="wezterm")
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            result = mux.create_session("ses", "win", "tid", str(tmp_path))
        assert result == "win"

    def test_pane_id_stored_in_registry(self, tmp_path):
        mux = WezTermMultiplexer(runner=lambda argv, env=None: _spawn_result("77"), wezterm_bin="wezterm")
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            mux.create_session("ses", "win", "tid", str(tmp_path))
        assert mux._sessions["ses"]["win"] == "77"

    def test_launch_spec_argv_appended_after_double_dash(self, tmp_path):
        calls: list[list[str]] = []

        def runner(argv, env=None):
            del env
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
        idx = argv.index("--")
        assert argv[idx + 1 : idx + 3] == ["codex.cmd", "--yolo"]

    def test_launch_spec_env_adds_set_environment(self, tmp_path):
        calls: list[list[str]] = []

        def runner(argv, env=None):
            del env
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
        env_pairs = [
            argv[i + 1]
            for i, token in enumerate(argv)
            if token == "--set-environment" and i + 1 < len(argv)
        ]
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


class TestCreateWindow:
    def test_create_window_stores_pane_in_existing_session(self, tmp_path):
        mux = WezTermMultiplexer(runner=lambda argv, env=None: _spawn_result("55"), wezterm_bin="wezterm")
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            mux.create_session("ses", "win1", "tid1", str(tmp_path))
            mux.create_window("ses", "win2", "tid2", str(tmp_path))
        assert mux._sessions["ses"]["win2"] == "55"

    def test_create_window_uses_new_window_flag(self, tmp_path):
        calls: list[list[str]] = []

        def runner(argv, env=None):
            del env
            calls.append(list(argv))
            return _spawn_result("10")

        mux = WezTermMultiplexer(runner=runner, wezterm_bin="wezterm")
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            mux.create_window("ses", "win", "tid", str(tmp_path))

        assert "--new-window" in calls[0]


class TestPasteText:
    def test_sends_send_text_with_pane_id_and_text(self, tmp_path):
        calls: list[list[str]] = []

        def runner(argv, env=None):
            del env
            calls.append(list(argv))
            return _spawn_result("11")

        mux = WezTermMultiplexer(runner=runner, wezterm_bin="wezterm")
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            mux.create_session("ses", "win", "tid", str(tmp_path))
        calls.clear()

        mux._paste_text("ses", "win", "hello world")

        argv = calls[0]
        assert argv[:3] == ["wezterm", "cli", "send-text"]
        assert argv[argv.index("--pane-id") + 1] == "11"
        assert argv[argv.index("--") + 1] == "hello world"
        assert "--no-paste" not in argv

    def test_raises_when_pane_not_found(self):
        mux = WezTermMultiplexer(runner=lambda argv, env=None: _spawn_result(), wezterm_bin="wezterm")
        with pytest.raises(KeyError, match="not found"):
            mux._paste_text("missing_ses", "missing_win", "text")


class TestSubmitInput:
    def test_submit_once_sends_carriage_return_with_no_paste(self, tmp_path):
        calls: list[list[str]] = []

        def runner(argv, env=None):
            del env
            calls.append(list(argv))
            return _spawn_result("22")

        mux = WezTermMultiplexer(
            runner=runner,
            wezterm_bin="wezterm",
            clock_sleep=lambda *_: None,
        )
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            mux.create_session("ses", "win", "tid", str(tmp_path))
        calls.clear()

        mux._submit_input("ses", "win", enter_count=1)

        argv = calls[0]
        assert argv[:3] == ["wezterm", "cli", "send-text"]
        assert "--no-paste" in argv
        assert argv[argv.index("--") + 1] == "\r"

    def test_submit_once_sleeps_300ms(self, tmp_path):
        sleep_calls: list[float] = []
        mux = WezTermMultiplexer(
            runner=lambda argv, env=None: _spawn_result("22"),
            wezterm_bin="wezterm",
            clock_sleep=lambda duration: sleep_calls.append(duration),
        )
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            mux.create_session("ses", "win", "tid", str(tmp_path))

        mux._submit_input("ses", "win", enter_count=1)

        assert sleep_calls == [pytest.approx(0.3)]

    def test_submit_three_times_produces_three_enter_calls(self, tmp_path):
        calls: list[list[str]] = []

        def runner(argv, env=None):
            del env
            calls.append(list(argv))
            return _spawn_result("33")

        mux = WezTermMultiplexer(
            runner=runner,
            wezterm_bin="wezterm",
            clock_sleep=lambda *_: None,
        )
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            mux.create_session("ses", "win", "tid", str(tmp_path))
        calls.clear()

        mux._submit_input("ses", "win", enter_count=3)

        enter_calls = [argv for argv in calls if "--no-paste" in argv and "\r" in argv]
        assert len(enter_calls) == 3

    def test_submit_three_times_sleeps_300ms_then_500ms_between(self, tmp_path):
        sleep_calls: list[float] = []
        mux = WezTermMultiplexer(
            runner=lambda argv, env=None: _spawn_result("33"),
            wezterm_bin="wezterm",
            clock_sleep=lambda duration: sleep_calls.append(duration),
        )
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            mux.create_session("ses", "win", "tid", str(tmp_path))

        mux._submit_input("ses", "win", enter_count=3)

        assert sleep_calls == [
            pytest.approx(0.3),
            pytest.approx(0.5),
            pytest.approx(0.5),
        ]


class TestSendKeys:
    def test_send_keys_calls_paste_then_submit(self, tmp_path):
        method_calls: list[str] = []
        mux = WezTermMultiplexer(
            runner=lambda argv, env=None: _spawn_result("44"),
            wezterm_bin="wezterm",
            clock_sleep=lambda *_: None,
        )
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            mux.create_session("ses", "win", "tid", str(tmp_path))

        original_paste = mux._paste_text
        original_submit = mux._submit_input

        def record_paste(session_name, window_name, text):
            method_calls.append("paste")
            original_paste(session_name, window_name, text)

        def record_submit(session_name, window_name, enter_count=1):
            method_calls.append("submit")
            original_submit(session_name, window_name, enter_count=enter_count)

        mux._paste_text = record_paste  # type: ignore[method-assign]
        mux._submit_input = record_submit  # type: ignore[method-assign]

        mux.send_keys("ses", "win", "text", enter_count=2)

        assert method_calls == ["paste", "submit"]


class TestSendSpecialKey:
    def test_enter_maps_to_carriage_return_no_paste(self, tmp_path):
        calls: list[list[str]] = []

        def runner(argv, env=None):
            del env
            calls.append(list(argv))
            return _spawn_result("50")

        mux = WezTermMultiplexer(runner=runner, wezterm_bin="wezterm")
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            mux.create_session("ses", "win", "tid", str(tmp_path))
        calls.clear()

        mux.send_special_key("ses", "win", "Enter")

        argv = calls[0]
        assert "--no-paste" in argv
        assert argv[argv.index("--") + 1] == "\r"

    def test_literal_true_sends_raw_bytes_no_paste(self, tmp_path):
        calls: list[list[str]] = []

        def runner(argv, env=None):
            del env
            calls.append(list(argv))
            return _spawn_result("51")

        mux = WezTermMultiplexer(runner=runner, wezterm_bin="wezterm")
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            mux.create_session("ses", "win", "tid", str(tmp_path))
        calls.clear()

        mux.send_special_key("ses", "win", "\x1b[B", literal=True)

        argv = calls[0]
        assert "--no-paste" in argv
        assert argv[argv.index("--") + 1] == "\x1b[B"

    def test_tab_maps_to_tab_character(self, tmp_path):
        calls: list[list[str]] = []

        def runner(argv, env=None):
            del env
            calls.append(list(argv))
            return _spawn_result("52")

        mux = WezTermMultiplexer(runner=runner, wezterm_bin="wezterm")
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            mux.create_session("ses", "win", "tid", str(tmp_path))
        calls.clear()

        mux.send_special_key("ses", "win", "Tab")

        argv = calls[0]
        assert argv[argv.index("--") + 1] == "\t"

    def test_up_arrow_maps_to_vt_sequence(self, tmp_path):
        calls: list[list[str]] = []

        def runner(argv, env=None):
            del env
            calls.append(list(argv))
            return _spawn_result("53")

        mux = WezTermMultiplexer(runner=runner, wezterm_bin="wezterm")
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            mux.create_session("ses", "win", "tid", str(tmp_path))
        calls.clear()

        mux.send_special_key("ses", "win", "Up")

        argv = calls[0]
        assert argv[argv.index("--") + 1] == "\x1b[A"

    def test_send_special_key_unknown_name_raises_actionable_keyerror(self, tmp_path):
        mux = WezTermMultiplexer(runner=lambda argv, env=None: _spawn_result("54"), wezterm_bin="wezterm")
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            mux.create_session("ses", "win", "tid", str(tmp_path))

        with pytest.raises(KeyError, match="Unknown special key"):
            mux.send_special_key("ses", "win", "NotAKey")


class TestGetHistory:
    def test_calls_get_text_without_escapes(self, tmp_path):
        calls: list[list[str]] = []

        def runner(argv, env=None):
            del env
            calls.append(list(argv))
            if "spawn" in argv:
                return _spawn_result("60")
            return _make_result(stdout="output line\n")

        mux = WezTermMultiplexer(runner=runner, wezterm_bin="wezterm")
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            mux.create_session("ses", "win", "tid", str(tmp_path))
        calls.clear()

        result = mux.get_history("ses", "win")

        argv = calls[0]
        assert argv[:3] == ["wezterm", "cli", "get-text"]
        assert "--escapes" not in argv
        assert result == "output line\n"

    def test_tail_lines_returns_last_n_lines(self, tmp_path):
        content = "\n".join(f"line{i}" for i in range(10))

        def runner(argv, env=None):
            del env
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
        with pytest.raises(KeyError, match="not found"):
            mux.get_history("missing_ses", "missing_win")


class TestKillSession:
    def test_removes_session_from_registry_and_kills_panes(self, tmp_path):
        calls: list[list[str]] = []

        def runner(argv, env=None):
            del env
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
        kill_calls = [argv for argv in calls if "kill-pane" in argv]
        assert len(kill_calls) == 1
        assert kill_calls[0][kill_calls[0].index("--pane-id") + 1] == "70"

    def test_returns_false_for_nonexistent_session(self):
        mux = WezTermMultiplexer(runner=lambda argv, env=None: _spawn_result(), wezterm_bin="wezterm")
        assert mux.kill_session("nonexistent") is False


class TestKillWindow:
    def test_removes_window_from_registry(self, tmp_path):
        calls: list[list[str]] = []

        def runner(argv, env=None):
            del env
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
        mux = WezTermMultiplexer(runner=lambda argv, env=None: _spawn_result("81"), wezterm_bin="wezterm")
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            mux.create_session("ses", "win", "tid", str(tmp_path))

        assert mux.kill_window("ses", "no_such_win") is False


class TestSessionExists:
    def test_returns_true_for_registered_session(self, tmp_path):
        mux = WezTermMultiplexer(runner=lambda argv, env=None: _spawn_result("90"), wezterm_bin="wezterm")
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            mux.create_session("ses", "win", "tid", str(tmp_path))
        assert mux.session_exists("ses") is True

    def test_returns_false_for_unknown_session(self):
        mux = WezTermMultiplexer(runner=lambda argv, env=None: _spawn_result(), wezterm_bin="wezterm")
        assert mux.session_exists("nope") is False


class TestListSessions:
    def test_returns_registered_session(self, tmp_path):
        mux = WezTermMultiplexer(runner=lambda argv, env=None: _spawn_result("91"), wezterm_bin="wezterm")
        with patch.object(mux, "_resolve_and_validate_working_directory", return_value=str(tmp_path)):
            mux.create_session("my-ses", "win", "tid", str(tmp_path))

        sessions = mux.list_sessions()
        assert [session["name"] for session in sessions] == ["my-ses"]

    def test_returns_empty_when_no_sessions(self):
        mux = WezTermMultiplexer(runner=lambda argv, env=None: _spawn_result(), wezterm_bin="wezterm")
        assert mux.list_sessions() == []


class TestGetPaneWorkingDirectory:
    def test_returns_none_for_unknown_window(self):
        mux = WezTermMultiplexer(runner=lambda argv, env=None: _spawn_result(), wezterm_bin="wezterm")
        assert mux.get_pane_working_directory("ses", "win") is None


class TestDiffSnapshot:
    def test_diff_snapshot_pure_append(self):
        mux = WezTermMultiplexer(runner=lambda argv, env=None: _spawn_result(), wezterm_bin="wezterm")
        assert mux._diff_snapshot("hello\n", "hello\nworld\n") == "world\n"

    def test_diff_snapshot_line_suffix_overlap(self):
        mux = WezTermMultiplexer(runner=lambda argv, env=None: _spawn_result(), wezterm_bin="wezterm")
        assert mux._diff_snapshot("a\nb\nc\n", "b\nc\nd\n") == "d\n"

    def test_diff_snapshot_redraw_no_overlap(self):
        mux = WezTermMultiplexer(runner=lambda argv, env=None: _spawn_result(), wezterm_bin="wezterm")
        assert mux._diff_snapshot("abc\n", "xyz\n") == "xyz\n"


class TestPipePane:
    def test_pipe_pane_raises_if_pane_not_registered(self, fake_runner: FakeRunner, tmp_path):
        mux = WezTermMultiplexer(
            runner=fake_runner,
            wezterm_bin="wezterm",
            poll_interval=0.001,
            clock_sleep=lambda *_: None,
        )

        with pytest.raises(KeyError, match="pane not found"):
            mux.pipe_pane("missing", "win", str(tmp_path / "pipe.log"))

    def test_pipe_pane_raises_if_already_running(self, multiplexer, tmp_path):
        path = tmp_path / "pipe.log"
        multiplexer.pipe_pane("sess", "win", str(path))

        with pytest.raises(RuntimeError, match="pipe_pane already running for sess:win"):
            multiplexer.pipe_pane("sess", "win", str(path))

    def test_after_one_tick_with_no_change_file_is_empty(self, multiplexer, fake_runner: FakeRunner, tmp_path):
        path = tmp_path / "pipe.log"
        fake_runner.queue_responses([""])

        multiplexer.pipe_pane("sess", "win", str(path))
        assert fake_runner.wait_for_queue_drain(timeout=1.0)
        multiplexer.stop_pipe_pane("sess", "win")

        assert path.read_text(encoding="utf-8") == ""

    def test_after_one_tick_with_text_file_contains_text(self, multiplexer, fake_runner: FakeRunner, tmp_path):
        path = tmp_path / "pipe.log"
        fake_runner.queue_responses(["hello\n"])

        multiplexer.pipe_pane("sess", "win", str(path))
        assert fake_runner.wait_for_queue_drain(timeout=1.0)
        multiplexer.stop_pipe_pane("sess", "win")

        assert path.read_text(encoding="utf-8") == "hello\n"

    def test_pure_append(self, multiplexer, tmp_path, fake_runner: FakeRunner):
        path = tmp_path / "pipe.log"
        fake_runner.queue_responses(["hello\n", "hello\nworld\n"])

        multiplexer.pipe_pane("sess", "win", str(path))
        assert fake_runner.wait_for_queue_drain(timeout=1.0)
        multiplexer.stop_pipe_pane("sess", "win")

        assert path.read_text(encoding="utf-8") == "hello\nworld\n"

    def test_redraw_appends_full_snapshot_when_no_overlap(
        self, multiplexer, tmp_path, fake_runner: FakeRunner
    ):
        path = tmp_path / "pipe.log"
        fake_runner.queue_responses(["abc\n", "xyz\n"])

        multiplexer.pipe_pane("sess", "win", str(path))
        assert fake_runner.wait_for_queue_drain(timeout=1.0)
        multiplexer.stop_pipe_pane("sess", "win")

        assert path.read_text(encoding="utf-8") == "abc\nxyz\n"

    def test_line_suffix_overlap_appends_only_new_lines(
        self, multiplexer, tmp_path, fake_runner: FakeRunner
    ):
        path = tmp_path / "pipe.log"
        fake_runner.queue_responses(["a\nb\nc\n", "b\nc\nd\n"])

        multiplexer.pipe_pane("sess", "win", str(path))
        assert fake_runner.wait_for_queue_drain(timeout=1.0)
        multiplexer.stop_pipe_pane("sess", "win")

        assert path.read_text(encoding="utf-8") == "a\nb\nc\nd\n"

    def test_pane_disappears_mid_poll_exits_cleanly(
        self, multiplexer, tmp_path, fake_runner: FakeRunner
    ):
        path = tmp_path / "pipe.log"
        fake_runner.queue_responses(["hello\n", RuntimeError("pane gone")])

        multiplexer.pipe_pane("sess", "win", str(path))
        assert fake_runner.wait_for_queue_drain(timeout=1.0)

        # After the thread exits due to RuntimeError from _get_pane_text, the
        # _poll_loop finally-block self-cleans the registry entry.
        state = multiplexer._pollers.get(("sess", "win"))
        if state is not None:
            state.thread.join(timeout=1.0)
        # After the thread has cleaned itself up, the registry entry is gone.
        import time as _time
        deadline = _time.monotonic() + 1.0
        while _time.monotonic() < deadline and ("sess", "win") in multiplexer._pollers:
            _time.sleep(0.01)

        assert ("sess", "win") not in multiplexer._pollers
        assert path.read_text(encoding="utf-8") == "hello\n"

    def test_stop_pipe_pane_cancels_thread_and_prevents_further_writes(
        self, multiplexer, tmp_path, fake_runner: FakeRunner
    ):
        path = tmp_path / "pipe.log"
        fake_runner.queue_responses(["hello\n"])

        multiplexer.pipe_pane("sess", "win", str(path))
        assert fake_runner.wait_for_queue_drain(timeout=1.0)
        multiplexer.stop_pipe_pane("sess", "win")

        fake_runner.queue_responses(["hello\nworld\n"])

        assert path.read_text(encoding="utf-8") == "hello\n"
        assert fake_runner.pending_get_text() == 1

    def test_stop_pipe_pane_raises_when_no_poller_exists(self, multiplexer):
        with pytest.raises(RuntimeError, match="pipe_pane not running for sess:win"):
            multiplexer.stop_pipe_pane("sess", "win")

    def test_kill_session_stops_the_poller_automatically(
        self, multiplexer, tmp_path, fake_runner: FakeRunner
    ):
        path = tmp_path / "pipe.log"
        fake_runner.queue_responses(["hello\n"])

        multiplexer.pipe_pane("sess", "win", str(path))
        assert fake_runner.wait_for_queue_drain(timeout=1.0)

        assert multiplexer.kill_session("sess") is True
        assert ("sess", "win") not in multiplexer._pollers
        assert path.read_text(encoding="utf-8") == "hello\n"

    def test_kill_window_stops_the_poller_automatically(
        self, multiplexer, tmp_path, fake_runner: FakeRunner
    ):
        path = tmp_path / "pipe.log"
        fake_runner.queue_responses(["hello\n"])

        multiplexer.pipe_pane("sess", "win", str(path))
        assert fake_runner.wait_for_queue_drain(timeout=1.0)

        assert multiplexer.kill_window("sess", "win") is True
        assert ("sess", "win") not in multiplexer._pollers
        assert path.read_text(encoding="utf-8") == "hello\n"

    def test_stop_pipe_pane_timeout_keeps_zombie_registry_entry(
        self, multiplexer, tmp_path
    ):
        """Zombie poller (join timeout) keeps its registry entry to block double-write.

        When stop_pipe_pane() times out waiting for the thread, the entry must
        remain so that a subsequent pipe_pane() call raises RuntimeError rather
        than starting a second thread writing to the same log file concurrently.

        We inject a synthetic _PollerState with a mock thread that reports
        is_alive()=True after join(), so there are no real threads racing
        against the registry check.
        """
        from unittest.mock import MagicMock as _MagicMock
        from cli_agent_orchestrator.multiplexers.wezterm import _PollerState

        path = tmp_path / "pipe.log"
        path.touch()

        # Build a mock thread that always appears alive (join is a no-op).
        mock_thread = _MagicMock(spec=threading.Thread)
        mock_thread.is_alive.return_value = True  # simulates join timeout
        mock_thread.join.return_value = None       # join returns immediately

        stop_event = threading.Event()
        state = _PollerState(thread=mock_thread, stop_event=stop_event)
        key = ("sess", "win")
        multiplexer._pollers[key] = state

        multiplexer.stop_pipe_pane("sess", "win")

        # Registry entry must still be present (zombie kept in place).
        assert key in multiplexer._pollers

        # A subsequent pipe_pane call must raise, not start a second thread.
        with pytest.raises(RuntimeError, match="pipe_pane already running for sess:win"):
            multiplexer.pipe_pane("sess", "win", str(path))

        # Cleanup: remove the synthetic entry so the fixture teardown doesn't fail.
        del multiplexer._pollers[key]

    def test_poll_loop_self_cleans_on_pane_disappearing(
        self, multiplexer, tmp_path, fake_runner: FakeRunner
    ):
        """When _get_pane_text raises, _poll_loop's finally block removes the registry entry.

        This verifies that a zombie that eventually exits cleans its own entry
        so that a new pipe_pane() call can succeed afterward.
        """
        path = tmp_path / "pipe.log"
        # First response writes content; second raises to simulate pane gone.
        fake_runner.queue_responses(["hello\n", RuntimeError("pane gone")])

        multiplexer.pipe_pane("sess", "win", str(path))
        assert fake_runner.wait_for_queue_drain(timeout=1.0)

        # Wait for the thread to exit and self-clean.
        import time as _time
        deadline = _time.monotonic() + 2.0
        while _time.monotonic() < deadline and ("sess", "win") in multiplexer._pollers:
            _time.sleep(0.01)

        assert ("sess", "win") not in multiplexer._pollers

        # Now pipe_pane can be called again without raising.
        fake_runner.queue_responses(["hello\n"])
        multiplexer.pipe_pane("sess", "win", str(path))
        multiplexer.stop_pipe_pane("sess", "win")
