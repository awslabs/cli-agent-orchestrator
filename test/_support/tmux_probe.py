"""Deliberately careless tmux tests, run only by a nested pytest.

Not collected by the suite (the name does not match ``test_*.py``).
``test/test_tmux_isolation.py`` runs some of these in a child pytest and checks
the result from outside. Each does nothing unless that test sets
``CAO_TEST_TMUX_PROBE`` to its name, so running this file by hand is a skip.
"""

import json
import os
import subprocess

import pytest

#: A pane command that does not depend on the runner's login shell.
PANE_COMMAND = "exec sleep 600"


def _selected(name: str) -> bool:
    return os.environ.get("CAO_TEST_TMUX_PROBE") == name


def _require(name: str) -> None:
    if not _selected(name):
        pytest.skip("only test/test_tmux_isolation.py runs this probe")


def _report(data: dict) -> None:
    with open(os.environ["CAO_TEST_TMUX_PROBE_REPORT"], "w", encoding="utf-8") as report:
        json.dump(data, report)


def _tmux_env() -> dict:
    return {name: os.environ.get(name) for name in ("TMUX", "TMUX_PANE", "TMUX_TMPDIR")}


def test_bare_kill_server():
    _require("kill-server")
    subprocess.run(["tmux", "kill-server"], capture_output=True, timeout=10, check=False)


def test_leave_a_server_running():
    """Start a server on the default socket, report where it is, and never stop it."""
    _require("leave-server")
    subprocess.run(
        ["tmux", "-f", "/dev/null", "new-session", "-d", "-s", "leftover", PANE_COMMAND],
        env=dict(os.environ, SHELL="/bin/sh"),
        capture_output=True,
        timeout=10,
        check=True,
    )
    socket, pid = subprocess.run(
        ["tmux", "display-message", "-p", "#{socket_path} #{pid}"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    ).stdout.split()
    _report({"socket": socket, "pid": pid, "tmpdir": os.environ.get("TMUX_TMPDIR")})


@pytest.fixture(scope="module")
def leak_report():
    """Record the tmux variables at module setup, in the next test, and at teardown.

    Module scope, so its teardown runs after the last test's function-scoped
    teardown: that is what a session fixture's teardown would see.
    """
    seen = {"at_setup": _tmux_env()}
    yield seen
    seen["after_teardown"] = _tmux_env()
    if _selected("leak-env"):
        _report(seen)


def _leak() -> None:
    # Deliberately not via monkeypatch: the harness must undo it.
    os.environ["TMUX"] = "/nonexistent-leaked-tmux,1,0"
    os.environ["TMUX_PANE"] = "%99"
    os.environ["TMUX_TMPDIR"] = "/nonexistent-leaked-tmpdir"


def test_leak_tmux_env(leak_report):
    _require("leak-env")
    _leak()


def test_after_a_leak(leak_report):
    """Record what the previous leak left, then leak again for the module teardown."""
    _require("leak-env")
    leak_report["next_test"] = _tmux_env()
    _leak()
