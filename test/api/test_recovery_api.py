"""API tests for the recovery control-plane surfaces (v2, fence, destructive, capabilities)."""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid

import pytest

from cli_agent_orchestrator.constants import COMPANION_DIR as REAL_COMPANION_DIR
from cli_agent_orchestrator.services import generation_fence as gf
from cli_agent_orchestrator.services import managed_launch_v2 as v2
from cli_agent_orchestrator.services.destructive_endpoint import write_binding_record

NONCE = "n" * 40


@pytest.fixture(autouse=True)
def _companion(tmp_path, monkeypatch):
    monkeypatch.setattr(v2, "COMPANION_DIR", tmp_path / "companion")
    monkeypatch.setattr("cli_agent_orchestrator.constants.COMPANION_DIR", tmp_path / "companion")
    return tmp_path / "companion"


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


def _reserve_payload(worktree, tmp_path, **changes):
    executable = tmp_path / "fake-provider"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    payload = {
        "protocol_version": "cao-managed-launch-v2",
        "reservation_id": str(uuid.uuid4()),
        "session_name": "cao-test",
        "provider": "codex",
        "agent_profile": "reviewer-sol-max",
        "caller_id": "deadbeef",
        "working_directory": str(worktree),
        "trusted_project_root": str(worktree),
        "expected_model": "gpt-5.6-sol",
        "expected_effort": "xhigh",
        "provider_executable": str(executable),
        "provider_executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "obligation_generation": "obgen-7c2e4a1b",
        "task_id": "self-heal-demo-task",
        "run_id": "run-0001",
        "delivery_id": str(uuid.uuid4()),
        "launch_nonce": NONCE,
    }
    payload.update(changes)
    return payload


def test_recovery_capabilities_truthful(client, tmp_path, monkeypatch):
    # Hermetic: an empty CAO home means zero durable proofs.  Never read the
    # operator's real home — an installed binary with a valid proof there is
    # a truthful capability (COND-0317), not ambient state this test may
    # depend on.
    home = tmp_path / "cao-home"
    home.mkdir()
    monkeypatch.setattr("cli_agent_orchestrator.constants.CAO_HOME_DIR", home)
    response = client.get("/managed/recovery-capabilities")
    assert response.status_code == 200
    payload = response.json()
    assert payload["protocol"] == "cao-recovery-capabilities-v1"
    assert payload["containment"] == "unproven"
    assert payload["observed_route"] == {
        "codex": "unsupported",
        "claude": "unsupported",
        "kimi": "unproven",
    }
    assert payload["resume"]["kimi"]["identity_available"] is False
    assert payload["resource_registry_version"] == 1


def _kimi_acp_driver(session_id="session_abc"):
    """A driver proving one exact session id across new→kill→load."""

    def drive(_binary):
        return {
            "session_id": session_id,
            "resumed": True,
            "exchange": {
                "session_new_id": session_id,
                "killed": True,
                "session_load_id": session_id,
                "transcript_sha256": hashlib.sha256(b"acp-transcript").hexdigest(),
            },
        }

    return drive


def _homebrew_kimi(tmp_path, monkeypatch, *, version="kimi 0.33.0"):
    """A Homebrew-shaped install: PATH exposes a symlink to the canonical CLI."""
    import os
    import shutil

    from cli_agent_orchestrator.api import main as api_main

    canonical = tmp_path / "Cellar" / "kimi" / "0.33.0" / "dist" / "main.mjs"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("// kimi 0.33.0 bundle\n")
    link_dir = tmp_path / "homebrew-bin"
    link_dir.mkdir()
    link = link_dir / "kimi"
    link.symlink_to(canonical)
    assert os.path.realpath(link) == str(canonical) != str(link)
    home = tmp_path / "cao-home"
    home.mkdir()
    monkeypatch.setattr("cli_agent_orchestrator.constants.CAO_HOME_DIR", home)
    monkeypatch.setattr(shutil, "which", lambda name: str(link) if name == "kimi" else None)
    monkeypatch.setattr(
        api_main,
        "_provider_version_output",
        lambda binary: version if binary == "kimi" else None,
    )
    return home, canonical, link


def test_recovery_capabilities_consumes_kimi_proof_through_a_symlinked_path_executable(
    client, isolated_memory_db, tmp_path, monkeypatch
):
    """COND-0317 regression: the endpoint must resolve the PATH symlink to
    the same canonical absolute identity ``run_identity_proof`` recorded —
    on Homebrew ``which("kimi")`` is /opt/homebrew/bin/kimi, a symlink to
    the canonical dist/main.mjs, and the proof binds the canonical path."""
    from cli_agent_orchestrator.services import kimi_acp_proof as kap

    home, canonical, _ = _homebrew_kimi(tmp_path, monkeypatch)
    kap.run_identity_proof(
        kimi_binary=canonical,
        version_output="kimi 0.33.0",
        state_dir=home / "recovery",
        acp_driver=_kimi_acp_driver(),
    )

    payload = client.get("/managed/recovery-capabilities").json()

    assert payload["resume"]["kimi"]["identity_available"] is True


def test_recovery_capabilities_kimi_proof_fails_closed_on_a_dangling_symlink(
    client, isolated_memory_db, tmp_path, monkeypatch
):
    """A PATH symlink whose target is gone resolves to a nonexistent
    canonical path: the proof load fails closed — no capability, no error."""
    import os
    import shutil

    home, canonical, link = _homebrew_kimi(tmp_path, monkeypatch)
    canonical.unlink()  # the target vanishes after the link was published
    assert not os.path.exists(os.path.realpath(link))
    monkeypatch.setattr(shutil, "which", lambda name: str(link) if name == "kimi" else None)

    payload = client.get("/managed/recovery-capabilities").json()

    assert payload["resume"]["kimi"]["identity_available"] is False


def test_v2_reserve_query_roundtrip(client, isolated_memory_db, worktree, tmp_path):
    payload = _reserve_payload(worktree, tmp_path)
    response = client.post("/managed-launch/v2/reservations", json=payload)
    assert response.status_code == 201
    record = response.json()
    assert record["created"] is True
    assert record["protocol_vintage"] == "v2"
    assert record["launch_nonce_digest"] == hashlib.sha256(NONCE.encode()).hexdigest()
    assert "launch_nonce" not in record["request"]
    again = client.post("/managed-launch/v2/reservations", json=payload)
    assert again.status_code == 201
    assert again.json()["created"] is False
    fetched = client.get(f"/managed-launch/v2/reservations/{payload['reservation_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["state"] == "reserved"
    missing = client.get(f"/managed-launch/v2/reservations/{uuid.uuid4()}")
    assert missing.status_code == 404


def test_v2_wrong_protocol_version_422(client, isolated_memory_db, worktree, tmp_path):
    payload = _reserve_payload(worktree, tmp_path, protocol_version="cao-managed-launch-v1")
    response = client.post("/managed-launch/v2/reservations", json=payload)
    assert response.status_code == 422


def test_fence_install_and_outcomes(client, isolated_memory_db, worktree, tmp_path):
    payload = _reserve_payload(worktree, tmp_path)
    record = client.post("/managed-launch/v2/reservations", json=payload).json()
    fence_request = {
        "schema": "cao-w13-fence-req-v1",
        "terminal_id": record["terminal_id"],
        "terminal_generation": record["generation"],
        "obligation_generation": record["obligation_generation"],
        "attempt_id": str(uuid.uuid4()),
        "intent_id": str(uuid.uuid4()),
        "report_sha256": "a" * 64,
    }
    installed = client.post("/managed-launch/v2/fence", json=fence_request)
    assert installed.status_code == 200
    assert installed.json()["outcome"] == "fenced"
    assert installed.json()["fence_receipt_sha256"]
    again = client.post("/managed-launch/v2/fence", json=fence_request)
    assert again.json()["outcome"] == "already-fenced"
    assert again.json()["fence_receipt_sha256"] == installed.json()["fence_receipt_sha256"]
    # A generation unknown to the fork gets the truthful outcome.
    unknown = client.post(
        "/managed-launch/v2/fence",
        json={
            **fence_request,
            "terminal_generation": str(uuid.uuid4()),
            "intent_id": str(uuid.uuid4()),
        },
    )
    assert unknown.json()["outcome"] == "unknown-generation"


def test_fence_install_binds_body_identity_to_reservation_row(
    client, isolated_memory_db, worktree, tmp_path
):
    # FENCE durable regression: a fence body naming a different terminal
    # than the v2 generation's owner is never acknowledged — the truthful
    # outcome is unknown-generation and nothing is written under the
    # attacker-selected path; the row's own terminal drives the state path.
    from cli_agent_orchestrator.services.generation_fence import fence_state_path

    payload = _reserve_payload(worktree, tmp_path)
    record = client.post("/managed-launch/v2/reservations", json=payload).json()
    companion = tmp_path / "companion"
    fence_request = {
        "schema": "cao-w13-fence-req-v1",
        "terminal_id": "feedface",  # attacker-selected, not the row's terminal
        "terminal_generation": record["generation"],
        "obligation_generation": record["obligation_generation"],
        "attempt_id": str(uuid.uuid4()),
        "intent_id": str(uuid.uuid4()),
        "report_sha256": "a" * 64,
    }
    refused = client.post("/managed-launch/v2/fence", json=fence_request)
    assert refused.status_code == 200
    assert refused.json()["outcome"] == "unknown-generation"
    assert not fence_state_path(companion, "feedface", record["generation"]).exists()
    assert not fence_state_path(companion, record["terminal_id"], record["generation"]).exists()
    # A mismatched obligation generation conflicts (never acknowledged).
    mismatched = client.post(
        "/managed-launch/v2/fence",
        json={
            **fence_request,
            "terminal_id": record["terminal_id"],
            "obligation_generation": "other-obligation",
        },
    )
    assert mismatched.status_code == 409
    # The correct identity still fences, under the row's terminal path.
    correct = client.post(
        "/managed-launch/v2/fence",
        json={**fence_request, "terminal_id": record["terminal_id"]},
    )
    assert correct.json()["outcome"] == "fenced"
    assert fence_state_path(companion, record["terminal_id"], record["generation"]).exists()


def test_park_requires_the_exact_bound_reservation_identity(
    client, isolated_memory_db, worktree, tmp_path
):
    """No body field can bind a durable park receipt to a different task."""
    from cli_agent_orchestrator.clients import database
    from cli_agent_orchestrator.services import heartbeat_store

    record = client.post(
        "/managed-launch/v2/reservations", json=_reserve_payload(worktree, tmp_path)
    ).json()
    attempt_id = str(uuid.uuid4())
    token = heartbeat_store.issue_fencing_token(
        tmp_path / "companion", record["terminal_id"], record["generation"], attempt_id
    )
    with database.SessionLocal() as db:
        row = (
            db.query(database.ManagedLaunchV2ReservationModel)
            .filter_by(reservation_id=record["reservation_id"])
            .one()
        )
        row.binding_json = json.dumps({"attempt_id": attempt_id, "fencing_token_id": token.id})
        db.commit()
    body = {
        "schema": "cao-m3-park-req-v1",
        "operation_id": str(uuid.uuid4()),
        "reservation_id": record["reservation_id"],
        "terminal_id": record["terminal_id"],
        "terminal_generation": record["generation"],
        "logical_task_id": record["task_id"],
        "retained_round": 0,
        "obligation_generation": record["obligation_generation"],
        "attempt_id": attempt_id,
        "report_sha256": "a" * 64,
    }
    installed = client.post("/managed-launch/v2/park", json=body)
    assert installed.status_code == 200
    assert installed.json()["outcome"] == "fenced"
    assert (
        client.get(
            f"/managed-launch/v2/park/{record['terminal_id']}/{record['generation']}/{body['operation_id']}"
        ).json()["park_receipt_sha256"]
        == installed.json()["park_receipt_sha256"]
    )
    retry = client.post("/managed-launch/v2/park", json=body)
    assert retry.status_code == 200
    assert retry.json()["outcome"] == "already-fenced"
    assert retry.json()["park_receipt_sha256"] == installed.json()["park_receipt_sha256"]
    changed_same_operation = client.post(
        "/managed-launch/v2/park", json={**body, "retained_round": 1}
    )
    assert changed_same_operation.status_code == 409
    wrong_query = client.get(
        f"/managed-launch/v2/park/{record['terminal_id']}/{record['generation']}/{uuid.uuid4()}"
    )
    assert wrong_query.status_code == 200
    assert wrong_query.json()["outcome"] == "not-found"
    # Reconciliation is read-only, but its identifier segments must still be
    # exact/safe before they can name a filesystem location.
    malformed_query = client.get(
        f"/managed-launch/v2/park/{record['terminal_id']}/{record['generation']}/not-a-uuid"
    )
    assert malformed_query.status_code == 409
    invalid_terminal_query = client.get(
        f"/managed-launch/v2/park/A1B2C3D4/{record['generation']}/{body['operation_id']}"
    )
    assert invalid_terminal_query.status_code == 409
    wrong_terminal = client.post(
        "/managed-launch/v2/park", json={**body, "terminal_id": "feedface"}
    )
    assert wrong_terminal.status_code == 200
    assert wrong_terminal.json()["outcome"] == "unknown-generation"
    # A distinct operation cannot use the reservation to publish a receipt
    # for arbitrary logical task B, even though every other field is valid.
    mismatch = client.post(
        "/managed-launch/v2/park",
        json={**body, "operation_id": str(uuid.uuid4()), "logical_task_id": "other-task"},
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["detail"]

    # Every reservation-bound identity is authoritative; no alternate
    # obligation, attempt, or generation may consume/publish this receipt.
    assert (
        client.post(
            "/managed-launch/v2/park",
            json={**body, "operation_id": str(uuid.uuid4()), "obligation_generation": "wrong"},
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/managed-launch/v2/park",
            json={**body, "operation_id": str(uuid.uuid4()), "attempt_id": str(uuid.uuid4())},
        ).status_code
        == 409
    )
    wrong_generation = client.post(
        "/managed-launch/v2/park",
        json={**body, "operation_id": str(uuid.uuid4()), "terminal_generation": str(uuid.uuid4())},
    )
    assert wrong_generation.status_code == 200
    assert wrong_generation.json()["outcome"] == "unknown-generation"

    def reserve_park_body(*, binding: object | None, malformed: bool = False):
        other = client.post(
            "/managed-launch/v2/reservations", json=_reserve_payload(worktree, tmp_path)
        ).json()
        other_attempt = str(uuid.uuid4())
        if binding is not None:
            other_token = heartbeat_store.issue_fencing_token(
                tmp_path / "companion",
                other["terminal_id"],
                other["generation"],
                other_attempt,
            )
            stored_binding = (
                "{"
                if malformed
                else {"attempt_id": other_attempt, "fencing_token_id": other_token.id}
            )
            with database.SessionLocal() as db:
                row = (
                    db.query(database.ManagedLaunchV2ReservationModel)
                    .filter_by(reservation_id=other["reservation_id"])
                    .one()
                )
                row.binding_json = (
                    stored_binding
                    if isinstance(stored_binding, str)
                    else json.dumps(stored_binding)
                )
                db.commit()
        return other, {
            "schema": "cao-m3-park-req-v1",
            "operation_id": str(uuid.uuid4()),
            "reservation_id": other["reservation_id"],
            "terminal_id": other["terminal_id"],
            "terminal_generation": other["generation"],
            "logical_task_id": other["task_id"],
            "retained_round": 0,
            "obligation_generation": other["obligation_generation"],
            "attempt_id": other_attempt,
            "report_sha256": "b" * 64,
        }

    # Missing or unreadable durable bindings are typed conflicts, before any
    # attacker-controlled generation path can be written.
    _, unbound_body = reserve_park_body(binding=None)
    assert client.post("/managed-launch/v2/park", json=unbound_body).status_code == 409
    _, malformed_body = reserve_park_body(binding=True, malformed=True)
    assert client.post("/managed-launch/v2/park", json=malformed_body).status_code == 409

    # Stored-receipt adoption wins after a successor has issued.  Conversely,
    # a new operation for a distinct superseded generation cannot park it.
    heartbeat_store.issue_fencing_token(
        tmp_path / "companion", record["terminal_id"], str(uuid.uuid4()), str(uuid.uuid4())
    )
    adopted_after_successor = client.post("/managed-launch/v2/park", json=body)
    assert adopted_after_successor.status_code == 200
    assert adopted_after_successor.json()["outcome"] == "already-fenced"
    assert (
        adopted_after_successor.json()["park_receipt_sha256"]
        == installed.json()["park_receipt_sha256"]
    )

    fresh, fresh_body = reserve_park_body(binding=True)
    heartbeat_store.issue_fencing_token(
        tmp_path / "companion", fresh["terminal_id"], str(uuid.uuid4()), str(uuid.uuid4())
    )
    superseded = client.post("/managed-launch/v2/park", json=fresh_body)
    assert superseded.status_code == 200
    assert superseded.json()["outcome"] == "superseded-generation"
    assert superseded.json()["park_receipt"] is None


def test_destructive_endpoint_refusal_and_execution(
    client, isolated_memory_db, worktree, tmp_path, monkeypatch
):
    companion = tmp_path / "companion"
    reservation_id = str(uuid.uuid4())
    generation = str(uuid.uuid4())
    attempt_id = str(uuid.uuid4())
    write_binding_record(
        companion,
        terminal_id="a1b2c3d4",
        generation=generation,
        reservation_id=reservation_id,
        attempt_id=attempt_id,
        launch_nonce_digest="a" * 64,
        fencing_token_id="token-1",
        provider="codex",
        native_session_id="thr_1",
    )
    # The v2 reservation lookup drives the effect's session identity.
    monkeypatch.setattr(
        "cli_agent_orchestrator.api.main.managed_launch_v2.get",
        lambda rid: {"session_name": "cao-test"},
    )
    intent = {
        "intent_id": str(uuid.uuid4()),
        "kind": "terminal-teardown",
        "terminal_id": "a1b2c3d4",
        "generation": generation,
        "reservation_id": reservation_id,
        "attempt_id": attempt_id,
        "fencing_token_id": "token-1",
    }
    monkeypatch.setattr(
        "cli_agent_orchestrator.api.main.terminal_service.delete_terminal",
        lambda *a, **k: True,
    )
    # Containment is derived server-side by effect class; with the
    # composition unproven the teardown refuses — there is no request bit.
    refused = client.post("/managed/destructive", json=intent)
    assert refused.status_code == 409
    # With a proven composition AND the durable dual-exit proof, it runs.
    from cli_agent_orchestrator.services import containment
    from cli_agent_orchestrator.services.destructive_endpoint import write_dual_exit_proof

    class _ProvenComposition:
        def status(self):
            return "proven"

    monkeypatch.setattr(containment, "ContainmentComposition", _ProvenComposition)
    write_dual_exit_proof(
        companion,
        terminal_id="a1b2c3d4",
        generation=generation,
        reservation_id=reservation_id,
        attempt_id=attempt_id,
        fencing_token_id="token-1",
        provider_exit={"pid": 1, "exit_code": 0},
        bridge_exit={"pid": 2, "exit_code": 0},
    )
    executed = client.post("/managed/destructive", json=intent)
    assert executed.status_code == 200
    assert executed.json()["outcome"] == "completed"
    # Idempotent re-issue of the same intent id.
    assert client.post("/managed/destructive", json=intent).json() == executed.json()
    # Binding mismatch refuses with zero effect.
    mismatch = client.post(
        "/managed/destructive",
        json={**intent, "intent_id": str(uuid.uuid4()), "fencing_token_id": "wrong"},
    )
    assert mismatch.status_code == 409


def test_v1_surface_unaffected_by_v2(client):
    response = client.get("/managed-launch/capabilities")
    assert response.status_code == 200
    assert response.json()["protocol_version"] == "cao-managed-launch-v1"


def test_bare_delete_of_v2_row_is_refused(client, isolated_memory_db):
    """The ordinary DELETE route never tears down a v2 row by id alone."""
    from cli_agent_orchestrator.clients import database

    generation = str(uuid.uuid4())
    database.create_terminal_v2(
        "a1b2c3d4",
        "cao-test",
        "managed-a1b2c3d4-000000000000",
        "codex",
        generation=generation,
    )
    bare = client.delete("/terminals/a1b2c3d4")
    assert bare.status_code == 409
    assert "supply its exact generation and session" in bare.json()["detail"]
    # Zero mutation: the v2 row survives the unqualified attempt.
    assert database.get_terminal_metadata_v2("a1b2c3d4") is not None


def test_exact_v2_identity_retires_through_the_http_route(
    client, isolated_memory_db, monkeypatch, tmp_path
):
    """The production HTTP seam completes identity-bound v2 retirement."""
    from unittest.mock import MagicMock

    from cli_agent_orchestrator.clients import database
    from cli_agent_orchestrator.services import terminal_service as terminals

    generation = str(uuid.uuid4())
    window = terminals.managed_window_name("a1b2c3d4", generation)
    database.create_terminal_v2(
        "a1b2c3d4",
        "cao-test",
        window,
        "codex",
        generation=generation,
    )
    backend = MagicMock()
    backend.get_history.return_value = ""
    backend.get_pane_working_directory.return_value = str(tmp_path)
    backend.window_exists.return_value = False
    monkeypatch.setattr(terminals, "get_backend", lambda: backend)
    monkeypatch.setattr(terminals, "get_herdr_inbox_service", lambda: None)
    monkeypatch.setattr(terminals, "fifo_manager", MagicMock())
    monkeypatch.setattr(terminals, "status_monitor", MagicMock())
    monkeypatch.setattr(terminals, "provider_manager", MagicMock())
    monkeypatch.setattr(terminals, "TERMINAL_LOG_DIR", tmp_path)
    monkeypatch.setattr(terminals, "dispatch_plugin_event", lambda *args, **kwargs: None)
    deregister = MagicMock()
    monkeypatch.setattr(terminals, "_deregister_v2_terminal_resources", deregister)

    response = client.delete(
        f"/terminals/a1b2c3d4?expected_generation={generation}&expected_session=cao-test"
    )

    assert response.status_code == 200
    assert response.json()["success"] is True
    backend.kill_window.assert_called_once_with("cao-test", window)
    assert database.get_terminal_metadata_v2("a1b2c3d4") is None
    deregister.assert_called_once()


def _admitted_v2_reservation(tmp_path, monkeypatch):
    """A live admitted v2 reservation + its delivery journal + CAO home."""
    import json

    from cli_agent_orchestrator.clients import database
    from cli_agent_orchestrator.services.delivery_journal import DeliveryJournal

    home = tmp_path / "cao-home"
    home.mkdir()
    monkeypatch.setattr("cli_agent_orchestrator.constants.CAO_HOME_DIR", home)
    generation = str(uuid.uuid4())
    reservation_id = str(uuid.uuid4())
    request = {"expected_model": "gpt-5.6-sol", "expected_effort": "xhigh"}
    with database.SessionLocal() as db:
        db.add(
            database.ManagedLaunchV2ReservationModel(
                reservation_id=reservation_id,
                terminal_id="a1b2c3d4",
                generation=generation,
                protocol_vintage="v2",
                session_name="cao-test",
                provider="codex",
                agent_profile="reviewer-sol-max",
                caller_id="deadbeef",
                working_directory=str(tmp_path),
                obligation_generation="obgen-7c2e4a1b",
                run_id="run-0001",
                launch_nonce_digest="d" * 64,
                state="admitted",
                request_json=json.dumps(request),
                created_at="2026-07-24T00:00:00Z",
                updated_at="2026-07-24T00:00:00Z",
            )
        )
        db.commit()
    root = home / "managed-provider-sessions" / reservation_id
    journal = DeliveryJournal(root / "delivery-journal.db")
    command = {"op": "admit", "delivery_id": "delivery-1", "message": "run the task"}
    digest = hashlib.sha256(
        json.dumps(command, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    journal.open_intent("obgen-7c2e4a1b", "delivery-1", digest)
    journal.mark_terminal_queued("obgen-7c2e4a1b", "delivery-1")
    return home, generation, digest


def _write_codex_route_receipt(home, generation, digest, **changes):
    from cli_agent_orchestrator.services import route_receipts

    args = {
        "state_dir": home / "recovery",
        "provider": "codex",
        "native_session_id": "thr_0192a7b4",
        "native_turn_id": "turn-1",
        "generation": generation,
        "terminal_id": "a1b2c3d4",
        "delivery_id": "delivery-1",
        "expected_model": "gpt-5.6-sol",
        "expected_effort": "xhigh",
        "observed_model": "gpt-5.6-sol",
        "observed_effort": "xhigh",
        "protocol": "app-server/1",
        "event_sequence": 1,
        "model_input_digest": digest,
        "provider_version": "codex 0.146.0",
    }
    args.update(changes)
    return route_receipts.write_route_receipt(**args)


def test_recovery_capabilities_consumes_provider_route_receipts(
    client, isolated_memory_db, tmp_path, monkeypatch
):
    """cond-0069 closure: the production endpoint's route authority comes
    only from the provider-generated authenticated durable receipt."""
    from cli_agent_orchestrator.api import main as api_main

    home, generation, digest = _admitted_v2_reservation(tmp_path, monkeypatch)
    monkeypatch.setattr(
        api_main,
        "_provider_version_output",
        lambda binary: "codex 0.146.0" if binary == "codex" else None,
    )
    _write_codex_route_receipt(home, generation, digest)

    response = client.get("/managed/recovery-capabilities")

    assert response.status_code == 200
    payload = response.json()
    assert payload["observed_route"]["codex"] == "proven"
    assert payload["enabled_providers"] == ["codex"]
    # Containment is still unproven: automated paths stay closed regardless.
    assert payload["automated_paths"] == {
        "recovery": False,
        "finalization": False,
        "destructive": False,
    }


def test_recovery_capabilities_rejects_drifted_or_unjournaled_route_evidence(
    client, isolated_memory_db, tmp_path, monkeypatch
):
    """Malformed/drifted/missing receipt evidence exposes no authority."""
    from cli_agent_orchestrator.api import main as api_main

    home, generation, digest = _admitted_v2_reservation(tmp_path, monkeypatch)
    monkeypatch.setattr(
        api_main,
        "_provider_version_output",
        lambda binary: "codex 0.146.0" if binary == "codex" else None,
    )
    # An authenticated receipt whose model input was never journaled for
    # this generation is not authority.
    _write_codex_route_receipt(home, generation, "f" * 64)
    payload = client.get("/managed/recovery-capabilities").json()
    assert payload["observed_route"]["codex"] == "unsupported"
    assert payload["enabled_providers"] == []

    # A receipt valid at write time but tampered afterwards (HMAC/content
    # address broken) is not authority either.
    _write_codex_route_receipt(home, generation, digest)
    published = list((home / "recovery").glob("route-receipt.*.json"))
    assert published
    import json

    for candidate in published:
        receipt = json.loads(candidate.read_bytes())
        receipt["observed_model"] = "different-model"
        import os

        os.chmod(candidate, 0o600)  # published receipts are 0400 by design
        candidate.write_bytes(json.dumps(receipt, sort_keys=True).encode() + b"\n")
    payload = client.get("/managed/recovery-capabilities").json()
    assert payload["observed_route"]["codex"] == "unsupported"
    assert payload["enabled_providers"] == []
