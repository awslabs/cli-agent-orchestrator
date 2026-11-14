"""Tests for the herdr single-pane browser view."""

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
                {"workspace_id": "w1", "label": "cao-a"},
                {"workspace_id": "w2", "label": "cao-b"},
            ],
            "tabs": [
                {"tab_id": "w1:t1", "workspace_id": "w1", "label": "dev-1"},
                {"tab_id": "w2:t1", "workspace_id": "w2", "label": "dev-1"},
            ],
            "panes": [
                {"pane_id": "w1:p1", "tab_id": "w1:t1"},
                {"pane_id": "w2:p1", "tab_id": "w2:t1"},
            ],
            "layouts": [
                {"tab_id": "w1:t1", "area": {"width": 190, "height": 38}},
                {"tab_id": "w2:t1", "area": {"width": 70, "height": 20}},
            ],
        }
    }

    def test_matches_workspace_and_tab_labels(self):
        with patch.object(view, "herdr_call", return_value=self.SNAPSHOT):
            pane = view.resolve_pane("/sock", "cao-b", "dev-1")
        assert pane.pane_id == "w2:p1"
        assert (pane.area_rows, pane.area_cols) == (20, 70)

    def test_none_when_the_tab_is_gone(self):
        with patch.object(view, "herdr_call", return_value=self.SNAPSHOT):
            assert view.resolve_pane("/sock", "cao-a", "dev-9") is None


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
            kwargs.setdefault("lock_path", str(tmp_path / "resize.lock"))
            kwargs.setdefault("chrome_path", str(tmp_path / "chrome.json"))
        return view._Sizer(client_pid=1, client_fd=99, **kwargs)

    def _run(self, sizer, areas):
        """Drive the sequence to its end, answering each read with the next area."""
        answers = list(areas)
        with (
            patch.object(view, "_set_window_size") as set_size,
            patch.object(view.os, "kill"),
        ):
            while sizer.attempts > 0:
                sizer.next_at = 0.0
                sizer.step(lambda: answers.pop(0) if answers else None)
        return set_size

    def test_resize_adds_herdr_chrome(self, tmp_path):
        sizer = self._sizer(tmp_path)
        sizer.resize(20, 70)
        set_size = self._run(sizer, [(20, 70)])
        set_size.assert_called_once_with(99, 21, 96)

    def test_calibrate_corrects_the_chrome_estimate(self, tmp_path):
        sizer = self._sizer(tmp_path)
        sizer.resize(20, 70)
        # herdr's sidebar was 4 columns wider than the estimate, then it fits.
        set_size = self._run(sizer, [(20, 66), (20, 70)])
        assert set_size.call_args_list[-1].args == (99, 21, 100)
        assert (sizer.chrome_rows, sizer.chrome_cols) == (1, 30)

    def test_the_measurement_is_shared_with_the_next_view(self, tmp_path):
        first = self._sizer(tmp_path)
        first.resize(20, 70)
        self._run(first, [(20, 66), (20, 70)])

        second = self._sizer(tmp_path)
        assert (second.chrome_rows, second.chrome_cols) == (1, 30)
        second.resize(20, 70)
        set_size = self._run(second, [(20, 70)])
        set_size.assert_called_once_with(99, 21, 100)

    def test_a_view_waits_while_another_is_resizing(self, tmp_path):
        holder = self._sizer(tmp_path)
        assert holder._acquire() is True

        waiting = self._sizer(tmp_path)
        waiting.resize(20, 70)
        with (
            patch.object(view, "_set_window_size") as set_size,
            patch.object(view.os, "kill"),
        ):
            waiting.next_at = 0.0
            waiting.step(lambda: pytest.fail("read the layout while another view was resizing"))
        set_size.assert_not_called()
        assert waiting.attempts == view.CALIBRATION_ATTEMPTS  # still to do, not given up

        holder._release()
        set_size = self._run(waiting, [(20, 70)])
        set_size.assert_called_once_with(99, 21, 96)

    def test_the_lock_is_released_when_the_pane_cannot_be_read(self, tmp_path):
        sizer = self._sizer(tmp_path)
        sizer.resize(20, 70)
        self._run(sizer, [None])
        assert sizer.attempts == 0
        assert self._sizer(tmp_path)._acquire() is True

    def test_it_gives_up_after_a_bounded_number_of_tries(self, tmp_path):
        sizer = self._sizer(tmp_path)
        sizer.resize(20, 70)
        set_size = self._run(sizer, [(20, 60), (20, 61), (20, 62), (20, 63), (20, 64)])
        assert len(set_size.call_args_list) <= view.CALIBRATION_ATTEMPTS
        assert not sizer.due()

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


def test_herdr_call_raises_on_an_error_answer(tmp_path):
    with patch.object(view.socket, "socket") as sock_cls:
        sock = sock_cls.return_value
        sock.recv.side_effect = [b'{"id":"web-view","error":{"code":"pane_not_found"}}\n']
        with pytest.raises(view.HerdrSocketError):
            view.herdr_call(str(tmp_path / "s"), "pane.read", {})
