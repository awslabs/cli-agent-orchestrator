"""Opt-in behavioural smoke: event-driven inbox delivery against a LIVE herdr.

This is the behavioural counterpart to the structural guard in
``test_herdr_contract.py`` (see #1378/#1379/#1403). It catches the one thing the
schema contract cannot: herdr keeping ``pane.agent_status_changed`` in its schema
but silently ceasing to *emit* it (or any future change that stops delivery
flowing through the event path), by asserting that an inbox message queued while
a real worker is busy is delivered via the EVENT path — ``herdr-event`` or
``herdr-settle`` — and NOT by the slow reconciliation sweep.

Why it is opt-in rather than part of the suite: herdr only emits
``pane.agent_status_changed`` for an agent it natively detects, and CAO *reads*
that status (``herdr pane get``) rather than reporting it — confirmed in
``herdr_backend.get_native_status``. So there is no credential-free or mock way
to drive the event; it needs a real, herdr-recognised agent CLI (claude/codex)
doing real work. This test therefore runs only when explicitly enabled and a real
agent profile is available, which in CI means a dedicated job that provisions an
agent. It stands up its OWN isolated cao-server (own port, own ``CAO_HOME_DIR``,
own herdr session) and never touches a developer's live server.

Enable with::

    CAO_TEST_REAL_HOME=1 CAO_HERDR_LIVE=1 \\
    CAO_HERDR_LIVE_PROFILE=<profile> CAO_HERDR_LIVE_PROVIDER=<provider> \\
        uv run pytest test/backends/test_herdr_live_smoke.py

The profile and provider have no default: a profile that falls back to the
default provider may not be herdr-detectable, and the smoke would then report a
false regression (herdr sends no event for an agent it does not recognise).
``CAO_TEST_REAL_HOME=1`` keeps the whole run under the real HOME so the agent
CLI stays logged in.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
import requests

from cli_agent_orchestrator.services import herdr_inbox_service as his

pytestmark = [
    # Real HOME at RUN time: the worker needs the agent CLI's real login. The
    # marker does not affect import-time paths (test/conftest.py:81-82), so the
    # profile store path is resolved inside the fixture, at run time.
    # CAO_HOME_DIR stays redirected, so the live DB is safe.
    pytest.mark.real_home,
    # The pytest process runs under real HOME (above) so MEMORY_BASE_DIR
    # resolves there; acknowledge it explicitly. This test does no memory I/O in
    # the pytest process — the cao-server subprocess uses its own temp
    # CAO_HOME_DIR — so nothing is written under the real home.
    pytest.mark.real_home_memory,
    pytest.mark.skipif(
        not os.environ.get("CAO_HERDR_LIVE"),
        reason="live herdr smoke is opt-in; set CAO_HERDR_LIVE=1 with a real agent available",
    ),
    pytest.mark.skipif(shutil.which("herdr") is None, reason="herdr binary not installed"),
    # No personal defaults: the profile must name a herdr-detectable agent and
    # the provider must be sent explicitly, or the server's kiro_cli fallback
    # can produce a worker herdr cannot see (false "#1378 regression").
    pytest.mark.skipif(
        not os.environ.get("CAO_HERDR_LIVE_PROFILE"),
        reason="set CAO_HERDR_LIVE_PROFILE to a profile backed by a herdr-detectable agent",
    ),
    pytest.mark.skipif(
        not os.environ.get("CAO_HERDR_LIVE_PROVIDER"),
        reason="set CAO_HERDR_LIVE_PROVIDER (e.g. claude_code) so the worker is not the kiro_cli default",
    ),
]

_PROFILE = os.environ.get("CAO_HERDR_LIVE_PROFILE", "")
_PROVIDER = os.environ.get("CAO_HERDR_LIVE_PROVIDER", "")
# Reconcile eligibility is INBOX_RECONCILE_INTERVAL + grace (~60s); the event
# path must deliver well inside that. The trigger tag is the real proof, not the
# timing, but bound the wait so a dead event path fails fast rather than hanging.
_DELIVERY_DEADLINE = 55.0
_HEALTH_DEADLINE = 15.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _delivered_line(home: Path, terminal_id: str) -> str | None:
    """Return the first 'Delivered … to <terminal_id>' log line, or None."""
    logs = home / "logs"
    if not logs.is_dir():
        return None
    for log in sorted(logs.glob("cao_*.log")):
        with contextlib.suppress(OSError):
            for line in log.read_text(errors="replace").splitlines():
                if "Delivered" in line and terminal_id in line:
                    return line
    return None


@pytest.fixture
def herdr_server(tmp_path: Path):
    """An isolated cao-server on the herdr backend with the real agent store.

    Keeps the real ``$HOME`` so the agent CLI stays logged in, but isolates
    ``CAO_HOME_DIR`` and the herdr session so nothing touches the developer's
    live server (the hazard called out in the herdr testing notes).
    """
    # Run-time resolution: under the real_home marker, import-time constants
    # still point at the fake home. No existence precheck — the server reads
    # provider folders, extra folders and built-in profiles too, so only its
    # own "Agent profile not found" answer decides (mapped to a skip below).
    real_cao_home = Path.home() / ".aws" / "cli-agent-orchestrator"

    cao_home = tmp_path / "cao-home"
    cao_home.mkdir(parents=True)
    for sub in ("agent-store", "agent-context"):
        src = real_cao_home / sub
        if src.is_dir():
            shutil.copytree(src, cao_home / sub)

    session = f"cao-smoke-{uuid.uuid4().hex[:8]}"
    port = _free_port()
    env = os.environ.copy()
    env.update(
        {
            "CAO_HOME_DIR": str(cao_home),
            "CAO_API_HOST": "127.0.0.1",
            "CAO_API_PORT": str(port),
            "CAO_TERMINAL_BACKEND": "herdr",
            "CAO_HERDR_SESSION": session,
            # The immediate POST path must not deliver mid-turn, or the event
            # path is never exercised (and the delivered line carries no trigger).
            "CAO_EAGER_INBOX_DELIVERY": "false",
            "OTEL_SDK_DISABLED": "true",
            # _delivered_line reads an INFO record; a WARNING/ERROR level from
            # the developer's shell would hide it and fake a delivery failure.
            "CAO_LOG_LEVEL": "INFO",
            "PYTHONUNBUFFERED": "1",
        }
    )
    log = open(cao_home / "server.out", "ab")  # noqa: SIM115 — closed in teardown
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "cli_agent_orchestrator.api.main",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--terminal",
            "herdr",
        ],
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        end = time.monotonic() + _HEALTH_DEADLINE
        while time.monotonic() < end:
            if proc.poll() is not None:
                pytest.fail(f"cao-server exited {proc.returncode} during startup")
            with contextlib.suppress(requests.RequestException):
                if requests.get(f"{url}/health", timeout=0.5).status_code == 200:
                    break
            time.sleep(0.1)
        else:
            pytest.fail("cao-server did not become healthy")
        yield url, cao_home, session
    finally:
        with contextlib.suppress(ProcessLookupError, OSError):
            os.killpg(proc.pid, signal.SIGTERM)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
        with contextlib.suppress(ProcessLookupError, OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            log.close()
        with contextlib.suppress(Exception):
            subprocess.run(["herdr", "--session", session, "server", "stop"], timeout=10)


def test_inbox_message_delivers_via_the_event_path_not_reconcile(herdr_server):
    """A message queued while a real worker is busy is delivered by the herdr
    event path (herdr-event/herdr-settle), proving event-driven delivery is live
    end-to-end — the exact behaviour #1378 broke silently."""
    url, cao_home, _ = herdr_server

    resp = requests.post(
        f"{url}/sessions",
        params={"agent_profile": _PROFILE, "provider": _PROVIDER},
        timeout=120,
    )
    if resp.status_code >= 500 and any(
        m in resp.text.lower()
        for m in (
            "initialization timed out",
            "not installed",
            "command not found",
            "agent profile not found",
        )
    ):
        pytest.skip(f"agent profile {_PROFILE!r} not usable on this host: {resp.text[:200]}")
    assert resp.status_code in (
        200,
        201,
    ), f"POST /sessions failed: {resp.status_code} {resp.text[:300]}"
    terminal_id = resp.json()["id"]

    # Wait for the worker to finish initializing and reach a ready state BEFORE
    # queuing anything. Agent-CLI init can take 30-60s; if the message is queued
    # while the worker is still booting, the 30s reconcile grace can elapse and
    # deliver it before the worker ever reaches idle — which would fail this
    # assertion for a reason unrelated to the event path. Pin the worker warm
    # first so the test measures delivery-on-completion, not boot time.
    ready_end = time.monotonic() + 90.0
    while time.monotonic() < ready_end:
        st = requests.get(f"{url}/terminals/{terminal_id}", timeout=10)
        if st.status_code == 200 and st.json().get("status") in ("idle", "completed"):
            break
        time.sleep(1.0)
    else:
        pytest.skip("worker never reached a ready state within 90s; agent too slow to boot here")

    # The registration reconnect runs the settled-pane sweep, and for a pane
    # that is already idle it arms a settle-retry loop that tags deliveries
    # herdr-settle WITHOUT a live event (and while that loop runs, the arm from
    # a real event is dropped). Let that loop lapse before the task starts, so
    # a herdr-settle delivery below can only come from an event-armed loop.
    time.sleep((his._SETTLE_RETRY_TICKS + 2) * his._SETTLE_RETRY_INTERVAL)

    # Give the warm worker a task so it is busy, then queue an inbox message
    # while it is still working — the case that only the event path delivers
    # promptly (reconcile would take 30s+).
    requests.post(
        f"{url}/terminals/{terminal_id}/input",
        params={
            "message": "Run exactly this bash command: sleep 6. Then reply with exactly SMOKE-OK.",
            "sender_id": "smoke",
        },
        timeout=30,
    )
    # Wait until the worker has actually entered its turn before queuing. POST
    # /input returns right after dispatch, and on herdr the status read can keep
    # reporting the previous turn's COMPLETED for a while; an inbox message
    # posted in that window is delivered by the immediate POST path (no trigger
    # tag), and the event path is never exercised.
    busy_end = time.monotonic() + 30.0
    while time.monotonic() < busy_end:
        st = requests.get(f"{url}/terminals/{terminal_id}", timeout=10)
        if st.status_code == 200 and st.json().get("status") == "processing":
            break
        time.sleep(0.5)
    else:
        pytest.skip(
            "worker never reported processing after the task dispatch; "
            "cannot stage the busy-delivery case this smoke exists to measure"
        )
    requests.post(
        f"{url}/terminals/{terminal_id}/inbox/messages",
        params={"message": "SMOKE-INBOX: reply with exactly ACK-SMOKE.", "sender_id": "smoke"},
        timeout=30,
    )

    end = time.monotonic() + _DELIVERY_DEADLINE
    line = None
    while time.monotonic() < end:
        line = _delivered_line(cao_home, terminal_id)
        if line:
            break
        time.sleep(1.0)

    with contextlib.suppress(Exception):
        requests.post(f"{url}/terminals/{terminal_id}/exit", timeout=10)

    assert line is not None, (
        "inbox message was never delivered within the deadline — event-driven "
        "delivery is broken (it would otherwise fall to the 30s reconcile sweep)"
    )
    if "trigger:" not in line:
        pytest.skip(
            "the immediate POST-path delivery landed before the worker entered "
            "its turn, so the event path was never exercised (setup race, not "
            f"a #1378 regression). Line:\n{line}"
        )
    assert "trigger: herdr-event" in line or "trigger: herdr-settle" in line, (
        "message was delivered, but NOT via the herdr event path — a delivery "
        "carrying any other trigger means the event path did not fire, which is "
        f"the #1378 regression class. Line:\n{line}"
    )
