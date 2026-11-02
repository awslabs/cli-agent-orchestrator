from __future__ import annotations

import copy
import json
import math
import threading
import time
from pathlib import Path

import pytest
from pydantic import ValidationError
from simulation import Scene, World

EXAMPLE = Path(__file__).resolve().parents[1]
DATA = json.loads((EXAMPLE / "factory.json").read_text())
FORMATIONS = {"west": "queue", "middle": "columns", "east": "diamond"}


def scene(**changes) -> Scene:
    data = copy.deepcopy(DATA)
    data.update(changes)
    return Scene.model_validate(data)


def run(world: World, seconds: float) -> None:
    for _ in range(round(seconds / world.scene.step_seconds)):
        world.tick()


def formed_world(**kwargs) -> World:
    world = World(scene(), allow_motion=True, **kwargs)
    for zone, formation in FORMATIONS.items():
        assert world.form_team(zone, "form-1", formation)["status"] == "accepted"
    run(world, 10)
    for zone in FORMATIONS:
        assert world.command(zone, "form-1")["status"] == "finished"
    return world


def start_order(world: World, *, units: int = 16, pace_a: float = 2, pace_b: float = 8) -> None:
    routine = ["spin", "turn", "run"]
    assert world.start_shipping("east", "ship-1", units, 2, routine)["status"] == "running"
    assert world.start_line("middle", "line-1", units, pace_b)["status"] == "running"
    assert world.start_line("west", "line-1", units, pace_a)["status"] == "running"


def test_the_committed_scene_has_three_teams_of_the_planned_kinds_and_sizes():
    floor = scene()
    assert {
        zone: (team.label, team.kind, len(team.slots)) for zone, team in floor.teams.items()
    } == {
        "west": ("Team A", "serial", 3),
        "middle": ("Team B", "parallel", 2),
        "east": ("Team C", "formation", 4),
    }
    # Team B stands in a vertical column: both robots have the same x.
    assert len({slot[0] for slot in floor.teams["middle"].slots.values()}) == 1
    # Team A stands in one queue along y = 0, from the raw store to the boundary.
    queue = [slot[0] for slot in floor.teams["west"].slots.values()]
    assert queue == sorted(queue) and {slot[1] for slot in floor.teams["west"].slots.values()} == {
        0
    }


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda d: d["places"]["west_middle"].update(zones=["west"]), "buffer joins two zones"),
        (lambda d: d["places"]["dock"].update(xy=[9, 0]), "inside every owning zone"),
        (lambda d: d["robots"]["a1"].update(zone="east"), "start inside its declared zone"),
        (lambda d: d["teams"]["west"]["slots"].update(b1=[-3, 0, 0]), "own zone"),
        (lambda d: d["teams"]["west"].update(input="west_middle"), "raw store"),
        (lambda d: d["teams"]["middle"].update(min_cycle_seconds=20), "must not exceed"),
        (lambda d: d["teams"]["east"].update(max_batch=5), "max_batch"),
        (lambda d: d["teams"]["east"].update(max_routine_moves=None), "max_routine_moves"),
        (lambda d: d["places"]["middle_east"].update(capacity=2), "capacity of the input buffer"),
        (lambda d: d["teams"]["east"].update(run_to=[4.3, 0]), "stay inside its zone"),
        (lambda d: d["teams"]["middle"]["slots"].pop("b2"), "exactly one team"),
        (lambda d: d.update(raw_store="west_middle"), "raw_store must be"),
    ],
)
def test_the_scene_refuses_an_inconsistent_floor(change, message):
    data = copy.deepcopy(DATA)
    change(data)
    with pytest.raises(ValidationError, match=message):
        Scene.model_validate(data)


def test_motion_needs_approval_and_a_team_in_the_zone_of_the_caller():
    world = World(scene())
    assert world.form_team("west", "form-1", "queue")["reason"] == "motion_not_approved"
    world = World(scene(), allow_motion=True)
    assert world.form_team("north", "form-1", "queue")["reason"] == "no_team_in_zone"
    assert world.form_team("west", "form-2", "diamond")["reason"] == "formation_unavailable"


def test_form_team_drives_only_the_robots_of_the_caller_to_their_slots():
    world = World(scene(), allow_motion=True)
    before = {name: (robot.x, robot.y) for name, robot in world.robots.items()}
    assert world.form_team("west", "form-1", "queue")["status"] == "accepted"
    assert world.team_status("west")["status"] == "forming"
    run(world, 10)
    assert world.command("west", "form-1")["status"] == "finished"
    assert world.team_status("west")["status"] == "ready"
    for robot, slot in world.scene.teams["west"].slots.items():
        assert world._xy(robot) == pytest.approx(slot[:2], abs=1e-6)
        assert world.robots[robot].heading == pytest.approx(slot[2])
    for robot in ("b1", "b2", "c1", "c2", "c3", "c4"):
        assert (world.robots[robot].x, world.robots[robot].y) == before[robot]


def test_a_command_id_is_idempotent_and_a_changed_reuse_is_a_conflict():
    world = World(scene(), allow_motion=True)
    first = world.form_team("west", "form-1", "queue")
    assert world.form_team("west", "form-1", "queue") == first
    assert world.form_team("west", "form-1", "other")["reason"] == "command_id_conflict"
    assert world.form_team("west", " ", "queue")["reason"] == "empty_command_id"


def test_a_program_needs_the_formation_and_valid_limits():
    world = World(scene(), allow_motion=True)
    assert world.start_line("west", "line-1", 8, 2)["reason"] == "formation_not_ready"
    world = formed_world()
    assert world.start_line("west", "line-1", 8, 1)["reason"] == "pace_out_of_range"
    assert world.start_line("west", "line-2", 17, 2)["reason"] == "count_out_of_range"
    assert world.start_line("east", "line-1", 8, 4)["reason"] == "wrong_team_kind"
    assert world.start_shipping("west", "ship-1", 8, 2, ["run"])["reason"] == "wrong_team_kind"
    assert world.start_shipping("east", "ship-1", 8, 5, ["run"])["reason"] == "batch_out_of_range"
    assert (
        world.start_shipping("east", "ship-2", 8, 2, ["spin", "turn"])["reason"]
        == "routine_needs_one_run"
    )
    assert (
        world.start_shipping("east", "ship-3", 8, 2, ["run", "run"])["reason"]
        == "routine_needs_one_run"
    )
    assert (
        world.start_shipping(
            "east", "ship-7", 8, 2, ["spin", "turn", "spin", "turn", "spin", "turn", "run"]
        )["reason"]
        == "routine_too_long"
    )
    assert world.set_pace("east", "pace-1", 4)["reason"] == "wrong_team_kind"
    assert world.start_line("west", "line-3", 8, 2)["status"] == "running"
    assert world.start_line("west", "line-4", 8, 2)["reason"] == "busy"
    assert world.form_team("west", "form-2", "queue")["reason"] == "busy"


def test_the_serial_line_passes_each_part_down_the_queue_one_by_one():
    world = formed_world()
    assert world.start_line("west", "line-1", 2, 2)["status"] == "running"
    path = [world.parts["part-01"].at]
    for _ in range(1000):
        world.tick()
        at = world.parts["part-01"].at
        if at and (not path or path[-1] != at):
            path.append(at)
        if world.team_status("west")["status"] == "done":
            break
    # The first part visits a1, then a2, then a3, in queue order, then the buffer.
    assert path == ["raw", "robot:a1", "robot:a2", "robot:a3", "west_middle"]
    assert world.contents["west_middle"] == ["part-01", "part-02"]
    assert all(world.parts[part].stage == "machined" for part in world.contents["west_middle"])
    assert world.command("west", "line-1")["status"] == "finished"


def test_a_fast_line_in_front_of_a_slow_line_blocks_and_a_slower_pace_relieves_it():
    world = formed_world()
    start_order(world)
    for _ in range(60):
        run(world, 1)
        west = world.team_status("west")
        if west["program"]["blocked_for_seconds"] >= world.scene.blocked_report_seconds:
            break
    assert west["status"] == "blocked"
    assert west["program"]["blocked_for_seconds"] >= world.scene.blocked_report_seconds
    assert len(world.contents["west_middle"]) == world.scene.places["west_middle"].capacity
    assert all(world.robots[name].label == "blocked" for name in ("a1", "a2", "a3"))
    # Takt-time balancing: the supervisor sets Team A to the pace of Team B.
    assert world.set_pace("west", "pace-1", 8)["status"] == "finished"
    assert world.team_status("west")["cycle_seconds"] == 8
    blocked_before = world.team_status("west")["program"]["blocked_seconds"]
    run(world, 30)
    blocked_after = world.team_status("west")["program"]["blocked_seconds"]
    assert blocked_after - blocked_before < 10


def test_the_parallel_lines_work_at_the_same_time():
    world = formed_world()
    start_order(world)
    busy = set()
    for _ in range(2500):
        world.tick()
        working = {name for name in ("b1", "b2") if world.robots[name].label == "working"}
        if len(working) == 2:
            busy = working
            break
    assert busy == {"b1", "b2"}


def test_the_formation_spins_turns_and_runs_to_the_dock_as_one_shape():
    world = formed_world()
    team = world.scene.teams["east"]
    for name in ("part-01", "part-02"):
        world._send(name, "place", "middle_east")
        world.parts[name].stage = "assembled"
    run(world, 6)
    assert len(world.contents["middle_east"]) == 2
    home = team.centre()
    assert world.start_shipping("east", "ship-1", 2, 2, ["spin", "turn", "run"])["status"]
    labels, headings, centres = set(), [], []
    for _ in range(1500):
        world.tick()
        labels.add(world.robots["c1"].label)
        headings.append(world.robots["c1"].heading)
        xs = [world.robots[name].x for name in team.slots]
        ys = [world.robots[name].y for name in team.slots]
        centres.append((sum(xs) / 4, sum(ys) / 4))
        # The shape keeps its size: every robot stays at the same distance from the centre.
        for name in team.slots:
            distance = math.dist((world.robots[name].x, world.robots[name].y), centres[-1])
            assert distance == pytest.approx(team.radius(), abs=1e-6)
        if world.team_status("east")["status"] == "done":
            break
    assert {"loading", "spinning", "turning", "running"} <= labels
    assert world.contents["dock"] == ["part-01", "part-02"]
    assert max(c[0] for c in centres) == pytest.approx(team.run_to[0], abs=1e-6)
    assert centres[-1] == pytest.approx(home, abs=1e-6)
    # One turn of 90 degrees: c1 now faces north, and the robots swapped places.
    assert world.robots["c1"].heading % 360 == pytest.approx(90, abs=1e-6)
    assert (world.robots["c1"].x, world.robots["c1"].y) == pytest.approx(
        team.slots["c2"][:2], abs=1e-6
    )


def test_a_full_order_moves_every_part_from_raw_to_the_dock():
    world = formed_world()
    start_order(world)
    for _ in range(200):
        run(world, 1)
        if world.team_status("west")["status"] == "blocked":
            world.set_pace("west", "pace-1", 8)
        if all(world.team_status(zone)["status"] == "done" for zone in FORMATIONS):
            break
    state = world.observe()
    assert {zone: team["status"] for zone, team in state["teams"].items()} == {
        "west": "done",
        "middle": "done",
        "east": "done",
    }
    assert sorted(state["places"]["dock"]["parts"]) == world.part_names
    assert {part["stage"] for part in state["parts"].values()} == {"assembled"}
    assert all(team["program"]["output"] == 16 for team in state["teams"].values())
    assert state["teams"]["middle"]["program"]["starved_seconds"] > 0
    # Robots that walk home after the last part become idle when they arrive.
    run(world, 5)
    assert {robot.label for robot in world.robots.values()} == {"idle"}
    finished = {
        (c["actor"], c["command_id"]) for c in state["commands"] if c["status"] == "finished"
    }
    assert {("west", "line-1"), ("middle", "line-1"), ("east", "ship-1")} <= finished


def test_observe_reports_live_poses_but_not_the_start_poses():
    world = World(scene(), allow_motion=True)
    state = world.observe()
    assert "xy" not in state["scene"]["robots"]["a1"]
    assert state["teams"]["west"]["robots"]["a1"]["xy"] == pytest.approx([-3.6, 1.0])
    assert state["places"]["raw"]["parts"] == world.part_names
    json.dumps(state, allow_nan=False)


def test_stop_interrupts_programs_and_locks_the_run():
    world = formed_world()
    start_order(world)
    run(world, 2)
    state = world.stop()
    assert state["stopped"] is True
    statuses = {(c["actor"], c["command_id"]): c for c in state["commands"]}
    assert statuses["west", "line-1"]["status"] == "interrupted"
    assert statuses["west", "line-1"]["reason"] == "operator_stop"
    poses = {name: (robot.x, robot.y) for name, robot in world.robots.items()}
    run(world, 2)
    assert {name: (robot.x, robot.y) for name, robot in world.robots.items()} == poses
    assert world.form_team("west", "form-9", "queue")["reason"] == "stopped"
    # A program that the stop interrupted reports stopped, not its last status.
    assert {zone: world.team_status(zone)["status"] for zone in FORMATIONS} == {
        "west": "stopped",
        "middle": "stopped",
        "east": "stopped",
    }
    assert world.set_pace("west", "pace-9", 4)["reason"] == "stopped"


def test_the_controller_thread_runs_the_world_and_closes():
    world = World(scene(), allow_motion=True)
    world.start()
    try:
        world.form_team("west", "form-1", "queue")
        deadline = time.monotonic() + 20
        while world.command("west", "form-1")["status"] != "finished":
            assert time.monotonic() < deadline
            time.sleep(0.05)
        with pytest.raises(RuntimeError):
            world.start()
    finally:
        state = world.close()
    assert state["stopped"] is True
    assert not any(t.name == "factory-simulation" for t in threading.enumerate())


def test_parts_in_flight_to_one_place_go_to_different_slots():
    world = World(scene(), allow_motion=True)
    for name in ("part-01", "part-02", "part-03"):
        world._send(name, "place", "dock")
    goals = [
        world._slot_xyz("dock", world.parts[name].slot)
        for name in ("part-01", "part-02", "part-03")
    ]
    assert len({tuple(goal) for goal in goals}) == 3
    run(world, 10)
    positions = {tuple(world.parts[name].xyz) for name in ("part-01", "part-02", "part-03")}
    assert len(positions) == 3
    assert world.contents["dock"] == ["part-01", "part-02", "part-03"]


def test_a_robot_within_the_arrival_tolerance_is_at_its_goal():
    world = World(scene(), allow_motion=True)
    robot = world.robots["a1"]
    tolerance = world.scene.arrival_tolerance_m
    robot.goal = (robot.x + 0.5 * tolerance, robot.y, robot.heading)
    assert world._drive(robot, 1e-6, 0.8)
    assert robot.goal is None
