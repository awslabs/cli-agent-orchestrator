"""A two-robot work cell whose motion comes from each robot's own controller.

The humanoid walks with a learned locomotion policy (Holosoma, run by Strands
Robots). The arm picks and places with inverse-kinematics motion primitives.
Neither motion is written by this example: the example only chooses where each
robot should go, and measures where it went.

One worker thread owns the simulator. Tool calls queue commands and return at
once; ``observe`` reports each command's status and the measured state, which is
the same accept-then-observe contract as ``start_task`` / ``get_task_status`` in
Strands Robots.
"""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any, Protocol

from pydantic import BaseModel, Field, StringConstraints, model_validator

LOGGER = logging.getLogger("cell")

Identifier = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]


class Point(BaseModel):
    x: float
    y: float


class Humanoid(BaseModel):
    model: str = "unitree_g1"
    policy: str = "holosoma"
    start: Identifier
    body: str = "pelvis"


class Arm(BaseModel):
    model: str = "so101"
    base: Point
    reach_m: float = Field(gt=0)
    exclusion_radius_m: float = Field(gt=0)
    gripper_body: str = "so101/gripper"


class Part(BaseModel):
    at: Point
    size_m: float = Field(default=0.025, gt=0)


class Scene(BaseModel):
    """The cell: named floor locations, two robots, the parts and the bins."""

    locations: dict[Identifier, Point]
    humanoids: dict[Identifier, Humanoid]
    arms: dict[Identifier, Arm]
    parts: dict[Identifier, Part]
    bins: dict[Identifier, Point]
    walk_tolerance_m: float = Field(default=0.25, gt=0)
    bin_tolerance_m: float = Field(default=0.06, gt=0)
    fallen_height_m: float = Field(default=0.55, gt=0)
    walk_budget_s: float = Field(default=40.0, gt=0)

    @model_validator(mode="after")
    def check_references(self) -> Scene:
        for name, humanoid in self.humanoids.items():
            if humanoid.start not in self.locations:
                raise ValueError(f"humanoid {name} starts at unknown location {humanoid.start}")
        for arm_name, arm in self.arms.items():
            for kind, places in (("part", self.parts), ("bin", self.bins)):
                for name, place in places.items():
                    point = place.at if isinstance(place, Part) else place
                    if math.dist((point.x, point.y), (arm.base.x, arm.base.y)) > arm.reach_m:
                        raise ValueError(f"{kind} {name} is out of reach of arm {arm_name}")
        return self


@dataclass
class Pose:
    x: float
    y: float
    z: float
    yaw: float


class Backend(Protocol):
    """What the cell needs from a simulator: build it, read it, drive the robots."""

    def start(self, scene: Scene) -> None: ...

    def humanoid_pose(self, robot: str) -> Pose: ...

    def part_position(self, part: str) -> tuple[float, float, float]: ...

    def walk_segment(self, robot: str, velocity: tuple[float, float], duration_s: float) -> dict: ...

    def pick_and_place(self, arm: str, part: str, target: Point) -> dict: ...

    def close(self) -> None: ...


@dataclass
class Command:
    command_id: str
    kind: str
    caller: str
    robot: str
    arguments: dict
    status: str = "accepted"
    exit_reason: str | None = None
    evidence: dict = field(default_factory=dict)
    accepted_at: str = field(default_factory=lambda: _now())
    finished_at: str | None = None

    def view(self) -> dict:
        return {
            "command_id": self.command_id,
            "kind": self.kind,
            "caller": self.caller,
            "robot": self.robot,
            "arguments": self.arguments,
            "status": self.status,
            "exit_reason": self.exit_reason,
            "evidence": self.evidence,
            "accepted_at": self.accepted_at,
            "finished_at": self.finished_at,
        }


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class Refused(ValueError):
    """A command the cell will not accept, with the reason an agent can act on."""


class Cell:
    def __init__(self, scene: Scene, backend: Backend, *, run_id: str, allow_motion: bool) -> None:
        self.scene = scene
        self.backend = backend
        self.run_id = run_id
        self.allow_motion = allow_motion
        self._commands: dict[str, Command] = {}
        self._queue: queue.Queue[Command | None] = queue.Queue()
        self._lock = threading.RLock()
        self._busy: dict[str, str] = {}
        self._snapshot: dict = {}
        self._stopped = False
        self._thread: threading.Thread | None = None

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self.backend.start(self.scene)
        self._refresh()
        self._thread = threading.Thread(target=self._run, name="cell-worker", daemon=True)
        self._thread.start()

    def close(self) -> dict:
        self._stopped = True
        self._queue.put(None)
        if self._thread is not None:
            self._thread.join(timeout=60)
        state = self.observe()
        self.backend.close()
        return state

    def stop(self) -> dict:
        """Operator stop: refuse every new command. A running segment finishes, then nothing more runs."""
        with self._lock:
            self._stopped = True
            for command in self._commands.values():
                if command.status == "accepted":
                    self._finish(command, "failed", "stopped by the operator before it started")
        state = self.observe()
        state["stopped"] = True
        return state

    # --- reading -----------------------------------------------------------

    def observe(self) -> dict:
        with self._lock:
            return {
                "run_id": self.run_id,
                "observed_at": _now(),
                "motion_allowed": self.allow_motion and not self._stopped,
                "measured": self._snapshot,
                "commands": [command.view() for command in self._commands.values()],
                "scene": {
                    "locations": {k: v.model_dump() for k, v in self.scene.locations.items()},
                    "humanoids": {k: {"model": v.model, "controller": f"{v.policy} policy"} for k, v in self.scene.humanoids.items()},
                    "arms": {
                        k: {
                            "model": v.model,
                            "controller": "inverse-kinematics motion primitives",
                            "base": v.base.model_dump(),
                            "exclusion_radius_m": v.exclusion_radius_m,
                        }
                        for k, v in self.scene.arms.items()
                    },
                    "bins": {k: v.model_dump() for k, v in self.scene.bins.items()},
                    "walk_tolerance_m": self.scene.walk_tolerance_m,
                    "bin_tolerance_m": self.scene.bin_tolerance_m,
                    "fallen_height_m": self.scene.fallen_height_m,
                },
            }

    def _refresh(self) -> None:
        humanoids = {}
        for name in self.scene.humanoids:
            pose = self.backend.humanoid_pose(name)
            humanoids[name] = {
                "position": [round(pose.x, 3), round(pose.y, 3)],
                "pelvis_height_m": round(pose.z, 3),
                "upright": pose.z >= self.scene.fallen_height_m,
                "nearest_location": self._nearest_location(pose),
                "inside_exclusion_of": [
                    arm for arm, spec in self.scene.arms.items() if self._distance(pose, spec.base) < spec.exclusion_radius_m
                ],
            }
        parts = {}
        for name in self.scene.parts:
            x, y, z = self.backend.part_position(name)
            parts[name] = {"position": [round(x, 3), round(y, 3), round(z, 3)], "in_bin": self._bin_of(x, y, z)}
        with self._lock:
            self._snapshot = {"humanoids": humanoids, "parts": parts}

    def _nearest_location(self, pose: Pose) -> str | None:
        best = min(self.scene.locations.items(), key=lambda item: self._distance(pose, item[1]))
        return best[0] if self._distance(pose, best[1]) <= self.scene.walk_tolerance_m else None

    def _bin_of(self, x: float, y: float, z: float) -> str | None:
        if z > 0.05:
            return None  # still held or in the air
        for name, point in self.scene.bins.items():
            if math.dist((x, y), (point.x, point.y)) <= self.scene.bin_tolerance_m:
                return name
        return None

    @staticmethod
    def _distance(pose: Pose, point: Point) -> float:
        return math.dist((pose.x, pose.y), (point.x, point.y))

    # --- commands ----------------------------------------------------------

    def walk_to(self, caller: str, command_id: str, robot: str, location: str) -> dict:
        with self._lock:
            replay = self._replay(command_id, "walk_to", caller, robot, {"location": location})
            if replay is not None:
                return replay
            self._check_motion()
            if robot not in self.scene.humanoids:
                raise Refused(f"{robot} is not a humanoid in this cell; humanoids: {sorted(self.scene.humanoids)}")
            if location not in self.scene.locations:
                raise Refused(f"unknown location {location}; locations: {sorted(self.scene.locations)}")
            return self._accept(Command(command_id, "walk_to", caller, robot, {"location": location}))

    def pick_and_place(self, caller: str, command_id: str, robot: str, part: str, bin: str) -> dict:
        arguments = {"part": part, "bin": bin}
        with self._lock:
            replay = self._replay(command_id, "pick_and_place", caller, robot, arguments)
            if replay is not None:
                return replay
            self._check_motion()
            if robot not in self.scene.arms:
                raise Refused(f"{robot} is not an arm in this cell; arms: {sorted(self.scene.arms)}")
            if part not in self.scene.parts:
                raise Refused(f"unknown part {part}; parts: {sorted(self.scene.parts)}")
            if bin not in self.scene.bins:
                raise Refused(f"unknown bin {bin}; bins: {sorted(self.scene.bins)}")
            if self._snapshot["parts"][part]["in_bin"] is not None:
                raise Refused(f"{part} is already in {self._snapshot['parts'][part]['in_bin']}")
            intruders = self._intruders(robot)
            if intruders:
                raise Refused(
                    f"interlock: {', '.join(intruders)} is inside the {self.scene.arms[robot].exclusion_radius_m} m "
                    f"exclusion radius of {robot}. Move it out first, then observe."
                )
            return self._accept(Command(command_id, "pick_and_place", caller, robot, arguments))

    def _intruders(self, arm: str) -> list[str]:
        return [name for name, state in self._snapshot["humanoids"].items() if arm in state["inside_exclusion_of"]]

    def _check_motion(self) -> None:
        if self._stopped:
            raise Refused("the operator stopped this run; no further motion")
        if not self.allow_motion:
            raise Refused("motion is disabled: the controller was started without --allow-motion")

    def _replay(self, command_id: str, kind: str, caller: str, robot: str, arguments: dict) -> dict | None:
        existing = self._commands.get(command_id)
        if existing is None:
            return None
        if (existing.kind, existing.caller, existing.robot, existing.arguments) != (kind, caller, robot, arguments):
            raise Refused(f"command_id {command_id} was already used for a different command")
        return existing.view()

    def _accept(self, command: Command) -> dict:
        if command.robot in self._busy:
            raise Refused(f"{command.robot} is busy with command {self._busy[command.robot]}; observe until it finishes")
        self._busy[command.robot] = command.command_id
        self._commands[command.command_id] = command
        LOGGER.info("%s accepted %s %s %s", command.caller, command.kind, command.robot, command.arguments)
        self._queue.put(command)
        return command.view()

    def _finish(self, command: Command, status: str, reason: str, **evidence: Any) -> None:
        command.status = status
        command.exit_reason = reason
        command.evidence.update(evidence)
        command.finished_at = _now()
        self._busy.pop(command.robot, None)
        LOGGER.info("%s %s: %s", command.command_id, status, reason)

    # --- worker ------------------------------------------------------------

    def _run(self) -> None:
        while True:
            command = self._queue.get()
            if command is None:
                return
            with self._lock:
                if command.status != "accepted":
                    continue
                if self._stopped:
                    self._finish(command, "failed", "stopped by the operator before it started")
                    continue
                command.status = "running"
            try:
                if command.kind == "walk_to":
                    self._walk(command)
                else:
                    self._pick(command)
            except Exception as error:  # a backend fault must end the command, not the worker
                LOGGER.exception("command %s faulted", command.command_id)
                with self._lock:
                    self._finish(command, "failed", f"controller fault: {error}")
            finally:
                self._refresh()

    def _walk(self, command: Command) -> None:
        goal = self.scene.locations[command.arguments["location"]]
        start = time.monotonic()
        segments = 0
        while True:
            pose = self.backend.humanoid_pose(command.robot)
            distance = self._distance(pose, goal)
            evidence = {
                "controller": f"{self.scene.humanoids[command.robot].policy} policy",
                "segments": segments,
                "final_position": [round(pose.x, 3), round(pose.y, 3)],
                "distance_to_goal_m": round(distance, 3),
                "pelvis_height_m": round(pose.z, 3),
            }
            if pose.z < self.scene.fallen_height_m:
                with self._lock:
                    self._finish(command, "failed", "the robot fell: pelvis below the fallen height", **evidence)
                return
            if distance <= self.scene.walk_tolerance_m:
                with self._lock:
                    self._finish(command, "succeeded", "measured position is within tolerance of the goal", **evidence)
                return
            if self._stopped:
                with self._lock:
                    self._finish(command, "failed", "stopped by the operator", **evidence)
                return
            if time.monotonic() - start > self.scene.walk_budget_s:
                with self._lock:
                    self._finish(command, "failed", "time budget exhausted before reaching the goal", **evidence)
                return
            # Command a base-frame velocity toward the goal. The policy turns it
            # into joint targets; this loop never sets a joint.
            dx, dy = goal.x - pose.x, goal.y - pose.y
            cos, sin = math.cos(-pose.yaw), math.sin(-pose.yaw)
            bx, by = cos * dx - sin * dy, sin * dx + cos * dy
            scale = min(0.45, 0.6 * distance) / max(distance, 1e-6)
            self.backend.walk_segment(command.robot, (bx * scale, by * scale), 2.0)
            segments += 1
            self._refresh()

    def _pick(self, command: Command) -> None:
        part, bin_name = command.arguments["part"], command.arguments["bin"]
        with self._lock:
            intruders = self._intruders(command.robot)
        if intruders:  # recheck at execution time, not only at acceptance
            with self._lock:
                self._finish(command, "failed", f"interlock: {', '.join(intruders)} entered the exclusion radius")
            return
        target = self.scene.bins[bin_name]
        result = self.backend.pick_and_place(command.robot, part, target)
        x, y, z = self.backend.part_position(part)
        landed = self._bin_of(x, y, z)
        evidence = {
            "controller": "inverse-kinematics motion primitives",
            "grasp_assist": result.get("grasp_assist", False),
            "final_position": [round(x, 3), round(y, 3), round(z, 3)],
            "in_bin": landed,
            "steps": result.get("steps", []),
        }
        with self._lock:
            if result.get("status") != "ok":
                self._finish(command, "failed", f"the arm refused: {result.get('detail', 'unknown')}", **evidence)
            elif landed != bin_name:
                self._finish(command, "failed", f"measured part is not in {bin_name}", **evidence)
            else:
                self._finish(command, "succeeded", f"measured part rests in {bin_name}", **evidence)


class FakeBackend:
    """A kinematic stand-in for tests and dry runs: no MuJoCo, no policy."""

    def __init__(self, *, fall_on_segment: int | None = None, drop_parts: frozenset[str] = frozenset()) -> None:
        self.fall_on_segment = fall_on_segment
        self.drop_parts = drop_parts
        self.segments = 0
        self.poses: dict[str, Pose] = {}
        self.parts: dict[str, list[float]] = {}

    def start(self, scene: Scene) -> None:
        for name, humanoid in scene.humanoids.items():
            point = scene.locations[humanoid.start]
            self.poses[name] = Pose(point.x, point.y, 0.79, 0.0)
        for name, part in scene.parts.items():
            self.parts[name] = [part.at.x, part.at.y, part.size_m / 2]

    def humanoid_pose(self, robot: str) -> Pose:
        return self.poses[robot]

    def part_position(self, part: str) -> tuple[float, float, float]:
        x, y, z = self.parts[part]
        return x, y, z

    def walk_segment(self, robot: str, velocity: tuple[float, float], duration_s: float) -> dict:
        self.segments += 1
        pose = self.poses[robot]
        if self.fall_on_segment is not None and self.segments >= self.fall_on_segment:
            pose.z = 0.2
            return {"status": "ok"}
        pose.x += velocity[0] * duration_s
        pose.y += velocity[1] * duration_s
        return {"status": "ok"}

    def pick_and_place(self, arm: str, part: str, target: Point) -> dict:
        if part in self.drop_parts:
            self.parts[part] = [target.x + 0.2, target.y, 0.0125]
        else:
            self.parts[part] = [target.x, target.y, 0.0125]
        return {"status": "ok", "grasp_assist": True, "steps": ["fake"]}

    def close(self) -> None:
        return None
