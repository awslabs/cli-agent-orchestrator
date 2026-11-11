"""#1199: ShadowRunner.submit_group, one bounded background task whose answer updates several rows."""

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def store(tmp_path):
    from cli_agent_orchestrator.clients.database import DecisionRecordModel
    from cli_agent_orchestrator.decisions.store import DecisionStore

    db = create_engine("sqlite:///" + str(tmp_path / "state.db"))
    DecisionRecordModel.__table__.create(db)
    return DecisionStore(sessionmaker(bind=db), tmp_path / "key", emit=lambda _: None)


def pending(store, count=4):
    return tuple(
        store.insert(
            dict(
                point=f"task.{name}",
                state="shadow",
                kind="assign",
                provider="claude_code",
                fallback_source="none",
                outcome="shadow",
                decision_status="pending",
            ),
            "brief",
        )
        for name in ("long_context", "deep_reasoning", "read_only", "needs_network")[:count]
    )


@pytest.mark.asyncio
async def test_each_row_gets_its_own_answer(store):
    from cli_agent_orchestrator.decisions.shadow import ShadowRunner

    runner = ShadowRunner(store)
    ids = pending(store)

    async def work():
        return {record_id: {"answer": "yes", "reason": None} for record_id in ids[:3]}

    assert runner.submit_group(ids, work)
    await runner.drain()
    rows = [store.get(record_id) for record_id in ids]
    assert [(r["answer"], r["reason"], r["decision_status"]) for r in rows[:3]] == [
        ("yes", None, "done")
    ] * 3
    # A row the work didn't answer is an error, never left pending.
    assert (rows[3]["reason"], rows[3]["decision_status"]) == ("error", "done")
    assert all(r["outcome"] == "shadow" and r["applied_value"] is None for r in rows)


@pytest.mark.asyncio
async def test_an_error_marks_every_row(store):
    from cli_agent_orchestrator.decisions.shadow import ShadowRunner

    runner = ShadowRunner(store)
    ids = pending(store)

    async def work():
        raise RuntimeError("synthetic")

    runner.submit_group(ids, work)
    await runner.drain()
    assert {(store.get(i)["reason"], store.get(i)["decision_status"]) for i in ids} == {
        ("error", "done")
    }


@pytest.mark.asyncio
async def test_a_full_runner_drops_every_row_and_counts_one_task(store):
    from cli_agent_orchestrator.decisions.shadow import ShadowRunner

    runner = ShadowRunner(store, max_pending=1)
    release = asyncio.Event()

    async def wait():
        await release.wait()
        return {}

    first, second = pending(store), pending(store)
    assert runner.submit_group(first, wait)
    assert len(runner._tasks) == 1
    assert not runner.submit_group(second, wait)
    assert {(store.get(i)["reason"], store.get(i)["decision_status"]) for i in second} == {
        ("shadow_dropped", "dropped")
    }
    release.set()
    await runner.drain()
    assert runner._tasks == {} and runner._groups == {}


@pytest.mark.asyncio
async def test_close_marks_every_row_of_a_group_interrupted(store):
    from cli_agent_orchestrator.decisions.shadow import ShadowRunner

    runner = ShadowRunner(store)
    ids = pending(store)

    async def forever():
        await asyncio.Event().wait()
        return {}

    runner.submit_group(ids, forever)
    await asyncio.sleep(0)
    await runner.close()
    assert {store.get(i)["decision_status"] for i in ids} == {"interrupted"}
    assert not runner.submit_group(pending(store, 1), forever)
