"""The cell's simulator: one MuJoCo world built and driven through Strands Robots.

Every motion here comes from a robot controller that Strands Robots runs:

* the humanoid walks with the ``holosoma`` locomotion policy (Amazon FAR,
  Apache-2.0 code and weights, fetched on first use at a pinned, sha256-checked
  revision), commanded with a base-frame velocity;
* the arm moves with inverse-kinematics ``move_to`` primitives.

Each robot has its own MuJoCo world, placed in the shared cell frame, the way
Strands Robots gives each robot its own ``Robot()``. The humanoid's world
advances only while its policy runs: a G1 with no balance controller running
falls over, and a real G1 sits in its standing controller between tasks, so the
example pauses that world instead of simulating an unpowered robot. The
interlock between the robots compares measured positions in the cell frame.

The SO-101 friction grasp does not lift a part in MuJoCo (see the Strands Robots
example ``18_so101_pick_and_lift.py``), so the arm holds the part with
``attach_bodies(mode="weld")`` while it carries it. That is a grasp assist, not
a physical grasp, and every pick reports ``grasp_assist: true``.

Each world also has a fixed ``cell_view`` camera for the live view. Rendering a
frame only reads the world; Strands Robots serializes it with the policy's own
stepping, so watching never moves a robot.
"""

from __future__ import annotations

import math
import time

from cell import Point, Pose, Scene

LIFT_M = 0.12
HOVER_M = 0.10
VIEW = "cell_view"
VIEW_SIZE = (640, 400)
ARM_SPEED_M_S = 0.15  # pace of the arm's moves in wall time, so a person can watch them
WAYPOINT_M = 0.02
BIN_COLORS = ([0.2, 0.7, 0.35, 1.0], [0.95, 0.65, 0.15, 1.0])


def _payload(result: dict) -> dict:
    """Return the JSON part of a Strands tool envelope, or the envelope itself."""
    if "position" in result:
        return result
    for item in result.get("content", []):
        if isinstance(item, dict) and "json" in item:
            return item["json"]
    return result


def _text(result: dict) -> str:
    return "; ".join(
        item["text"]
        for item in result.get("content", [])
        if isinstance(item, dict) and item.get("text")
    )


class StrandsBackend:
    def __init__(self) -> None:
        self.worlds: dict = {}
        self.scene: Scene | None = None
        self.part_world: str | None = None

    def start(self, scene: Scene) -> None:
        from strands_robots.simulation import create_simulation

        self.scene = scene
        for name, humanoid in scene.humanoids.items():
            sim = self.worlds[name] = create_simulation("mujoco")
            self._ok("create_world", sim.create_world())
            point = scene.locations[humanoid.start]
            self._ok(
                f"add_robot({name})",
                sim.add_robot(humanoid.model, position=[point.x, point.y, 0.0]),
            )
        for name, arm in scene.arms.items():
            sim = self.worlds[name] = create_simulation("mujoco")
            self._ok("create_world", sim.create_world())
            self._ok(
                f"add_robot({name})",
                sim.add_robot(arm.model, position=[arm.base.x, arm.base.y, 0.0]),
            )
        # The parts sit on the arm's table, so they live in the arm's world.
        self.part_world = next(iter(scene.arms))
        parts_sim = self.worlds[self.part_world]
        for name, part in scene.parts.items():
            self._ok(
                f"add_object({name})",
                parts_sim.add_object(
                    name=name,
                    shape="box",
                    position=[part.at.x, part.at.y, part.size_m / 2],
                    size=[part.size_m] * 3,
                    color=[0.9, 0.25, 0.2, 1.0],
                    mass=0.02,
                ),
            )
        # A thin static pad under each bin, so the view shows where the parts go.
        # The bins are only positions to the cell; a pad changes no measurement.
        for index, (name, point) in enumerate(scene.bins.items()):
            self._ok(
                f"add_object({name})",
                parts_sim.add_object(
                    name=f"{name}_pad",
                    shape="box",
                    position=[point.x, point.y, 0.001],
                    size=[0.1, 0.1, 0.002],
                    color=BIN_COLORS[index % len(BIN_COLORS)],
                    is_static=True,
                ),
            )
        self._ok("step(settle)", parts_sim.step(200))
        self._add_views(scene)

    def _add_views(self, scene: Scene) -> None:
        """One fixed camera per world, framed on what that world's robot does."""
        width, height = VIEW_SIZE
        floor = [*scene.locations.values(), *(arm.base for arm in scene.arms.values())]
        cx = (min(p.x for p in floor) + max(p.x for p in floor)) / 2
        cy = (min(p.y for p in floor) + max(p.y for p in floor)) / 2
        for name in scene.humanoids:
            # Behind the floor's front edge, high enough to see every location.
            front = min(p.y for p in floor) - 1.9
            self._ok(
                f"add_camera({name})",
                self.worlds[name].add_camera(
                    VIEW,
                    position=[cx, front, 1.7],
                    target=[cx, cy, 0.35],
                    width=width,
                    height=height,
                ),
            )
        for name, arm in scene.arms.items():
            table = [part.at for part in scene.parts.values()] + list(scene.bins.values())
            tx = sum(p.x for p in table) / len(table)
            ty = sum(p.y for p in table) / len(table)
            self._ok(
                f"add_camera({name})",
                self.worlds[name].add_camera(
                    VIEW,
                    position=[tx + 0.05, ty - 0.5, 0.42],
                    target=[tx, (ty + arm.base.y) / 2, 0.06],
                    width=width,
                    height=height,
                ),
            )

    def frame(self, world: str):
        """The latest picture of one world, as an (H, W, 3) uint8 array."""
        rgb, _depth = self.worlds[world].get_frame(VIEW)
        return rgb

    def project(
        self, world: str, points: dict[str, tuple[float, float, float]]
    ) -> dict[str, list[int]]:
        """Pixel positions of named cell-frame points in a world's view (points behind the camera are left out)."""
        import numpy as np

        params = self.worlds[world].get_camera_params(VIEW)
        cam_from_world = np.linalg.inv(params.T_world_cam)
        pixels = {}
        for label, (x, y, z) in points.items():
            px, py, pz, _ = cam_from_world @ np.array([x, y, z, 1.0])
            if pz >= -1e-6:  # OpenGL camera frame: -Z is forward
                continue
            u = params.K[0, 0] * px / -pz + params.K[0, 2]
            v = params.K[1, 1] * -py / -pz + params.K[1, 2]
            if 0 <= u < params.width and 0 <= v < params.height:
                pixels[label] = [round(float(u)), round(float(v))]
        return pixels

    def _ok(self, step: str, result: dict) -> dict:
        if result.get("status") == "error":
            raise RuntimeError(f"{step} was refused: {_text(result)}")
        return result

    def _model(self, robot: str) -> str:
        assert self.scene is not None
        if robot in self.scene.humanoids:
            return self.scene.humanoids[robot].model
        return self.scene.arms[robot].model

    def humanoid_pose(self, robot: str) -> Pose:
        assert self.scene is not None
        state = _payload(self.worlds[robot].get_body_state(self.scene.humanoids[robot].body))
        x, y, z = state["position"]
        w, qx, qy, qz = state["quaternion"]
        yaw = math.atan2(2 * (w * qz + qx * qy), 1 - 2 * (qy * qy + qz * qz))
        return Pose(x, y, z, yaw)

    def part_position(self, part: str) -> tuple[float, float, float]:
        x, y, z = _payload(self.worlds[self.part_world].get_body_state(part))["position"]
        return x, y, z

    def walk_segment(self, robot: str, velocity: tuple[float, float], duration_s: float) -> dict:
        assert self.scene is not None
        result = self._ok(
            "run_policy",
            self.worlds[robot].run_policy(
                robot_name=self._model(robot),
                policy_provider=self.scene.humanoids[robot].policy,
                policy_kwargs={"target_velocity": [velocity[0], velocity[1], 0.0]},
                duration=duration_s,
                control_frequency=50.0,
            ),
        )
        return {"status": "ok", "summary": _payload(result).get("stopped_reason")}

    def pick_and_place(self, arm: str, part: str, target: Point) -> dict:
        assert self.scene is not None
        spec = self.scene.arms[arm]
        model = spec.model
        steps: list[str] = []
        sim = self.worlds[arm]
        x, y, z = self.part_position(part)
        at = list(_payload(sim.get_body_state(spec.gripper_body))["position"])

        def step(name: str, result: dict) -> None:
            if result.get("status") == "error":
                raise _ArmRefused(name, _text(result), steps)
            steps.append(name)

        def move(name: str, goal: list[float]) -> None:
            """move_to in short waypoints, paced to ARM_SPEED_M_S, so the view shows the path.

            The IK primitive itself runs as fast as the CPU allows; only the wait
            between waypoints is paced, and it holds no lock of the world.
            """
            distance = math.dist(at, goal)
            count = max(1, math.ceil(distance / WAYPOINT_M))
            for index in range(1, count + 1):
                started = time.monotonic()
                waypoint = [a + (b - a) * index / count for a, b in zip(at, goal)]
                result = sim.move_to(robot_name=model, position=waypoint, tol=0.02)
                if result.get("status") == "error":
                    raise _ArmRefused(name, _text(result), steps)
                time.sleep(
                    max(0.0, distance / count / ARM_SPEED_M_S - (time.monotonic() - started))
                )
            at[:] = goal
            steps.append(name)

        try:
            step("open", sim.set_gripper(robot_name=model, state="open"))
            move("hover", [x, y, z + HOVER_M])
            move("descend", [x, y, z + 0.005])
            step("close", sim.set_gripper(robot_name=model, state="close"))
            step(
                "grasp_assist", sim.attach_bodies(parent=spec.gripper_body, child=part, mode="weld")
            )
            move("lift", [x, y, z + LIFT_M])
            move("carry", [target.x, target.y, z + HOVER_M])
            move("lower", [target.x, target.y, z + 0.02])
            step("release", sim.detach_bodies(parent=spec.gripper_body, child=part))
            step("open", sim.set_gripper(robot_name=model, state="open"))
            step("settle", sim.step(150))
        except _ArmRefused as refused:
            # Never leave a part welded to the gripper after a refusal.
            sim.detach_bodies(parent=spec.gripper_body, child=part)
            return {
                "status": "refused",
                "detail": f"{refused.step}: {refused.detail}",
                "grasp_assist": True,
                "steps": steps,
            }
        return {"status": "ok", "grasp_assist": True, "steps": steps}

    def close(self) -> None:
        self.worlds.clear()


class _ArmRefused(RuntimeError):
    def __init__(self, step: str, detail: str, steps: list[str]) -> None:
        super().__init__(f"{step}: {detail}")
        self.step, self.detail, self.steps = step, detail, steps
