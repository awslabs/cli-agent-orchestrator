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
"""

from __future__ import annotations

import math

from cell import Point, Pose, Scene

LIFT_M = 0.12
HOVER_M = 0.10


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
        item["text"] for item in result.get("content", []) if isinstance(item, dict) and item.get("text")
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
            self._ok(f"add_robot({name})", sim.add_robot(humanoid.model, position=[point.x, point.y, 0.0]))
        for name, arm in scene.arms.items():
            sim = self.worlds[name] = create_simulation("mujoco")
            self._ok("create_world", sim.create_world())
            self._ok(f"add_robot({name})", sim.add_robot(arm.model, position=[arm.base.x, arm.base.y, 0.0]))
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
        self._ok("step(settle)", parts_sim.step(200))

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

        def step(name: str, result: dict) -> None:
            if result.get("status") == "error":
                raise _ArmRefused(name, _text(result), steps)
            steps.append(name)

        try:
            step("open", sim.set_gripper(robot_name=model, state="open"))
            step("hover", sim.move_to(robot_name=model, position=[x, y, z + HOVER_M], tol=0.02))
            step("descend", sim.move_to(robot_name=model, position=[x, y, z + 0.005], tol=0.02))
            step("close", sim.set_gripper(robot_name=model, state="close"))
            step("grasp_assist", sim.attach_bodies(parent=spec.gripper_body, child=part, mode="weld"))
            step("lift", sim.move_to(robot_name=model, position=[x, y, z + LIFT_M], tol=0.02))
            step("carry", sim.move_to(robot_name=model, position=[target.x, target.y, z + HOVER_M], tol=0.02))
            step("lower", sim.move_to(robot_name=model, position=[target.x, target.y, z + 0.02], tol=0.02))
            step("release", sim.detach_bodies(parent=spec.gripper_body, child=part))
            step("open", sim.set_gripper(robot_name=model, state="open"))
            step("settle", sim.step(150))
        except _ArmRefused as refused:
            # Never leave a part welded to the gripper after a refusal.
            sim.detach_bodies(parent=spec.gripper_body, child=part)
            return {"status": "refused", "detail": f"{refused.step}: {refused.detail}", "grasp_assist": True, "steps": steps}
        return {"status": "ok", "grasp_assist": True, "steps": steps}

    def close(self) -> None:
        self.worlds.clear()


class _ArmRefused(RuntimeError):
    def __init__(self, step: str, detail: str, steps: list[str]) -> None:
        super().__init__(f"{step}: {detail}")
        self.step, self.detail, self.steps = step, detail, steps
