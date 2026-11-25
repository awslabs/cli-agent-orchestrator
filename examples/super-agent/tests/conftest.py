"""Shared helpers for the super-agent kit tests.

SUPER_AGENT_KIT points the tests at a kit root (default: this kit). The mutation
driver sets it to a mutant copy. Every script runs as a subprocess with a small
allowlisted environment, so nothing from the caller's shell leaks in.
"""
import json
import os
import subprocess
import sys
import socket
from pathlib import Path

import pytest

KIT = Path(os.environ.get("SUPER_AGENT_KIT", Path(__file__).resolve().parent.parent))
BIN = KIT / "bin"
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))  # a few tests import the scripts directly
# The tests run as the operator "your-login" (the fixture identity in their data). Your real
# login lives in workplace.local.json, which a test never reads.
WORKPLACE_FIXTURE = TESTS / "fixtures" / "workplace.local.json"
# mandates.json is your own file and never shipped, so the tests always use the shipped example.
# Scripts that default to <kit>/mandates.json get it passed explicitly by run(), so no gate ever
# needs an environment override of its mandates file.
KIT_MANDATES = KIT / "mandates.example.json"
# Test-only sitecustomize that blocks the system logger, /dev/log and syslog in child Pythons.
JOURNAL_GUARD = TESTS / "fixtures" / "no_journal"


def base_env(tmp_path, **extra):
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "PYTHONDONTWRITEBYTECODE": "1",
        "SUPER_AGENT_STATE_DIR": str(tmp_path / "state"),
        "CAO_WORKPLACE_CONFIG": str(WORKPLACE_FIXTURE),
    }
    guard = os.environ.get("KIT_TEST_NO_JOURNAL")
    if guard:  # set per test by no_real_hook_journal; child Pythons load the guard sitecustomize
        env.update({"KIT_TEST_NO_JOURNAL": guard, "PYTHONPATH": str(JOURNAL_GUARD)})
        if os.environ.get("KIT_TEST_JOURNAL_DECOY"):
            env["KIT_TEST_JOURNAL_DECOY"] = os.environ["KIT_TEST_JOURNAL_DECOY"]
    env.update({k: str(v) for k, v in extra.items()})
    return env


def with_mandates(script, args):
    """Add --mandates KIT_MANDATES where the script would read its <kit>/mandates.json default."""
    args = [str(a) for a in args]
    if "--mandates" in args or any(a.startswith("--mandates=") for a in args):
        return args
    if script in ("check_mandate.py", "automerge_gate.py"):
        return args + ["--mandates", str(KIT_MANDATES)]
    if script == "holds.py" and args[:1] == ["clear"]:
        return args + ["--mandates", str(KIT_MANDATES)]
    if script == "go_ledger.py" and args[:1] == ["add"] and "--repo" not in args:
        return args + ["--mandates", str(KIT_MANDATES)]  # add takes its repo from push_urls
    return args


def run(script, *args, env):
    return subprocess.run(
        [sys.executable, "-I", str(BIN / script), *with_mandates(script, args)],
        capture_output=True, text=True, env=env, timeout=60,
    )


CONDUCTOR_TERMINAL = "c0ffee01"


def ledger(cmd, *args, env, role="conductor", terminal=CONDUCTOR_TERMINAL):
    """go_ledger.py as the conductor runs it: --writer-role conductor, its CAO_TERMINAL_ID."""
    env = dict(env)
    if terminal is not None:
        env["CAO_TERMINAL_ID"] = terminal
    return run("go_ledger.py", cmd, *(["--writer-role", role] if role is not None else []), *args, env=env)


def write_json(path, data):
    path.write_text(json.dumps(data))
    return path


class JournalGuard:
    """What the journal guard blocked during one test, in this process and in child Pythons."""

    def __init__(self, record):
        self.record = record
        self.blocked = []

    def child_attempts(self):
        try:
            return self.record.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []

    def clear(self):
        self.blocked.clear()
        self.record.unlink(missing_ok=True)


def _load_journal_guard():
    """The one copy of the guard logic, shared with child Pythons (see its docstring)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("kit_journal_guard",
                                                  JOURNAL_GUARD / "sitecustomize.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


JOURNAL_GUARD_LOGIC = _load_journal_guard()


@pytest.fixture(autouse=True)
def no_real_hook_journal(monkeypatch, tmp_path_factory):
    """Keep every test away from the operator's journal (the audit sink of the kit hooks).

    In this process the system logger, /dev/log and syslog raise OSError, which the hooks treat
    as a sink failure, so nothing reaches the journal. base_env carries the same guard into child
    Pythons through tests/fixtures/no_journal/sitecustomize.py. Any attempt fails the test at
    teardown, where no hook error handling can swallow it.
    """
    guard = JournalGuard(tmp_path_factory.mktemp("journal-guard") / "attempts.log")
    monkeypatch.setenv("KIT_TEST_NO_JOURNAL", str(guard.record))

    def blocked(what):
        guard.blocked.append(what)
        raise OSError(f"kit test journal guard blocked {what}")

    for owner, name, value in JOURNAL_GUARD_LOGIC.patches(blocked):
        monkeypatch.setattr(owner, name, value)
    yield guard
    attempts = guard.blocked + guard.child_attempts()
    if attempts:
        pytest.fail(f"test tried to write the real journal: {attempts}")


@pytest.fixture
def kit():
    return KIT


@pytest.fixture(autouse=True)
def writer_state(tmp_path, monkeypatch, request):
    """Original writer tests explicitly start on the local writer host.

    Fence tests own their missing/wrong state setup and must never be repaired here.
    """
    if request.node.name == 'test_first_id_is_k1_and_reads_nothing_into_being':
        return
    if request.module.__name__ in ('test_writer_lease', 'test_linux_resources', 'test_top_mem', 'test_docs'):
        return
    state = tmp_path / 'state'
    state.mkdir(exist_ok=True)
    (state / 'WRITER').write_text(f'host={socket.gethostname()} since=2026-11-13T00:00:00Z\n')
    monkeypatch.setenv('SUPER_AGENT_STATE_DIR', str(state))
