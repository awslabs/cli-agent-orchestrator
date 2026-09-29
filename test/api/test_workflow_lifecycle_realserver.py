"""THE CROSS-PROCESS COMPOSITION GUARD for the async workflow-run lifecycle (#505).

This is the real-HTTP sibling of ``test/api/test_workflow_lifecycle_integration.py``.
That module drives the REAL assembled ASGI app in-process via Starlette's
``TestClient`` (persistent-portal loop + real temp SQLite journal + real YAML/script
engine) and is the fast, load-bearing, per-assertion coverage. It STAYS. But it
carries a DISCLOSED limitation, stated in its own docstring: the in-process transport
"CANNOT prove true cross-OS-process durability ... nor the real ``cao`` CLI /
``cao-mcp-server`` clients (which speak real ``requests`` to a listening socket the
in-process transport does not expose)."

THIS module closes exactly that disclosed gap. It composes the finished U1-U9 lifecycle
over a REAL ``cao-server`` subprocess — a genuine HTTP seam over real localhost sockets —
using the shipped session-scoped ``cao_server`` fixture (``test/fixtures/cao_server.py``,
registered as a pytest plugin in ``test/conftest.py``). Every request below crosses a
process boundary via ``requests`` to ``{cao_server.url}/...``; the assertions that a run
is durable-before-ack and answerable-detached are therefore proven ACROSS PROCESSES
(the submitter is THIS test process; the journal + drive live in the subprocess), which
is the one thing the TestClient guard structurally could not assert.

TEST VEHICLE -- the no-provider tier, disclosed per the honesty standard
========================================================================
The bare ``cao-server`` subprocess has NO provider CLIs (claude/codex/kiro-cli) and no
usable tmux agent substrate, so a YAML workflow's agent steps cannot drive to a terminal
state there. We therefore compose against the SCRIPT tier, which needs no provider: a
self-contained ``.py`` spec that prints the ``CAO_WORKFLOW_OUTPUT:`` sentinel and exits 0
drives to COMPLETED through the REAL ``script_runner`` subprocess spawned+reaped by the
server process. This is the exact vehicle the shipped ``test/e2e/script_runner`` e2e
suite uses for its no-provider OS-touching proofs. The spec is written into the server's
own ``WORKFLOW_SPEC_DIR`` (under the fixture's isolated ``$HOME``) and resolved by bare
name over HTTP, so the subprocess reads it off its own disk exactly as a real caller's
would be.

DISCLOSED (never a passing stub):
- The drive-to-COMPLETED-over-real-HTTP assertion is proven for the SCRIPT tier only.
  The YAML/agent tier cannot complete in a bare subprocess (no provider CLI, no tmux);
  its full-drive proof remains owned by the provider-gated ``test/e2e`` suite and the
  in-process TestClient guard (which stubs only the ``run_agent_step`` leaf). See the
  ``test_yaml_tier_drive_to_completed_needs_provider_env`` skip below -- an explicit
  skip-with-reason, not a green stub that would pretend the assertion ran.
- The AA-3 "CLI Ctrl-C detaches, does not cancel" behavior is a property of the ``cao``
  CLI follower process, not the server; it stays owned by the CLI unit/e2e tests.

MARKER -- deliberate, disclosed deviation from the fixture self-test
====================================================================
``test/fixtures/test_cao_server.py`` (the fixture's own self-test) marks its
real-subprocess cases ``@pytest.mark.e2e``. We deliberately use ``@pytest.mark.integration``
instead, for a load-bearing reason: the repo's default ``addopts`` (pyproject.toml) and
its CI unit job both run ``-m 'not e2e'``, which DESELECTS every ``e2e``-marked test
(verified: an ``e2e`` file collects 0 tests under the default invocation). An ``e2e`` mark
here would make this guard silently absent from build-and-test -- the precise #516 failure
mode (green checks accompanying an unexercised feature) that #505's composition guards
exist to prevent. ``integration``-marked tests DO run under ``-m 'not e2e'`` (so they run
in CI build-and-test) yet are skipped by the documented fast unit loop
(``-m 'not integration'``, DEVELOPMENT.md), which is exactly the "don't slow every unit
run, but still RUN in build-and-test" contract this suite needs. These tests spawn a
subprocess, so they belong out of the innermost loop but inside the merge gate.

Style: pytest parity with ``test/api/``; black + isort (line 100). Additive, test-only.
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from test.fixtures.cao_server import CaoServer
from typing import Optional

import pytest
import requests

from cli_agent_orchestrator.constants import CAO_HOME_DIR, WORKFLOW_SPEC_DIR

pytestmark = pytest.mark.integration

# Poll budget for driving a run to terminal over a real socket. The script sleeps
# ~0.2s then prints the sentinel; the reaper settles it shortly after. Generous
# ceiling so a loaded CI box never flakes, but the loop breaks the instant the run
# is terminal so the happy path is fast.
_POLL_ATTEMPTS = 300
_POLL_SLEEP = 0.1
_HTTP_TIMEOUT = 5.0

# A self-contained script-tier spec: no provider, no run-step callback, no disallowed
# import (only stdlib ``time``/``json`` -- ``time`` is a lint WARNING, never an ERROR,
# so the pre-spawn lint gate passes). It prints the run-level sentinel and exits 0, so
# the REAL script_runner subprocess drives it to COMPLETED. Mirrors the vehicle in
# test/e2e/script_runner/test_script_runner_e2e.py::test_real_spawn_completes_with_sentinel.
_SCRIPT_FAST = (
    "import time, json\n"
    "time.sleep(0.2)\n"
    'print("CAO_WORKFLOW_OUTPUT:" + json.dumps({"done": True}))\n'
)

# A long-running script that stays RUNNING well past interrupt latency, so a cancel
# lands while the run is still live (the AA-3/AA-5 assumption) rather than racing a
# just-completed run into a 409.
_SCRIPT_LONG = "import time\ntime.sleep(120)\n"


# ---------------------------------------------------------------------------
# Real-HTTP helpers -- every call crosses the process boundary over a socket.
# ---------------------------------------------------------------------------
def _spec_dir(server: CaoServer) -> Path:
    """The server subprocess's own ``WORKFLOW_SPEC_DIR`` under its isolated ``$HOME``.

    An explicit ``CAO_HOME_DIR`` is inherited unchanged by the subprocess and
    already reflected in the shared constant. Otherwise, map the constant's
    home-relative path under the fixture's redirected ``$HOME``. In either case,
    a spec written here lands on the SAME disk path the subprocess resolves.
    """
    if os.environ.get("CAO_HOME_DIR", "").strip():
        return WORKFLOW_SPEC_DIR
    # Fallback only when CAO_HOME_DIR is unset; constant is home-derived at import.
    return server.home_dir / WORKFLOW_SPEC_DIR.relative_to(CAO_HOME_DIR.parent.parent)


def _write_script_spec(server: CaoServer, source: str, name: str) -> str:
    """Write a ``.py`` script spec into the server's spec dir; return its bare name.

    The subprocess resolves ``name_or_path=<name>`` by rebuilding its file-backed
    index off this directory, so the run really loads a file the server process
    reads itself (not a path this test process fabricated in its own tmp).
    """
    spec_dir = _spec_dir(server)
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / f"{name}.py").write_text(source, encoding="utf-8")
    return name


def _submit(server: CaoServer, name_or_path: str, run_id: str) -> requests.Response:
    """POST the async submit route over real HTTP."""
    return requests.post(
        f"{server.url}/workflows/runs:submit",
        json={"name_or_path": name_or_path, "inputs": {}, "run_id": run_id},
        timeout=_HTTP_TIMEOUT,
    )


def _get_state(server: CaoServer, run_id: str) -> Optional[str]:
    """GET the status snapshot over real HTTP; return its ``state`` (or None on 404)."""
    resp = requests.get(f"{server.url}/workflows/runs/{run_id}", timeout=_HTTP_TIMEOUT)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json().get("state")


def _poll_terminal(server: CaoServer, run_id: str) -> Optional[str]:
    """Poll the status route over real HTTP until the run reaches a terminal state.

    Unlike the in-process guard, NOTHING here pumps a shared event loop: the drive
    runs autonomously inside the server process, so this is a genuine cross-process
    observation of a run advancing on its own.
    """
    final: Optional[str] = None
    for _ in range(_POLL_ATTEMPTS):
        final = _get_state(server, run_id)
        if final in ("completed", "failed", "cancelled"):
            return final
        time.sleep(_POLL_SLEEP)
    return final


def _wait_running(server: CaoServer, run_id: str) -> bool:
    """Wait until the run is observably RUNNING over real HTTP (so cancel is not a 409)."""
    for _ in range(_POLL_ATTEMPTS):
        if _get_state(server, run_id) == "running":
            return True
        time.sleep(_POLL_SLEEP)
    return False


def _rid(tag: str) -> str:
    """A unique, WORKFLOW_NAME_RE-legal run id (the session server is shared, so ids
    must not collide across tests -> a stray 409 on the admission gate)."""
    return f"rs-{tag}-{uuid.uuid4().hex[:8]}"


# ===========================================================================
# RS-1 (AA-1, IP-1): the composed happy path over REAL HTTP, cross-process.
# submit -> 202 + run_id + links -> IMMEDIATE durable-before-ack read over the
# socket -> autonomous drive to terminal -> full result. The immediate status
# read is the assertion the TestClient guard could not make: the id is durable
# and readable from ANOTHER process the instant the 202 lands.
# ===========================================================================
def test_composed_flow_over_real_http_script_tier(cao_server: CaoServer) -> None:
    name = _write_script_spec(cao_server, _SCRIPT_FAST, f"rs_fast_{uuid.uuid4().hex[:8]}")
    run_id = _rid("flow")

    resp = _submit(cao_server, name, run_id)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["run_id"] == run_id
    assert body["state"] == "running"
    # The links map is the composition contract the later steps consume (ADR-1).
    assert body["links"]["cancel"] == f"/workflows/runs/{run_id}/cancel"
    assert body["links"]["result"] == f"/workflows/runs/{run_id}/result"
    assert body["links"]["self"] == f"/workflows/runs/{run_id}"

    # AA-1 / IP-1, CROSS-PROCESS: the run is readable over the socket the INSTANT the
    # 202 returns -- proving the awaited durable insert committed before the ack, in a
    # real server, observed from a different process. (A 404 here would mean the ack
    # preceded durability -- the exact wired-but-inert failure #505 guards against.)
    status = requests.get(f"{cao_server.url}/workflows/runs/{run_id}", timeout=_HTTP_TIMEOUT)
    assert status.status_code == 200, f"durable-before-ack BROKEN over real HTTP: {status.text}"
    assert status.json()["run_id"] == run_id

    # The autonomous background drive advances the run to COMPLETED with no loop-pump
    # from this process (real script subprocess spawned+reaped inside the server).
    final = _poll_terminal(cao_server, run_id)
    assert final == "completed", f"expected completed, got {final}"

    # AA-6: the retained result assembles from durable journal state, fetched over HTTP.
    result = requests.get(f"{cao_server.url}/workflows/runs/{run_id}/result", timeout=_HTTP_TIMEOUT)
    assert result.status_code == 200, result.text
    result_body = result.json()
    assert result_body["state"] == "completed"
    assert result_body["run_id"] == run_id


# ===========================================================================
# RS-2 (AA-4, U4): run listing over REAL HTTP. The submitted run appears in the
# collection route; a legal state filter narrows it; an illegal filter is a 400
# (the state-legality check that lives at the REST boundary, LR-1).
# ===========================================================================
def test_run_listing_over_real_http(cao_server: CaoServer) -> None:
    name = _write_script_spec(cao_server, _SCRIPT_FAST, f"rs_list_{uuid.uuid4().hex[:8]}")
    run_id = _rid("list")

    assert _submit(cao_server, name, run_id).status_code == 202
    assert _poll_terminal(cao_server, run_id) == "completed"

    listed = requests.get(f"{cao_server.url}/workflows/runs", timeout=_HTTP_TIMEOUT)
    assert listed.status_code == 200
    rows = listed.json()
    assert isinstance(rows, list)
    assert any(r["run_id"] == run_id for r in rows), "submitted run absent from the list route"

    # State filter works over HTTP: the completed run is present under ?state=completed.
    completed = requests.get(
        f"{cao_server.url}/workflows/runs",
        params={"state": "completed"},
        timeout=_HTTP_TIMEOUT,
    )
    assert completed.status_code == 200
    assert any(r["run_id"] == run_id for r in completed.json())
    assert all(r["state"] == "completed" for r in completed.json())

    # A legal-but-unmatched filter is a 200 with the run absent (never a 404): cancelled
    # matches nothing here, and our run must not appear under it.
    cancelled = requests.get(
        f"{cao_server.url}/workflows/runs",
        params={"state": "cancelled"},
        timeout=_HTTP_TIMEOUT,
    )
    assert cancelled.status_code == 200
    assert not any(r["run_id"] == run_id for r in cancelled.json())

    # An illegal state filter is rejected at the boundary with a 400 (LR-1).
    bad = requests.get(
        f"{cao_server.url}/workflows/runs",
        params={"state": "not-a-real-state"},
        timeout=_HTTP_TIMEOUT,
    )
    assert bad.status_code == 400, bad.text


# ===========================================================================
# RS-3 (AA-3/AA-5, IP-2): cancel over REAL HTTP. A live long-running run is
# cancelled via the EXACT relative link the 202 handed back (round-tripped onto
# the server's base URL), reaches CANCELLED, and is answerable afterward -- all
# over the socket, cross-process.
# ===========================================================================
def test_cancel_over_real_http(cao_server: CaoServer) -> None:
    name = _write_script_spec(cao_server, _SCRIPT_LONG, f"rs_long_{uuid.uuid4().hex[:8]}")
    run_id = _rid("cancel")

    body = _submit(cao_server, name, run_id).json()
    assert _wait_running(cao_server, run_id), "run never observed RUNNING over HTTP"

    # Cancel via the 202's own ``links.cancel`` (relative) joined onto the real base URL.
    cancel_url = f"{cao_server.url}{body['links']['cancel']}"
    cancelled = requests.post(cancel_url, timeout=_HTTP_TIMEOUT)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["success"] is True

    final = _poll_terminal(cao_server, run_id)
    assert final == "cancelled", f"expected cancelled, got {final}"

    # Answerable after cancel: both status and result report CANCELLED over HTTP.
    assert _get_state(cao_server, run_id) == "cancelled"
    result = requests.get(f"{cao_server.url}/workflows/runs/{run_id}/result", timeout=_HTTP_TIMEOUT)
    assert result.status_code == 200
    assert result.json()["state"] == "cancelled"


# ===========================================================================
# RS-4 (U4, RO-1/RO-2, NFR-2a): the routing-shadow guard over REAL HTTP.
# GET /workflows/runs MUST resolve to the LIST handler (a real 200 JSON array),
# NOT the GET /workflows/{name} catch-all (which would treat "runs" as a name and
# 404). This proves the declaration-order route ordering holds in a real server,
# not merely in the in-process app object.
# ===========================================================================
def test_runs_list_not_shadowed_by_name_catchall_over_real_http(cao_server: CaoServer) -> None:
    resp = requests.get(f"{cao_server.url}/workflows/runs", timeout=_HTTP_TIMEOUT)
    # The list route answers with a JSON ARRAY. The catch-all would 404 on the
    # nonexistent workflow named "runs" -- so a 404 here is the shadowing regression.
    assert resp.status_code == 200, (
        "GET /workflows/runs was shadowed by the /workflows/{name} catch-all over real "
        f"HTTP (status {resp.status_code}: {resp.text})"
    )
    assert isinstance(resp.json(), list), "list route did not return a JSON array"

    # Contrast: a genuinely-unknown single-segment name DOES hit the catch-all and 404s,
    # confirming the array above came from the list handler, not a coincidental 200.
    unknown = requests.get(f"{cao_server.url}/workflows/{uuid.uuid4().hex}", timeout=_HTTP_TIMEOUT)
    assert unknown.status_code == 404, unknown.text


# ===========================================================================
# RS-5 (AA-4/AA-6, IP-2), CROSS-PROCESS DETACHED: the strongest assertion this
# module owns and the TestClient guard cannot. The TestClient "detach" simulates
# a gone submitter by clearing the in-process registry dict. HERE the submitter
# genuinely IS a different process: this test submits, then a FRESH client (new
# TCP connection, no shared state) reads status + result. A COMPLETED run must be
# fully answerable from the server's durable journal alone, over the socket.
# ===========================================================================
def test_detached_run_answerable_over_real_http(cao_server: CaoServer) -> None:
    name = _write_script_spec(cao_server, _SCRIPT_FAST, f"rs_detach_{uuid.uuid4().hex[:8]}")
    run_id = _rid("detach")

    assert _submit(cao_server, name, run_id).status_code == 202
    assert _poll_terminal(cao_server, run_id) == "completed"

    # A brand-new Session = a new connection with zero shared in-process state; the
    # only thing it can consult is the server's own durable journal across the socket.
    with requests.Session() as fresh:
        status = fresh.get(f"{cao_server.url}/workflows/runs/{run_id}", timeout=_HTTP_TIMEOUT)
        assert status.status_code == 200, "AA-4 BROKEN: detached status unanswerable over HTTP"
        assert status.json()["state"] == "completed"

        result = fresh.get(
            f"{cao_server.url}/workflows/runs/{run_id}/result", timeout=_HTTP_TIMEOUT
        )
        assert result.status_code == 200, "AA-6 BROKEN: detached result unanswerable over HTTP"
        assert result.json()["state"] == "completed"
        assert result.json()["run_id"] == run_id


# ===========================================================================
# RS-6 (error-path parity over REAL HTTP): an unknown run id is a 404 on BOTH the
# status and result routes over the socket, and an unknown workflow name is a 404
# on submit -- confirming the composed error mapping survives the real transport,
# not just the in-process app.
# ===========================================================================
def test_unknown_ids_404_over_real_http(cao_server: CaoServer) -> None:
    missing = _rid("missing")
    assert (
        requests.get(
            f"{cao_server.url}/workflows/runs/{missing}", timeout=_HTTP_TIMEOUT
        ).status_code
        == 404
    )
    assert (
        requests.get(
            f"{cao_server.url}/workflows/runs/{missing}/result", timeout=_HTTP_TIMEOUT
        ).status_code
        == 404
    )
    # Submit against a workflow name that does not exist on the server's disk -> 404.
    resp = _submit(cao_server, f"no_such_workflow_{uuid.uuid4().hex[:8]}", _rid("nospec"))
    assert resp.status_code == 404, resp.text


# ===========================================================================
# RS-7 (DISCLOSED DEFERRAL): the YAML/agent tier's drive-to-COMPLETED cannot be
# proven over a bare real subprocess -- it has no provider CLI and no tmux agent
# substrate, so an agent step never settles. This is an explicit skip-with-reason
# (NOT a green stub): the YAML full-drive proof is owned by the provider-gated
# test/e2e suite and by the in-process TestClient guard (which stubs only the
# run_agent_step leaf). The submit+durable-read+list+cancel+404 assertions above
# already exercise the YAML-agnostic composition over real HTTP; only the
# terminal-COMPLETED-of-an-agent-run assertion needs the provider e2e environment.
# ===========================================================================
@pytest.mark.skip(
    reason=(
        "YAML/agent-tier drive-to-COMPLETED needs a provider CLI + tmux substrate the "
        "bare cao-server subprocess lacks; owned by the provider-gated test/e2e suite "
        "and the in-process TestClient guard. Disclosed, never faked."
    )
)
def test_yaml_tier_drive_to_completed_needs_provider_env(  # pragma: no cover
    cao_server: CaoServer,
) -> None:
    """When run in the provider e2e environment, this would submit a YAML workflow
    over real HTTP and assert it drives to COMPLETED. Intentionally skipped here (not
    stubbed green) because the bare subprocess cannot run a real agent step."""
    raise AssertionError("requires the provider e2e environment (provider CLI + tmux)")
