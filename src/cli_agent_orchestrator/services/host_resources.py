"""Host resource sample for the AG-UI fleet snapshot (#1155, host resource view).

Numbers only: host CPU %, load per core, memory used % and a memory-pressure level. No
pid, process name, argv, cwd, hostname or user ever leaves this module. Only the LOCAL
host can be probed, so the sample is one entry (``runner_id`` is ``None``).

Every field degrades to *absent*: a probe that fails, times out, cannot be parsed or does
not exist on this platform leaves its key out of the entry. Nothing is ever reported as 0
for "unknown". The module is split into pure parsers (tested on canned strings, on any
platform) and thin readers (patched in tests).

CPU % is computed here from ``psutil.cpu_times()`` deltas rather than ``cpu_percent(None)``,
whose first call is a meaningless 0.0 and whose window is "since the last call". The first
sample, and any sample whose window is longer than ``MAX_CPU_WINDOW_SECONDS``, therefore has
no ``cpu_percent`` and re-primes the baseline.

``memory_used_percent`` is psutil's available-based figure (``100 * (total - available) /
total``), which is not the "used" of Activity Monitor or ``top``; compare it with those only
loosely. CPU needs two samples 30 to 60 seconds apart, so a sample taken more than 60 seconds
after the previous one has no ``cpu_percent`` (an idle fleet shows a dash).

Event loop. ``sample()`` runs inside the stream's snapshot closure, on the event loop, so it
never probes inline: it returns the cached entry at once and, when the cache is older than
the TTL, starts ONE background thread to refresh it (``asyncio.to_thread`` at start-up for the
prime). The slowest probe is the macOS ``sysctl`` call, bounded at 2 seconds, and that runs off
the loop. The cost is that a snapshot served right after the TTL lapses shows the previous
sample (its ``sampled_at`` says when) and the next one shows the new one. Laptop cost follows
the cross-cutting rules of #519: one cheap sample per 30 seconds of fleet activity, no timer.

Memory pressure is a heuristic and is not comparable across platforms: Linux uses the PSI
``some avg10`` figure with the thresholds below, macOS uses the kernel's own level.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import psutil

logger = logging.getLogger(__name__)

#: A sample is reused for this long (the same pattern as the profile catalog).
HOST_TTL_SECONDS = 30.0
#: A CPU window longer than this (2x the TTL) is stale: report no CPU % and re-prime.
MAX_CPU_WINDOW_SECONDS = 2 * HOST_TTL_SECONDS
#: Linux PSI ``some avg10`` thresholds (percent of time stalled). A labelled heuristic.
PSI_WARN = 10.0
PSI_CRITICAL = 40.0
#: Every subprocess is bounded.
SUBPROCESS_TIMEOUT_SECONDS = 2

PRESSURE_LEVELS = ("normal", "warn", "critical")
#: The only keys an entry may carry (a failed field is omitted, so a subset is valid).
HOST_KEYS = frozenset(
    {
        "runner_id",
        "sampled_at",
        "cpu_percent",
        "load_per_core",
        "memory_used_percent",
        "memory_pressure",
    }
)

# --- pure parsers ---------------------------------------------------------------------


def parse_psi_some_avg10(text: str) -> Optional[float]:
    """``some avg10=1.23 ...`` line of ``/proc/pressure/memory`` -> 1.23, else None."""
    for line in (text or "").splitlines():
        parts = line.split()
        if parts and parts[0] == "some":
            for part in parts[1:]:
                if part.startswith("avg10="):
                    try:
                        value = float(part[len("avg10=") :])
                    except ValueError:
                        return None
                    return value if value >= 0 else None
    return None


def pressure_from_psi(avg10: Optional[float]) -> Optional[str]:
    """PSI ``some avg10`` -> normal / warn / critical (heuristic), None when unknown."""
    if avg10 is None:
        return None
    if avg10 >= PSI_CRITICAL:
        return "critical"
    if avg10 >= PSI_WARN:
        return "warn"
    return "normal"


def parse_macos_pressure_level(text: str) -> Optional[str]:
    """``kern.memorystatus_vm_pressure_level``: 1 normal, 2 warn, 4 critical, else None."""
    try:
        level = int((text or "").strip())
    except ValueError:
        return None
    return {1: "normal", 2: "warn", 4: "critical"}.get(level)


def cpu_percent_from_times(
    prev: Tuple[float, float], curr: Tuple[float, float], window: float
) -> Optional[int]:
    """CPU busy % between two ``(busy, total)`` readings, None if there is no basis.

    Absent for a window outside ``(0, MAX_CPU_WINDOW_SECONDS]`` or no elapsed CPU time.
    """
    if not 0 < window <= MAX_CPU_WINDOW_SECONDS:
        return None
    busy = curr[0] - prev[0]
    total = curr[1] - prev[1]
    if total <= 0 or busy < 0:
        return None
    return max(0, min(100, int(round(100.0 * busy / total))))


# --- thin readers (patched in tests) --------------------------------------------------


def busy_total_from_cpu_times(times: Any) -> Tuple[float, float]:
    """``(busy, total)`` seconds from a ``psutil.cpu_times()`` tuple, as psutil's own
    ``cpu_percent`` computes it: ``guest`` and ``guest_nice`` are already inside ``user`` and
    ``nice`` on Linux, so they are not added twice, and ``iowait`` counts as idle.
    """
    total = float(sum(times))
    total -= float(getattr(times, "guest", 0.0))
    total -= float(getattr(times, "guest_nice", 0.0))
    busy = total - float(getattr(times, "idle", 0.0)) - float(getattr(times, "iowait", 0.0))
    return busy, total


def _read_cpu_times() -> Optional[Tuple[float, float]]:
    try:
        return busy_total_from_cpu_times(psutil.cpu_times())
    except Exception:
        return None


def _read_load_per_core() -> Optional[float]:
    try:
        load = os.getloadavg()[0]
        try:
            cores = len(os.sched_getaffinity(0))  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            cores = os.cpu_count() or 0
        if cores <= 0:
            return None
        return round(load / cores, 1)
    except Exception:
        return None


def _read_memory_used_percent() -> Optional[int]:
    try:
        return int(round(psutil.virtual_memory().percent))
    except Exception:
        return None


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError):
        return None


def _read_macos_pressure_text() -> Optional[str]:
    try:
        result = subprocess.run(
            ["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _read_memory_pressure() -> Optional[str]:
    platform = sys.platform
    if platform.startswith("linux"):
        text = _read_text("/proc/pressure/memory")
        return pressure_from_psi(parse_psi_some_avg10(text)) if text is not None else None
    if platform == "darwin":
        text = _read_macos_pressure_text()
        return parse_macos_pressure_level(text) if text is not None else None
    return None


# --- sampler --------------------------------------------------------------------------


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class HostSampler:
    """Keeps the CPU baseline and one cached sample; safe to call from any thread.

    ``sample()`` never probes on the caller's thread (see the module docstring);
    ``refresh()`` is the synchronous probe, used by the background thread, the start-up prime
    and tests.
    """

    def __init__(self, ttl: float = HOST_TTL_SECONDS) -> None:
        self._ttl = ttl
        self._lock = threading.Lock()  # guards the cache, baseline and flag: held briefly
        self._probe_lock = threading.Lock()  # one probe at a time; never held by sample()
        self._baseline: Optional[Tuple[Tuple[float, float], float]] = None
        self._cached: Optional[Tuple[float, List[Dict[str, Any]]]] = None
        self._refreshing = False
        self._worker: Optional[threading.Thread] = None

    def reset(self) -> None:
        with self._lock:
            self._baseline = None
            self._cached = None
            self._refreshing = False
            self._worker = None

    def sample(self) -> List[Dict[str, Any]]:
        """The cached host list (copies, so streams never share a mutable entry).

        Empty until the first refresh has finished. A stale cache starts one background
        refresh and is returned as it is.
        """
        with self._lock:
            cached = self._cached
            stale = cached is None or time.monotonic() - cached[0] >= self._ttl
            start = stale and not self._refreshing
            if start:
                self._refreshing = True
                self._worker = threading.Thread(
                    target=self._refresh_in_background, name="cao-host-sample", daemon=True
                )
            worker = self._worker
            entries = [dict(entry) for entry in cached[1]] if cached is not None else []
        if start and worker is not None:
            try:
                worker.start()
            except Exception:
                # A thread that cannot start (resource exhaustion) must not leave the flag
                # set for the life of the process: clear it so the next call can retry.
                logger.debug("agui_stream: host sample thread did not start", exc_info=True)
                with self._lock:
                    self._refreshing = False
                    self._worker = None
        return entries

    def _refresh_in_background(self) -> None:
        try:
            self.refresh()
        except Exception:
            logger.debug("agui_stream: host refresh failed", exc_info=True)
        finally:
            with self._lock:
                self._refreshing = False

    def refresh(self) -> List[Dict[str, Any]]:
        """Probe now, cache the result for the TTL (a failure too) and return a copy."""
        with self._probe_lock:
            now = time.monotonic()
            try:
                hosts = self._probe(now)
            except Exception:
                logger.debug("agui_stream: host sample failed", exc_info=True)
                hosts = []
            with self._lock:
                self._cached = (now, hosts)
            return [dict(entry) for entry in hosts]

    def _probe(self, now: float) -> List[Dict[str, Any]]:
        entry: Dict[str, Any] = {"runner_id": None}
        reading = _read_cpu_times()
        baseline = self._baseline
        if reading is not None:
            if baseline is not None:
                cpu = cpu_percent_from_times(baseline[0], reading, now - baseline[1])
                if cpu is not None:
                    entry["cpu_percent"] = cpu
            self._baseline = (reading, now)
        # Whatever a reader returns is validated here, so a contaminated reader cannot put a
        # string, a container or an unknown level on the wire.
        load = _read_load_per_core()
        if _number(load):
            entry["load_per_core"] = load
        memory = _read_memory_used_percent()
        if _number(memory):
            entry["memory_used_percent"] = memory
        pressure = _read_memory_pressure()
        if pressure in PRESSURE_LEVELS:
            entry["memory_pressure"] = pressure
        if len(entry) == 1:
            return []  # nothing readable: "not reported"
        entry["sampled_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return [entry]


_sampler = HostSampler()


def sample_host() -> List[Dict[str, Any]]:
    """Process-wide cached host sample, never blocking (see :class:`HostSampler`)."""
    return _sampler.sample()


def prime_host_sampler() -> None:
    """Take the CPU baseline and the first sample now (server start, off the event loop)."""
    _sampler.refresh()


def refresh_host_now() -> List[Dict[str, Any]]:
    """Probe now (tests and the prime); ``sample_host`` itself never blocks."""
    return _sampler.refresh()


def reset_host_sampler() -> None:
    """Forget the baseline and cache (tests)."""
    _sampler.reset()
