from __future__ import annotations

import hashlib
import uuid

from cli_agent_orchestrator.models.managed_launch import PROTOCOL_VERSION
from cli_agent_orchestrator.services import managed_launch
from cli_agent_orchestrator.services import managed_launch_v2 as v2
from cli_agent_orchestrator.services import muse_native_launch


def _reservation(tmp_path):
    # P1-9 (final conformance §20.2f): managed reservations pin the provider
    # executable's absolute canonical path + digest.
    executable = tmp_path / "fake-provider"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    return {
        "protocol_version": PROTOCOL_VERSION,
        "reservation_id": str(uuid.uuid4()),
        "session_name": "cao-test",
        "provider": "codex",
        "agent_profile": "reviewer-sol-max",
        "caller_id": "deadbeef",
        "project": "test-project",
        "task_id": "test-task",
        "delivery_id": str(uuid.uuid4()),
        "working_directory": str(tmp_path),
        "trusted_project_root": str(tmp_path),
        "expected_model": "gpt-5.6-sol",
        "expected_effort": "xhigh",
        "provider_executable": str(executable),
        "provider_executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
    }


def _evidence(record, kind):
    return {
        "protocol_version": PROTOCOL_VERSION,
        "observation_id": str(uuid.uuid4()),
        "kind": kind,
        "terminal_id": record["terminal_id"],
        "generation": record["generation"],
        "provider": record["provider"],
        "agent_profile": record["agent_profile"],
        "model": record["request"]["expected_model"],
        "effort": record["request"]["expected_effort"],
        "evidence_digest": hashlib.sha256(kind.encode()).hexdigest(),
    }


def _expected_native_tui_providers():
    """The expected native_tui provider blocks, version facts derived.

    The static identity facts (id source, readiness receipt kind,
    executable) are pinned literally — they are the wiring.  The version
    facts are derived from the contract tables in both directions: a
    hand-maintained copy frozen beside the derived advertisement is a gate
    that decays, and deriving the baseline makes a drift on either side
    fail here.
    """
    from cli_agent_orchestrator.services import provider_contracts as contracts

    def version_facts(executable):
        return {
            "pinned_version": contracts.PINNED_VERSIONS[executable],
            "supported_versions": list(contracts.SUPPORTED_VERSIONS[executable]),
            "version_enforcement": contracts.version_enforcement_mode(executable),
        }

    return {
        "codex": {
            "supported": True,
            "id_source": "app_server_thread_start",
            "readiness_receipt_kind": "codex-native-thread-start",
            "executable": "codex",
            **version_facts("codex"),
        },
        "claude_code": {
            "supported": True,
            "id_source": "cli_session_id",
            "readiness_receipt_kind": "claude-native-session-start",
            # The executable, which is a different namespace from
            # the canonical provider key this map is keyed by.
            "executable": "claude",
            **version_facts("claude"),
        },
        "kimi_cli": {
            "supported": True,
            "id_source": "acp_session_new",
            "readiness_receipt_kind": "kimi-native-tui-attached",
            "executable": "kimi",
            **version_facts("kimi"),
        },
        "muse_cli": {
            "supported": True,
            "id_source": "provider_status_discovered",
            "readiness_receipt_kind": "muse-native-status-idle",
            "executable": "muse",
            **version_facts("muse"),
            "profile_carrier_capability": (muse_native_launch.PROFILE_CARRIER_CAPABILITY_CELL),
            "profile_carrier_inner_sha256": (
                "4290bfafa5bbb81a6fd493aaea12f848c789b1d22edfa0c4b849151deba3e70c"
            ),
        },
    }


def test_capability_handshake_is_exact_and_versioned(client, monkeypatch):
    accepted_carrier = muse_native_launch.MuseProfileCarrierCapability(
        True,
        "",
        cell=muse_native_launch.PROFILE_CARRIER_CAPABILITY_CELL,
        full_banner="Muse Code 0.1.0 (0.1.0-R708.1)",
        inner_executable="/fixture/muse-bin-0.1.0-R708.1",
        inner_executable_sha256="4290bfafa5bbb81a6fd493aaea12f848c789b1d22edfa0c4b849151deba3e70c",
    )
    monkeypatch.setattr(
        v2.muse_native_launch,
        "installed_profile_carrier_capability",
        lambda: accepted_carrier,
    )
    response = client.get("/managed-launch/capabilities")
    assert response.status_code == 200
    assert response.json() == {
        "protocol_version": PROTOCOL_VERSION,
        "reservation_query": True,
        "reservation_reconcile": True,
        "no_task_launch": True,
        "generation_bound_readiness": True,
        "idempotent_task_admission": True,
        "generation_bound_negative": True,
        "generation_bound_cancel": True,
        "generation_bound_cleanup": True,
        "provider_submission_receipt": True,
        "provider_native_exact_session_receipts": True,
        "zero_task_route_attestation": True,
        "pinned_provider_executable": True,
        "reservation_bound_delivery_id": True,
        "provider_bound_bridge_environment": True,
        "bridge_environment_inventory": "names-only-sha256",
        "post_allocation_bridge_failure_finalization": True,
        "launch_failure_evidence_schema": "cao-managed-bridge-launch-failure-v1",
        # The one transient bind refusal, advertised so a new consumer can
        # negotiate it against an old peer rather than assume it. Additive:
        # a consumer that ignores both keys behaves exactly as before,
        # which is why this carries no protocol-version bump.
        "native_bind_not_ready_status": 425,
        "native_bind_not_ready_reason": "bind-bridge-not-durably-ready",
        "trusted_project_root_providers": ["codex"],
        "readiness_providers": ["claude_code", "codex", "kimi_cli"],
        "execution_mode_selection": True,
        "glm_route_envelope": True,
        "deepseek_acp_route_envelope": True,
        # Only the modes this surface can actually run. Native TUI is
        # absent until a native launch branch exists, so a consumer that
        # gates a native claim on this list is fail-closed by default.
        "execution_modes": ["acp"],
        # The v2 surface reserves any resolvable mode but launches
        # only what it has a branch for, so the two permissions are
        # advertised separately.
        "v2_launchable_execution_modes": ["acp", "native_tui"],
        # Additive and per-provider. The flat mode list above cannot say
        # *which* providers have a native adapter, and relaxing it to mean
        # "all of them" would advertise a launch for providers with no
        # branch. A consumer must satisfy both gates, from this one
        # response: native_tui in the launchable modes AND
        # providers[<provider>].supported. An older peer omits this key
        # entirely, which reads as unsupported.
        "native_tui": {
            "schema_version": 1,
            "providers": _expected_native_tui_providers(),
        },
    }


def test_advertised_execution_modes_are_the_surface_support_set(client):
    """The advertisement is read from the support set, not restated.

    A hand-maintained second copy of this list is the failure mode worth
    preventing: it would keep advertising a mode after the branch behind
    it was removed, or fail to advertise one that had landed, and a
    consumer trusting the handshake has no way to detect either.
    """
    advertised = client.get("/managed-launch/capabilities").json()["execution_modes"]

    assert advertised == list(managed_launch.SUPPORTED_EXECUTION_MODES)


def test_advertisement_follows_the_support_set_when_it_widens(client, monkeypatch):
    """Adding a launch branch changes the handshake with no edit here."""
    monkeypatch.setattr(managed_launch, "SUPPORTED_EXECUTION_MODES", ("acp", "native_tui"))

    advertised = client.get("/managed-launch/capabilities").json()["execution_modes"]

    assert advertised == ["acp", "native_tui"]


def test_v2_launchable_modes_are_read_from_the_v2_surface(client, monkeypatch):
    """v2 publishes what it can launch, independently of what v1 supports.

    Kept distinct because the two surfaces gain their native branches at
    different times; a consumer reading one for the other would be
    fail-open for whichever lands second.
    """
    from cli_agent_orchestrator.services import managed_launch_v2

    body = client.get("/managed-launch/capabilities").json()
    assert body["v2_launchable_execution_modes"] == list(
        managed_launch_v2.LAUNCHABLE_EXECUTION_MODES
    )

    monkeypatch.setattr(managed_launch_v2, "LAUNCHABLE_EXECUTION_MODES", ("acp",))
    narrowed = client.get("/managed-launch/capabilities").json()
    assert narrowed["v2_launchable_execution_modes"] == ["acp"]
    # The two surfaces advertise independently: v2 carries a native
    # launch branch and v1 does not, so the handshake must be able to
    # show one without the other.
    assert narrowed["execution_modes"] == ["acp"]


def test_reserve_query_reconcile_and_cancel_round_trip(client, isolated_memory_db, tmp_path):
    payload = _reservation(tmp_path)
    created = client.post("/managed-launch/reservations", json=payload)
    assert created.status_code == 201
    record = created.json()
    assert record["created"] is True

    duplicate = client.post("/managed-launch/reservations", json=payload)
    assert duplicate.status_code == 201
    assert duplicate.json()["created"] is False
    assert duplicate.json()["generation"] == record["generation"]

    queried = client.get(f"/managed-launch/reservations/{payload['reservation_id']}")
    assert queried.status_code == 200
    assert queried.json()["generation"] == record["generation"]

    reconciled = client.post(f"/managed-launch/reservations/{payload['reservation_id']}/reconcile")
    assert reconciled.status_code == 200
    assert reconciled.json()["recovery_only"] is False

    cancelled = client.post(
        f"/managed-launch/reservations/{payload['reservation_id']}/cancel",
        json=_evidence(record, "cancelled"),
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"


def test_negative_and_cancel_endpoints_reject_wrong_kind(client, isolated_memory_db, tmp_path):
    payload = _reservation(tmp_path)
    record = client.post("/managed-launch/reservations", json=payload).json()
    response = client.post(
        f"/managed-launch/reservations/{payload['reservation_id']}/negative",
        json=_evidence(record, "cancelled"),
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "endpoint requires kind=negative"


def test_protocol_version_mismatch_fails_closed(client, isolated_memory_db, tmp_path):
    payload = _reservation(tmp_path)
    payload["protocol_version"] = "cao-managed-launch-v0"
    response = client.post("/managed-launch/reservations", json=payload)
    assert response.status_code == 422
