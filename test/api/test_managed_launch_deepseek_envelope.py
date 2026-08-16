"""v1 reserve surface: the typed DeepSeek ACP provider-route envelope.

A conductor must negotiate ``deepseek_acp_route_envelope`` before
reservation; the surface accepts, validates, persists, and echoes only
the supported DeepSeek ACP envelope — path names and digests, never
credential bytes — and refuses everything else before any reservation
exists.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import uuid

from cli_agent_orchestrator.models.managed_launch import PROTOCOL_VERSION


def _envelope(tmp_path: pathlib.Path, *, model: str = "deepseek-v4-flash") -> dict:
    worktree = tmp_path / "worktree"
    worktree.mkdir(exist_ok=True)
    wrapper = tmp_path / "shims" / "claude"
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text('#!/bin/sh\nexec "$CAO_CONDUCTOR_REAL_CLAUDE" "$@"\n')
    wrapper.chmod(0o755)
    inner = tmp_path / "real-claude"
    inner.write_text("#!/bin/sh\necho 'claude 2.1.233'\n")
    inner.chmod(0o755)
    token = tmp_path / "deepseek-token.txt"
    token.write_text("sk-one-shot\n")
    token.chmod(0o600)
    marker = tmp_path / "deepseek-token.consumed"
    route_map = tmp_path / "deepseek-routes.json"
    route_map.write_text(
        json.dumps(
            {
                "routes": {
                    str(worktree): {
                        "route": "deepseek",
                        "model": model,
                        "token_path": str(token),
                        "consumed_path": str(marker),
                    }
                }
            }
        )
    )
    return {
        "wrapper_executable": str(wrapper),
        "wrapper_executable_sha256": hashlib.sha256(wrapper.read_bytes()).hexdigest(),
        "inner_executable": str(inner),
        "inner_executable_sha256": hashlib.sha256(inner.read_bytes()).hexdigest(),
        "route_map_path": str(route_map),
        "worktree_realpath": str(worktree),
        "token_path": str(token),
        "consumed_marker_path": str(marker),
    }


def _deepseek_reservation(tmp_path: pathlib.Path, **overrides: object) -> dict:
    envelope = _envelope(tmp_path)
    executable = pathlib.Path(envelope["inner_executable"])
    payload = {
        "protocol_version": PROTOCOL_VERSION,
        "reservation_id": str(uuid.uuid4()),
        "session_name": "cao-deepseek",
        "provider": "claude_code",
        "agent_profile": "implementer",
        "caller_id": "deadbeef",
        "project": "test-project",
        "task_id": "test-task",
        "delivery_id": str(uuid.uuid4()),
        "working_directory": envelope["worktree_realpath"],
        "expected_model": "deepseek-v4-flash",
        "expected_effort": "high",
        "provider_executable": str(executable),
        "provider_executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "provider_route": "deepseek",
        "route_envelope": envelope,
    }
    payload.update(overrides)
    return payload


class TestCapabilityAdvertisement:
    def test_deepseek_envelope_capability_is_advertised(self, client) -> None:
        response = client.get("/managed-launch/capabilities")
        assert response.status_code == 200
        capabilities = response.json()
        # A conductor must see this exact key before sending route fields:
        # an older fork ignores provider_route and would run Anthropic.
        assert capabilities["deepseek_acp_route_envelope"] is True


class TestReserveAcceptance:
    def test_valid_deepseek_envelope_is_persisted_and_echoed(self, client, tmp_path) -> None:
        payload = _deepseek_reservation(tmp_path)
        response = client.post("/managed-launch/reservations", json=payload)
        assert response.status_code == 201, response.text
        record = response.json()
        assert record["created"] is True
        assert record["request"]["provider_route"] == "deepseek"
        assert record["request"]["route_envelope"] == payload["route_envelope"]

        fetched = client.get(f"/managed-launch/reservations/{payload['reservation_id']}")
        assert fetched.status_code == 200
        assert fetched.json()["request"]["route_envelope"] == payload["route_envelope"]

    def test_idempotent_replay_returns_the_same_reservation(self, client, tmp_path) -> None:
        payload = _deepseek_reservation(tmp_path)
        first = client.post("/managed-launch/reservations", json=payload)
        assert first.status_code == 201
        second = client.post("/managed-launch/reservations", json=payload)
        assert second.status_code == 201
        assert second.json()["created"] is False
        assert second.json()["request"] == first.json()["request"]


class TestReserveRefusals:
    def test_deepseek_without_envelope_is_refused(self, client, tmp_path) -> None:
        payload = _deepseek_reservation(tmp_path)
        payload.pop("route_envelope")
        response = client.post("/managed-launch/reservations", json=payload)
        assert response.status_code == 409

    def test_anthropic_with_envelope_is_refused(self, client, tmp_path) -> None:
        envelope = _envelope(tmp_path)
        executable = pathlib.Path(envelope["inner_executable"])
        payload = {
            "protocol_version": PROTOCOL_VERSION,
            "reservation_id": str(uuid.uuid4()),
            "session_name": "cao-anthropic",
            "provider": "claude_code",
            "agent_profile": "implementer",
            "caller_id": "deadbeef",
            "project": "test-project",
            "task_id": "test-task",
            "delivery_id": str(uuid.uuid4()),
            "working_directory": envelope["worktree_realpath"],
            "expected_model": "claude-3-7-sonnet-20250219",
            "expected_effort": "high",
            "provider_executable": str(executable),
            "provider_executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
            "provider_route": "anthropic",
            "route_envelope": envelope,
        }
        response = client.post("/managed-launch/reservations", json=payload)
        assert response.status_code == 409

    def test_drifted_wrapper_digest_is_refused(self, client, tmp_path) -> None:
        payload = _deepseek_reservation(tmp_path)
        payload["route_envelope"]["wrapper_executable_sha256"] = "0" * 64
        response = client.post("/managed-launch/reservations", json=payload)
        assert response.status_code == 409

    def test_route_map_model_mismatch_is_refused(self, client, tmp_path) -> None:
        payload = _deepseek_reservation(tmp_path)
        # Overwrite the route map (same paths) with a different model AFTER
        # the payload was built: the envelope's own digests still match the
        # files, but the route map now disagrees with the reservation.
        envelope = _envelope(tmp_path, model="deepseek-v4-pro")
        payload["route_envelope"] = envelope
        response = client.post("/managed-launch/reservations", json=payload)
        assert response.status_code == 409

    def test_non_claude_provider_is_refused(self, client, tmp_path) -> None:
        payload = _deepseek_reservation(tmp_path)
        payload["provider"] = "codex"
        response = client.post("/managed-launch/reservations", json=payload)
        assert response.status_code == 409

    def test_unpinnable_model_is_refused(self, client, tmp_path) -> None:
        payload = _deepseek_reservation(tmp_path)
        payload["expected_model"] = "deepseek-chat"
        response = client.post("/managed-launch/reservations", json=payload)
        assert response.status_code == 409

    def test_replay_with_changed_envelope_conflicts(self, client, tmp_path) -> None:
        payload = _deepseek_reservation(tmp_path)
        first = client.post("/managed-launch/reservations", json=payload)
        assert first.status_code == 201
        changed = dict(payload)
        changed["route_envelope"] = dict(payload["route_envelope"])
        changed["route_envelope"]["consumed_marker_path"] = str(tmp_path / "other.consumed")
        second = client.post("/managed-launch/reservations", json=changed)
        assert second.status_code == 409
