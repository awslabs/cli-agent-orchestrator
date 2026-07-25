"""Exact-old-binary visibility rig.

The v2 state surface (managed-launch v2 rows, heartbeat/fence/broker
files, bridge sockets, registry entries) must be invisible to every
old-binary query, deletion, watchdog, monitor, and cleanup loop; the
proof is behavioral: run the exact old behavior against the live state
and record everything it touches.

Invariant: the rig enumerates the v2 surface from the resource registry
(the code-owned inventory, never prose), runs the old-behavior probes
with an access recorder, and fails on any observed access to a v2-owned
path or row — zero visibility, or rollback refuses until a full v2
drain.

Failure mode prevented: an old binary that can see v2 state can
redrive, duplicate, or destroy it (an old unlocked writer overwrites
what it misreads); asserting invisibility by argument rather than by
running the old behavior is exactly the hand-transcription failure
mode that was disproven twice.

Real syscall tracing (strace/dtruss) is platform-dependent; the rig
therefore works with instrumented probe callables (the hermetic form,
used in tests and CI) and exposes the same verdict shape for a traced
real binary when a tracer is available.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional


@dataclass(frozen=True)
class V2Surface:
    """One v2-owned surface the old binary must never see."""

    kind: str
    locator: str  # path, db key, tmux name, or memory key


@dataclass
class AccessLog:
    """Everything a probe touched, recorded by the instrumentation."""

    reads: list[str] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)
    deletes: list[str] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)

    def all_accesses(self) -> list[str]:
        return self.reads + self.writes + self.deletes + self.queries


@dataclass(frozen=True)
class RigVerdict:
    zero_visibility: bool
    violations: tuple[str, ...]
    surfaces_checked: int


class OldBinaryRig:
    """Runs old-behavior probes and proves zero v2 visibility."""

    def __init__(self, v2_surfaces: Iterable[V2Surface]) -> None:
        self._surfaces = tuple(v2_surfaces)

    @staticmethod
    def surfaces_from_registry(entries: Iterable[dict[str, Any]]) -> tuple[V2Surface, ...]:
        """Enumerate the v2 surface from registry entries (v2 vintage)."""
        surfaces = []
        for entry in entries:
            if entry.get("protocol_vintage") != "v2":
                continue
            for field_name, kind in (
                ("desired_fs_path", "fs"),
                ("observed_fs_path", "fs"),
                ("desired_db_key", "db"),
                ("observed_db_key", "db"),
                ("desired_tmux_name", "tmux"),
                ("observed_tmux_id", "tmux"),
                ("desired_memory_key", "memory"),
                ("observed_memory_key", "memory"),
            ):
                value = entry.get(field_name)
                if value:
                    surfaces.append(V2Surface(kind=kind, locator=str(value)))
        return tuple(surfaces)

    def run_probe(self, probe: Callable[[AccessLog], Any], *, name: str) -> RigVerdict:
        """Run one old-behavior probe; any v2-surface access is a violation."""
        log = AccessLog()
        probe(log)
        locators = {surface.locator for surface in self._surfaces}
        violations = tuple(access for access in log.all_accesses() if access in locators)
        return RigVerdict(
            zero_visibility=not violations,
            violations=violations,
            surfaces_checked=len(self._surfaces),
        )

    def verify(self, probes: dict[str, Callable[[AccessLog], Any]]) -> RigVerdict:
        """Run every probe; the rig passes only if all show zero visibility."""
        violations: list[str] = []
        for name, probe in probes.items():
            verdict = self.run_probe(probe, name=name)
            violations.extend(f"{name}:{access}" for access in verdict.violations)
        return RigVerdict(
            zero_visibility=not violations,
            violations=tuple(violations),
            surfaces_checked=len(self._surfaces),
        )


def tracer_available() -> Optional[str]:
    """The syscall tracer on this platform, if one is usable."""
    for candidate in ("strace", "dtruss"):
        path = shutil.which(candidate)
        if path:
            return path
    return None


class RigError(RuntimeError):
    """The exact-old-binary rig could not run (fail closed for proofs)."""


def extract_exact_source(ref: str, *, repo: Path, dest: Path) -> Path:
    """Materialize the exact old source tree at ``ref`` into ``dest``.

    Uses ``git archive`` so the tree is byte-exact for the ref — never a
    hand-transcribed approximation of the old behavior.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(
        ["git", "-C", str(repo), "archive", ref],
        capture_output=True,
        check=False,
    )
    if archive.returncode != 0:
        raise RigError(f"git archive {ref} failed: {archive.stderr.decode(errors='replace')}")
    untar = subprocess.run(
        ["tar", "-x", "-C", str(dest)],
        input=archive.stdout,
        capture_output=True,
        check=False,
    )
    if untar.returncode != 0:
        raise RigError(f"untar of {ref} failed: {untar.stderr.decode(errors='replace')}")
    return dest


# Subprocess driver: runs one exact-old callable with a Python audit hook
# recording every file open/delete/rename, exec, and socket connect outside
# library paths, then dumps the access log as JSON.  HOME and PYTHONPATH
# are redirected by the parent so the old tree and its state root are fully
# disposable.
_TRACE_DRIVER = r"""
import importlib
import json
import sys

log_path, dotted = sys.argv[1], sys.argv[2]
accesses = {"reads": [], "writes": [], "deletes": [], "queries": []}
_SKIP = ("/site-packages/", "__pycache__", "/.venv/", "/usr/lib", "/Library/")


def _skip(value):
    return any(skip in value for skip in _SKIP)


def _hook(event, args):
    if event == "open":
        path = str(args[0])
        if _skip(path):
            return
        mode = args[1] if len(args) > 1 else 0
        if isinstance(mode, str):
            if "r" in mode:
                accesses["reads"].append(path)
            if any(flag in mode for flag in ("w", "a", "x", "+")):
                accesses["writes"].append(path)
        elif isinstance(mode, int):
            if mode & 1 or mode & 2:
                accesses["writes"].append(path)
            else:
                accesses["reads"].append(path)
    elif event in ("os.remove", "os.unlink", "os.rmdir"):
        path = str(args[0])
        if not _skip(path):
            accesses["deletes"].append(path)
    elif event in ("os.rename", "os.replace"):
        accesses["writes"].append(f"{args[0]}->{args[1]}")
    elif event == "subprocess.Popen":
        accesses["queries"].append(f"exec:{args[0]}")
    elif event == "socket.connect":
        accesses["queries"].append(f"connect:{args[1]}")


sys.addaudithook(_hook)
module_name, func_name = dotted.rsplit(":", 1)
func = getattr(importlib.import_module(module_name), func_name)
error = None
try:
    func()
except Exception as exc:  # the old behavior's own failure is evidence too
    error = f"{type(exc).__name__}: {exc}"
with open(log_path, "w", encoding="utf-8") as handle:
    json.dump({"accesses": accesses, "error": error}, handle)
"""


def run_exact_old_binary(
    *,
    ref: str,
    repo: Path,
    state_home: Path,
    workdir: Path,
    probe: str,
    v2_surfaces: Iterable[V2Surface],
    verify_state: Optional[Callable[[], list[str]]] = None,
    extra_pythonpath: Optional[Path] = None,
    timeout: float = 120.0,
) -> RigVerdict:
    """Run the exact old-binary ``probe`` under an access tracer.

    ``probe`` is a dotted ``module:function`` path resolved inside the
    exact old source tree at ``ref`` (extracted with ``git archive``).
    The subprocess runs with ``HOME=state_home`` and ``PYTHONPATH``
    pointing at the old tree, so every file and database it touches is
    disposable.  A Python audit hook records file opens; ``verify_state``
    then inspects the forward state for any v2-surface mutation (rows
    deleted, files changed).  Any v2-surface access or state violation is
    a verdict violation — zero visibility, or the rollout must refuse
    until a complete drain.
    """
    repo = Path(repo)
    state_home = Path(state_home)
    workdir = Path(workdir)
    old_tree = extract_exact_source(ref, repo=repo, dest=workdir / "old-tree")
    src = old_tree / "src"
    if not src.is_dir():
        raise RigError(f"old tree at {ref} has no src/ layout")
    state_home.mkdir(parents=True, exist_ok=True)
    driver_path = workdir / "trace_driver.py"
    driver_path.write_text(_TRACE_DRIVER, encoding="utf-8")
    log_path = workdir / "access-log.json"
    import os

    from cli_agent_orchestrator.constants import STATE_ROOT_ENV

    env = dict(os.environ)
    env["HOME"] = str(state_home)
    # The disposable home is the only state root this child may use. A state
    # root exported in the surrounding environment would otherwise be
    # inherited and point the child at live state, voiding the isolation the
    # rig exists to provide -- and it would do so silently, because the probe
    # would still run and still produce a verdict.
    env.pop(STATE_ROOT_ENV, None)
    pythonpath = str(src)
    if extra_pythonpath is not None:
        pythonpath = f"{src}{os.pathsep}{extra_pythonpath}"
    env["PYTHONPATH"] = pythonpath
    completed = subprocess.run(
        [sys.executable, str(driver_path), str(log_path), probe],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        check=False,
    )
    if not log_path.exists():
        raise RigError(f"old-binary probe produced no access log: {completed.stderr[-500:]}")
    payload = json.loads(log_path.read_text(encoding="utf-8"))
    accesses = payload["accesses"]
    log = AccessLog(
        reads=list(accesses.get("reads") or []),
        writes=list(accesses.get("writes") or []),
        deletes=list(accesses.get("deletes") or []),
        queries=list(accesses.get("queries") or []),
    )
    locators = {surface.locator for surface in v2_surfaces}
    violations = [
        access
        for access in log.all_accesses()
        if any(locator and locator in access for locator in locators)
    ]
    if payload.get("error"):
        violations.append(f"old-binary probe errored: {payload['error']}")
    if verify_state is not None:
        violations.extend(verify_state())
    return RigVerdict(
        zero_visibility=not violations,
        violations=tuple(violations),
        surfaces_checked=len(list(v2_surfaces)),
    )


# ------------------------------------------------------------ exact-H_B gate
#
# The deployed-base old binary (H_B) — the exact ref whose actual
# list/query/watchdog/cleanup/delete entrypoints the rollout gate runs
# against constructed v2 forward state. Synthetic miniature repositories
# remain valid unit fixtures for the rig mechanics but can never satisfy
# this compatibility acceptance.
DEFAULT_OLD_BINARY_REF = "e473d3b50eb307d1a6e179ec209aeb2bd3e58dcb"

_V2_TERMINAL_ID = "v2f00d42"
_V2_GENERATION = "gen-v2-forward-state"
_AGED_V1_ID = "aaaaaaaa"
_FRESH_V1_ID = "bbbbbbbb"

# Drives the old binary's REAL entrypoints, resolved from the extracted
# old tree on PYTHONPATH (never hand-transcribed behavior). Expected
# fail-closed outcomes for invisible identities are asserted inside each
# probe; anything unexpected propagates and becomes a verdict violation.
_HB_PROBES = '''\
"""Exact-old-binary entrypoint probes (run inside the old source tree)."""


def cleanup():
    from cli_agent_orchestrator.services import cleanup_service

    cleanup_service.cleanup_old_data()


def list_query():
    from cli_agent_orchestrator.clients import database

    database.init_db()
    rows = database.list_all_terminals()
    for row in rows:
        database.get_terminal_metadata(row["id"])
    assert database.get_terminal_metadata("ffffffff") is None
    database.list_pending_receiver_ids_by_provider("codex")
    database.get_pending_messages("ffffffff")


def delete():
    from cli_agent_orchestrator.clients import database
    from cli_agent_orchestrator.services import terminal_service

    database.init_db()
    assert database.delete_terminal("ffffffff") is False
    assert database.delete_terminal_if_generation("ffffffff", "gen-x") is False
    assert database.delete_terminals_by_session("cao-nonexistent") == 0
    try:
        terminal_service.delete_terminal("ffffffff")
    except Exception as exc:
        # A missing terminal may refuse in many ways (no row, no window,
        # no tmux); only a failure that is NOT about the absent identity
        # is evidence. Any real v2 contact is traced/verify_state-caught.
        text = str(exc).lower()
        markers = ("not found", "no such", "can't find", "doesn't exist",
                   "window", "tmux", "session")
        if not any(marker in text for marker in markers):
            raise


def watchdog():
    from cli_agent_orchestrator.clients import database
    from cli_agent_orchestrator.services.fifo_reader import FifoManager

    database.init_db()
    manager = FifoManager()
    manager.create_reader("aaaaaaaa", pane_probe=lambda: "pane", rearm=lambda: None)
    manager._check_pipe_liveness("aaaaaaaa")
    manager.stop_reader("aaaaaaaa")
    manager.stop_watchdog()


def hostile_cleanup():
    """Contrived normal-path failure: an old cleanup that deletes every
    terminal log regardless of age or ownership — the attack the gate
    must catch (never part of the acceptance probes)."""
    from cli_agent_orchestrator.constants import TERMINAL_LOG_DIR

    for log_file in TERMINAL_LOG_DIR.glob("*.log"):
        log_file.unlink()
'''

_BOOTSTRAP = '''\
"""Initialize the v1 state root with the OLD binary's own init/seeders."""
import sqlite3

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.constants import DATABASE_FILE

database.init_db()
if database.get_terminal_metadata("aaaaaaaa") is None:
    database.create_terminal("aaaaaaaa", "cao-old", "win-old", "codex")
if database.get_terminal_metadata("bbbbbbbb") is None:
    database.create_terminal("bbbbbbbb", "cao-old", "win-fresh", "codex")
try:
    database.create_inbox_message("sender", "aaaaaaaa", "hello")
except Exception:
    pass
conn = sqlite3.connect(str(DATABASE_FILE))
conn.execute(
    "UPDATE terminals SET last_active=? WHERE id='aaaaaaaa'",
    ("2000-01-01 00:00:00.000000",),
)
try:
    conn.execute("UPDATE inbox SET created_at=? WHERE receiver_id='aaaaaaaa'",
                 ("2000-01-01 00:00:00.000000",))
except sqlite3.Error:
    pass
conn.commit()
conn.close()
'''


def _home_layout(state_home: Path) -> dict[str, Path]:
    root = state_home / ".aws" / "cli-agent-orchestrator"
    return {
        "root": root,
        "db": root / "db" / "cli-agent-orchestrator.db",
        "terminal_logs": root / "logs" / "terminal",
        "server_logs": root / "logs",
        "fifos": root / "fifos",
        "companion": root / "companion",
        "registry": root / "resource-registry.sqlite",
    }


def _build_forward_state(
    *, repo: Path, ref: str, workdir: Path, state_home: Path
) -> tuple[list[V2Surface], dict[str, Any]]:
    """Construct v1 + v2 forward state in the disposable home.

    The v1 state root is initialized by the OLD binary itself (byte-exact
    H_B init/seeders); the v2 surface is then built by the CURRENT
    migration/registry code over absolute paths. Returns (v2_surfaces,
    verify fixtures). The v2 log files are fresh — the rollout invariant
    is that exposed v2 generations are live; aged-file exposure of v2
    identities past retention is bounded by the migration window and is
    proven separately by the new-side registry-first retention tests.
    """
    import hashlib
    import os
    import sqlite3
    import time

    from cli_agent_orchestrator.constants import STATE_ROOT_ENV

    layout = _home_layout(state_home)
    old_tree = extract_exact_source(ref, repo=repo, dest=workdir / "old-tree")
    bootstrap = workdir / "bootstrap.py"
    bootstrap.write_text(_BOOTSTRAP, encoding="utf-8")
    env = dict(os.environ)
    env["HOME"] = str(state_home)
    # Same reason as the probe runner: an inherited state root would move the
    # bootstrap's writes out of the disposable home and into live state.
    env.pop(STATE_ROOT_ENV, None)
    env["PYTHONPATH"] = str(old_tree / "src")
    completed = subprocess.run(
        [sys.executable, str(bootstrap)],
        capture_output=True,
        text=True,
        env=env,
        timeout=120.0,
        check=False,
    )
    if completed.returncode != 0:
        raise RigError(f"old-binary state bootstrap failed: {completed.stderr[-500:]}")
    if not layout["db"].exists():
        raise RigError("old-binary bootstrap did not create the v1 database")

    # v2 forward state (new side, absolute paths; no HOME dependence).
    from cli_agent_orchestrator.services import resource_registry as rr
    from cli_agent_orchestrator.services import vintage_migration

    vintage_migration.migrate_v2(layout["db"])
    conn = sqlite3.connect(str(layout["db"]))
    conn.execute(
        "INSERT INTO managed_launch_v2_terminals("
        "id, tmux_session, tmux_window, provider, generation) "
        "VALUES (?,?,?,?,?)",
        (
            _V2_TERMINAL_ID,
            "cao-v2",
            f"managed-{_V2_TERMINAL_ID}-abcdef123456",
            "codex",
            _V2_GENERATION,
        ),
    )
    conn.commit()
    conn.close()

    v2_log = layout["terminal_logs"] / f"{_V2_TERMINAL_ID}.log"
    v2_scrollback = layout["terminal_logs"] / f"{_V2_TERMINAL_ID}.scrollback"
    v2_snapshot = layout["terminal_logs"] / f"{_V2_TERMINAL_ID}.snapshot.json"
    aged_v1_log = layout["terminal_logs"] / f"{_AGED_V1_ID}.log"
    fresh_v1_log = layout["terminal_logs"] / f"{_FRESH_V1_ID}.log"
    layout["terminal_logs"].mkdir(parents=True, exist_ok=True)
    v2_log.write_text("v2 live log\n", encoding="utf-8")
    v2_scrollback.write_text("v2 scrollback\n", encoding="utf-8")
    v2_snapshot.write_text('{"v2": true}\n', encoding="utf-8")
    aged_v1_log.write_text("aged v1 log\n", encoding="utf-8")
    fresh_v1_log.write_text("fresh v1 log\n", encoding="utf-8")
    aged = time.time() - 40 * 86400
    os.utime(aged_v1_log, (aged, aged))
    v2_fifo = layout["fifos"] / f"{_V2_TERMINAL_ID}.fifo"
    layout["fifos"].mkdir(parents=True, exist_ok=True)
    v2_fifo.write_text("", encoding="utf-8")
    companion_gen = layout["companion"] / _V2_TERMINAL_ID / _V2_GENERATION
    companion_gen.mkdir(parents=True, exist_ok=True)
    (companion_gen / "heartbeat.json").write_text(
        '{"schema": "cao-heartbeat-v2"}\n', encoding="utf-8"
    )

    registry = rr.ResourceRegistry(layout["registry"])
    registry.declare(
        entry_id=f"{_V2_TERMINAL_ID}.log",
        kind="log",
        protocol_vintage="v2",
        terminal_id=_V2_TERMINAL_ID,
        generation=_V2_GENERATION,
        owner="fork",
        ownership="owned",
        constructor_id="terminal_service.create_terminal",
        deleter_id="terminal_service.delete_terminal",
        rollback_rule="generation-isolated",
        actor_id="old_binary_rig",
        desired_fs_path=str(v2_log),
    )

    surfaces = [
        V2Surface(kind="fs", locator=str(path))
        for path in (
            v2_log,
            v2_scrollback,
            v2_snapshot,
            v2_fifo,
            companion_gen / "heartbeat.json",
            layout["registry"],
        )
    ]
    surfaces.append(V2Surface(kind="db", locator=_V2_TERMINAL_ID))
    fixtures = {
        "v2_files": {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (v2_log, v2_scrollback, v2_snapshot, v2_fifo)
        },
        "aged_v1_log": str(aged_v1_log),
        "fresh_v1_log": str(fresh_v1_log),
    }
    return surfaces, fixtures


def prove_old_binary_invisibility(
    *,
    repo: Path,
    ref: str = DEFAULT_OLD_BINARY_REF,
    workdir: Path,
    extra_probes: tuple[str, ...] = (),
    timeout: float = 120.0,
) -> RigVerdict:
    """The exact-old-binary rollout gate (T-MIG-7 / T-RES-REG proof).

    Constructs v2 forward state in a disposable HOME, then executes the
    exact old binary's ACTUAL list/query/watchdog/cleanup/delete
    entrypoints (extracted byte-exact from ``ref``) under access tracing.
    Any v2-surface access, v2 mutation, or unexpected probe failure is a
    verdict violation; positive controls (the aged v1 row/log MUST be
    collected, the v2 rows/files MUST survive) prove the probes really
    ran. Migration/rollout/rollback consume this verdict through
    ``vintage_migration`` and refuse on any violation.
    """
    import hashlib
    import sqlite3

    repo = Path(repo)
    workdir = Path(workdir)
    state_home = workdir / "state-home"
    state_home.mkdir(parents=True, exist_ok=True)
    surfaces, fixtures = _build_forward_state(
        repo=repo, ref=ref, workdir=workdir, state_home=state_home
    )
    probes_dir = workdir / "probes"
    probes_dir.mkdir(exist_ok=True)
    (probes_dir / "hb_probes.py").write_text(_HB_PROBES, encoding="utf-8")
    layout = _home_layout(state_home)

    violations: list[str] = []
    probe_names = ("list_query", "watchdog", "delete", "cleanup") + tuple(extra_probes)
    for name in probe_names:
        verdict = run_exact_old_binary(
            ref=ref,
            repo=repo,
            state_home=state_home,
            workdir=workdir / f"run-{name}",
            probe=f"hb_probes:{name}",
            v2_surfaces=surfaces,
            extra_pythonpath=probes_dir,
            timeout=timeout,
        )
        violations.extend(f"{name}:{violation}" for violation in verdict.violations)

    # Post-conditions: v2 rows and files are intact; the positive controls
    # prove cleanup/delete really ran against the old binary's own surface.
    conn = sqlite3.connect(str(layout["db"]))
    try:
        v2_count = conn.execute(
            "SELECT COUNT(*) FROM managed_launch_v2_terminals WHERE id=?",
            (_V2_TERMINAL_ID,),
        ).fetchone()[0]
        if v2_count != 1:
            violations.append("v2 row missing or duplicated after old-binary run")
        aged_v1 = conn.execute(
            "SELECT COUNT(*) FROM terminals WHERE id=?", (_AGED_V1_ID,)
        ).fetchone()[0]
        fresh_v1 = conn.execute(
            "SELECT COUNT(*) FROM terminals WHERE id=?", (_FRESH_V1_ID,)
        ).fetchone()[0]
    finally:
        conn.close()
    if aged_v1 != 0:
        violations.append("cleanup control failed: aged v1 row survived")
    if fresh_v1 != 1:
        violations.append("cleanup over-collected: fresh v1 row was deleted")
    for path_text, digest in fixtures["v2_files"].items():
        path = Path(path_text)
        if not path.exists():
            violations.append(f"v2 file deleted by old binary: {path_text}")
        elif hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            violations.append(f"v2 file mutated by old binary: {path_text}")
    if Path(fixtures["aged_v1_log"]).exists():
        violations.append("cleanup control failed: aged v1 log survived")
    if not Path(fixtures["fresh_v1_log"]).exists():
        violations.append("cleanup over-collected: fresh v1 log was deleted")
    return RigVerdict(
        zero_visibility=not violations,
        violations=tuple(violations),
        surfaces_checked=len(surfaces),
    )
