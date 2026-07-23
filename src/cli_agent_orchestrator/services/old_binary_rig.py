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
# recording every file open outside library paths, then dumps the access
# log as JSON.  HOME and PYTHONPATH are redirected by the parent so the
# old tree and its state root are fully disposable.
_TRACE_DRIVER = r"""
import importlib
import json
import sys

log_path, dotted = sys.argv[1], sys.argv[2]
accesses = {"reads": [], "writes": [], "deletes": [], "queries": []}
_SKIP = ("/site-packages/", "__pycache__", "/.venv/", "/usr/lib", "/Library/")


def _hook(event, args):
    if event != "open":
        return
    path = str(args[0])
    if any(skip in path for skip in _SKIP):
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

    env = dict(os.environ)
    env["HOME"] = str(state_home)
    env["PYTHONPATH"] = str(src)
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
