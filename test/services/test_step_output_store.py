"""Tests for the in-memory structured-return store (issue #312, Bolt 2 / N4).

Covers validate-pass -> COMPLETED, validate-fail -> COMPLETED_UNVALIDATED (no
raise), no-schema -> trivially valid, cap eviction (oldest dropped, never
raises), and the synthetic-key round-trip.
"""

import pytest

from cli_agent_orchestrator.models.workflow import StepOutputRecord, StepState
from cli_agent_orchestrator.services.step_output_store import (
    StepOutputStore,
    record_step_output,
    step_output_store,
)

_SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "integer"}},
    "required": ["value"],
}


class TestRecordStepOutput:
    def test_no_schema_is_trivially_valid(self):
        rec = record_step_output("run1", "stepA", {"anything": True})
        assert rec.validated is True
        assert rec.errors == []
        assert rec.state == StepState.COMPLETED

    def test_schema_pass_marks_completed(self):
        rec = record_step_output("run2", "stepA", {"value": 7}, output_schema=_SCHEMA)
        assert rec.validated is True
        assert rec.state == StepState.COMPLETED

    def test_schema_fail_marks_unvalidated_without_raising(self):
        rec = record_step_output("run3", "stepA", {"value": "not-an-int"}, output_schema=_SCHEMA)
        assert rec.validated is False
        assert rec.errors  # collapsed reason recorded
        assert rec.state == StepState.COMPLETED_UNVALIDATED

    def test_round_trip_via_synthetic_keys(self):
        record_step_output("synthrun", "synthstep", {"value": 1}, output_schema=_SCHEMA)
        fetched = step_output_store.get("synthrun", "synthstep")
        assert fetched is not None
        assert fetched.output == {"value": 1}
        assert fetched.validated is True

    def test_malformed_run_id_raises_valueerror(self):
        with pytest.raises(ValueError):
            record_step_output("..", "stepA", {"x": 1})

    def test_malformed_step_id_raises_valueerror(self):
        with pytest.raises(ValueError):
            record_step_output("run", "bad/step", {"x": 1})


class TestStoreCapEviction:
    def test_cap_evicts_oldest_and_never_raises(self):
        store = StepOutputStore(max_entries=3)
        for i in range(5):
            rec = StepOutputRecord(
                run_id="r",
                step_id=f"s{i}",
                output={"i": i},
                validated=True,
                state=StepState.COMPLETED,
            )
            store.put("r", f"s{i}", rec)
        assert len(store) == 3
        # Oldest two evicted.
        assert store.get("r", "s0") is None
        assert store.get("r", "s1") is None
        # Newest retained.
        assert store.get("r", "s4") is not None
        assert store.get("r", "s2") is not None

    def test_last_write_wins_overwrites_in_place(self):
        store = StepOutputStore(max_entries=10)
        first = StepOutputRecord(
            run_id="r",
            step_id="s",
            output={"v": 1},
            validated=False,
            state=StepState.COMPLETED_UNVALIDATED,
        )
        second = StepOutputRecord(
            run_id="r",
            step_id="s",
            output={"v": 2},
            validated=True,
            state=StepState.COMPLETED,
        )
        store.put("r", "s", first)
        store.put("r", "s", second)
        assert len(store) == 1
        got = store.get("r", "s")
        assert got.output == {"v": 2}
        assert got.validated is True

    def test_reinsert_refreshes_eviction_position(self):
        store = StepOutputStore(max_entries=2)

        def _rec(step):
            return StepOutputRecord(
                run_id="r", step_id=step, output={}, validated=True, state=StepState.COMPLETED
            )

        store.put("r", "a", _rec("a"))
        store.put("r", "b", _rec("b"))
        # Touch "a" so it becomes newest; adding "c" should evict "b", not "a".
        store.put("r", "a", _rec("a"))
        store.put("r", "c", _rec("c"))
        assert store.get("r", "a") is not None
        assert store.get("r", "b") is None
        assert store.get("r", "c") is not None
