"""Tests for the W13 generation fence (T-RP-7 fork side, cond-0054 fixture)."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from cli_agent_orchestrator.services import generation_fence as gf


def _request(**changes):
    request = {
        "schema": gf.FENCE_REQUEST_SCHEMA,
        "terminal_generation": "gen-000042",
        "obligation_generation": "obgen-7c2e4a1b",
        "attempt_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
        "intent_id": "0f8fad5a-1c87-4d3e-9b96-1b6b2c8e5f10",
        "report_sha256": "a" * 64,
    }
    request.update(changes)
    return request


def _park_request(**changes):
    request = {
        "schema": gf.PARK_REQUEST_SCHEMA,
        "operation_id": "0f8fad5a-1c87-4d3e-9b96-1b6b2c8e5f10",
        "reservation_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
        "terminal_id": "a1b2c3d4",
        "terminal_generation": "gen-000042",
        "logical_task_id": "cond-0329",
        "retained_round": 3,
        "obligation_generation": "obgen-7c2e4a1b",
        "attempt_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
        "report_sha256": "a" * 64,
    }
    request.update(changes)
    return request


@pytest.fixture
def store(tmp_path):
    return tmp_path / "companion"


def test_install_then_already_fenced_idempotent(store):
    first = gf.install_fence(
        store,
        terminal_id="a1b2c3d4",
        generation="gen-000042",
        vintage="v2",
        request=_request(),
        fencing_token_id="token-1",
    )
    assert first["outcome"] == gf.OUTCOME_FENCED
    assert isinstance(first["fence_receipt_sha256"], str)
    second = gf.install_fence(
        store,
        terminal_id="a1b2c3d4",
        generation="gen-000042",
        vintage="v2",
        request=_request(),
        fencing_token_id="token-1",
    )
    assert second["outcome"] == gf.OUTCOME_ALREADY_FENCED
    # Crash-after-CAS-before-response reconciliation: identical receipt.
    assert second["fence_receipt_sha256"] == first["fence_receipt_sha256"]


def test_distinct_intent_single_use_violation_refused(store):
    gf.install_fence(
        store,
        terminal_id="a1b2c3d4",
        generation="gen-000042",
        vintage="v2",
        request=_request(),
        fencing_token_id="token-1",
    )
    with pytest.raises(gf.FenceRequestError):
        gf.install_fence(
            store,
            terminal_id="a1b2c3d4",
            generation="gen-000042",
            vintage="v2",
            request=_request(intent_id="3d813cbb-47fb-42ba-91df-831e1593ac29"),
            fencing_token_id="token-1",
        )
    with pytest.raises(gf.FenceRequestError):
        gf.install_fence(
            store,
            terminal_id="a1b2c3d4",
            generation="gen-000042",
            vintage="v2",
            request=_request(report_sha256="b" * 64),
            fencing_token_id="token-1",
        )


def test_unknown_generation_outcome(store):
    response = gf.install_fence(
        store,
        terminal_id="a1b2c3d4",
        generation="gen-000042",
        vintage="v2",
        request=_request(terminal_generation="gen-999999"),
        fencing_token_id="token-1",
    )
    assert response["outcome"] == gf.OUTCOME_UNKNOWN_GENERATION
    assert response["fence_receipt_sha256"] is None


def test_vintage_mismatch_outcome(store):
    response = gf.install_fence(
        store,
        terminal_id="a1b2c3d4",
        generation="gen-000042",
        vintage="v1",
        request=_request(),
        fencing_token_id="token-1",
    )
    assert response["outcome"] == gf.OUTCOME_VINTAGE_MISMATCH


def test_superseded_generation_outcome(store):
    response = gf.install_fence(
        store,
        terminal_id="a1b2c3d4",
        generation="gen-000042",
        vintage="v2",
        request=_request(),
        fencing_token_id="token-1",
        superseded=True,
    )
    assert response["outcome"] == gf.OUTCOME_SUPERSEDED


def test_committed_fence_retry_adopts_before_supersession(store):
    first = gf.install_fence(
        store,
        terminal_id="a1b2c3d4",
        generation="gen-000042",
        vintage="v2",
        request=_request(),
        fencing_token_id="token-1",
    )
    adopted = gf.install_fence(
        store,
        terminal_id="a1b2c3d4",
        generation="gen-000042",
        vintage="v2",
        request=_request(),
        fencing_token_id="token-successor",
        superseded=True,
    )
    assert adopted["outcome"] == gf.OUTCOME_ALREADY_FENCED
    assert adopted["fence_receipt_sha256"] == first["fence_receipt_sha256"]


def test_m3_park_receipt_adopts_exactly_and_reconciles(store):
    first = gf.install_park(store, request=_park_request(), fencing_token_id="token-1")
    assert first["outcome"] == gf.OUTCOME_FENCED
    retry = gf.install_park(
        store, request=_park_request(), fencing_token_id="token-successor", superseded=True
    )
    assert retry["outcome"] == gf.OUTCOME_ALREADY_FENCED
    assert retry["park_receipt_sha256"] == first["park_receipt_sha256"]
    queried = gf.query_park(
        store,
        terminal_id="a1b2c3d4",
        generation="gen-000042",
        operation_id=_park_request()["operation_id"],
    )
    assert queried["park_receipt_sha256"] == first["park_receipt_sha256"]
    with pytest.raises(gf.ParkRequestError):
        gf.install_park(
            store, request=_park_request(logical_task_id="other-task"), fencing_token_id="token-1"
        )


def test_m3_park_adopts_compatible_w13_fence(store):
    fence = gf.install_fence(
        store,
        terminal_id="a1b2c3d4",
        generation="gen-000042",
        vintage="v2",
        request=_request(),
        fencing_token_id="token-w13",
    )
    parked = gf.install_park(store, request=_park_request(), fencing_token_id="ignored")
    receipt = parked["park_receipt"]
    assert receipt["fence_receipt_sha256"] == fence["fence_receipt_sha256"]
    assert receipt["fence_receipt"]["fencing_token_id"] == "token-w13"


@pytest.mark.parametrize(
    "w13_changes",
    (
        {"obligation_generation": "other-obligation"},
        {"attempt_id": "11111111-1111-4111-8111-111111111111"},
        {"report_sha256": "b" * 64},
    ),
)
def test_m3_park_refuses_incompatible_w13_fence(store, w13_changes):
    """M3 may adopt only the W13 seal for its exact obligation/attempt/report."""
    isolated = store / next(iter(w13_changes))
    gf.install_fence(
        isolated,
        terminal_id="a1b2c3d4",
        generation="gen-000042",
        vintage="v2",
        request=_request(**w13_changes),
        fencing_token_id="token-w13",
    )

    with pytest.raises(gf.ParkRequestError, match="incompatible"):
        gf.install_park(isolated, request=_park_request(), fencing_token_id="token-m3")


def test_m3_park_receipt_digest_is_independent_of_mapping_insertion_order(store):
    parked = gf.install_park(store, request=_park_request(), fencing_token_id="token-1")
    receipt = parked["park_receipt"]
    reordered = {
        key: (
            {nested_key: receipt[key][nested_key] for nested_key in reversed(tuple(receipt[key]))}
            if key == "fence_receipt"
            else receipt[key]
        )
        for key in reversed(tuple(receipt))
    }

    assert gf.park_receipt_digest(reordered) == gf.park_receipt_digest(receipt)


@pytest.mark.parametrize(
    ("terminal_id", "generation"),
    (
        ("A1B2C3D4", "gen-000042"),
        ("a1b2c3d4", "../outside"),
        ("a1b2c3d4", "generation\\outside"),
    ),
)
def test_m3_park_query_refuses_unsafe_identity_path_segments(store, terminal_id, generation):
    with pytest.raises(gf.ParkRequestError):
        gf.query_park(
            store,
            terminal_id=terminal_id,
            generation=generation,
            operation_id="11111111-1111-4111-8111-111111111111",
        )


def test_fenced_generation_rejects_input_admission(store):
    gf.assert_admission_open(store, "a1b2c3d4", "gen-000042")  # open before fence
    gf.install_fence(
        store,
        terminal_id="a1b2c3d4",
        generation="gen-000042",
        vintage="v2",
        request=_request(),
        fencing_token_id="token-1",
    )
    # The cond-0054 fixture: queued unsubmitted input is rejected at the
    # admission boundary, and a post-report same-turn tool call is prevented.
    with pytest.raises(gf.FencedError):
        gf.assert_admission_open(store, "a1b2c3d4", "gen-000042")


def test_stale_managed_binding_becomes_the_shared_typed_fence_refusal(store):
    from cli_agent_orchestrator.services import heartbeat_store

    stale = heartbeat_store.issue_fencing_token(store, "a1b2c3d4", "gen-000042", "attempt-1")
    heartbeat_store.issue_fencing_token(store, "a1b2c3d4", "gen-successor", "attempt-2")
    with pytest.raises(gf.FencedError, match="no longer current"):
        with gf.managed_admission_critical_section(
            store,
            "a1b2c3d4",
            "gen-000042",
            attempt_id="attempt-1",
            fencing_token_id=stale.id,
        ):
            pytest.fail("a stale producer must not enter the byte section")


def test_admission_critical_section_recheck_and_post_fence_refusal(store):
    # The critical section re-verifies the generation under the fence lock;
    # once a fence lands, entering the section refuses (FENCE-1 boundary).
    with gf.admission_critical_section(store, "a1b2c3d4", "gen-000042"):
        pass  # provider/model/tool-entry I/O would run here
    gf.install_fence(
        store,
        terminal_id="a1b2c3d4",
        generation="gen-000042",
        vintage="v2",
        request=_request(),
        fencing_token_id="token-1",
    )
    with pytest.raises(gf.FencedError):
        with gf.admission_critical_section(store, "a1b2c3d4", "gen-000042"):
            pass


def test_admission_critical_section_blocks_concurrent_fence_install(store):
    # FENCE-1 durable regression: a fence install issued while an admission
    # holds the critical section must wait — it cannot land between the
    # final fence recheck and the provider I/O (no check-then-submit gap).
    import threading

    in_section = threading.Event()
    finish_io = threading.Event()
    installed: list = []

    def admit():
        with gf.admission_critical_section(store, "a1b2c3d4", "gen-000042"):
            in_section.set()
            assert finish_io.wait(timeout=10)

    def install():
        installed.append(
            gf.install_fence(
                store,
                terminal_id="a1b2c3d4",
                generation="gen-000042",
                vintage="v2",
                request=_request(),
                fencing_token_id="token-1",
            )
        )

    admission = threading.Thread(target=admit)
    admission.start()
    assert in_section.wait(timeout=10)
    installer = threading.Thread(target=install)
    installer.start()
    installer.join(timeout=2)
    assert installer.is_alive()  # blocked behind the held fence lock
    finish_io.set()
    admission.join(timeout=10)
    installer.join(timeout=10)
    assert not admission.is_alive() and not installer.is_alive()
    assert installed[0]["outcome"] == gf.OUTCOME_FENCED


def test_verify_fence_freshness(store):
    response = gf.install_fence(
        store,
        terminal_id="a1b2c3d4",
        generation="gen-000042",
        vintage="v2",
        request=_request(),
        fencing_token_id="token-1",
    )
    assert gf.verify_fence(
        store,
        terminal_id="a1b2c3d4",
        generation="gen-000042",
        expected_receipt_sha256=response["fence_receipt_sha256"],
    )
    assert not gf.verify_fence(
        store,
        terminal_id="a1b2c3d4",
        generation="gen-000042",
        expected_receipt_sha256="0" * 64,
    )


def test_lost_fence_detectable_and_reinstall_idempotent(store):
    first = gf.install_fence(
        store,
        terminal_id="a1b2c3d4",
        generation="gen-000042",
        vintage="v2",
        request=_request(),
        fencing_token_id="token-1",
    )
    gf.fence_state_path(store, "a1b2c3d4", "gen-000042").unlink()  # simulated loss
    assert not gf.verify_fence(
        store,
        terminal_id="a1b2c3d4",
        generation="gen-000042",
        expected_receipt_sha256=first["fence_receipt_sha256"],
    )
    second = gf.install_fence(
        store,
        terminal_id="a1b2c3d4",
        generation="gen-000042",
        vintage="v2",
        request=_request(),
        fencing_token_id="token-1",
    )
    assert second["outcome"] == gf.OUTCOME_FENCED
    # The receipt digest is stable because it covers the receipt object
    # only; installed_at is re-minted after a genuine loss, so the caller
    # re-records the new digest (final-verified freshness).
    assert gf.verify_fence(
        store,
        terminal_id="a1b2c3d4",
        generation="gen-000042",
        expected_receipt_sha256=second["fence_receipt_sha256"],
    )


def test_request_validation(store):
    with pytest.raises(gf.FenceRequestError):
        gf.install_fence(
            store,
            terminal_id="a1b2c3d4",
            generation="gen-000042",
            vintage="v2",
            request=_request(schema="cao-w13-fence-req-v0"),
            fencing_token_id="token-1",
        )
    with pytest.raises(gf.FenceRequestError):
        gf.install_fence(
            store,
            terminal_id="a1b2c3d4",
            generation="gen-000042",
            vintage="v2",
            request=_request(report_sha256="not-hex"),
            fencing_token_id="token-1",
        )


def test_seal_intent_validation():
    gf.validate_seal_intent(
        {
            "schema": gf.SEAL_INTENT_SCHEMA,
            "project": "p",
            "task_id": "t",
            "run_id": "r",
            "terminal_generation": "gen-000042",
            "obligation_generation": "obgen-1",
            "attempt_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
            "report_sha256": "a" * 64,
            "intent_id": "0f8fad5a-1c87-4d3e-9b96-1b6b2c8e5f10",
            "at": "2026-07-23T12:00:00Z",
        }
    )
    with pytest.raises(gf.FenceRequestError):
        gf.validate_seal_intent({"schema": gf.SEAL_INTENT_SCHEMA})


class _BlockingAdmission:
    """Thread-visible fake used to exercise async flock ownership handoff."""

    def __init__(self, *, block_enter: bool = False, fail_enter: bool = False):
        self.block_enter = block_enter
        self.fail_enter = fail_enter
        self.entered = threading.Event()
        self.allow_enter = threading.Event()
        self.exited = threading.Event()
        self.enters = 0
        self.exits = 0

    def __enter__(self):
        self.enters += 1
        self.entered.set()
        if self.block_enter:
            assert self.allow_enter.wait(timeout=5)
        if self.fail_enter:
            raise RuntimeError("enter failed")
        return self

    def __exit__(self, *_exc):
        self.exits += 1
        self.exited.set()


@pytest.mark.asyncio
async def test_async_admission_cancelled_while_acquiring_never_enters_and_releases_once(
    store, monkeypatch
):
    """Cancellation may race flock acquisition, but cannot strand its owner."""
    blocked = _BlockingAdmission(block_enter=True)
    successor = _BlockingAdmission()
    managers = iter((blocked, successor))
    monkeypatch.setattr(gf, "admission_critical_section", lambda *_args: next(managers))
    body_entered = asyncio.Event()

    async def waiter():
        async with gf.async_admission_critical_section(store, "a1b2c3d4", "gen-000042"):
            body_entered.set()

    task = asyncio.create_task(waiter())
    assert await asyncio.to_thread(blocked.entered.wait, 5)
    # The loop remains live while flock acquisition is in its worker.
    tick = asyncio.Event()
    asyncio.get_running_loop().call_soon(tick.set)
    await asyncio.wait_for(tick.wait(), timeout=1)
    task.cancel()
    blocked.allow_enter.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(blocked.exited.wait, 5)
    assert blocked.enters == blocked.exits == 1
    assert not body_entered.is_set()

    async with gf.async_admission_critical_section(store, "a1b2c3d4", "gen-000042"):
        pass
    assert successor.enters == successor.exits == 1


@pytest.mark.asyncio
async def test_async_admission_cancellation_and_exception_close_the_owner_once(store, monkeypatch):
    """Body cancellation, body exception, and enter failure leave no held lock."""
    cancelled = _BlockingAdmission()
    exceptional = _BlockingAdmission()
    failed = _BlockingAdmission(fail_enter=True)
    managers = iter((cancelled, exceptional, failed))
    monkeypatch.setattr(gf, "admission_critical_section", lambda *_args: next(managers))
    entered = asyncio.Event()

    async def held_body():
        async with gf.async_admission_critical_section(store, "a1b2c3d4", "gen-000042"):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(held_body())
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.exits == 1

    with pytest.raises(ValueError):
        async with gf.async_admission_critical_section(store, "a1b2c3d4", "gen-000042"):
            raise ValueError("body failed")
    assert exceptional.exits == 1

    with pytest.raises(RuntimeError, match="enter failed"):
        async with gf.async_admission_critical_section(store, "a1b2c3d4", "gen-000042"):
            pass
    assert failed.enters == 1
    assert failed.exits == 0


@pytest.mark.asyncio
async def test_async_flock_waiters_do_not_starve_default_executor_or_park(store, monkeypatch):
    """Contended flock acquisition is isolated from native work and park I/O.

    The holder deliberately needs the two-worker default executor after two
    same-generation waiters have begun acquiring the flock.  Before the
    dedicated acquisition pool, those waiters occupied both workers and this
    deterministic holder/park cycle could not progress.
    """
    loop = asyncio.get_running_loop()
    constrained = ThreadPoolExecutor(max_workers=2)
    loop.set_default_executor(constrained)
    real_enter = gf._enter_or_abandon
    waiters_started = asyncio.Event()
    worker_calls = 0
    worker_calls_lock = threading.Lock()

    def observe_enter(manager, handoff):
        nonlocal worker_calls
        with worker_calls_lock:
            worker_calls += 1
            # The holder's acquisition completed before we create either
            # waiter.  Waiting for call three proves *both* contended
            # acquisitions reached the dedicated flock pool before the
            # holder asks the constrained default executor for native work.
            both_waiters_started = worker_calls == 3
        if both_waiters_started:
            loop.call_soon_threadsafe(waiters_started.set)
        return real_enter(manager, handoff)

    monkeypatch.setattr(gf, "_enter_or_abandon", observe_enter)
    holder_entered = asyncio.Event()
    allow_holder_work = asyncio.Event()
    holder_work_finished = asyncio.Event()
    release_holder = asyncio.Event()
    park_started = asyncio.Event()

    async def holder():
        async with gf.async_admission_critical_section(store, "a1b2c3d4", "gen-000042"):
            holder_entered.set()
            await allow_holder_work.wait()
            await asyncio.to_thread(lambda: "native-readiness-and-tmux-work")
            holder_work_finished.set()
            await release_holder.wait()

    async def waiter():
        try:
            async with gf.async_admission_critical_section(store, "a1b2c3d4", "gen-000042"):
                return ("entered", None)
        except gf.FencedError as exc:
            # Once the holder releases, flock makes no FIFO promise between
            # an already-blocked admission and park.  A waiter therefore may
            # either enter before park or observe its typed absorbing fence;
            # neither outcome changes the executor-liveness contract below.
            return ("fenced", exc)

    def park():
        loop.call_soon_threadsafe(park_started.set)
        return gf.install_park(store, request=_park_request(), fencing_token_id="token-1")

    try:
        holder_task = asyncio.create_task(holder())
        await asyncio.wait_for(holder_entered.wait(), timeout=1)
        waiters = [asyncio.create_task(waiter()) for _ in range(2)]
        await asyncio.wait_for(waiters_started.wait(), timeout=1)
        park_task = asyncio.create_task(asyncio.to_thread(park))
        await asyncio.wait_for(park_started.wait(), timeout=1)
        allow_holder_work.set()
        await asyncio.wait_for(holder_work_finished.wait(), timeout=1)
        # It has reached install_park, but the holder still owns the exact
        # generation lock.  The assertion is a lock-state oracle, not a
        # scheduler-timing guess.
        assert not park_task.done()
        release_holder.set()
        results = await asyncio.wait_for(
            asyncio.gather(holder_task, park_task, *waiters, return_exceptions=True), timeout=3
        )
        assert all(not isinstance(result, BaseException) for result in results)
        holder_result, park_result, *waiter_results = results
        assert holder_result is None
        assert park_result["outcome"] == gf.OUTCOME_FENCED
        assert park_result["park_receipt"]["fence_receipt"]["fencing_token_id"] == "token-1"
        for outcome, detail in waiter_results:
            assert outcome in {"entered", "fenced"}
            if outcome == "entered":
                assert detail is None
            else:
                assert isinstance(detail, gf.FencedError)
    finally:
        # A failed assertion must not poison later tests with a shut-down or
        # constrained loop-default executor.
        replacement = ThreadPoolExecutor(max_workers=4)
        loop.set_default_executor(replacement)
        constrained.shutdown(wait=True)
