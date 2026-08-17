"""M6a provider-terminal recovery evidence and durable episode identity."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import provider_recovery_evidence as recovery
from cli_agent_orchestrator.services import stable_agent_roster

BOX = "─" * 60
CONNECTION_CLOSED = [
    "✻ Crunched for 7m 21s",
    "API Error: Connection closed mid-response. The response above may be incomplete.",
    BOX,
    "❯",
    BOX,
]


def _install_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'recovery-evidence.db'}",
        connect_args={"check_same_thread": False},
    )
    database.Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(
        database,
        "SessionLocal",
        sessionmaker(autocommit=False, autoflush=False, bind=engine),
    )
    return engine


def _observe(lines, **changes):
    values = {
        "terminal_id": "abcd1234",
        "generation": "generation-1",
        "native_session_id": "native-1",
        "provider": "claude_code",
        "provider_version": "2.1.220",
        "agent_id": "agent-1",
        "incarnation_id": "incarnation-1",
        "screen_lines": lines,
    }
    values.update(changes)
    return recovery.observe(**values)


def test_only_proven_connection_closed_match_executes_nudge_contract():
    matched = recovery.detect("claude_code", CONNECTION_CLOSED)

    assert matched is not None
    assert matched.pattern == "claude.connection-closed-mid-response"
    assert matched.turn_state == "terminal"
    assert matched.recovery_action == "nudge"
    assert matched.confidence == "high"
    assert matched.status.value == "error"


def test_the_2_1_233_connection_lost_wording_matches_the_same_contract():
    """2.1.233 reworded "closed" to "lost"; anchoring only on the old text
    leaves the detector blind on every current install."""
    matched = recovery.detect(
        "claude_code",
        [
            "✻ Crunched for 7m 21s",
            "API Error: Connection lost mid-response. The response above may be incomplete.",
            BOX,
            "❯",
            BOX,
        ],
    )

    assert matched is not None
    assert matched.pattern == "claude.connection-lost-mid-response"
    assert matched.turn_state == "terminal"
    assert matched.recovery_action == "nudge"
    assert matched.confidence == "high"
    assert matched.status.value == "error"


def test_the_two_mid_response_wordings_keep_distinct_occurrence_identity():
    """Same condition, different build text.  Distinct fingerprints keep an
    upgrade from reading as a recurrence of the pre-upgrade occurrence."""
    closed = recovery.detect("claude_code", CONNECTION_CLOSED)
    lost = recovery.detect(
        "claude_code",
        [
            "API Error: Connection lost mid-response. The response above may be incomplete.",
            BOX,
            "❯",
            BOX,
        ],
    )

    assert closed is not None and lost is not None
    # Assert the pattern ids, not just that both matched: the generic
    # API-error fallback also returns a match with a different fingerprint,
    # so identity alone passes even when the "lost" wording is unrecognised.
    assert closed.pattern == "claude.connection-closed-mid-response"
    assert lost.pattern == "claude.connection-lost-mid-response"
    assert closed.fingerprint != lost.fingerprint


def test_connection_closed_match_survives_one_viewport_line_wrap():
    matched = recovery.detect(
        "claude_code",
        [
            "API Error: Connection closed mid-response. The response above may be",
            "incomplete.",
            BOX,
            "❯",
            BOX,
        ],
    )

    assert matched is not None
    assert matched.recovery_action == "nudge"


def test_connection_closed_match_survives_three_physical_rows():
    matched = recovery.detect(
        "claude_code",
        [
            "API Error: Connection closed",
            "mid-response. The response above may",
            "be incomplete.",
            BOX,
            "❯",
            BOX,
        ],
    )

    assert matched is not None
    assert matched.recovery_action == "nudge"


def test_connection_closed_reconstruction_is_bounded_to_three_rows():
    matched = recovery.detect(
        "claude_code",
        [
            "API Error: Connection",
            "closed mid-response. The",
            "response above may be",
            "incomplete.",
            BOX,
            "❯",
            BOX,
        ],
    )

    assert matched is None


def test_old_connection_error_above_later_response_is_not_current():
    matched = recovery.detect(
        "claude_code",
        [
            "API Error: Connection closed mid-response. The response above may be incomplete.",
            "● A later turn completed successfully.",
            BOX,
            "❯",
            BOX,
        ],
    )

    assert matched is None


def test_retry_banner_is_self_retrying_and_never_nudged():
    matched = recovery.detect(
        "claude_code",
        ["✻ API error · Retrying in 1s · attempt 1/10"],
    )

    assert matched is not None
    assert matched.pattern == "claude.retry-banner"
    assert matched.turn_state == "self-retrying"
    assert matched.recovery_action == "ignore"
    assert matched.status.value == "processing"


def test_generic_api_error_is_preserved_for_layer_two_without_guessing():
    matched = recovery.detect(
        "claude_code",
        ["API Error: A new provider failure that M6a has not proven"],
    )

    assert matched is not None
    assert matched.pattern == "claude.generic-api-error"
    assert matched.turn_state == "unknown"
    assert matched.recovery_action == "layer-2"
    assert matched.status is None


def test_raw_error_text_is_utf8_bounded_but_digest_covers_the_observation():
    raw = "API Error: " + ("é" * 2000)
    matched = recovery.detect("claude_code", [raw])

    assert matched is not None
    assert len(matched.raw_text.encode("utf-8")) <= recovery.RAW_TEXT_MAX_BYTES
    assert matched.raw_text_truncated is True
    assert matched.raw_sha256 != hashlib.sha256(matched.raw_text.encode()).hexdigest()


def test_quoted_or_other_provider_text_does_not_enter_the_allowlist():
    assert (
        recovery.detect(
            "claude_code",
            ["The pane said API Error: Connection closed mid-response yesterday."],
        )
        is None
    )
    assert recovery.detect("codex", CONNECTION_CLOSED) is None


def test_same_episode_survives_poll_and_fresh_service_instance(tmp_path, monkeypatch):
    engine = _install_db(tmp_path, monkeypatch)
    try:
        first = _observe(CONNECTION_CLOSED)
        polled = _observe(CONNECTION_CLOSED)

        # There is deliberately no process-local cache to reset: a second read
        # resolves the occurrence from SQLite, which is the daemon-restart path.
        restarted = _observe(CONNECTION_CLOSED)

        assert first == polled == restarted
        assert set(first) == {
            "schema",
            "occurrence_id",
            "agent_id",
            "incarnation_id",
            "detector",
            "detector_version",
            "pattern",
            "terminal_id",
            "generation",
            "native_session_id",
            "provider",
            "provider_version",
            "turn_state",
            "recovery_action",
            "raw_text",
            "raw_sha256",
            "raw_text_truncated",
            "confidence",
            "reason",
            "signals",
            "opened_at",
        }
        assert first["occurrence_id"]
        assert first["terminal_id"] == "abcd1234"
        assert first["generation"] == "generation-1"
        assert first["native_session_id"] == "native-1"
        assert first["agent_id"] == "agent-1"
        assert first["incarnation_id"] == "incarnation-1"
        assert first["provider"] == "claude_code"
        assert first["provider_version"] == "2.1.220"
        assert first["raw_text"]
        assert len(first["raw_text"].encode("utf-8")) <= recovery.RAW_TEXT_MAX_BYTES
        assert len(first["raw_sha256"]) == 64
    finally:
        engine.dispose()


def test_clear_then_same_generation_recurrence_gets_new_occurrence(tmp_path, monkeypatch):
    engine = _install_db(tmp_path, monkeypatch)
    try:
        first = _observe(CONNECTION_CLOSED)
        assert _observe([BOX, "❯", BOX]) is None
        second = _observe(CONNECTION_CLOSED)

        assert second["occurrence_id"] != first["occurrence_id"]
        with database.SessionLocal() as db:
            rows = (
                db.query(database.ProviderRecoveryEpisodeModel)
                .order_by(database.ProviderRecoveryEpisodeModel.opened_at)
                .all()
            )
            assert [row.active for row in rows] == [0, 1]
            assert rows[0].closed_at is not None
    finally:
        engine.dispose()


def test_different_match_closes_prior_episode_and_opens_another(tmp_path, monkeypatch):
    engine = _install_db(tmp_path, monkeypatch)
    try:
        first = _observe(CONNECTION_CLOSED)
        retrying = _observe(["✻ API error · Retrying in 1s · attempt 1/10"])

        assert retrying["occurrence_id"] != first["occurrence_id"]
        assert retrying["turn_state"] == "self-retrying"
        with database.SessionLocal() as db:
            active = (
                db.query(database.ProviderRecoveryEpisodeModel)
                .filter(database.ProviderRecoveryEpisodeModel.active == 1)
                .one()
            )
            assert active.pattern == "claude.retry-banner"
    finally:
        engine.dispose()


def test_generation_change_cannot_adopt_an_old_occurrence(tmp_path, monkeypatch):
    engine = _install_db(tmp_path, monkeypatch)
    try:
        prior = _observe(CONNECTION_CLOSED)
        successor = _observe(CONNECTION_CLOSED, generation="generation-2")

        assert successor["occurrence_id"] != prior["occurrence_id"]
        assert successor["generation"] == "generation-2"
    finally:
        engine.dispose()


def test_sqlite_locked_writer_retries_and_preserves_short_lived_match(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'locked-recovery-evidence.db'}",
        connect_args={"check_same_thread": False, "timeout": 0},
    )
    database.Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(
        database,
        "SessionLocal",
        sessionmaker(autocommit=False, autoflush=False, bind=engine),
    )
    holder = engine.connect()
    transaction = holder.begin()
    holder.execute(
        database.ProviderRecoveryEpisodeModel.__table__.insert().values(
            occurrence_id="unrelated-writer",
            terminal_id="other-terminal",
            generation_key="other-generation",
            generation="other-generation",
            provider="claude_code",
            pattern="other-pattern",
            fingerprint="f" * 64,
            match_json="{}",
            active=1,
            opened_at="2026-08-15T00:00:00Z",
            last_observed_at="2026-08-15T00:00:00Z",
        )
    )
    sleeps = []

    def release_writer(delay):
        sleeps.append(delay)
        transaction.commit()

    monkeypatch.setattr(recovery.time, "sleep", release_writer)
    try:
        observed = _observe(CONNECTION_CLOSED)

        assert observed["occurrence_id"]
        assert observed["turn_state"] == "terminal"
        assert sleeps == [recovery.SQLITE_CONTENTION_RETRY_DELAYS[0]]
    finally:
        if transaction.is_active:
            transaction.rollback()
        holder.close()
        engine.dispose()


def test_unrelated_operational_error_is_not_retried(tmp_path, monkeypatch):
    engine = _install_db(tmp_path, monkeypatch)
    calls = 0

    def broken_session():
        nonlocal calls
        calls += 1
        raise OperationalError(
            "SELECT provider_recovery_episodes",
            {},
            sqlite3.OperationalError("no such table: provider_recovery_episodes"),
        )

    monkeypatch.setattr(database, "SessionLocal", broken_session)
    try:
        with pytest.raises(recovery.RecoveryEvidenceUnavailable, match="no such table"):
            _observe(CONNECTION_CLOSED)
        assert calls == 1
    finally:
        engine.dispose()


def test_concurrent_observers_adopt_one_winning_occurrence(tmp_path, monkeypatch):
    engine = _install_db(tmp_path, monkeypatch)
    barrier = threading.Barrier(2)
    local = threading.local()
    original_active = recovery._active

    def synchronized_active(db, terminal_id, generation_key):
        active = original_active(db, terminal_id, generation_key)
        if not getattr(local, "initial_read_complete", False):
            local.initial_read_complete = True
            barrier.wait(timeout=5)
        return active

    monkeypatch.setattr(recovery, "_active", synchronized_active)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(_observe, CONNECTION_CLOSED) for _ in range(2)]
            observations = [future.result(timeout=10) for future in futures]

        assert observations[0]["occurrence_id"] == observations[1]["occurrence_id"]
        with database.SessionLocal() as db:
            rows = db.query(database.ProviderRecoveryEpisodeModel).all()
            assert len(rows) == 1
            assert rows[0].active == 1
    finally:
        engine.dispose()


def test_identity_context_reads_the_generation_bound_provider_build(tmp_path, monkeypatch):
    engine = _install_db(tmp_path, monkeypatch)
    try:
        with database.SessionLocal() as db:
            db.add(
                database.ManagedLaunchV2ReservationModel(
                    reservation_id="reservation-1",
                    terminal_id="abcd1234",
                    generation="generation-1",
                    protocol_vintage="v2",
                    session_name="cao-test",
                    provider="claude_code",
                    agent_profile="reviewer",
                    caller_id="caller-1",
                    working_directory="/tmp/worktree",
                    obligation_generation="obligation-1",
                    run_id="run-1",
                    launch_nonce_digest="a" * 64,
                    state="bound",
                    request_json="{}",
                    binding_json=json.dumps({"provider_version": "2.1.220"}),
                    created_at="2026-08-15T00:00:00Z",
                    updated_at="2026-08-15T00:00:00Z",
                )
            )
            db.commit()

        context = recovery.identity_context(
            terminal_id="abcd1234",
            generation="generation-1",
            native_session_id="native-1",
        )

        assert context["native_session_id"] == "native-1"
        assert context["provider_version"] == "2.1.220"
        assert context["agent_id"] is None
        assert context["incarnation_id"] is None
    finally:
        engine.dispose()


def test_identity_context_publishes_stable_agent_identity_when_available(tmp_path, monkeypatch):
    engine = _install_db(tmp_path, monkeypatch)
    try:
        monkeypatch.setattr(
            stable_agent_roster,
            "get_incarnation_by_terminal",
            lambda terminal_id, generation: {
                "terminal_id": terminal_id,
                "generation": generation,
                "agent_id": "agent-1",
                "incarnation_id": "incarnation-1",
            },
        )

        context = recovery.identity_context(
            terminal_id="abcd1234",
            generation="generation-1",
            native_session_id="native-1",
        )

        assert context["agent_id"] == "agent-1"
        assert context["incarnation_id"] == "incarnation-1"
    finally:
        engine.dispose()
