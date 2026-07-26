"""The v2 recovery surface over HTTP — the wire ``conduct spawn --recover`` drives.

The service-level behaviour is proven in
``test/services/test_v2_recovery_surface.py``; this suite proves the three
routes exist, validate their request models, and return the status codes a
recovering conductor branches on — including the one that matters most:
the presence of these routes is what tells the conductor the fork speaks
v2 recovery at all.  An old fork returns 404 for the whole path, which the
conductor must read as typed ``recovery-unsupported`` (preserve the run and
its breaker) rather than falling back to the v1 verbs.
"""

from __future__ import annotations

import hashlib
import subprocess
import uuid
from typing import Any

import pytest

from cli_agent_orchestrator.models.managed_launch_v2 import PROTOCOL_VERSION_V2
from cli_agent_orchestrator.services import managed_launch_v2 as v2

V2_ROOT = "/managed-launch/v2/reservations"


@pytest.fixture(autouse=True)
def _companion(tmp_path, monkeypatch):
    monkeypatch.setattr(v2, "COMPANION_DIR", tmp_path / "companion")


@pytest.fixture(autouse=True)
def _stub_native_teardown(monkeypatch):
    """The cleanup route now drives the generation-bound terminal teardown.

    This suite proves the wire contract (status codes, request validation),
    not tmux teardown, so the exact teardown is stubbed to a confirming
    no-op. The teardown contract itself is exercised in
    ``test_v2_cleanup_teardown.py``.
    """

    def _delete_terminal(
        terminal_id, *, registry=None, expected_generation=None, expected_session=None, **_
    ):
        return True

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.delete_terminal",
        _delete_terminal,
    )


@pytest.fixture
def worktree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def _reserve_payload(worktree, tmp_path, **changes) -> dict[str, Any]:
    executable = tmp_path / "fake-kimi"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    payload = {
        "protocol_version": PROTOCOL_VERSION_V2,
        "reservation_id": str(uuid.uuid4()),
        "session_name": "cao-test",
        "provider": "kimi_cli",
        "agent_profile": "reviewer",
        "caller_id": "deadbeef",
        "working_directory": str(worktree),
        "expected_model": "gpt-5.6-sol",
        "expected_effort": "xhigh",
        "provider_executable": str(executable),
        "provider_executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "obligation_generation": "obgen-7c2e4a1b",
        "task_id": "self-heal-demo-task",
        "run_id": "run-0001",
        "delivery_id": str(uuid.uuid4()),
        "launch_nonce": "n" * 40,
        "execution_mode": "native_tui",
        "worker_class": "persistent",
    }
    payload.update(changes)
    return payload


def _blocked_reservation(client, worktree, tmp_path) -> dict[str, Any]:
    payload = _reserve_payload(worktree, tmp_path)
    reserved = client.post(V2_ROOT, json=payload)
    assert reserved.status_code == 201, reserved.text
    record = reserved.json()
    v2._mark_preflight_blocked(
        record["reservation_id"],
        "native session bootstrap failed",
        reason=v2.PREFLIGHT_REASON_SESSION_BOOTSTRAP,
    )
    return record


def _negative_body(record, **changes) -> dict[str, Any]:
    body = {
        "protocol_version": PROTOCOL_VERSION_V2,
        "finalize_id": str(uuid.uuid4()),
        "terminal_id": record["terminal_id"],
        "generation": record["generation"],
        "obligation_generation": record["obligation_generation"],
        "reason": "conduct recover",
    }
    body.update(changes)
    return body


def _cleanup_body(record, **changes) -> dict[str, Any]:
    body = {
        "protocol_version": PROTOCOL_VERSION_V2,
        "cleanup_id": str(uuid.uuid4()),
        "terminal_id": record["terminal_id"],
        "generation": record["generation"],
    }
    body.update(changes)
    return body


def test_get_surfaces_the_preflight_failure_envelope(
    client, isolated_memory_db, worktree, tmp_path
):
    record = _blocked_reservation(client, worktree, tmp_path)
    got = client.get(f"{V2_ROOT}/{record['reservation_id']}")
    assert got.status_code == 200, got.text
    env = got.json()["preflight_failure"]
    assert env["schema"] == "cao-managed-launch-v2-preflight-failure-v1"
    assert env["task_bytes_submitted"] is False


def test_negative_finalizes_over_the_wire(client, isolated_memory_db, worktree, tmp_path):
    record = _blocked_reservation(client, worktree, tmp_path)
    resp = client.post(
        f"{V2_ROOT}/{record['reservation_id']}/negative", json=_negative_body(record)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "negative"


def test_negative_is_idempotent_over_the_wire(client, isolated_memory_db, worktree, tmp_path):
    record = _blocked_reservation(client, worktree, tmp_path)
    body = _negative_body(record)
    first = client.post(f"{V2_ROOT}/{record['reservation_id']}/negative", json=body)
    again = client.post(f"{V2_ROOT}/{record['reservation_id']}/negative", json=body)
    assert first.status_code == again.status_code == 200
    assert first.json()["admission"] == again.json()["admission"]


def test_negative_wrong_identity_is_409(client, isolated_memory_db, worktree, tmp_path):
    record = _blocked_reservation(client, worktree, tmp_path)
    resp = client.post(
        f"{V2_ROOT}/{record['reservation_id']}/negative",
        json=_negative_body(record, generation=str(uuid.uuid4())),
    )
    assert resp.status_code == 409, resp.text


def test_negative_unknown_reservation_is_404(client, isolated_memory_db, worktree, tmp_path):
    # A truly-unknown reservation is 404 — the same status an old fork
    # without the route returns, which the conductor reads as typed
    # recovery-unsupported rather than a v1 fallback.
    record = _blocked_reservation(client, worktree, tmp_path)
    resp = client.post(f"{V2_ROOT}/{uuid.uuid4()}/negative", json=_negative_body(record))
    assert resp.status_code == 404, resp.text


def test_negative_malformed_body_is_422(client, isolated_memory_db, worktree, tmp_path):
    record = _blocked_reservation(client, worktree, tmp_path)
    resp = client.post(
        f"{V2_ROOT}/{record['reservation_id']}/negative",
        json=_negative_body(record, terminal_id="NOT-HEX"),
    )
    assert resp.status_code == 422, resp.text


def test_reconcile_over_the_wire(client, isolated_memory_db, worktree, tmp_path):
    record = _blocked_reservation(client, worktree, tmp_path)
    resp = client.post(f"{V2_ROOT}/{record['reservation_id']}/reconcile")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["recovery_only"] is True
    assert body["terminal_record_present"] is False


def test_cleanup_over_the_wire(client, isolated_memory_db, worktree, tmp_path):
    record = _blocked_reservation(client, worktree, tmp_path)
    client.post(f"{V2_ROOT}/{record['reservation_id']}/negative", json=_negative_body(record))
    resp = client.post(f"{V2_ROOT}/{record['reservation_id']}/cleanup", json=_cleanup_body(record))
    assert resp.status_code == 200, resp.text
    assert "cleanup" in resp.json()


def test_cleanup_before_finalization_is_409(client, isolated_memory_db, worktree, tmp_path):
    record = _blocked_reservation(client, worktree, tmp_path)
    resp = client.post(f"{V2_ROOT}/{record['reservation_id']}/cleanup", json=_cleanup_body(record))
    assert resp.status_code == 409, resp.text


def test_cleanup_route_forwards_the_live_plugin_registry(
    client, isolated_memory_db, worktree, tmp_path, monkeypatch
):
    """The teardown must run with the endpoint's live registry, not None.

    Normal terminal teardown dispatches plugin events (post_kill_terminal)
    and resource cleanup through the registry; passing None would silently
    skip them, so a cleaned generation would leave the same orphans the row
    -only cleanup did.
    """
    from cli_agent_orchestrator.api.main import app

    record = _blocked_reservation(client, worktree, tmp_path)
    client.post(f"{V2_ROOT}/{record['reservation_id']}/negative", json=_negative_body(record))

    seen = {}

    def _record(terminal_id, *, registry=None, **_):
        seen["registry"] = registry
        return True

    monkeypatch.setattr("cli_agent_orchestrator.services.terminal_service.delete_terminal", _record)

    resp = client.post(f"{V2_ROOT}/{record['reservation_id']}/cleanup", json=_cleanup_body(record))
    assert resp.status_code == 200, resp.text
    assert seen["registry"] is app.state.plugin_registry
