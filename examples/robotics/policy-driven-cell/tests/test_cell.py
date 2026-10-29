from __future__ import annotations

import time
from pathlib import Path

import pytest
from cell import Cell, FakeBackend, Refused, Scene

EXAMPLE = Path(__file__).resolve().parents[1]


def scene() -> Scene:
    return Scene.model_validate_json((EXAMPLE / "cell.json").read_text())


def started(backend=None, *, allow_motion=True) -> Cell:
    cell = Cell(scene(), backend or FakeBackend(), run_id="test", allow_motion=allow_motion)
    cell.start()
    return cell


def finished(cell: Cell, command_id: str, timeout: float = 10) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = next(c for c in cell.observe()["commands"] if c["command_id"] == command_id)
        if record["status"] in ("succeeded", "failed"):
            return record
        time.sleep(0.01)
    raise AssertionError(f"{command_id} did not finish")


def test_humanoid_starts_inside_the_exclusion_radius_so_the_arm_is_interlocked():
    cell = started()
    try:
        assert cell.observe()["measured"]["humanoids"]["g1"]["inside_exclusion_of"] == ["arm"]
        with pytest.raises(Refused, match="interlock"):
            cell.pick_and_place("arm_op", "early", "arm", "part_a", "pass_bin")
    finally:
        cell.close()


def test_the_default_job_succeeds_on_measured_state():
    cell = started()
    try:
        assert (
            finished(cell, cell.walk_to("g1_op", "w1", "g1", "inspection")["command_id"])["status"]
            == "succeeded"
        )
        pick = finished(
            cell, cell.pick_and_place("arm_op", "p1", "arm", "part_a", "pass_bin")["command_id"]
        )
        assert pick["status"] == "succeeded"
        assert pick["evidence"]["grasp_assist"] is True
        assert pick["evidence"]["in_bin"] == "pass_bin"
        assert (
            finished(
                cell,
                cell.pick_and_place("arm_op", "p2", "arm", "part_b", "rework_bin")["command_id"],
            )["status"]
            == "succeeded"
        )
        assert (
            finished(cell, cell.walk_to("g1_op", "w2", "g1", "dock")["command_id"])["status"]
            == "succeeded"
        )
        measured = cell.observe()["measured"]
        assert measured["humanoids"]["g1"]["nearest_location"] == "dock"
        assert {p: s["in_bin"] for p, s in measured["parts"].items()} == {
            "part_a": "pass_bin",
            "part_b": "rework_bin",
        }
    finally:
        cell.close()


def test_a_fall_fails_the_walk_with_the_measured_reason():
    cell = started(FakeBackend(fall_on_segment=1))
    try:
        record = finished(cell, cell.walk_to("g1_op", "w1", "g1", "inspection")["command_id"])
        assert record["status"] == "failed"
        assert "fell" in record["exit_reason"]
        assert cell.observe()["measured"]["humanoids"]["g1"]["upright"] is False
    finally:
        cell.close()


def test_a_reported_pick_is_failed_when_the_part_is_not_measured_in_the_bin():
    cell = started(FakeBackend(drop_parts=frozenset({"part_a"})))
    try:
        finished(cell, cell.walk_to("g1_op", "w1", "g1", "inspection")["command_id"])
        record = finished(
            cell, cell.pick_and_place("arm_op", "p1", "arm", "part_a", "pass_bin")["command_id"]
        )
        assert record["status"] == "failed"
        assert record["evidence"]["in_bin"] is None
    finally:
        cell.close()


def test_command_ids_replay_only_the_identical_call():
    cell = started()
    try:
        first = cell.walk_to("g1_op", "w1", "g1", "inspection")
        assert cell.walk_to("g1_op", "w1", "g1", "inspection")["command_id"] == first["command_id"]
        with pytest.raises(Refused, match="already used"):
            cell.walk_to("g1_op", "w1", "g1", "dock")
        finished(cell, "w1")
    finally:
        cell.close()


def test_a_busy_robot_refuses_a_second_command():
    class Slow(FakeBackend):
        def walk_segment(self, robot, velocity, duration_s):
            time.sleep(0.05)
            return super().walk_segment(robot, velocity, duration_s)

    cell = started(Slow())
    try:
        cell.walk_to("g1_op", "w1", "g1", "inspection")
        with pytest.raises(Refused, match="busy"):
            cell.walk_to("g1_op", "w2", "g1", "dock")
        finished(cell, "w1")
    finally:
        cell.close()


def test_stop_and_disabled_motion_refuse_new_commands():
    cell = started(allow_motion=False)
    try:
        with pytest.raises(Refused, match="disabled"):
            cell.walk_to("g1_op", "w1", "g1", "inspection")
    finally:
        cell.close()
    cell = started()
    try:
        assert cell.stop()["stopped"] is True
        with pytest.raises(Refused, match="stopped"):
            cell.walk_to("g1_op", "w1", "g1", "inspection")
    finally:
        cell.close()


def test_scene_refuses_a_bin_out_of_the_arms_reach():
    data = scene().model_dump()
    data["bins"]["far_bin"] = {"x": 0.0, "y": 0.0}
    with pytest.raises(ValueError, match="out of reach"):
        Scene.model_validate(data)


@pytest.mark.sim
def test_the_default_job_on_the_real_simulator():
    """Runs Holosoma and the SO-101 in MuJoCo. Opt in with: pytest -m sim"""
    from strands_backend import StrandsBackend

    cell = started(StrandsBackend())
    try:
        with pytest.raises(Refused, match="interlock"):
            cell.pick_and_place("arm_op", "early", "arm", "part_a", "pass_bin")
        for view in (cell.walk_to("g1_op", "w1", "g1", "inspection"),):
            assert finished(cell, view["command_id"], timeout=120)["status"] == "succeeded"
        assert (
            finished(
                cell,
                cell.pick_and_place("arm_op", "p1", "arm", "part_a", "pass_bin")["command_id"],
                120,
            )["status"]
            == "succeeded"
        )
        g1 = cell.observe()["measured"]["humanoids"]["g1"]
        assert g1["upright"] and g1["nearest_location"] == "inspection"
    finally:
        cell.close()
