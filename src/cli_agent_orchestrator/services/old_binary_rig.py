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

import shutil
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
