"""Tests for the herdr single-pane browser view."""

import json
import os
import select
import signal
import socket
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.backends import herdr_web_view as view

ESC = "\x1b"


class TestRender:
    def test_paints_each_row_and_erases_the_rest(self):
        out = view.render("\r\nfirst\r\nsecond\r\n", rows=10)
        assert out.startswith(f"{ESC}[1;1Hfirst{ESC}[0m{ESC}[K")
        assert f"{ESC}[2;1Hsecond{ESC}[0m{ESC}[K" in out
        assert out.endswith(f"{ESC}[3;1H{ESC}[0m{ESC}[J")

    def test_keeps_the_bottom_rows_when_the_browser_is_shorter(self):
        screen = "\r\n" + "\r\n".join(f"line{i}" for i in range(10))
        out = view.render(screen, rows=3)
        assert "line6" not in out
        assert f"{ESC}[1;1Hline7" in out and f"{ESC}[3;1Hline9" in out

    def test_drops_trailing_blank_rows(self):
        out = view.render("\r\nprompt\r\n   \r\n\r\n", rows=10)
        assert out.endswith(f"{ESC}[2;1H{ESC}[0m{ESC}[J")


class TestResolvePane:
    SNAPSHOT = {
        "snapshot": {
            "workspaces": [
                {"workspace_id": "w1", "label": "cao-a", "focused": False},
                {"workspace_id": "w2", "label": "cao-b", "focused": True},
            ],
            "tabs": [
                {"tab_id": "w1:t1", "workspace_id": "w1", "label": "dev-1", "focused": True},
                {"tab_id": "w2:t1", "workspace_id": "w2", "label": "dev-1", "focused": True},
                {"tab_id": "w2:t2", "workspace_id": "w2", "label": "dev-2", "focused": False},
            ],
            "panes": [
                {"pane_id": "w1:p1", "tab_id": "w1:t1"},
                {"pane_id": "w2:p1", "tab_id": "w2:t1"},
                {"pane_id": "w2:p2", "tab_id": "w2:t2"},
            ],
            "layouts": [
                {"tab_id": "w1:t1", "area": {"width": 190, "height": 38}},
                {"tab_id": "w2:t1", "area": {"width": 70, "height": 20}},
                {"tab_id": "w2:t2", "area": {"width": 70, "height": 20}},
            ],
        }
    }

    def test_matches_workspace_and_tab_labels(self):
        with patch.object(view, "herdr_call", return_value=self.SNAPSHOT):
            pane = view.resolve_pane("/sock", "cao-b", "dev-1")
        assert pane.pane_id == "w2:p1" and pane.tab_id == "w2:t1"
        assert (pane.area_rows, pane.area_cols) == (20, 70)

    def test_focused_needs_both_the_tab_and_its_workspace(self):
        with patch.object(view, "herdr_call", return_value=self.SNAPSHOT):
            assert view.resolve_pane("/sock", "cao-b", "dev-1").focused is True
            assert view.resolve_pane("/sock", "cao-b", "dev-2").focused is False
            # Focused within its workspace, but the workspace is not.
            assert view.resolve_pane("/sock", "cao-a", "dev-1").focused is False

    def test_none_when_the_tab_is_gone(self):
        with patch.object(view, "herdr_call", return_value=self.SNAPSHOT):
            assert view.resolve_pane("/sock", "cao-a", "dev-9") is None


def test_focus_tab_asks_herdr_to_focus_it():
    with patch.object(view, "herdr_call", return_value={}) as call:
        view.focus_tab("/sock", "w2:t1")
    assert call.call_args.args[1:] == ("tab.focus", {"tab_id": "w2:t1"})


class TestSplitMouse:
    def test_wheel_reports_are_taken_out(self):
        data = f"a{ESC}[<64;10;5M{ESC}[<64;10;5Mb{ESC}[<65;1;1M".encode()
        keys, wheel, rest = view.split_mouse(data)
        assert keys == b"ab" and rest == b""
        assert wheel == [(64, 10, 5), (64, 10, 5), (65, 1, 1)]

    def test_clicks_are_dropped_not_forwarded(self):
        keys, wheel, rest = view.split_mouse(f"{ESC}[<0;3;4M{ESC}[<0;3;4mx".encode())
        assert (keys, wheel, rest) == (b"x", [], b"")

    def test_a_report_cut_off_is_carried_over(self):
        keys, wheel, rest = view.split_mouse(f"hi{ESC}[<64;1".encode())
        assert (keys, wheel, rest) == (b"hi", [], f"{ESC}[<64;1".encode())
        keys, wheel, rest = view.split_mouse(rest + b";2M")
        assert (keys, wheel, rest) == (b"", [(64, 1, 2)], b"")

    def test_escape_and_arrow_keys_pass_through(self):
        keys, wheel, _ = view.split_mouse(f"{ESC}[A{ESC}".encode())
        assert (keys, wheel) == (f"{ESC}[A{ESC}".encode(), [])


class TestSizer:
    def _sizer(self, tmp_path=None, **kwargs):
        if tmp_path is not None:
            kwargs.setdefault("lock_path", str(tmp_path / "focus.lock"))
            kwargs.setdefault("chrome_path", str(tmp_path / "chrome.json"))
        return view._Sizer(client_pid=1, client_fd=99, **kwargs)

    def _run(self, sizer, states):
        """Drive the sequence to its end, answering each check with the next state."""
        answers = list(states)
        with (
            patch.object(view, "_set_window_size") as set_size,
            patch.object(view, "_write_all") as write_all,
            patch.object(view.os, "kill"),
        ):
            while sizer.attempts > 0:
                sizer.next_at = 0.0
                sizer.step(lambda: answers.pop(0) if answers else None)
        self.write_all = write_all
        return set_size

    def test_resize_adds_herdr_chrome(self, tmp_path):
        sizer = self._sizer(tmp_path)
        sizer.resize(20, 70)
        set_size = self._run(sizer, [(20, 70, True)])
        set_size.assert_called_once_with(99, 21, 96)

    def test_it_focuses_its_tab_then_claims_it_then_checks(self, tmp_path):
        order = []
        sizer = self._sizer(tmp_path, focus_own_tab=lambda: order.append("focus"))
        sizer.resize(20, 70)
        answers = [(20, 70, True)]
        with (
            patch.object(view, "_set_window_size", side_effect=lambda *a: order.append("size")),
            patch.object(view, "_write_all", side_effect=lambda fd, b: order.append(b)),
            patch.object(view.os, "kill"),
        ):
            while sizer.attempts > 0:
                sizer.next_at = 0.0
                sizer.step(lambda: order.append("check") or answers.pop(0))
        assert order == ["focus", "size", view.FOCUS_REPORT, "check"]

    def test_the_first_step_only_waits_out_input_in_flight(self, tmp_path):
        focus = []
        sizer = self._sizer(tmp_path, focus_own_tab=lambda: focus.append(1))
        sizer.resize(20, 70)
        sizer.step(lambda: pytest.fail("checked before resizing"))
        assert focus == []
        assert sizer.next_at > time.monotonic() + view.INPUT_GRACE_S - 0.1

    def test_a_matching_area_does_not_count_while_another_tab_is_focused(self, tmp_path):
        """herdr reports the foreground client's area for every tab (review P2)."""
        focus = []
        sizer = self._sizer(tmp_path, focus_own_tab=lambda: focus.append(1))
        sizer.resize(20, 70)
        set_size = self._run(sizer, [(20, 70, False), (20, 70, True)])
        assert len(focus) == 2  # took its tab back before trusting the layout
        assert len(set_size.call_args_list) == 2
        assert sizer.attempts == 0 and sizer._lock_fd is None

    def test_calibrate_corrects_the_chrome_estimate(self, tmp_path):
        sizer = self._sizer(tmp_path)
        sizer.resize(20, 70)
        # herdr's sidebar was 4 columns wider than the estimate, then it fits.
        set_size = self._run(sizer, [(20, 66, True), (20, 70, True)])
        assert set_size.call_args_list[-1].args == (99, 21, 100)
        assert (sizer.chrome_rows, sizer.chrome_cols) == (1, 30)

    def test_every_resize_claims_the_tab_again(self, tmp_path):
        sizer = self._sizer(tmp_path)
        sizer.resize(20, 70)
        self._run(sizer, [(20, 66, True), (20, 70, True)])
        claims = [c for c in self.write_all.call_args_list if c.args == (99, view.FOCUS_REPORT)]
        assert len(claims) == 2

    def test_the_measurement_is_shared_with_the_next_view(self, tmp_path):
        first = self._sizer(tmp_path)
        first.resize(20, 70)
        self._run(first, [(20, 66, True), (20, 70, True)])

        second = self._sizer(tmp_path)
        assert (second.chrome_rows, second.chrome_cols) == (1, 30)
        second.resize(20, 70)
        set_size = self._run(second, [(20, 70, True)])
        set_size.assert_called_once_with(99, 21, 100)

    def test_a_view_waits_while_the_focus_lock_is_held(self, tmp_path):
        holder = self._sizer(tmp_path)
        assert holder._acquire() is True

        focus = []
        waiting = self._sizer(tmp_path, focus_own_tab=lambda: focus.append(1))
        waiting.resize(20, 70)
        with (
            patch.object(view, "_set_window_size") as set_size,
            patch.object(view.os, "kill"),
        ):
            for _ in range(3):
                waiting.next_at = 0.0
                waiting.step(lambda: pytest.fail("read the layout while the lock was held"))
        set_size.assert_not_called()
        assert focus == []  # never moved herdr's focus under the holder
        assert waiting.attempts == view.CALIBRATION_ATTEMPTS  # still to do, not given up

        holder._release()
        set_size = self._run(waiting, [(20, 70, True)])
        set_size.assert_called_once_with(99, 21, 96)

    def test_the_lock_is_released_when_the_pane_cannot_be_read(self, tmp_path):
        sizer = self._sizer(tmp_path)
        sizer.resize(20, 70)
        self._run(sizer, [None])
        assert sizer.attempts == 0
        assert self._sizer(tmp_path)._acquire() is True

    def test_a_failed_focus_lets_input_through_and_tries_again(self, tmp_path):
        calls = []

        def _focus():
            calls.append(1)
            if len(calls) == 1:
                raise view.HerdrSocketError("tab.focus: gone")

        sizer = self._sizer(tmp_path, focus_own_tab=_focus)
        sizer.resize(20, 70)
        sizer.step(lambda: None)  # the input grace
        sizer.next_at = 0.0
        with pytest.raises(view.HerdrSocketError):
            sizer.step(lambda: None)
        other = self._sizer(tmp_path)
        assert other._acquire() is True  # the lock was let go
        other._release()
        set_size = self._run(sizer, [(20, 70, True)])
        set_size.assert_called_once_with(99, 21, 96)
        assert len(calls) == 2

    def test_it_gives_up_after_a_bounded_number_of_tries(self, tmp_path):
        sizer = self._sizer(tmp_path)
        sizer.resize(20, 70)
        states = [(20, 60, True), (20, 61, False), (20, 62, True), (20, 63, False)] * 3
        set_size = self._run(sizer, states)
        assert len(set_size.call_args_list) <= view.CALIBRATION_ATTEMPTS
        assert not sizer.due() and sizer._lock_fd is None

    def test_wheel_report_lands_on_the_pane_past_herdr_chrome(self):
        assert self._sizer().wheel_report(65, 3, 4) == f"{ESC}[<65;29;5M".encode()


class TestReadScreen:
    def test_asks_for_the_visible_screen_with_colours(self):
        with patch.object(view, "herdr_call", return_value={"read": {"text": "\r\nhi"}}) as call:
            assert view.read_screen("/sock", "w1:p1") == "\r\nhi"
        method, params = call.call_args.args[1:]
        assert method == "pane.read"
        assert params == {
            "pane_id": "w1:p1",
            "source": "visible",
            "format": "ansi",
            "strip_ansi": False,
        }


class TestSessionStateDir:
    def test_creates_a_private_directory(self, tmp_path):
        with patch.object(view.tempfile, "gettempdir", return_value=str(tmp_path)):
            path = view.session_state_dir("cao")
        assert path is not None
        assert oct(os.stat(path).st_mode & 0o777) == oct(0o700)

    def test_refuses_a_directory_others_can_write(self, tmp_path):
        root = tmp_path / f"cao-herdr-web-view-{os.getuid()}"
        root.mkdir()
        root.chmod(0o777)
        with patch.object(view.tempfile, "gettempdir", return_value=str(tmp_path)):
            assert view.session_state_dir("cao") is None

    def test_refuses_a_symlink(self, tmp_path):
        target = tmp_path / "elsewhere"
        target.mkdir(mode=0o700)
        (tmp_path / f"cao-herdr-web-view-{os.getuid()}").symlink_to(target)
        with patch.object(view.tempfile, "gettempdir", return_value=str(tmp_path)):
            assert view.session_state_dir("cao") is None


def test_herdr_call_raises_on_an_error_answer(tmp_path):
    with patch.object(view.socket, "socket") as sock_cls:
        sock = sock_cls.return_value
        sock.recv.side_effect = [b'{"id":"web-view","error":{"code":"pane_not_found"}}\n']
        with pytest.raises(view.HerdrSocketError):
            view.herdr_call(str(tmp_path / "s"), "pane.read", {})


class _FakeHerdr:
    """herdr's socket API for one tab, enough for the view's main loop."""

    def __init__(self, path, rows, cols):
        self.path = path
        self.rows, self.cols = rows, cols
        self.focused = False
        self.tab_open = True
        self.methods = []
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(path)
        self._sock.listen(16)
        self._sock.settimeout(0.2)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=5)
        self._sock.close()

    def _answer(self, method, params):
        self.methods.append((method, params))
        if method == "tab.focus":
            self.focused = True
            return {"tab": {"tab_id": params["tab_id"]}}
        if method == "pane.read":
            return {"read": {"text": "\r\nhello from the agent\r\n"}}
        tabs, panes = [], []
        if self.tab_open:
            tabs = [
                {"tab_id": "w1:t1", "workspace_id": "w1", "label": "dev-1", "focused": self.focused}
            ]
            panes = [{"pane_id": "w1:p1", "tab_id": "w1:t1"}]
        return {
            "snapshot": {
                "workspaces": [{"workspace_id": "w1", "label": "cao-a", "focused": True}],
                "tabs": tabs,
                "panes": panes,
                "layouts": [{"tab_id": "w1:t1", "area": {"width": self.cols, "height": self.rows}}],
            }
        }

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            with conn:
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                request = json.loads(buf)
                result = self._answer(request["method"], request["params"])
                conn.sendall((json.dumps({"id": request["id"], "result": result}) + "\n").encode())


class TestRun:
    """The main loop against a fake herdr socket and a fake ``herdr`` client."""

    ROWS, COLS = 10, 40

    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        client_input = tmp_path / "client-input"
        fake = bin_dir / "herdr"
        # Raw mode, so the focus report reaches the file without a newline.
        fake.write_text(f'#!/bin/sh\nstty raw -echo\nexec cat > "{client_input}"\n')
        fake.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setattr(view.tempfile, "gettempdir", lambda: str(tmp_path))
        for name, value in [
            ("INPUT_GRACE_S", 0.05),
            ("FOCUS_SETTLE_S", 0.05),
            ("RESIZE_SETTLE_S", 0.1),
            ("RESOLVE_INTERVAL_S", 0.3),
        ]:
            monkeypatch.setattr(view, name, value)
        master, slave = os.openpty()
        view._set_window_size(slave, self.ROWS, self.COLS)
        previous = signal.getsignal(signal.SIGWINCH)
        out = bytearray()
        stop = threading.Event()

        def _read():
            while not stop.is_set():
                if select.select([master], [], [], 0.05)[0]:
                    try:
                        out.extend(os.read(master, 65536))
                    except OSError:
                        return

        reader = threading.Thread(target=_read, daemon=True)
        reader.start()
        yield SimpleNamespace(
            tmp_path=tmp_path, master=master, slave=slave, out=out, client_input=client_input
        )
        stop.set()
        reader.join(timeout=5)
        signal.signal(signal.SIGWINCH, previous)
        os.close(master)
        os.close(slave)

    @staticmethod
    def _on_the_pty(env, monkeypatch):
        """Run the view on the test PTY. Patched in the test body: pytest's output
        capture puts its own sys.stdout back once the fixtures are set up."""
        tty_io = SimpleNamespace(fileno=lambda: env.slave)
        monkeypatch.setattr(view.sys, "stdin", tty_io)
        monkeypatch.setattr(view.sys, "stdout", tty_io)

    def _until(self, condition, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(0.02)
        return False

    def test_paints_claims_its_tab_and_exits_when_the_tab_closes(self, env, monkeypatch):
        self._on_the_pty(env, monkeypatch)
        with _FakeHerdr(str(env.tmp_path / "h.sock"), self.ROWS, self.COLS) as herdr:
            result = {}

            def _close_tab_once_sized():
                if self._until(
                    lambda: herdr.focused
                    and env.client_input.exists()
                    and view.FOCUS_REPORT in env.client_input.read_bytes()
                ):
                    time.sleep(0.3)  # the check that ends the resize
                herdr.tab_open = False

            closer = threading.Thread(target=_close_tab_once_sized, daemon=True)
            closer.start()
            result["code"] = view.run("cao", herdr.path, "cao-a", "dev-1")
            closer.join(timeout=5)

        assert result["code"] == 0
        assert self._until(lambda: b"The agent's herdr tab closed." in env.out)
        assert b"hello from the agent" in env.out
        assert ("tab.focus", {"tab_id": "w1:t1"}) in herdr.methods
        assert view.FOCUS_REPORT in env.client_input.read_bytes()
        # The resize finished and let the focus lock go.
        lock = view.focus_lock_path("cao")
        fd = view.try_lock(lock)
        assert fd is not None
        view.unlock(fd)

    def test_keys_reach_the_hidden_client(self, env, monkeypatch):
        self._on_the_pty(env, monkeypatch)
        with _FakeHerdr(str(env.tmp_path / "h.sock"), self.ROWS, self.COLS) as herdr:

            def _type_then_close():
                self._until(lambda: b"hello from the agent" in env.out)
                os.write(env.master, b"typed\r")
                self._until(
                    lambda: env.client_input.exists()
                    and b"typed\r" in env.client_input.read_bytes()
                )
                herdr.tab_open = False

            typist = threading.Thread(target=_type_then_close, daemon=True)
            typist.start()
            assert view.run("cao", herdr.path, "cao-a", "dev-1") == 0
            typist.join(timeout=5)

        assert b"typed\r" in env.client_input.read_bytes()

    def test_an_unreachable_socket_is_one_line_not_a_traceback(self, env, monkeypatch):
        self._on_the_pty(env, monkeypatch)
        assert view.run("cao", str(env.tmp_path / "missing.sock"), "cao-a", "dev-1") == 1
        assert self._until(lambda: b"Cannot reach herdr session 'cao'" in env.out)
        assert b"Traceback" not in env.out
