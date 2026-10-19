"""The live probe's allowlist must also reach the isolated server unchanged."""

from test.fixtures.cao_server import _subprocess_env

import pytest


def test_explicit_environment_drops_parent_state(tmp_path, monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "parent-must-not-be-inherited")
    env = {
        "PATH": "/usr/bin",
        "LANG": "en_US.UTF-8",
        "TERM": "xterm",
        "HOME": str(tmp_path),
        "CAO_HOME_DIR": str(tmp_path / "cao"),
        "TMUX_TMPDIR": str(tmp_path / "tmux"),
        "CAO_API_PORT": "54321",
    }
    assert _subprocess_env(tmp_path, 54321, base_env=env) == env


def test_explicit_environment_cannot_merge_extra_overrides(tmp_path):
    with pytest.raises(ValueError, match="cannot be combined"):
        _subprocess_env(tmp_path, 54321, base_env={}, extra={"CAO_TERMINAL_ID": "abcd1234"})


@pytest.mark.parametrize("inside", [False, True])
def test_creator_checks_owned_socket_before_yield(tmp_path, monkeypatch, inside):
    from test.e2e import test_ephemeral_agents as live
    from types import SimpleNamespace
    from unittest.mock import Mock

    handle = tmp_path / "handle"
    home = handle / "home"
    server = SimpleNamespace(home_dir=home, port=54321, url="http://127.0.0.1:54321")
    response = Mock()
    response.json.return_value = {"id": "abcd1234", "session_name": "owned-session"}
    post = Mock(return_value=response)
    delete = Mock(return_value=Mock())
    socket = handle / "tmux" / "default" if inside else tmp_path / "foreign" / "default"
    probe = Mock(return_value=str(socket) + "\n")
    monkeypatch.setattr(live.requests, "post", post)
    monkeypatch.setattr(live.requests, "delete", delete)
    monkeypatch.setattr(live.subprocess, "check_output", probe)
    generator = live.creator.__wrapped__(server, monkeypatch)
    if inside:
        assert next(generator) == "abcd1234"
        generator.close()
        assert delete.call_count == 2
    else:
        with pytest.raises(AssertionError):
            next(generator)
    probe.assert_called_once()
    env = probe.call_args.kwargs["env"]
    assert set(env) <= {
        "PATH",
        "LANG",
        "TERM",
        "HOME",
        "CAO_HOME_DIR",
        "TMUX_TMPDIR",
        "CAO_API_PORT",
    }
    assert env["HOME"] == str(home)
    assert env["TMUX_TMPDIR"] == str(handle / "tmux")
    assert post.call_count == 1
