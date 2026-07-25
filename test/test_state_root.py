"""Tests for ``CAO_STATE_ROOT``, the one knob that relocates CAO state.

Every test here runs a fresh interpreter. The variable is read exactly once,
while ``constants.py`` is being imported, and the SQLAlchemy engine is bound
from the result during that same import — so no assertion made inside an
already-running interpreter can observe the decision honestly. A subprocess
can.

The probe records, through an audit hook installed before CAO is imported,
every path the child touches under the *default* state root. That is the
claim worth proving: an isolated root is only isolated if the live tree is
left alone, and "I set an env var" is not evidence of that.

Nothing here sets or overrides ``HOME``. The point of the knob is that
isolating CAO's state no longer requires lying to the process about who is
running it.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import cli_agent_orchestrator

SRC_DIR = str(Path(cli_agent_orchestrator.__file__).resolve().parent.parent)

STATE_ROOT_ENV = "CAO_STATE_ROOT"

# The historical location, spelled the way the shipped code spells it.
DEFAULT_ROOT = Path.home() / ".aws" / "cli-agent-orchestrator"

PROBE = '''
"""Import CAO in a fresh interpreter and report where its state landed."""
import json
import os
import sys
from pathlib import Path

DEFAULT_ROOT = str(Path.home() / ".aws" / "cli-agent-orchestrator")
DEFAULT_ROOT_REAL = os.path.realpath(DEFAULT_ROOT)

# Events that create, read, or rearrange a file. ``os.stat`` is not among
# CPython's audit events, so a bare existence check is invisible here; every
# event that actually opens or mutates something is not.
WATCHED = {
    "open",
    "os.mkdir",
    "os.rmdir",
    "os.remove",
    "os.rename",
    "os.replace",
    "os.symlink",
    "os.link",
    "os.chmod",
    "os.listdir",
    "os.scandir",
    "os.truncate",
    "shutil.copyfile",
    "shutil.move",
    "shutil.rmtree",
}

default_hits = []


def _record(event, args):
    if event not in WATCHED:
        return
    # Only the leading arguments are paths; ``open`` also passes a mode
    # string and a flags int, which are not.
    for arg in args[:2]:
        if isinstance(arg, bytes):
            arg = arg.decode("utf-8", "replace")
        if not isinstance(arg, str) or not (os.path.isabs(arg) or os.sep in arg):
            continue
        if arg.startswith(DEFAULT_ROOT) or os.path.realpath(arg).startswith(DEFAULT_ROOT_REAL):
            default_hits.append(event + " " + arg)


sys.addaudithook(_record)

report = {}
try:
    from cli_agent_orchestrator import constants
    from cli_agent_orchestrator.clients import database
except BaseException as exc:  # noqa: BLE001 - the refusal is the measurement
    report["import_error"] = "{}: {}".format(type(exc).__name__, exc)
    report["default_hits"] = default_hits
    sys.stdout.write(json.dumps(report))
    sys.exit(1)

paths = {
    "root": constants.CAO_HOME_DIR,
    "db_dir": constants.DB_DIR,
    "database_file": constants.DATABASE_FILE,
    "log_dir": constants.LOG_DIR,
    "terminal_log_dir": constants.TERMINAL_LOG_DIR,
    "fifo_dir": constants.FIFO_DIR,
    "companion_dir": constants.COMPANION_DIR,
    "env_file": constants.CAO_ENV_FILE,
}
report["paths"] = {name: str(value) for name, value in paths.items()}
report["existing_dirs"] = sorted(name for name, value in paths.items() if Path(value).is_dir())
report["database_url"] = constants.DATABASE_URL
report["engine_url"] = str(database.engine.url)
report["default_hits"] = default_hits
sys.stdout.write(json.dumps(report))
'''


def _run_probe(probe_path, state_root):
    """Run the probe in a fresh interpreter; ``state_root=None`` means unset.

    ``HOME`` is inherited untouched, deliberately: a knob that only works
    when the process is also lied to about its home directory would not be
    the knob this is meant to be.
    """
    env = dict(os.environ)
    env.pop(STATE_ROOT_ENV, None)
    if state_root is not None:
        env[STATE_ROOT_ENV] = str(state_root)
    env["PYTHONPATH"] = os.pathsep.join(part for part in (SRC_DIR, env.get("PYTHONPATH")) if part)
    completed = subprocess.run(
        [sys.executable, str(probe_path)],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
        check=False,
    )
    assert completed.stdout, f"probe wrote nothing: {completed.stderr[-2000:]}"
    return completed, json.loads(completed.stdout)


@pytest.fixture
def probe(tmp_path):
    path = tmp_path / "state_root_probe.py"
    path.write_text(PROBE, encoding="utf-8")
    return path


@pytest.fixture
def scratch(tmp_path):
    """A canonical scratch directory to point the knob at.

    ``realpath`` first: on macOS ``tmp_path`` already lives under a symlinked
    ``/var/folders``, so an un-resolved expectation would pass there for the
    wrong reason and fail on Linux.
    """
    base = Path(os.path.realpath(tmp_path)) / "state"
    return base


@pytest.fixture(scope="module")
def default_run(tmp_path_factory):
    """One unset run, shared: it is both the compatibility case and the control."""
    path = tmp_path_factory.mktemp("default-probe") / "state_root_probe.py"
    path.write_text(PROBE, encoding="utf-8")
    return _run_probe(path, None)


class TestStateRootBindsEveryDerivedPath:
    """A state root moves the whole tree, not merely the constant."""

    def test_every_derived_path_lands_beneath_it(self, probe, scratch):
        completed, report = _run_probe(probe, scratch)
        assert completed.returncode == 0, completed.stderr[-2000:]

        assert report["paths"]["root"] == str(scratch)
        for name, value in report["paths"].items():
            assert value.startswith(f"{scratch}{os.sep}") or value == str(
                scratch
            ), f"{name} escaped the state root: {value}"

    def test_the_import_time_engine_follows_it(self, probe, scratch):
        """The engine is the reason this is an env var and not an argument."""
        completed, report = _run_probe(probe, scratch)
        assert completed.returncode == 0, completed.stderr[-2000:]

        expected = f"sqlite:///{scratch / 'db' / 'cli-agent-orchestrator.db'}"
        assert report["database_url"] == expected
        assert report["engine_url"] == expected

    def test_the_directories_are_really_created_there(self, probe, scratch):
        """Not just named there — importing CAO builds the tree on disk."""
        completed, report = _run_probe(probe, scratch)
        assert completed.returncode == 0, completed.stderr[-2000:]

        assert set(report["existing_dirs"]) >= {
            "root",
            "db_dir",
            "log_dir",
            "terminal_log_dir",
            "fifo_dir",
            "companion_dir",
        }
        assert (scratch / "db").is_dir()
        assert (scratch / "fifos").is_dir()

    def test_the_default_home_tree_is_never_touched(self, probe, scratch):
        """The whole point. An isolated root that still writes live state is not one."""
        completed, report = _run_probe(probe, scratch)
        assert completed.returncode == 0, completed.stderr[-2000:]

        assert report["default_hits"] == []

    def test_the_recorder_would_have_noticed(self, default_run):
        """Control for the assertion above, which is otherwise unfalsifiable.

        An audit hook that recorded nothing at all would make the empty list
        above look like proof of isolation. The unset run touches the default
        tree by definition, so a non-empty list here is what makes the empty
        one mean something.
        """
        _, report = default_run
        assert report["default_hits"], "the recorder saw no default-root access at all"

    def test_two_spellings_of_one_root_are_one_root(self, probe, tmp_path):
        """A symlinked directory in the path does not make it a second root.

        Built here rather than borrowed from the host: naming a platform's own
        alias makes the property unprovable anywhere that alias is absent.
        """
        base = Path(os.path.realpath(tmp_path))
        real = base / "real"
        real.mkdir()
        alias = base / "alias"
        alias.symlink_to(real, target_is_directory=True)
        canonical = real / "state"
        aliased = alias / "state"
        # Asserted, not assumed: were these one string, the test would pass
        # while proving nothing about canonicalization.
        assert str(aliased) != str(canonical)

        completed, report = _run_probe(probe, aliased)
        assert completed.returncode == 0, completed.stderr[-2000:]
        assert report["paths"]["root"] == str(canonical)


class TestNoStateRootChangesNothing:
    """Unset is the shipped default and must stay byte-for-byte what it was."""

    def test_the_root_keeps_its_historical_spelling(self, default_run):
        completed, report = default_run
        assert completed.returncode == 0, completed.stderr[-2000:]
        assert report["paths"]["root"] == str(DEFAULT_ROOT)

    def test_the_derived_paths_keep_their_historical_spellings(self, default_run):
        _, report = default_run
        assert report["paths"]["db_dir"] == str(DEFAULT_ROOT / "db")
        assert report["paths"]["log_dir"] == str(DEFAULT_ROOT / "logs")
        assert report["paths"]["terminal_log_dir"] == str(DEFAULT_ROOT / "logs" / "terminal")
        assert report["paths"]["fifo_dir"] == str(DEFAULT_ROOT / "fifos")
        assert report["paths"]["companion_dir"] == str(DEFAULT_ROOT / "companion")
        assert report["paths"]["env_file"] == str(DEFAULT_ROOT / ".env")

    def test_the_engine_keeps_its_historical_url(self, default_run):
        _, report = default_run
        expected = f"sqlite:///{DEFAULT_ROOT / 'db' / 'cli-agent-orchestrator.db'}"
        assert report["database_url"] == expected
        assert report["engine_url"] == expected

    def test_the_default_is_left_unresolved(self, default_run):
        """Canonicalization applies to a value we were given, never to the default.

        Resolving the default would silently rewrite the path of every
        installation whose home directory is reached through a symlink.
        """
        _, report = default_run
        assert report["paths"]["root"] == str(Path.home() / ".aws" / "cli-agent-orchestrator")


class TestAnUnusableStateRootRefusesToStart:
    """Refusing is the safe answer; falling back to live state is not."""

    def _reject(self, probe, value):
        completed, report = _run_probe(probe, value)
        assert completed.returncode != 0, f"accepted {value!r}: {completed.stdout[:500]}"
        assert "StateRootError" in report["import_error"], report["import_error"]
        assert STATE_ROOT_ENV in report["import_error"], report["import_error"]
        # Refused *before* deciding anything, so the live tree stays untouched.
        assert report["default_hits"] == []
        assert "paths" not in report
        return report

    def test_an_empty_value_is_refused(self, probe):
        report = self._reject(probe, "")
        assert "empty" in report["import_error"]

    def test_a_whitespace_only_value_is_refused(self, probe):
        self._reject(probe, "   ")

    def test_a_relative_path_is_refused(self, probe):
        report = self._reject(probe, "cao-state")
        assert "absolute" in report["import_error"]

    def test_a_dot_relative_path_is_refused(self, probe):
        self._reject(probe, "./cao-state")

    def test_a_path_that_is_a_file_is_refused(self, probe, tmp_path):
        occupied = Path(os.path.realpath(tmp_path)) / "not-a-directory"
        occupied.write_text("", encoding="utf-8")
        self._reject(probe, occupied)

    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        reason="root ignores the permission bits this case depends on",
    )
    def test_a_path_under_an_unwritable_parent_is_refused(self, probe, tmp_path):
        parent = Path(os.path.realpath(tmp_path)) / "sealed"
        parent.mkdir(mode=0o500)
        try:
            self._reject(probe, parent / "state")
        finally:
            # Restored so pytest can clean the temp tree up.
            parent.chmod(0o700)

    def test_the_refusal_reaches_a_plain_import(self, tmp_path):
        """No probe, no audit hook: importing CAO simply fails.

        The harness above catches the exception in order to report it, which
        could hide a refusal that something downstream swallows. This is the
        same check with nothing in the way.
        """
        env = dict(os.environ)
        env[STATE_ROOT_ENV] = "relative-is-not-allowed"
        env["PYTHONPATH"] = os.pathsep.join(
            part for part in (SRC_DIR, env.get("PYTHONPATH")) if part
        )
        completed = subprocess.run(
            [sys.executable, "-c", "import cli_agent_orchestrator.constants"],
            capture_output=True,
            text=True,
            env=env,
            timeout=180,
            check=False,
        )
        assert completed.returncode != 0
        assert "StateRootError" in completed.stderr
        assert STATE_ROOT_ENV in completed.stderr
