"""Tests for Claude hooks, the Kimi ACP proof, and the old-binary rig."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from cli_agent_orchestrator.services import claude_hooks as ch
from cli_agent_orchestrator.services import generation_fence as gf
from cli_agent_orchestrator.services import heartbeat_store as hb
from cli_agent_orchestrator.services import kimi_acp_proof as kap
from cli_agent_orchestrator.services.old_binary_rig import (
    AccessLog,
    OldBinaryRig,
    V2Surface,
)

UTC = timezone.utc


# ------------------------------------------------------------ Claude hooks


@pytest.fixture
def claude_setup(tmp_path):
    store = tmp_path / "companion"
    identity = hb.HeartbeatIdentity(
        project="p",
        task_id="t",
        run_id="r",
        obligation_generation="obgen-1",
        reservation_id="11111111-1111-4111-8111-111111111111",
        launch_nonce_digest="a" * 64,
        terminal_id="a1b2c3d4",
        generation="gen-000042",
        attempt_id="7c9e6679-7425-40de-944b-e07fc1f90ae7",
        provider="claude",
        provider_version="2.1.218",
        native_session_id="uuid-session-1",
        assigned_policy_sha256="7" * 64,
        segment_hash="9" * 64,
    )
    token = hb.issue_fencing_token(store, "a1b2c3d4", "gen-000042", identity.attempt_id)
    now = [datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)]
    producer = hb.HeartbeatProducer(
        companion_dir=store, identity=identity, token=token, clock=lambda: now[0]
    )
    receiver = ch.ClaudeHookReceiver(
        companion_dir=store,
        producer=producer,
        native_session_id="uuid-session-1",
        terminal_id="a1b2c3d4",
        generation="gen-000042",
    )
    return store, receiver, now


def test_session_start_binds_identity(claude_setup):
    _, receiver, _ = claude_setup
    receipt = receiver.handle_hook(
        {
            "hook_event_name": "SessionStart",
            "session_id": "uuid-session-1",
            "transcript_path": "/home/u/.claude/projects/x/uuid-session-1.jsonl",
        }
    )
    assert receipt["accepted"] and receipt["beat_written"]
    assert receiver.session_started


def test_wrong_session_refused(claude_setup):
    _, receiver, _ = claude_setup
    with pytest.raises(ch.ClaudeHookRefused, match="bound native session"):
        receiver.handle_hook({"hook_event_name": "SessionStart", "session_id": "someone-else"})


def test_tool_sequence_monotone(claude_setup):
    _, receiver, now = claude_setup
    receiver.handle_hook({"hook_event_name": "SessionStart", "session_id": "uuid-session-1"})
    now[0] = now[0].replace(second=30)
    receiver.handle_hook(
        {"hook_event_name": "PostToolUse", "session_id": "uuid-session-1", "tool_sequence": 1}
    )
    now[0] = now[0].replace(second=55)
    with pytest.raises(ch.ClaudeHookRefused, match="strictly increase"):
        receiver.handle_hook(
            {"hook_event_name": "PostToolUse", "session_id": "uuid-session-1", "tool_sequence": 1}
        )


def test_terminal_hook_writes_terminal_beat(claude_setup):
    store, receiver, _ = claude_setup
    receiver.handle_hook({"hook_event_name": "Stop", "session_id": "uuid-session-1"})
    import json

    record = json.loads(hb.heartbeat_path(store, "a1b2c3d4", "gen-000042").read_bytes())
    assert record["turn"]["state"] == "terminal"
    assert record["evidence"]["kind"] == "hook_event"


def test_fenced_generation_refuses_activity_hooks(claude_setup):
    store, receiver, _ = claude_setup
    gf.install_fence(
        store,
        terminal_id="a1b2c3d4",
        generation="gen-000042",
        vintage="v2",
        request={
            "schema": gf.FENCE_REQUEST_SCHEMA,
            "terminal_generation": "gen-000042",
            "obligation_generation": "obgen-1",
            "attempt_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
            "intent_id": "0f8fad5a-1c87-4d3e-9b96-1b6b2c8e5f10",
            "report_sha256": "a" * 64,
        },
        fencing_token_id="token-1",
    )
    with pytest.raises(gf.FencedError):
        receiver.handle_hook(
            {"hook_event_name": "PostToolUse", "session_id": "uuid-session-1", "tool_sequence": 1}
        )
    # Terminal quiescence hooks are still recorded (they prove the turn ended).
    receipt = receiver.handle_hook({"hook_event_name": "Stop", "session_id": "uuid-session-1"})
    assert receipt["accepted"]


def test_malformed_hook_body_refused():
    with pytest.raises(ch.ClaudeHookRefused):
        ch.parse_hook_body(b"{{{")
    with pytest.raises(ch.ClaudeHookRefused):
        ch.parse_hook_body(b"[1,2]")


# --------------------------------------------------------- Kimi ACP proof


@pytest.fixture
def kimi_binary(tmp_path):
    binary = tmp_path / "kimi"
    binary.write_text("#!/bin/sh\necho kimi 0.29.0\n")
    binary.chmod(0o755)
    return binary


def test_proof_success_and_enablement(kimi_binary, tmp_path):
    receipt = kap.run_identity_proof(
        kimi_binary=kimi_binary,
        version_output="kimi 0.29.0",
        state_dir=tmp_path / "state",
        acp_driver=lambda b: {"session_id": "session_abc", "resumed": True},
    )
    assert receipt["schema"] == kap.PROOF_SCHEMA
    assert receipt["binary_sha256"]
    assert kap.kimi_identity_enabled(
        state_dir=tmp_path / "state",
        kimi_binary=kimi_binary,
        version_output="kimi 0.29.0",
    )


def test_proof_failure_refuses(kimi_binary, tmp_path):
    with pytest.raises(kap.KimiAcpProofError):
        kap.run_identity_proof(
            kimi_binary=kimi_binary,
            version_output="kimi 0.29.0",
            state_dir=tmp_path / "state",
            acp_driver=lambda b: {"session_id": "session_abc", "resumed": False},
        )


def test_identity_disabled_without_proof(tmp_path, kimi_binary):
    assert not kap.kimi_identity_enabled(
        state_dir=tmp_path / "empty",
        kimi_binary=kimi_binary,
        version_output="kimi 0.29.0",
    )


def test_binary_drift_invalidates_proof(kimi_binary, tmp_path):
    kap.run_identity_proof(
        kimi_binary=kimi_binary,
        version_output="kimi 0.29.0",
        state_dir=tmp_path / "state",
        acp_driver=lambda b: {"session_id": "session_abc", "resumed": True},
    )
    kimi_binary.write_text("#!/bin/sh\necho kimi 0.29.0-patched\n")
    assert not kap.kimi_identity_enabled(
        state_dir=tmp_path / "state",
        kimi_binary=kimi_binary,
        version_output="kimi 0.29.0",
    )


def test_version_drift_blocks_proof(kimi_binary, tmp_path):
    with pytest.raises(kap.KimiAcpProofError):
        kap.run_identity_proof(
            kimi_binary=kimi_binary,
            version_output="kimi 0.28.0",
            state_dir=tmp_path / "state",
            acp_driver=lambda b: {"session_id": "s", "resumed": True},
        )


# ------------------------------------------------------------ old-binary rig


def test_rig_proves_zero_visibility():
    surfaces = (
        V2Surface(kind="fs", locator="/state/v2/heartbeat.json"),
        V2Surface(kind="db", locator="managed_launch_v2_reservations"),
    )
    rig = OldBinaryRig(surfaces)

    def old_query(log: AccessLog):
        log.queries.append("terminals")
        log.reads.append("/state/v1/config.json")

    def old_cleanup(log: AccessLog):
        log.deletes.append("/state/v1/stale.fifo")

    verdict = rig.verify({"old-query": old_query, "old-cleanup": old_cleanup})
    assert verdict.zero_visibility
    assert verdict.surfaces_checked == 2


def test_rig_detects_violation():
    surfaces = (V2Surface(kind="fs", locator="/state/v2/heartbeat.json"),)
    rig = OldBinaryRig(surfaces)

    def sloppy_old_reader(log: AccessLog):
        log.reads.append("/state/v2/heartbeat.json")

    verdict = rig.run_probe(sloppy_old_reader, name="sloppy")
    assert not verdict.zero_visibility
    assert verdict.violations == ("/state/v2/heartbeat.json",)


def test_surfaces_from_registry():
    entries = [
        {
            "protocol_vintage": "v2",
            "desired_fs_path": "/state/v2/a.sock",
            "observed_fs_path": None,
            "desired_db_key": "v2-table",
            "observed_db_key": None,
            "desired_tmux_name": "managed-abc",
            "observed_tmux_id": "@9",
            "desired_memory_key": None,
            "observed_memory_key": "status-map-1",
        },
        {"protocol_vintage": "v1", "desired_fs_path": "/state/v1/old"},
    ]
    surfaces = OldBinaryRig.surfaces_from_registry(entries)
    locators = {surface.locator for surface in surfaces}
    assert "/state/v2/a.sock" in locators
    assert "v2-table" in locators
    assert "@9" in locators
    assert "status-map-1" in locators
    assert "/state/v1/old" not in locators
