"""Find and stop the processes that own a CAO-managed private CLI home.

A provider that gives each terminal its own home directory (Grok's
``GROK_HOME``, Kiro's per-launch ``KIRO_HOME``) can only delete that directory
once nothing can write into it again. Killing the terminal's window is not
enough: a CLI can leave children behind (Grok's updater, Kiro's chat and MCP
processes) that keep writing for seconds after the pane is gone, and would
recreate a partial tree after the delete.

Ownership is read from each candidate process's environment, never from its
command line: the exact home path in the provider's env key is the capability
boundary, so an operator's own CLI process with another home is never touched.
A returned ``psutil.Process`` has already cached its creation time, and
``send_signal`` on that same object re-checks the PID/create-time identity
before signalling, so a PID reused between inspection and delivery is not hit.

Uncertainty is retained, not guessed: a same-user candidate whose identity or
environment cannot be read makes the caller keep the home for a later retry.
"""

from __future__ import annotations

import logging
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Optional, Sequence

import psutil

logger = logging.getLogger(__name__)

# One inspection: a verified owner, ``False`` (not an owner), or ``None`` (uncertain).
Inspection = psutil.Process | Literal[False] | None


@dataclass(frozen=True)
class HomeOwners:
    """Which same-user processes may own a private home, and how to recognise them.

    ``possible_owner`` filters on the short process name before anything that
    can be protected is read. ``verified_owner``, when given, then checks the
    executable path and argv; ``None`` skips that step and trusts the name and
    the exact environment value alone.
    """

    env_key: str
    label: str
    possible_owner: Callable[[str], bool]
    verified_owner: Optional[Callable[[str, Sequence[str]], bool]] = None


def inspect_home_process(pid: int, home: Path, owners: HomeOwners) -> Inspection:
    """Return a verified, PID-reuse-safe process whose ``owners.env_key`` is ``home``."""

    try:
        proc = psutil.Process(pid)
        # Seed psutil's pid+creation-time identity before examining the
        # process. Its send_signal() then refuses a PID reused between this
        # verification and delivery.
        proc.create_time()
        if proc.uids().effective != os.geteuid():
            return False
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied as exc:
        # We cannot establish same-user ownership, so this can still be an
        # owner of the private home. Retain it for a later retry rather than
        # racing a live writer.
        logger.warning("Cannot inspect process %s before %s cleanup: %s", pid, owners.label, exc)
        return None
    except psutil.Error as exc:
        logger.warning("Cannot inspect process %s before %s cleanup: %s", pid, owners.label, exc)
        return False

    try:
        if not owners.possible_owner(proc.name().lower()):
            return False
        executable = proc.exe() if owners.verified_owner is not None else ""
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied as exc:
        # Same-user, with a candidate name, but its identity cannot be fully
        # verified. Fail closed so cleanup never removes a home a live process
        # may recreate.
        logger.warning("Cannot inspect process %s before %s cleanup: %s", pid, owners.label, exc)
        return None
    except psutil.Error as exc:
        logger.warning("Cannot inspect process %s before %s cleanup: %s", pid, owners.label, exc)
        return None

    try:
        if owners.verified_owner is not None and not owners.verified_owner(
            executable, proc.cmdline()
        ):
            return False
        environ = proc.environ()
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied as exc:
        logger.warning("Cannot inspect process %s before %s cleanup: %s", pid, owners.label, exc)
        return None
    except psutil.Error as exc:
        logger.warning("Cannot inspect process %s before %s cleanup: %s", pid, owners.label, exc)
        return None
    return proc if environ.get(owners.env_key) == str(home) else False


def pids_using_home(
    home: Path,
    pid_uses_home: Callable[[int, Path], Optional[bool]],
    label: str,
) -> Optional[set[int]]:
    """Return the processes ``pid_uses_home`` says own ``home``; ``None`` when uncertain.

    This process is never a candidate: cleanup can run in the server, and a
    failed inspection must never turn into a signal to ourselves.
    """

    current_pid = os.getpid()
    pids: set[int] = set()
    try:
        process_ids = psutil.pids()
    except psutil.Error as exc:
        logger.warning(
            "Cannot enumerate processes before cleaning %s home %s: %s", label, home, exc
        )
        # A failed inspection is not evidence of an owner. Retain state for a
        # later retry.
        return None

    for pid in process_ids:
        if pid == current_pid:
            continue
        uses_home = pid_uses_home(pid, home)
        if uses_home is None:
            # Uncertainty only follows a same-user candidate whose environment
            # cannot be read. Retain the home rather than race that process.
            return None
        if uses_home:
            pids.add(pid)
    return pids


def stop_home_processes(
    home: Path,
    *,
    pids_using_home: Callable[[Path], Optional[set[int]]],
    inspect_home_process: Callable[[int, Path], Inspection],
    label: str,
    term_grace_seconds: float,
    kill_grace_seconds: float,
) -> bool:
    """SIGTERM, then SIGKILL, every owner of ``home``; True once none remains.

    Each signal goes through a fresh ``inspect_home_process``, so only a
    process that still owns the home at that moment is signalled. Any
    uncertainty, or an owner still alive after both graces, returns False: the
    caller keeps the home and its retry handle.
    """

    def stop(pids: set[int], sig: signal.Signals) -> bool:
        delivered = True
        for pid in pids:
            inspected = inspect_home_process(pid, home)
            if inspected is None:
                delivered = False
                continue
            if inspected is False:
                continue
            try:
                inspected.send_signal(sig)
            except psutil.NoSuchProcess:
                continue
            except psutil.Error as exc:
                logger.warning("Cannot signal %s-home process %s for %s: %s", label, pid, home, exc)
                delivered = False
        return delivered

    def wait_gone(grace: float) -> Optional[bool]:
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            remaining = pids_using_home(home)
            if remaining is None:
                return None
            if not remaining:
                return True
            time.sleep(0.05)
        return False

    pids = pids_using_home(home)
    if pids is None:
        return False
    if not pids:
        return True
    logger.info("Stopping residual %s processes %s before cleanup of %s", label, sorted(pids), home)
    if not stop(pids, signal.SIGTERM):
        return False
    gone = wait_gone(term_grace_seconds)
    if gone is not False:
        return bool(gone)

    pids = pids_using_home(home)
    if pids is None:
        return False
    if not pids:
        return True
    logger.warning("%s-home processes did not exit after SIGTERM: %s", label, sorted(pids))
    if not stop(pids, signal.SIGKILL):
        return False
    gone = wait_gone(kill_grace_seconds)
    if gone is not False:
        return bool(gone)
    remaining = pids_using_home(home)
    logger.warning(
        "Retaining %s home %s because processes remain alive or cannot be inspected: %s",
        label,
        home,
        sorted(remaining) if remaining is not None else "unknown",
    )
    return False
