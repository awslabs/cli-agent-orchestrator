"""Tests for the actor broker (T-RP-6 fork side)."""

from __future__ import annotations

import socket
from datetime import datetime, timedelta, timezone

import pytest

from cli_agent_orchestrator.services import actor_broker
from cli_agent_orchestrator.services.actor_broker import (
    ActorBroker,
    ActorRefused,
    ActorUnavailable,
    AssertionInvalid,
    PeerCredentials,
)

UTC = timezone.utc


def _issue_kwargs():
    return {
        "report_sha256": "a" * 64,
        "report_path": "/abs/runs/task/report.md",
        "project": "cao-conductor-self-heal",
        "task_id": "self-heal-demo-task",
        "run_id": "run-0001",
        "obligation_generation": "obgen-7c2e4a1b",
        "attempt_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
        "native_session_id": "thr_0192a7b4",
        "launch_nonce_digest": "b" * 64,
        "route_chain_head": "c" * 64,
    }


@pytest.fixture
def broker(tmp_path):
    now = [datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)]
    instance = ActorBroker(
        state_dir=tmp_path / "broker",
        terminal_generation="gen-000042",
        provider_pids=frozenset({1000}),
        lineage_checker=lambda pid: pid in {2000, 2001},  # in-tree peers
        clock=lambda: now[0],
        signing_key=b"k" * 32,
    )
    instance._test_now = now
    return instance


def test_issue_and_consume_once(broker):
    assertion = broker.issue(None, peer=PeerCredentials(pid=2000, uid=501), **_issue_kwargs())
    assert broker.check(assertion)
    broker.verify_and_consume(assertion)
    with pytest.raises(AssertionInvalid, match="replay"):
        broker.verify_and_consume(assertion)


def test_same_uid_sibling_refused_at_lineage(broker):
    # A same-UID process outside the provider tree (collector/reconciler/
    # sibling) never obtains an assertion.
    with pytest.raises(ActorRefused):
        broker.issue(None, peer=PeerCredentials(pid=9999, uid=501), **_issue_kwargs())


def test_signature_forgery_refused(broker):
    assertion = broker.issue(None, peer=PeerCredentials(pid=2000, uid=501), **_issue_kwargs())
    forged = dict(assertion, report_sha256="f" * 64)
    with pytest.raises(AssertionInvalid):
        broker.verify_and_consume(forged)
    forged_sig = dict(assertion, signature="0" * 64)
    with pytest.raises(AssertionInvalid):
        broker.verify_and_consume(forged_sig)


def test_expired_assertion_refused(broker):
    assertion = broker.issue(None, peer=PeerCredentials(pid=2000, uid=501), **_issue_kwargs())
    broker._test_now[0] += timedelta(seconds=actor_broker.ASSERTION_TTL_SECONDS + 1)
    with pytest.raises(AssertionInvalid, match="expired"):
        broker.verify_and_consume(assertion)


def test_superseded_generation_fails_binding(tmp_path):
    current = [True]
    instance = ActorBroker(
        state_dir=tmp_path / "broker",
        terminal_generation="gen-000042",
        provider_pids=frozenset({1000}),
        lineage_checker=lambda pid: True,
        signing_key=b"k" * 32,
        generation_current=lambda: current[0],
    )
    assertion = instance.issue(None, peer=PeerCredentials(pid=2000, uid=501), **_issue_kwargs())
    current[0] = False  # a resume superseded the generation
    with pytest.raises(AssertionInvalid, match="superseded"):
        instance.verify_and_consume(assertion)
    with pytest.raises(AssertionInvalid):
        instance.issue(None, peer=PeerCredentials(pid=2000, uid=501), **_issue_kwargs())


def test_wrong_generation_assertion_refused(tmp_path):
    first = ActorBroker(
        state_dir=tmp_path / "a",
        terminal_generation="gen-000042",
        provider_pids=frozenset({1}),
        lineage_checker=lambda pid: True,
        signing_key=b"k" * 32,
    )
    second = ActorBroker(
        state_dir=tmp_path / "b",
        terminal_generation="gen-000043",
        provider_pids=frozenset({1}),
        lineage_checker=lambda pid: True,
        signing_key=b"k" * 32,
    )
    assertion = first.issue(None, peer=PeerCredentials(pid=2000, uid=501), **_issue_kwargs())
    with pytest.raises(AssertionInvalid, match="different terminal generation"):
        second.verify_and_consume(assertion)


def test_assertion_binds_all_required_fields(broker):
    assertion = broker.issue(None, peer=PeerCredentials(pid=2000, uid=501), **_issue_kwargs())
    for field in actor_broker.ASSERTION_FIELD_ORDER:
        assert field in assertion
    assert assertion["terminal_generation"] == "gen-000042"
    digest = ActorBroker.assertion_digest(assertion)
    assert len(digest) == 64


def test_consumption_is_durable_across_broker_restart(tmp_path):
    kwargs = dict(
        state_dir=tmp_path / "broker",
        terminal_generation="gen-000042",
        provider_pids=frozenset({1000}),
        lineage_checker=lambda pid: True,
        signing_key=b"k" * 32,
    )
    first = ActorBroker(**kwargs)
    assertion = first.issue(None, peer=PeerCredentials(pid=2000, uid=501), **_issue_kwargs())
    first.verify_and_consume(assertion)
    restarted = ActorBroker(**kwargs)  # new instance, same state dir + key
    with pytest.raises(AssertionInvalid, match="replay"):
        restarted.verify_and_consume(assertion)


def test_concurrent_cross_broker_consumption_is_single_use(tmp_path):
    # ACT-1 durable regression: two independent broker instances over one
    # state dir/key racing verify_and_consume at P-MUT must produce exactly
    # one acceptance — consumption is a cross-process transaction (flock),
    # not an unlocked load/check/write.
    import threading

    kwargs = dict(
        state_dir=tmp_path / "broker",
        terminal_generation="gen-000042",
        provider_pids=frozenset({1000}),
        lineage_checker=lambda pid: True,
        signing_key=b"k" * 32,
    )
    issuer = ActorBroker(**kwargs)
    assertion = issuer.issue(None, peer=PeerCredentials(pid=2000, uid=501), **_issue_kwargs())
    left, right = ActorBroker(**kwargs), ActorBroker(**kwargs)
    barrier = threading.Barrier(2)
    outcomes: list = []

    def consume(broker):
        barrier.wait(timeout=5)
        try:
            broker.verify_and_consume(assertion)
            outcomes.append("accepted")
        except AssertionInvalid:
            outcomes.append("refused")

    threads = [threading.Thread(target=consume, args=(broker,)) for broker in (left, right)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads)
    assert sorted(outcomes) == ["accepted", "refused"]


def test_platform_peer_credentials_real_socketpair():
    # On supported platforms the kernel path works for a real socketpair.
    if not actor_broker.platform_supported():
        pytest.skip("platform lacks kernel peer credentials")
    left, right = socket.socketpair()
    try:
        creds = actor_broker.peer_credentials(left)
        import os

        assert creds.pid == os.getpid()
        assert creds.uid == os.getuid()
    finally:
        left.close()
        right.close()


def test_platform_inability_is_actor_unavailable(monkeypatch):
    monkeypatch.setattr(actor_broker.sys, "platform", "plan9")
    left, right = socket.socketpair()
    try:
        with pytest.raises(ActorUnavailable):
            actor_broker.peer_credentials(left)
    finally:
        left.close()
        right.close()


def test_key_is_memory_only(tmp_path):
    ActorBroker(
        state_dir=tmp_path / "broker",
        terminal_generation="gen-000042",
        provider_pids=frozenset({1}),
        lineage_checker=lambda pid: True,
    )
    # Nothing under the state dir may contain key material (the only file
    # ever written is the consumed-assertion store).
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert path.name == "actor-assertions.json"
