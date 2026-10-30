"""#1068's pre-bind recheck next to #1079's sweep: one collection and one release per name.

Real threads over a SQLite file, as in the sweep's own race tests.
"""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from test.services.test_ephemeral_auto_policy import AUTO, BOUNDS
from test.services.test_ephemeral_service import CALLER, create_store  # noqa: F401
from test.services.test_ephemeral_sweep import (  # noqa: F401
    CHILD,
    LEASE,
    file_store,
    live,
    row,
    store,
)
from test.services.test_ephemeral_tier_policy import mapped

import pytest


@pytest.fixture
def auto_file_store(file_store, create_store):
    mapped(create_store[4])
    file_store.config.update(BOUNDS)
    return file_store


def claimed_auto(s):
    """A claimed auto name whose decision is running: its inputs were handed out."""
    name = s.service.create_ephemeral_agent(dict(AUTO), CALLER)["name"]
    token = s.service.claim_ephemeral_agent(name, {}, CALLER)["claim_id"]
    assert s.service.decision_inputs(name, token, CALLER, "claude_code") is not None
    return name, token


def released(name):
    archive = json.loads(Path(row(name)["audit_path"]).read_bytes())
    return [event for event in archive["events"] if event["event"] == "released"], archive


def test_a_recheck_collection_overlapping_a_sweep_is_released_once(auto_file_store, monkeypatch):
    s = auto_file_store
    name, token = claimed_auto(s)
    # Neither pass touches the claim while its decision runs.
    assert s.service.sweep(startup=False) == {} and s.service.sweep(startup=True) == {}
    assert row(name)["state"] == "claimed" and all(path.is_file() for path in live(s, name))
    # The operator lowers the ceiling under the applied answer while the decider runs.
    s.config.update(max_tier="medium", default_tier="medium")
    original = s.service._finish_gc
    collected, go = threading.Event(), threading.Event()

    def finish(gc_row):
        if threading.current_thread().name.startswith("recheck"):
            collected.set()
            assert go.wait(timeout=10)
        return original(gc_row)

    monkeypatch.setattr(s.service, "_finish_gc", finish)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="recheck") as pool:
        rechecking = pool.submit(s.service.recheck_decided, name, token, CALLER, "large")
        assert collected.wait(timeout=10)
        try:
            # Collected but not yet cleaned: a pass only retries the two file deletions.
            assert row(name)["gc_reason"] == "policy_changed"
            assert s.service.sweep(startup=False) == {"retried": 1}
        finally:
            go.set()
        with pytest.raises(s.service.EphemeralPolicyError) as err:
            rechecking.result(timeout=10)
    assert err.value.rule == "policy_changed_since_create:tier_exceeds_policy"
    final = row(name)
    assert final["state"] == "gc" and final["gc_reason"] == "policy_changed"
    assert not any(path.exists() for path in live(s, name))
    events, archive = released(name)
    assert [event["gc_reason"] for event in events] == ["policy_changed"]
    assert archive["gc_reason"] == "policy_changed"
    assert s.service.sweep(startup=False) == {}
    assert released(name)[0] == events
    with pytest.raises(s.service.EphemeralPolicyError) as err:
        s.service.bind_ephemeral_agent(name, CHILD, CALLER, "claude_code", token, None)
    assert err.value.rule == "ephemeral_expired"


def test_a_decision_that_outlives_its_lease_is_lapsed_by_the_sweep_not_collected(
    auto_file_store,
):
    s = auto_file_store
    name, token = claimed_auto(s)
    s.clock.at(seconds=LEASE)
    s.config.update(max_tier="medium", default_tier="medium")
    # A lapse back to pending is not a collection, so the pass reports nothing.
    assert s.service.sweep(startup=False) == {}
    lapsed = row(name)
    assert lapsed["state"] == "pending" and lapsed["claim_id"] is None
    # The recheck sees no live claim: it neither collects nor refuses, and the bind refuses.
    assert s.service.recheck_decided(name, token, CALLER, "large") is None
    assert row(name)["state"] == "pending"
    assert all(path.is_file() for path in live(s, name))
    assert released(name)[0] == []
    with pytest.raises(s.service.EphemeralPolicyError) as err:
        s.service.bind_ephemeral_agent(name, CHILD, CALLER, "claude_code", token, None)
    assert err.value.rule == "claim_expired"
