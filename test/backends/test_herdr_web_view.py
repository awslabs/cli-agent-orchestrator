"""Tests for the herdr single-pane browser view."""

from unittest.mock import patch

import pytest

from cli_agent_orchestrator.backends import herdr_web_view as view


class TestRender:
    def test_paints_each_row_and_erases_the_rest(self):
        out = view.render("\r\nfirst\r\nsecond\r\n", rows=10)
        assert out.startswith("\x1b[1;1Hfirst\x1b[0m\x1b[K")
        assert "\x1b[2;1Hsecond\x1b[0m\x1b[K" in out
        assert out.endswith("\x1b[3;1H\x1b[0m\x1b[J")

    def test_keeps_the_bottom_rows_when_the_browser_is_shorter(self):
        screen = "\r\n" + "\r\n".join(f"line{i}" for i in range(10))
        out = view.render(screen, rows=3)
        assert "line6" not in out
        assert "\x1b[1;1Hline7" in out and "\x1b[3;1Hline9" in out

    def test_drops_trailing_blank_rows(self):
        out = view.render("\r\nprompt\r\n   \r\n\r\n", rows=10)
        assert out.endswith("\x1b[2;1H\x1b[0m\x1b[J")


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
        }
    }

    def test_matches_workspace_and_tab_labels(self):
        with patch.object(view, "herdr_call", return_value=self.SNAPSHOT):
            assert view.resolve_pane("/sock", "cao-b", "dev-1") == "w2:p1"

    def test_none_when_the_tab_is_gone(self):
        with patch.object(view, "herdr_call", return_value=self.SNAPSHOT):
            assert view.resolve_pane("/sock", "cao-a", "dev-9") is None


class TestReadScreen:
    def test_asks_for_the_visible_screen_with_colours(self):
        with patch.object(
            view, "herdr_call", return_value={"read": {"text": "\r\nhi"}}
        ) as call:
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
