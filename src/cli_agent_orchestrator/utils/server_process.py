"""Process-lifecycle helpers for the detached ``cao-server`` daemon.

These back the ``cao server`` subcommands and the ``cao launch`` auto-start
path. The goal is to manage the background daemon without leaving ``cao``:
probe ``/health`` to know if it is up, spawn the existing ``cao-server`` entry
point detached, track its PID in a pidfile, and tear it down with SIGTERM.

The ``cao-server`` binary itself is left untouched (it is referenced by MCP
configs and the devcontainer); these helpers only wrap it.
"""

import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import requests

from cli_agent_orchestrator.constants import (
    API_BASE_URL,
    LOG_DIR,
    SERVER_HOST,
    SERVER_PIDFILE,
    SERVER_PORT,
)

logger = logging.getLogger(__name__)

# How long to wait for /health after spawning the daemon before giving up.
_START_TIMEOUT_S = 30.0
# How long to wait for /health to stop responding after SIGTERM.
_STOP_TIMEOUT_S = 15.0


def server_error_hint() -> str:
    """Return a multi-line troubleshooting hint pointing at the server log.

    A 500/timeout from the API rarely explains itself at the CLI; the
    explanation is in the server log. Shared by ``cao launch`` and
    ``cao session`` so a failed call names the latest ``cao_*.log`` plus the
    commands that surface it. See rec #5.
    """
    from cli_agent_orchestrator.utils.logging import latest_server_log_path

    hints = ["", "Troubleshooting:", "  - View the server log: cao logs --server"]
    log_path = latest_server_log_path()
    if log_path is not None:
        hints.append(f"    (latest: {log_path})")
    hints.append(
        "  - If a provider is slow to initialize: cao doctor --live "
        "and raise provider_init_timeout in settings.json"
    )
    return "\n".join(hints)


def health(timeout: float = 2.0) -> Optional[dict]:
    """Return the parsed ``/health`` JSON if the server answers, else None."""
    try:
        resp = requests.get(f"{API_BASE_URL}/health", timeout=timeout)
    except requests.exceptions.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def is_server_running(timeout: float = 2.0) -> bool:
    """Return True when the server answers ``/health`` with 200."""
    return health(timeout=timeout) is not None


def read_pidfile() -> Optional[int]:
    """Return the PID recorded in the pidfile, or None if absent/unreadable."""
    try:
        text = SERVER_PIDFILE.read_text().strip()
    except (OSError, ValueError):
        return None
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def write_pidfile(pid: int) -> None:
    """Record ``pid`` in the pidfile (creating CAO_HOME_DIR if needed)."""
    SERVER_PIDFILE.parent.mkdir(parents=True, exist_ok=True)
    SERVER_PIDFILE.write_text(str(pid))


def clear_pidfile() -> None:
    """Remove the pidfile if present (best-effort)."""
    try:
        SERVER_PIDFILE.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning(f"Failed to remove pidfile {SERVER_PIDFILE}: {e}")


def _pid_alive(pid: int) -> bool:
    """Return True if a process with ``pid`` exists (signal 0 probe)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _server_command(host: Optional[str], port: Optional[int]) -> list[str]:
    """Build the argv that launches the ``cao-server`` entry point.

    Prefers the installed ``cao-server`` console script; falls back to
    ``python -m`` invocation of the same entry point so it works from a source
    checkout where the script may not be on PATH.
    """
    cao_server = shutil.which("cao-server")
    if cao_server:
        cmd = [cao_server]
    else:
        cmd = [sys.executable, "-m", "cli_agent_orchestrator.api.main"]
    if host:
        cmd += ["--host", host]
    if port:
        cmd += ["--port", str(port)]
    return cmd


def _startup_log_path() -> Path:
    """Path the detached daemon's stdout/stderr is redirected to."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / "server-startup.log"


def start_server_detached(
    host: Optional[str] = None,
    port: Optional[int] = None,
    timeout: float = _START_TIMEOUT_S,
) -> int:
    """Spawn ``cao-server`` detached and wait for ``/health``.

    Returns the child PID. Raises ``RuntimeError`` if the server does not
    become healthy within ``timeout`` seconds. If a server is already running,
    returns its recorded PID (or 0 if unknown) without spawning a second one.
    """
    if is_server_running():
        return read_pidfile() or 0

    cmd = _server_command(host, port)
    startup_log = _startup_log_path()
    # Open in append mode so successive starts keep history; the daemon
    # detaches via start_new_session so it survives the parent CLI exiting.
    log_fh = open(startup_log, "ab")
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        log_fh.close()

    write_pidfile(proc.pid)

    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_server_running():
            return proc.pid
        # Bail early if the child died before becoming healthy.
        if proc.poll() is not None:
            raise RuntimeError(
                f"cao-server exited during startup (code {proc.returncode}); " f"see {startup_log}"
            )
        time.sleep(0.5)

    raise RuntimeError(
        f"cao-server did not become healthy within {timeout:.0f}s; see {startup_log}"
    )


def stop_server(timeout: float = _STOP_TIMEOUT_S) -> bool:
    """Stop the running server via SIGTERM to the pidfile PID.

    Returns True if the server stopped (``/health`` no longer responds),
    False if no PID was known or it could not be confirmed stopped.
    """
    pid = read_pidfile()
    if pid is None:
        # No pidfile — nothing we can target. Report based on health.
        return not is_server_running()

    if _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError as e:
            logger.warning(f"Not permitted to signal pid {pid}: {e}")
            return False

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_server_running() and not _pid_alive(pid):
            clear_pidfile()
            return True
        time.sleep(0.5)

    return not is_server_running()
