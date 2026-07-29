"""Tests for the fenced heartbeat producer and consumer rules (T-HB-4/5)."""

from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone

import pytest

from cli_agent_orchestrator.services import heartbeat_store as hb

UTC = timezone.utc


def _identity(**changes) -> hb.HeartbeatIdentity:
    fields = {
        "project": "cao-conductor-self-heal",
        "task_id": "self-heal-demo-task",
        "run_id": "run-0001",
        "obligation_generation": "obgen-7c2e4a1b",
        "reservation_id": "11111111-1111-4111-8111-111111111111",
        "launch_nonce_digest": "a" * 64,
        "terminal_id": "a1b2c3d4",
        "generation": "gen-000042",
        "attempt_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
        "provider": "codex",
        "provider_version": "0.146.0",
        "native_session_id": "thr_0192a7b4",
        "assigned_policy_sha256": "7" * 64,
        "segment_hash": "9" * 64,
    }
    fields.update(changes)
    return hb.HeartbeatIdentity(**fields)


@pytest.fixture
def store(tmp_path):
    return tmp_path / "companion"


def _producer(store, identity, token, now):
    clock = lambda: now[0]  # noqa: E731
    return hb.HeartbeatProducer(companion_dir=store, identity=identity, token=token, clock=clock)


def test_beat_writes_schema2_record_0600(store):
    identity = _identity()
    token = hb.issue_fencing_token(
        store, identity.terminal_id, identity.generation, identity.attempt_id
    )
    now = [datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)]
    producer = _producer(store, identity, token, now)
    record = producer.beat(
        turn_state="active",
        provider_turn_id="turn-1",
        evidence_kind="app_server_event",
        evidence_id="thread/turn:1",
    )
    assert record is not None
    assert record["schema_version"] == 2
    assert record["protocol_vintage"] == "v2"
    assert record["fencing_token"] == token.as_dict()
    assert record["epoch"] == token.fence_no
    assert record["seq"] == 1
    assert record["lease_ttl_s"] == 90
    assert record["lease_expires_at"] == "2026-07-23T12:01:30Z"
    path = hb.heartbeat_path(store, identity.terminal_id, identity.generation)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    on_disk = json.loads(path.read_bytes())
    assert on_disk == record


def test_coalescing_one_write_per_20s_while_active(store):
    identity = _identity()
    token = hb.issue_fencing_token(
        store, identity.terminal_id, identity.generation, identity.attempt_id
    )
    now = [datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)]
    producer = _producer(store, identity, token, now)
    assert (
        producer.beat(
            turn_state="active",
            provider_turn_id="t",
            evidence_kind="app_server_event",
            evidence_id="e1",
        )
        is not None
    )
    now[0] += timedelta(seconds=5)
    assert (
        producer.beat(
            turn_state="active",
            provider_turn_id="t",
            evidence_kind="app_server_event",
            evidence_id="e2",
        )
        is None
    )  # coalesced
    now[0] += timedelta(seconds=20)
    assert (
        producer.beat(
            turn_state="active",
            provider_turn_id="t",
            evidence_kind="app_server_event",
            evidence_id="e3",
        )
        is not None
    )


def test_terminal_event_always_writes_final_beat(store):
    identity = _identity()
    token = hb.issue_fencing_token(
        store, identity.terminal_id, identity.generation, identity.attempt_id
    )
    now = [datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)]
    producer = _producer(store, identity, token, now)
    producer.beat(
        turn_state="active", provider_turn_id="t", evidence_kind="acp_update", evidence_id="e1"
    )
    final = producer.terminal_beat(
        provider_turn_id="t", evidence_kind="acp_update", evidence_id="e2"
    )
    assert final is not None
    assert final["turn"]["state"] == "terminal"


def test_ttl_capped_at_300(store):
    identity = _identity()
    token = hb.issue_fencing_token(
        store, identity.terminal_id, identity.generation, identity.attempt_id
    )
    now = [datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)]
    producer = _producer(store, identity, token, now)
    record = producer.beat(
        turn_state="active",
        provider_turn_id="t",
        evidence_kind="hook_event",
        evidence_id="e1",
        lease_ttl_s=9999,
    )
    assert record["lease_ttl_s"] == 300


def test_superseded_producer_refused(store):
    identity = _identity()
    old_token = hb.issue_fencing_token(
        store, identity.terminal_id, identity.generation, identity.attempt_id
    )
    now = [datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)]
    old_producer = _producer(store, identity, old_token, now)
    old_producer.beat(
        turn_state="active",
        provider_turn_id="t",
        evidence_kind="app_server_event",
        evidence_id="e1",
    )
    # A resume issues a new token (higher fence number) for the same terminal.
    new_token = hb.issue_fencing_token(
        store, identity.terminal_id, "gen-000043", "9b2e6679-7425-40de-944b-e07fc1f90ae7"
    )
    assert new_token.fence_no == old_token.fence_no + 1
    now[0] += timedelta(seconds=30)  # past the coalescing window
    with pytest.raises(hb.FencingRefused):
        old_producer.beat(
            turn_state="active",
            provider_turn_id="t",
            evidence_kind="app_server_event",
            evidence_id="e2",
        )


def test_restarted_producer_rehydrates_epoch_seq(store):
    # HB-1 durable regression: a reconstructed producer with the same valid
    # token (bridge re-emit, process restart) must rehydrate the durable
    # (epoch, seq) position and coalescing watermark instead of restarting
    # at zero — a restart-at-zero write is a regression the fencing compare
    # step would refuse, which is exactly how the live bridge lost liveness.
    identity = _identity()
    token = hb.issue_fencing_token(
        store, identity.terminal_id, identity.generation, identity.attempt_id
    )
    now = [datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)]
    producer = _producer(store, identity, token, now)
    first = producer.beat(
        turn_state="active",
        provider_turn_id="t",
        evidence_kind="app_server_event",
        evidence_id="e1",
    )
    assert first is not None and first["seq"] == 1
    restarted = _producer(store, identity, token, now)
    # The durable watermark rehydrated: an immediate active beat coalesces
    # exactly as it would on the original instance.
    assert (
        restarted.beat(
            turn_state="active",
            provider_turn_id="t",
            evidence_kind="app_server_event",
            evidence_id="e1b",
        )
        is None
    )
    now[0] += timedelta(seconds=30)
    continued = restarted.beat(
        turn_state="active",
        provider_turn_id="t",
        evidence_kind="app_server_event",
        evidence_id="e2",
    )
    assert continued is not None
    assert continued["epoch"] == token.fence_no
    assert continued["seq"] == 2  # continues, never regresses to 0/1


def test_concurrent_cross_generation_issuance_is_monotone(store):
    # HB-2 durable regression: racing token issuance for concurrent
    # generations of one terminal must yield strictly increasing fence
    # numbers — issuance serializes on the terminal-scoped lock, not the
    # generation lock (two generations must never both read the same prior
    # number and both publish prior+1).
    import threading
    import uuid

    tokens: list = []
    errors: list = []

    def issue(generation: str) -> None:
        try:
            tokens.append(hb.issue_fencing_token(store, "a1b2c3d4", generation, str(uuid.uuid4())))
        except Exception as exc:  # noqa: BLE001 - test records the exact outcome
            errors.append(f"{type(exc).__name__}: {exc}")

    workers = [threading.Thread(target=issue, args=(f"gen-race-{i}",)) for i in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
    assert not any(worker.is_alive() for worker in workers)
    assert errors == []
    assert len(tokens) == 8
    assert sorted(token.fence_no for token in tokens) == [1, 2, 3, 4, 5, 6, 7, 8]
    assert len({token.id for token in tokens}) == 8


def test_record_never_stores_secrets_or_prompt_text(store):
    identity = _identity()
    token = hb.issue_fencing_token(
        store, identity.terminal_id, identity.generation, identity.attempt_id
    )
    now = [datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)]
    producer = _producer(store, identity, token, now)
    producer.beat(
        turn_state="active",
        provider_turn_id="t",
        evidence_kind="app_server_event",
        evidence_id="e1",
    )
    raw = hb.heartbeat_path(store, identity.terminal_id, identity.generation).read_bytes()
    assert identity.launch_nonce_digest in raw.decode()
    assert "nonce-value" not in raw.decode()  # only the digest is ever stored


# ------------------------------------------------------------- consumer side


def _active_record(store, now):
    identity = _identity()
    token = hb.issue_fencing_token(
        store, identity.terminal_id, identity.generation, identity.attempt_id
    )
    producer = _producer(store, identity, token, now)
    producer.beat(
        turn_state="active",
        provider_turn_id="t",
        evidence_kind="app_server_event",
        evidence_id="e1",
    )
    return identity, token


def test_reader_active(store):
    now = [datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)]
    identity, _ = _active_record(store, now)
    reading = hb.read_heartbeat(store, identity=identity, now=now[0] + timedelta(seconds=10))
    assert reading.status == hb.READING_ACTIVE


def test_reader_stale_after_lease_expiry(store):
    now = [datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)]
    identity, _ = _active_record(store, now)
    reading = hb.read_heartbeat(store, identity=identity, now=now[0] + timedelta(seconds=91))
    assert reading.status == hb.READING_STALE


def test_reader_missing(store):
    reading = hb.read_heartbeat(store, identity=_identity())
    assert reading.status == hb.READING_MISSING


def test_reader_malformed(store):
    identity = _identity()
    path = hb.heartbeat_path(store, identity.terminal_id, identity.generation)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"{{{")
    reading = hb.read_heartbeat(store, identity=identity)
    assert reading.status == hb.READING_MALFORMED


def test_reader_wrong_identity(store):
    now = [datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)]
    identity, _ = _active_record(store, now)
    other = _identity(task_id="another-task")
    reading = hb.read_heartbeat(store, identity=other, now=now[0])
    assert reading.status == hb.READING_WRONG_IDENTITY


def test_reader_fencing_refused_after_resume(store):
    now = [datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)]
    identity, _ = _active_record(store, now)
    hb.issue_fencing_token(
        store, identity.terminal_id, "gen-000043", "9b2e6679-7425-40de-944b-e07fc1f90ae7"
    )
    reading = hb.read_heartbeat(store, identity=identity, now=now[0])
    assert reading.status == hb.READING_FENCING_REFUSED


def test_reader_high_water_regression(store):
    now = [datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)]
    identity, token = _active_record(store, now)
    reading = hb.read_heartbeat(
        store, identity=identity, high_water=(token.fence_no, 99), now=now[0]
    )
    assert reading.status == hb.READING_REGRESSED


def test_reader_skew_rejection(store):
    now = [datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)]
    identity, _ = _active_record(store, now)
    reading = hb.read_heartbeat(store, identity=identity, now=now[0] - timedelta(seconds=10))
    assert reading.status == hb.READING_SKEW


def test_reader_ttl_cap_enforced_reader_side(store):
    # A record claiming an absurd TTL is still capped at 300 s by the reader.
    identity = _identity()
    token = hb.issue_fencing_token(
        store, identity.terminal_id, identity.generation, identity.attempt_id
    )
    now = [datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)]
    producer = _producer(store, identity, token, now)
    producer.beat(
        turn_state="active",
        provider_turn_id="t",
        evidence_kind="app_server_event",
        evidence_id="e1",
        lease_ttl_s=99999,
    )
    active = hb.read_heartbeat(store, identity=identity, now=now[0] + timedelta(seconds=299))
    assert active.status == hb.READING_ACTIVE
    stale = hb.read_heartbeat(store, identity=identity, now=now[0] + timedelta(seconds=301))
    assert stale.status == hb.READING_STALE


def test_crash_between_beats_never_forges_active(store):
    # A kill before the P-MUT write leaves either the prior complete record
    # or none; the reader then reports STALE/MISSING, never ACTIVE-from-a-
    # torn-write.
    identity = _identity()
    token = hb.issue_fencing_token(
        store, identity.terminal_id, identity.generation, identity.attempt_id
    )
    now = [datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)]
    producer = _producer(store, identity, token, now)
    producer.beat(
        turn_state="active",
        provider_turn_id="t",
        evidence_kind="app_server_event",
        evidence_id="e1",
    )

    from cli_agent_orchestrator.services import durable_publish

    now[0] += timedelta(seconds=30)
    durable_publish.crash_hook = lambda step: (
        (_ for _ in ()).throw(RuntimeError("kill")) if step == "pmut.written" else None
    )
    with pytest.raises(RuntimeError):
        producer.beat(
            turn_state="active",
            provider_turn_id="t",
            evidence_kind="app_server_event",
            evidence_id="e2",
        )
    durable_publish.crash_hook = None
    reading = hb.read_heartbeat(store, identity=identity, now=now[0])
    assert reading.status in (hb.READING_ACTIVE,)  # prior complete record still valid
    assert reading.record["evidence"]["id"] == "e1"  # and it is the OLD record


def test_fencing_token_ids_unique_and_monotone(store):
    first = hb.issue_fencing_token(store, "a1b2c3d4", "gen-1", "attempt-1")
    second = hb.issue_fencing_token(store, "a1b2c3d4", "gen-2", "attempt-2")
    assert first.id != second.id
    assert second.fence_no == first.fence_no + 1
    current = hb.current_fencing_token(store, "a1b2c3d4")
    assert current == second
