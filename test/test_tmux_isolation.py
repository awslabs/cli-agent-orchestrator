"""The suite cannot reach a tmux server it did not start, and leaves none behind.

See "tmux isolation" in ``test/conftest.py``. Each "outside" server here is a
throwaway one on an explicit socket in its own /tmp dir, so these tests are safe
to run on a tree without the isolation: there they fail, and the only server a
bare ``kill-server`` can reach is one of these.

A failed assertion here never compares against ``os.environ`` directly, because
pytest would then print the whole environment.

Only the tests that start tmux servers need tmux (``needs_tmux``). They are also
``must_run("unit")``: the Unit Tests (3.12) job enforces that gate, so a runner
image without tmux fails that job instead of skipping them silently. The tests
that only read the environment run everywhere.
"""

import errno
import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from test import conftest

import pytest

pytestmark = pytest.mark.skipif(
    "path" not in conftest._ISOLATED_HOME,
    reason="this session keeps the real HOME and tmux environment on purpose",
)


def needs_tmux(test):
    """Skip without tmux, and make that skip a failure where the "unit" gate is enforced."""
    test = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")(test)
    return pytest.mark.must_run("unit")(test)


REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = "test/_support/tmux_probe.py"
#: A pane command that does not depend on the runner's login shell.
PANE_COMMAND = "exec sleep 600"


def _suite_tmux_dir():
    return conftest._ISOLATED_TMUX.get("tmpdir")


def _clean_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    env["SHELL"] = "/bin/sh"
    return env


def _tmux(socket: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["tmux", "-S", socket, *args],
        env=_clean_env(),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def _kill(socket) -> None:
    """Stop the server on ``socket`` if there is one; never raises."""
    if not socket:
        return
    try:
        _tmux(socket, "kill-server")
    except Exception:
        pass


def _still_answers(socket: str) -> bool:
    """True if the server on ``socket`` is up after giving a kill time to land.

    ``kill-server`` returns before the server has exited, so one immediate check
    could see a dying server as alive.
    """
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if _tmux(socket, "has-session", "-t", "outside").returncode != 0:
            return False
        time.sleep(0.1)
    return True


def _process_alive(pid: int) -> bool:
    """True if ``pid`` is still our running process after giving a kill up to 2s to land.

    ``ESRCH``: no such process. ``EPERM``: a process exists but belongs to another
    user, so the pid was reused and ours is gone. Both mean "not ours, not alive".
    Any other error is unexpected and raised. A sandbox that returns ``EPERM`` for
    our own pid would read as gone, but the ``has-session`` assert before this
    still catches a server that answers.
    """
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError as error:
            if error.errno in (errno.ESRCH, errno.EPERM):
                return False
            raise
        time.sleep(0.1)
    return True


@pytest.fixture
def outside_servers():
    """Two outside servers: one on a named socket, one on a default socket.

    ``named`` is what ``TMUX`` points at when pytest starts in a tmux pane.
    ``tmpdir`` holds a ``default`` server, which is what a bare command reaches
    once ``TMUX`` is gone but ``TMUX_TMPDIR`` is still the inherited one.
    """
    named_dir = tempfile.mkdtemp(prefix="cao-out-", dir="/tmp")
    default_dir = tempfile.mkdtemp(prefix="cao-out-", dir="/tmp")
    socket_dir = os.path.join(default_dir, f"tmux-{os.getuid()}")
    os.mkdir(socket_dir, 0o700)
    named = os.path.join(named_dir, "outside")
    default = os.path.join(socket_dir, "default")
    try:
        for socket in (named, default):
            started = _tmux(
                socket, "-f", "/dev/null", "new-session", "-d", "-s", "outside", PANE_COMMAND
            )
            assert started.returncode == 0, started.stderr
        pid = _tmux(named, "display-message", "-p", "#{pid}").stdout.strip()
        yield {"named": named, "pid": pid, "tmpdir": default_dir, "default": default}
    finally:
        for socket in (named, default):
            _kill(socket)
        shutil.rmtree(named_dir, ignore_errors=True)
        shutil.rmtree(default_dir, ignore_errors=True)


def _child_env(probe: str) -> dict:
    """The current environment minus pytest's, tmux's and the outer gates' variables.

    ``CAO_MUST_RUN`` names the assurance gates of the outer session (CI sets it);
    the child runs only a probe, so an inherited gate would fail it for an
    unrelated reason.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("PYTEST_", "COV_CORE_", "TMUX"))
        and k not in ("_CAO_TEST_HOME_ISOLATION", "CAO_MUST_RUN")
    }
    env["CAO_TEST_TMUX_PROBE"] = probe
    return env


def _run_probe(env: dict, *test_names: str) -> None:
    child = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *(f"{PROBE}::{name}" for name in test_names),
            "-q",
            "-p",
            "no:cacheprovider",
            "-o",
            "addopts=",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    passed = f"{len(test_names)} passed"
    if child.returncode != 0 or passed not in child.stdout:
        # pytrace=False: a traceback would print this function's ``env`` argument.
        pytest.fail(
            f"probe run did not report {passed}:\n{child.stdout}\n{child.stderr}", pytrace=False
        )


def test_suite_environment_names_only_the_private_tmux_dir():
    tmpdir = _suite_tmux_dir()
    assert tmpdir, "test/conftest.py did not isolate tmux"
    tmux, pane, tmux_tmpdir = (os.environ.get(n) for n in ("TMUX", "TMUX_PANE", "TMUX_TMPDIR"))
    assert tmux is None
    assert pane is None
    assert tmux_tmpdir == tmpdir
    # tmux-<uid>/<name> must still fit the ~104-byte macOS socket path limit.
    assert len(os.path.join(tmpdir, f"tmux-{os.getuid()}", "x" * 32)) < 100


@needs_tmux
def test_bare_kill_server_from_the_suite_cannot_reach_an_outside_server(outside_servers):
    tmpdir = _suite_tmux_dir()
    tmux, tmux_tmpdir = os.environ.get("TMUX"), os.environ.get("TMUX_TMPDIR")
    # Refuse to run a bare kill-server unless it can only reach the private dir.
    if not tmpdir or tmux is not None or tmux_tmpdir != tmpdir:
        pytest.fail("the suite environment is not isolated; not running a bare kill-server")
    subprocess.run(["tmux", "kill-server"], capture_output=True, timeout=10, check=False)
    assert _still_answers(outside_servers["named"])
    assert _still_answers(outside_servers["default"])


@needs_tmux
@pytest.mark.parametrize("started_in_tmux", [True, False], ids=["inside-tmux", "default-socket"])
def test_a_suite_started_with_an_outside_tmux_env_cannot_kill_it(outside_servers, started_in_tmux):
    """A child pytest inherits an environment naming an outside server.

    Its probe test runs a bare ``tmux kill-server``. With ``TMUX`` set, that
    reaches the named server; without it, the default server under the inherited
    ``TMUX_TMPDIR``. Both must survive.
    """
    env = _child_env("kill-server")
    env["TMUX_TMPDIR"] = outside_servers["tmpdir"]
    if started_in_tmux:
        env["TMUX"] = f"{outside_servers['named']},{outside_servers['pid']},0"
        env["TMUX_PANE"] = "%0"
    _run_probe(env, "test_bare_kill_server")
    assert _still_answers(outside_servers["named"])
    assert _still_answers(outside_servers["default"])


@needs_tmux
def test_a_session_leaves_no_tmux_server_or_dir_behind(tmp_path):
    """A child pytest starts a server and never stops it; the session end must.

    The child gets its own throwaway ``TMUX_TMPDIR``, so without the harness the
    leftover server lives there and this test stops it by explicit socket.
    """
    fallback = tempfile.mkdtemp(prefix="cao-out-", dir="/tmp")
    report_path = tmp_path / "report.json"
    env = _child_env("leave-server")
    env["TMUX_TMPDIR"] = fallback
    env["CAO_TEST_TMUX_PROBE_REPORT"] = str(report_path)
    report = {}
    try:
        _run_probe(env, "test_leave_a_server_running")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert _tmux(report["socket"], "has-session").returncode != 0, "a tmux server answers"
        assert not _process_alive(int(report["pid"])), "the tmux server process is still running"
        assert not os.path.exists(report["tmpdir"]), "the private TMUX_TMPDIR was left"
    finally:
        _kill(report.get("socket"))
        _kill(os.path.join(fallback, f"tmux-{os.getuid()}", "default"))
        shutil.rmtree(fallback, ignore_errors=True)


def test_a_leaked_tmux_env_is_gone_by_the_next_test_and_after_teardown(tmp_path):
    """A child pytest sets ``TMUX`` and friends without monkeypatch, twice.

    The second test must not see the first one's values, and a module fixture's
    teardown, which runs after the last test's teardown, must not see the second
    one's. A child keeps both tests in one process, which ``pytest -n`` would not.
    """
    fallback = tempfile.mkdtemp(prefix="cao-out-", dir="/tmp")
    report_path = tmp_path / "report.json"
    env = _child_env("leak-env")
    env["TMUX_TMPDIR"] = fallback
    env["CAO_TEST_TMUX_PROBE_REPORT"] = str(report_path)
    try:
        _run_probe(env, "test_leak_tmux_env", "test_after_a_leak")
        report = json.loads(report_path.read_text(encoding="utf-8"))
    finally:
        shutil.rmtree(fallback, ignore_errors=True)
    clean = report["at_setup"]
    assert clean["TMUX"] is None and clean["TMUX_PANE"] is None
    assert clean["TMUX_TMPDIR"] != fallback, "the child did not isolate tmux"
    assert report["next_test"] == clean
    assert report["after_teardown"] == clean


@pytest.mark.real_home
def test_a_real_home_marker_does_not_opt_a_test_out_of_tmux_isolation():
    """The per-test ``real_home`` marker restores HOME only; tmux stays isolated."""
    tmpdir = _suite_tmux_dir()
    assert tmpdir, "test/conftest.py did not isolate tmux"
    tmux, pane, tmux_tmpdir = (os.environ.get(n) for n in ("TMUX", "TMUX_PANE", "TMUX_TMPDIR"))
    assert tmux is None
    assert pane is None
    assert tmux_tmpdir == tmpdir


def test_a_session_that_keeps_the_real_home_leaves_the_tmux_env_alone(tmp_path):
    """``CAO_TEST_REAL_HOME=1`` opts the whole session out of tmux isolation too.

    A child pytest runs with that flag and with ``TMUX``, ``TMUX_PANE`` and
    ``TMUX_TMPDIR`` set. Its probe reports that ``_ISOLATED_TMUX`` is empty and
    the three values are exactly as given. The values name no server, and the
    probe starts no tmux. The child's "real" HOME and CAO home are throwaway dirs.
    """
    scratch = tmp_path / "real-home-session"
    (scratch / "home").mkdir(parents=True)
    report_path = tmp_path / "report.json"
    given = {
        "TMUX": "/nonexistent-outer-socket,1,0",
        "TMUX_PANE": "%7",
        "TMUX_TMPDIR": str(scratch / "outer-tmux"),
    }
    env = _child_env("report-env")
    env.update(given)
    env.update(
        CAO_TEST_REAL_HOME="1",
        HOME=str(scratch / "home"),
        CAO_HOME_DIR=str(scratch / "cao-home"),
        CAO_TEST_TMUX_PROBE_REPORT=str(report_path),
    )
    _run_probe(env, "test_report_tmux_env")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["isolated_tmux"] == {}
    assert report["env"] == given


@pytest.mark.parametrize(
    "error, alive",
    [
        (ProcessLookupError(errno.ESRCH, "no such process"), False),
        (PermissionError(errno.EPERM, "not permitted"), False),
        (None, True),
    ],
    ids=["ESRCH", "EPERM", "running"],
)
def test_process_alive_reads_errno(monkeypatch, error, alive):
    def fake_kill(pid, sig):
        if error is not None:
            raise error

    monkeypatch.setattr(os, "kill", fake_kill)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    clock = itertools.chain([0.0, 0.0], itertools.repeat(3.0))
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))
    assert _process_alive(12345) is alive


def test_process_alive_raises_an_unexpected_error(monkeypatch):
    def fake_kill(pid, sig):
        raise OSError(errno.EINVAL, "invalid signal")

    monkeypatch.setattr(os, "kill", fake_kill)
    with pytest.raises(OSError) as raised:
        _process_alive(12345)
    assert raised.value.errno == errno.EINVAL
