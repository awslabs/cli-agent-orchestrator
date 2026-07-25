"""Tests for ``CAO_STATE_ROOT``, the one knob that relocates CAO state.

Every test here runs a fresh interpreter. The variable is read exactly once,
while ``constants.py`` is being imported, and the SQLAlchemy engine is bound
from the result during that same import — so no assertion made inside an
already-running interpreter can observe the decision honestly. A subprocess
can.

**No test in this file ever imports CAO with the knob absent.** Every child
either runs under a scratch state root or refuses to start, so none of them
can create or write anything under the operator's live tree. A suite that
proved isolation by exercising the un-isolated path would be writing to the
very directories it claims to protect — the first draft of this file did
exactly that, and the audit hook below is what makes the claim checkable
rather than asserted.

The default location is proven instead by calling ``_resolve_cao_home_dir``
directly, after the module has already been imported under a scratch root,
with ``Path.home`` pointed at a synthetic directory. The unset branch neither
canonicalizes nor creates, so that call touches no disk at all.

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

PROBE = '''
"""Import CAO in a fresh interpreter and report where its state landed."""
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

STATE_ROOT_ENV = "CAO_STATE_ROOT"

DEFAULT_ROOT = os.path.realpath(str(Path.home() / ".aws" / "cli-agent-orchestrator"))
_requested = os.environ.get(STATE_ROOT_ENV) or ""
STATE_ROOT = os.path.realpath(_requested) if _requested.strip() else None

# Events that create, open, or rearrange a file. ``os.stat`` is not among
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
root_hits = []


def _under(path, base):
    return path == base or path.startswith(base + os.sep)


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
        real = os.path.realpath(arg)
        if _under(real, DEFAULT_ROOT) or _under(arg, DEFAULT_ROOT):
            default_hits.append(event + " " + arg)
        elif STATE_ROOT is not None and _under(real, STATE_ROOT):
            root_hits.append(event + " " + (real[len(STATE_ROOT):] or os.sep))


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

# What the resolver answers with no state root set. The module is already
# imported -- under the scratch root above -- so this runs the unset branch
# and nothing else: no import, no engine, and (because the unset branch does
# not canonicalize or create) no filesystem contact whatsoever. The home it
# is shown is synthetic, so the real one is never even named.
synthetic_home = sys.argv[1] if len(sys.argv) > 1 else None
if synthetic_home:
    restore = os.environ.pop(STATE_ROOT_ENV, None)
    try:
        with patch.object(Path, "home", return_value=Path(synthetic_home)):
            report["unset_root"] = str(constants._resolve_cao_home_dir())
    finally:
        if restore is not None:
            os.environ[STATE_ROOT_ENV] = restore

report["default_hits"] = default_hits
report["root_hits"] = root_hits
sys.stdout.write(json.dumps(report))
'''


def _run_probe(probe_path, state_root, synthetic_home=None):
    """Run the probe in a fresh interpreter under ``state_root``.

    ``state_root`` is mandatory, including for the cases that expect a
    refusal. A child launched without one would import CAO against the real
    home and create directories there, which is the outcome this whole file
    exists to rule out.

    ``HOME`` is inherited untouched, deliberately: a knob that only worked
    when the process was also lied to about its home directory would not be
    the knob this is meant to be.
    """
    assert state_root is not None, "a probe without a state root would write to live state"
    env = dict(os.environ)
    env[STATE_ROOT_ENV] = str(state_root)
    env["PYTHONPATH"] = os.pathsep.join(part for part in (SRC_DIR, env.get("PYTHONPATH")) if part)
    argv = [sys.executable, str(probe_path)]
    if synthetic_home is not None:
        argv.append(str(synthetic_home))
    completed = subprocess.run(
        argv,
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
    return Path(os.path.realpath(tmp_path)) / "state"


@pytest.fixture
def aliased_home(tmp_path):
    """``(aliased, real)`` — one directory named two ways, neither in use.

    Stands in for a home directory so the unset branch can be checked against
    a spelling that ``realpath`` would visibly change. Nothing is created
    inside either one; the test asserts that.
    """
    base = Path(os.path.realpath(tmp_path))
    real = base / "home-real"
    real.mkdir()
    alias = base / "home-alias"
    alias.symlink_to(real, target_is_directory=True)
    # Asserted, not assumed: were these one string, the canonicalization
    # assertions below would pass while proving nothing.
    assert str(alias) != str(real)
    assert os.path.realpath(alias) == str(real)
    return alias, real


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

    def test_the_derived_names_are_the_historical_ones(self, probe, scratch):
        """Relocated, not renamed — the tree's shape below the root is fixed."""
        completed, report = _run_probe(probe, scratch)
        assert completed.returncode == 0, completed.stderr[-2000:]

        relative = {
            name: str(Path(value).relative_to(scratch))
            for name, value in report["paths"].items()
            if name != "root"
        }
        assert relative == {
            "db_dir": "db",
            "database_file": os.path.join("db", "cli-agent-orchestrator.db"),
            "log_dir": "logs",
            "terminal_log_dir": os.path.join("logs", "terminal"),
            "fifo_dir": "fifos",
            "companion_dir": "companion",
            "env_file": ".env",
        }

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

    def test_the_recorder_sees_those_writes_at_the_scratch_root(self, probe, scratch):
        """Control for the assertion above, which is otherwise unfalsifiable.

        A hook that recorded nothing at all would make an empty
        ``default_hits`` look like proof of isolation. The same hook, watching
        the same events, records the import-time writes at the scratch root —
        so the empty list means they went there instead of somewhere else,
        not that the recorder was asleep.
        """
        completed, report = _run_probe(probe, scratch)
        assert completed.returncode == 0, completed.stderr[-2000:]

        recorded = set(report["root_hits"])
        expected = {
            f"os.mkdir {os.sep}{os.path.join('logs', 'terminal')}",
            f"os.mkdir {os.sep}fifos",
            f"os.mkdir {os.sep}companion",
            f"os.mkdir {os.sep}db",
            f"os.chmod {os.sep}db",
        }
        assert expected <= recorded, sorted(expected - recorded)

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
    """Unset is the shipped default and must stay byte-for-byte what it was.

    Checked by asking the resolver, never by importing CAO without a state
    root — that import is precisely the thing that would write to the live
    tree.
    """

    def test_the_unset_branch_returns_the_historical_path(self, probe, scratch, aliased_home):
        alias, _ = aliased_home
        completed, report = _run_probe(probe, scratch, synthetic_home=alias)
        assert completed.returncode == 0, completed.stderr[-2000:]

        assert report["unset_root"] == str(alias / ".aws" / "cli-agent-orchestrator")

    def test_the_unset_branch_does_not_canonicalize(self, probe, scratch, aliased_home):
        """The aliased spelling survives.

        Resolving the default would silently rewrite the path of every
        installation whose home directory is reached through a symlink.
        """
        alias, real = aliased_home
        completed, report = _run_probe(probe, scratch, synthetic_home=alias)
        assert completed.returncode == 0, completed.stderr[-2000:]

        assert report["unset_root"] != str(real / ".aws" / "cli-agent-orchestrator")
        assert report["unset_root"].startswith(f"{alias}{os.sep}")

    def test_the_unset_branch_creates_nothing(self, probe, scratch, aliased_home):
        """Naming a default is not the same as building it.

        Only the ``CAO_STATE_ROOT`` branch may create a directory; the unset
        branch hands back a path and leaves the disk alone, which is what
        makes it safe to ask this question at all.
        """
        alias, real = aliased_home
        completed, report = _run_probe(probe, scratch, synthetic_home=alias)
        assert completed.returncode == 0, completed.stderr[-2000:]

        assert not (real / ".aws").exists()
        assert not (alias / ".aws").exists()
        assert sorted(p.name for p in real.iterdir()) == []

    def test_asking_the_question_touches_no_live_state(self, probe, scratch, aliased_home):
        alias, _ = aliased_home
        completed, report = _run_probe(probe, scratch, synthetic_home=alias)
        assert completed.returncode == 0, completed.stderr[-2000:]

        assert report["default_hits"] == []


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
