from test import conftest as guard
from types import SimpleNamespace

import pytest


@pytest.fixture
def session_home(monkeypatch, tmp_path):
    from cli_agent_orchestrator import constants

    home = tmp_path / "startup-home"
    home.mkdir()
    monkeypatch.setattr(constants, "CAO_HOME_DIR", home)
    messages = []
    reporter = SimpleNamespace(write_sep=lambda *args: messages.append(args))
    config = SimpleNamespace(pluginmanager=SimpleNamespace(get_plugin=lambda name: reporter))
    session = SimpleNamespace(stash={}, config=config, exitstatus=pytest.ExitCode.OK)
    return session, home, messages


@pytest.mark.parametrize("preexisting", [False, True])
def test_session_guard_accepts_unchanged_home_files(session_home, preexisting):
    session, home, messages = session_home
    if preexisting:
        (home / "decisions.halt").touch()
        (home / "decisions-egress.lock").touch()
    guard.pytest_sessionstart(session)
    guard.pytest_sessionfinish(session, pytest.ExitCode.OK)
    assert session.exitstatus == pytest.ExitCode.OK
    assert not messages


@pytest.mark.parametrize("mutation", ["create", "replace", "size", "mtime", "delete"])
def test_session_guard_fails_changed_home_file(session_home, mutation):
    import os

    session, home, messages = session_home
    path = home / "decisions-egress.lock"
    if mutation != "create":
        path.touch()
    guard.pytest_sessionstart(session)
    if mutation == "create":
        path.touch()
    elif mutation == "replace":
        replacement = home / "replacement"
        replacement.touch()
        replacement.replace(path)
    elif mutation == "size":
        path.write_bytes(b"changed")
    elif mutation == "mtime":
        stat = path.lstat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    else:
        path.unlink()
    guard.pytest_sessionfinish(session, pytest.ExitCode.OK)
    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED
    assert "decisions-egress.lock" in messages[0][1]
