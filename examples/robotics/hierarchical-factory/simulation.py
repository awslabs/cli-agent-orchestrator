"""A shared MuJoCo factory floor, not a planner or a physical-robot controller.

Three robot teams work on one floor, one team in each zone. A manager agent
starts and paces the program of its team through the MCP tools in
factory_mcp.py. The robots then run the program themselves:

- serial: the robots stand in one queue. A part goes from robot to robot, and
  each robot works on it for the same cycle time.
- parallel: each robot is its own assembly line. It fetches a part, assembles
  it, and delivers the unit.
- formation: the robots carry a batch of units together. They do a routine of
  spin, turn, and run moves, and unload the batch at the dock.

The motion is kinematic. Mocap bodies move at set speeds, with no contact,
grasp, or wheel dynamics.
"""

from __future__ import annotations

import logging
import math
import threading
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Annotated, Callable, Literal

import mujoco
import robot_model
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Identifier = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]*$", max_length=64)]
Positive = Annotated[float, Field(gt=0)]
XY = tuple[float, float]
Pose = tuple[float, float, float]
LOGGER = logging.getLogger("factory")
ACTIVE = ("accepted", "running")
MOVES = ("spin", "turn", "run")
TURN_DEGREES = 90.0
SPIN_DEGREES = 360.0
MIN_INTERVAL_SAMPLES = 3


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Zone(Model):
    bounds: tuple[float, float, float, float]

    def contains(self, xy: XY) -> bool:
        x0, y0, x1, y1 = self.bounds
        return x0 <= xy[0] <= x1 and y0 <= xy[1] <= y1


class Place(Model):
    """A raw-material store, a buffer between two zones, or a dock.

    A buffer has a capacity and belongs to the two zones that it connects. A
    store or a dock has no capacity limit.
    """

    xy: XY
    zones: set[Identifier] = Field(min_length=1)
    capacity: int | None = Field(default=None, ge=1, le=8)


class Robot(Model):
    zone: Identifier
    xy: XY
    heading_deg: float = 0
    speed_m_s: Positive


class Team(Model):
    """The team of one zone: its robots, its formation, and its program.

    For a serial team, the order of ``slots`` is the order of the queue, from
    the input to the output.
    """

    label: Annotated[str, StringConstraints(min_length=1, max_length=40)]
    kind: Literal["serial", "parallel", "formation"]
    formation: Identifier
    slots: dict[Identifier, Pose] = Field(min_length=1)
    input: Identifier
    output: Identifier
    min_cycle_seconds: Positive | None = None
    max_cycle_seconds: Positive | None = None
    max_batch: int | None = Field(default=None, ge=1)
    max_routine_moves: int | None = Field(default=None, ge=1)
    run_to: XY | None = None
    run_speed_m_s: Positive | None = None
    turn_deg_s: Positive = 90
    spin_deg_s: Positive = 360

    def centre(self) -> XY:
        xs = [slot[0] for slot in self.slots.values()]
        ys = [slot[1] for slot in self.slots.values()]
        return sum(xs) / len(xs), sum(ys) / len(ys)

    def radius(self) -> float:
        centre = self.centre()
        return max(math.dist(slot[:2], centre) for slot in self.slots.values())


class Scene(Model):
    zones: dict[Identifier, Zone] = Field(min_length=1)
    places: dict[Identifier, Place] = Field(min_length=1)
    robots: dict[Identifier, Robot] = Field(min_length=1)
    teams: dict[Identifier, Team] = Field(min_length=1)
    raw_store: Identifier
    parts: int = Field(ge=1, le=24)
    step_seconds: Positive = 0.02
    part_speed_m_s: Positive = 1.5
    robot_turn_deg_s: Positive = 270
    arrival_tolerance_m: Positive = 0.01
    blocked_report_seconds: Positive = 3
    action_timeout_seconds: Positive = 30

    @model_validator(mode="after")
    def validate_floor(self) -> Scene:
        for zone in self.zones.values():
            x0, y0, x1, y1 = zone.bounds
            if not (x0 < x1 and y0 < y1):
                raise ValueError("zone bounds must describe a nonempty rectangle")
        for place in self.places.values():
            if not place.zones <= self.zones.keys():
                raise ValueError("every place must belong to declared zones")
            if any(not self.zones[zone].contains(place.xy) for zone in place.zones):
                raise ValueError("places must lie inside every owning zone")
            if (place.capacity is not None) != (len(place.zones) > 1):
                raise ValueError("a buffer joins two zones and has a capacity; other places not")
        if self.raw_store not in self.places or self.places[self.raw_store].capacity:
            raise ValueError("raw_store must be a declared store without a capacity")
        for robot in self.robots.values():
            if robot.zone not in self.zones or not self.zones[robot.zone].contains(robot.xy):
                raise ValueError("a robot must start inside its declared zone")
        for name, team in self.teams.items():
            self._validate_team(name, team)
        if set(self.robots) != {robot for team in self.teams.values() for robot in team.slots}:
            raise ValueError("every robot must belong to exactly one team")
        if self.step_seconds > self.action_timeout_seconds:
            raise ValueError("the simulation step must not exceed the action timeout")
        return self

    def _validate_team(self, name: str, team: Team) -> None:
        if name not in self.zones:
            raise ValueError("a team is keyed by the zone that it works in")
        zone = self.zones[name]
        for robot, slot in team.slots.items():
            if robot not in self.robots or self.robots[robot].zone != name:
                raise ValueError("a team may use only the robots of its own zone")
            if not zone.contains(slot[:2]):
                raise ValueError("formation slots must lie inside the zone of the team")
        for place in (team.input, team.output):
            if place not in self.places or name not in self.places[place].zones:
                raise ValueError("the input and output of a team must be places of its zone")
        source, target = self.places[team.input], self.places[team.output]
        if team.kind in ("serial", "parallel"):
            if team.min_cycle_seconds is None or team.max_cycle_seconds is None:
                raise ValueError("a serial or parallel team needs a cycle time range")
            if team.min_cycle_seconds > team.max_cycle_seconds:
                raise ValueError("min_cycle_seconds must not exceed max_cycle_seconds")
            if target.capacity is None:
                raise ValueError("a serial or parallel team delivers to a buffer")
        if team.kind == "serial" and team.input != self.raw_store:
            raise ValueError("a serial team takes its parts from the raw store")
        if team.kind == "parallel" and source.capacity is None:
            raise ValueError("a parallel team takes its parts from a buffer")
        if team.kind == "formation":
            if source.capacity is None or target.capacity is not None:
                raise ValueError("a formation team takes units from a buffer to a dock")
            if team.max_batch is None or team.max_batch > len(team.slots):
                raise ValueError("max_batch must be set and must not exceed the team size")
            # The formation waits for a full batch in its input buffer, and the
            # team before it never fills the buffer past its capacity.
            if team.max_batch > source.capacity:
                raise ValueError("max_batch must not exceed the capacity of the input buffer")
            if team.max_routine_moves is None:
                raise ValueError("max_routine_moves must be set for a formation team")
            if team.run_to is None or team.run_speed_m_s is None:
                raise ValueError("a formation team needs run_to and run_speed_m_s")
            # The formation turns about its centre, so every slot must stay in
            # the zone at any angle, at home and at the end of the run.
            reach = team.radius() + 0.2
            for cx, cy in (team.centre(), team.run_to):
                if not all(
                    zone.contains(corner)
                    for corner in ((cx - reach, cy - reach), (cx + reach, cy + reach))
                ):
                    raise ValueError("a turning formation must stay inside its zone")


# Render colors. Zone tiles are pale; a robot has the strong shade of its zone.
ZONE_COLORS = ("0.56 0.7 0.95 1", "0.6 0.84 0.6 1", "0.95 0.74 0.55 1", "0.78 0.64 0.95 1")
ROBOT_COLORS = (
    (0.12, 0.32, 0.9, 1),
    (0.08, 0.55, 0.2, 1),
    (0.88, 0.42, 0.08, 1),
    (0.5, 0.25, 0.85, 1),
)
BLOCKED_COLOR = (0.86, 0.08, 0.1, 1)
BUFFER_COLOR = "1 0.72 0.1 1"
PLACE_COLOR = "0.3 0.3 0.34 1"
# The robots are Open Duck Mini v2 robots (see robot_model.py): the battery lid
# and the top panel have the color of the team, and a lamp above the head shows
# the state. Without the downloaded model, a simple biped of the same size
# stands in for it, so that the tests and CI need no download.
SHELL_COLOR = (0.92, 0.92, 0.93, 1)
JOINT_COLOR = "0.22 0.22 0.25 1"
VISOR_COLOR = "0.08 0.09 0.11 1"
FOOT_COLOR = "0.93 0.66 0.12 1"
LAMP_COLORS = {
    "working": (0.15, 0.85, 0.3, 1),
    "waiting": (1.0, 0.72, 0.1, 1),
    "blocked": (0.95, 0.1, 0.1, 1),
}
# Part colors by stage: grey raw part, pink machined part, purple assembled unit.
STAGE_COLORS = {
    "raw": (0.62, 0.62, 0.66, 1),
    "machined": (0.9, 0.12, 0.5, 1),
    "assembled": (0.5, 0.18, 0.86, 1),
}
CAMERA_TILT_DEGREES = 50.0
CAMERA_FOVY_DEGREES = 45.0
CAMERA_ASPECT = 16 / 9
PART_HALF = 0.05
STRIDE_M = 0.2
STEP_DEGREES = 30.0
WAITING_LABELS = ("idle", "waiting", "starved")
# The lamp and the carried part of the simple biped, relative to its root.
SIMPLE_LAMP = (0.03, 0.0, 0.47)
SIMPLE_BACK = (-0.03, 0.0, 0.33)


def _rgba(color) -> str:
    return " ".join(str(value) for value in color)


def _add_simple_robot(root: ET.Element, name: str, color: tuple) -> None:
    """A simple biped with the joints of the Open Duck Mini that the world animates."""
    no_contact = {"contype": "0", "conaffinity": "0"}

    def geom(body: ET.Element, part: str, **attributes) -> None:
        ET.SubElement(body, "geom", name=f"robot/{name}/{part}", **attributes, **no_contact)

    def joint(body: ET.Element, joint_name: str, axis: str = "0 1 0") -> None:
        ET.SubElement(body, "joint", name=f"robot/{name}/{joint_name}", type="hinge", axis=axis)

    geom(
        root, "shell", type="box", size="0.09 0.075 0.065", pos="0 0 0.27", rgba=_rgba(SHELL_COLOR)
    )
    for side, y in (("left", 0.078), ("right", -0.078)):
        geom(
            root,
            f"panel_{side}",
            type="box",
            size="0.07 0.005 0.045",
            pos=f"0 {y} 0.27",
            rgba=_rgba(color),
        )
    geom(root, "lid", type="box", size="0.07 0.06 0.006", pos="-0.01 0 0.337", rgba=_rgba(color))
    neck = ET.SubElement(root, "body", name=f"robot/{name}/neck", pos="0.03 0 0.335")
    joint(neck, "neck_pitch")
    geom(neck, "neck", type="capsule", fromto="0 0 0 0 0 0.04", size="0.015", rgba=JOINT_COLOR)
    head = ET.SubElement(neck, "body", name=f"robot/{name}/head", pos="0 0 0.06")
    joint(head, "head_pitch")
    geom(head, "head", type="box", size="0.05 0.07 0.035", pos="0 0 0.0", rgba=_rgba(SHELL_COLOR))
    geom(head, "visor", type="box", size="0.006 0.055 0.018", pos="0.05 0 0.003", rgba=VISOR_COLOR)
    for side, y in (("left", 1), ("right", -1)):
        antenna = ET.SubElement(
            head, "body", name=f"robot/{name}/{side}_antenna_body", pos=f"-0.01 {0.05 * y} 0.035"
        )
        joint(antenna, f"{side}_antenna", axis="1 0 0")
        geom(
            antenna,
            f"antenna_{side}",
            type="capsule",
            fromto="0 0 0 0 0 0.05",
            size="0.004",
            rgba=JOINT_COLOR,
        )
    for side, y, axis in (("left", 0.045, "0 1 0"), ("right", -0.045, "0 -1 0")):
        hip = ET.SubElement(root, "body", name=f"robot/{name}/{side}_hip", pos=f"0 {y} 0.2")
        joint(hip, f"{side}_hip_pitch", axis)
        geom(
            hip,
            f"thigh_{side}",
            type="capsule",
            fromto="0 0 0 0 0 -0.1",
            size="0.02",
            rgba=_rgba(SHELL_COLOR),
        )
        knee = ET.SubElement(hip, "body", name=f"robot/{name}/{side}_shin", pos="0 0 -0.1")
        joint(knee, f"{side}_knee")
        geom(
            knee,
            f"shin_{side}",
            type="capsule",
            fromto="0 0 0 0 0 -0.09",
            size="0.016",
            rgba=JOINT_COLOR,
        )
        foot = ET.SubElement(knee, "body", name=f"robot/{name}/{side}_foot", pos="0 0 -0.09")
        joint(foot, f"{side}_ankle")
        geom(
            foot,
            f"foot_{side}",
            type="box",
            size="0.05 0.025 0.01",
            pos="0.015 0 -0.01",
            rgba=FOOT_COLOR,
        )


def _add_robot(
    parent: ET.Element, name: str, robot: Robot, color: tuple, duck: robot_model.DuckModel | None
) -> None:
    """Add one robot: a mocap root, the Open Duck Mini (or the simple biped), and a lamp.

    The joints only animate the robot: the world sets their angles from the
    motion of the robot. No geom has contact.
    """
    root = ET.SubElement(
        parent,
        "body",
        name=f"robot/{name}",
        mocap="true",
        pos=f"{robot.xy[0]} {robot.xy[1]} 0",
        quat=_rgba(_quat(robot.heading_deg)),
    )
    if duck is not None:
        duck.add_robot(root, f"robot/{name}", _rgba(color))
        lamp = duck.lamp
    else:
        _add_simple_robot(root, name, color)
        lamp = SIMPLE_LAMP
    ET.SubElement(
        root,
        "geom",
        name=f"robot/{name}/lamp",
        type="sphere",
        size="0.022",
        pos=_rgba(lamp),
        rgba=_rgba(LAMP_COLORS["waiting"]),
        contype="0",
        conaffinity="0",
    )


def _add_scene_visuals(root: ET.Element, world: ET.Element, scene: Scene) -> None:
    """Add a floor, zone tiles, place markers, a light, and an overview camera.

    These elements exist only so that the floor can be rendered (see
    recorder.py). Every geom has contype=0 and conaffinity=0, and gravity is
    off, so they cannot change the simulated motion or the measured poses.
    """
    xs = [edge for zone in scene.zones.values() for edge in (zone.bounds[0], zone.bounds[2])]
    ys = [edge for zone in scene.zones.values() for edge in (zone.bounds[1], zone.bounds[3])]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    half_w, half_h = (max(xs) - min(xs)) / 2, (max(ys) - min(ys)) / 2
    no_contact = {"contype": "0", "conaffinity": "0"}

    visual = ET.SubElement(root, "visual")
    ET.SubElement(visual, "global", offwidth="1280", offheight="720")
    ET.SubElement(
        visual, "headlight", ambient="0.3 0.3 0.3", diffuse="0.35 0.35 0.35", specular="0 0 0"
    )
    asset = ET.SubElement(root, "asset")
    ET.SubElement(
        asset,
        "texture",
        type="skybox",
        builtin="gradient",
        rgb1="0.97 0.98 1",
        rgb2="0.82 0.86 0.92",
        width="64",
        height="64",
    )
    ET.SubElement(
        asset,
        "texture",
        name="floor",
        type="2d",
        builtin="checker",
        rgb1="0.86 0.86 0.86",
        rgb2="0.8 0.8 0.8",
        width="256",
        height="256",
    )
    ET.SubElement(asset, "material", name="floor", texture="floor", texrepeat="16 16")
    ET.SubElement(
        world,
        "light",
        name="visual/sun",
        directional="true",
        dir="0 0.4 -1",
        diffuse="0.45 0.45 0.45",
        specular="0 0 0",
    )
    ET.SubElement(
        world,
        "geom",
        name="visual/floor",
        type="plane",
        size=f"{half_w + 0.8} {half_h + 0.8} 0.1",
        pos=f"{cx} {cy} -0.03",
        material="floor",
        **no_contact,
    )
    for index, (name, zone) in enumerate(scene.zones.items()):
        x0, y0, x1, y1 = zone.bounds
        inset = min(0.03, (x1 - x0) / 4, (y1 - y0) / 4)
        ET.SubElement(
            world,
            "geom",
            name=f"visual/zone/{name}",
            type="box",
            size=f"{(x1 - x0) / 2 - inset} {(y1 - y0) / 2 - inset} 0.005",
            pos=f"{(x0 + x1) / 2} {(y0 + y1) / 2} -0.015",
            rgba=ZONE_COLORS[index % len(ZONE_COLORS)],
            **no_contact,
        )
    for name, place in scene.places.items():
        x, y = place.xy
        ET.SubElement(
            world,
            "geom",
            name=f"visual/place/{name}",
            type="cylinder",
            size=f"{0.18 if place.capacity else 0.3} 0.004",
            pos=f"{x} {y} -0.005",
            rgba=BUFFER_COLOR if place.capacity else PLACE_COLOR,
            **no_contact,
        )
    # Look at the centre of the zones from the -y side, tilted from vertical,
    # far enough back that every zone fits a 16:9 frame with a margin.
    tilt = math.radians(CAMERA_TILT_DEGREES)
    tan_v = math.tan(math.radians(CAMERA_FOVY_DEGREES) / 2)
    distance = 1.0 * max((half_w + 0.3) / (tan_v * CAMERA_ASPECT), (half_h + 0.3) / tan_v)
    ET.SubElement(
        world,
        "camera",
        name="overview",
        fovy=str(CAMERA_FOVY_DEGREES),
        pos=f"{cx} {cy - distance * math.sin(tilt)} {distance * math.cos(tilt)}",
        xyaxes=f"1 0 0 0 {math.cos(tilt)} {math.sin(tilt)}",
    )


def _quat(heading_deg: float) -> list[float]:
    half = math.radians(heading_deg) / 2
    return [math.cos(half), 0.0, 0.0, math.sin(half)]


def _rotate(offset: XY, degrees: float) -> XY:
    angle = math.radians(degrees)
    c, s = math.cos(angle), math.sin(angle)
    return offset[0] * c - offset[1] * s, offset[0] * s + offset[1] * c


def _sign(value: float) -> float:
    return 0.0 if abs(value) < 1e-9 else math.copysign(1.0, value)


@dataclass
class Command:
    actor: str
    command_id: str
    operation: str
    arguments: tuple[str, ...]
    deadline: float
    status: str = "accepted"
    reason: str | None = None
    updated_at: str = field(default_factory=utc_now)

    def report(self) -> dict:
        return {
            "actor": self.actor,
            "command_id": self.command_id,
            "operation": self.operation,
            "arguments": list(self.arguments),
            "status": self.status,
            "reason": self.reason,
            "updated_at": self.updated_at,
        }


@dataclass
class RobotState:
    x: float
    y: float
    heading: float
    goal: Pose | None = None
    label: str = "idle"
    carrying: str | None = None
    # The heading to turn to after the robot arrives at its goal.
    face: float | None = None
    # Animation only: the phase of the walk, and the last rendered pose.
    gait: float = 0.0
    shown: Pose | None = None


@dataclass
class PartState:
    stage: str = "raw"
    at: str | None = None
    xyz: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    goal: tuple[str, str] | None = None
    on_arrival: Callable[[], None] | None = None
    # The slot of a place that a part in flight goes to.
    slot: int = 0


@dataclass
class Program:
    command: Command
    target: int
    batch: int | None = None
    routine: tuple[str, ...] = ()
    started: int = 0
    output: int = 0
    output_times: list[float] = field(default_factory=list)
    blocked_for: float = 0.0
    blocked_seconds: float = 0.0
    starved_seconds: float = 0.0
    done: bool = False


@dataclass
class TeamState:
    cycle_seconds: float | None
    formed: bool = False
    forming: Command | None = None
    program: Program | None = None
    # serial: one station for each robot, in queue order
    stations: list[dict] = field(default_factory=list)
    # parallel: one line for each robot
    lines: dict[str, dict] = field(default_factory=dict)
    # formation: the pose of the formation and the step of the routine
    centre: XY = (0.0, 0.0)
    angle: float = 0.0
    spin: float = 0.0
    phase: str = "waiting"
    step: int = 0
    turned: float = 0.0
    leg: str = "out"
    loading: list[str] = field(default_factory=list)
    blocked: bool = False
    starved: bool = False


class World:
    def __init__(self, scene: Scene, *, allow_motion: bool = False, run_id: str | None = None):
        self.scene = scene.model_copy(deep=True)
        self.allow_motion = allow_motion
        self.run_id = run_id or uuid.uuid4().hex
        self.stopped = False
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._commands: dict[tuple[str, str], Command] = {}
        self.robots = {
            name: RobotState(robot.xy[0], robot.xy[1], robot.heading_deg)
            for name, robot in scene.robots.items()
        }
        self.part_names = [f"part-{index:02d}" for index in range(1, scene.parts + 1)]
        self.parts = {name: PartState() for name in self.part_names}
        self.contents: dict[str, list[str]] = {name: [] for name in scene.places}
        self.incoming: dict[str, int] = {name: 0 for name in scene.places}
        self.contents[scene.raw_store] = list(self.part_names)
        self.teams = {
            name: TeamState(cycle_seconds=team.min_cycle_seconds, centre=team.centre())
            for name, team in scene.teams.items()
        }
        zone_index = {zone: index for index, zone in enumerate(scene.zones)}

        duck = robot_model.load() if robot_model.available() else None
        self.robot_look = "open_duck_mini_v2" if duck else "simple"
        self.robot_back = duck.back if duck else SIMPLE_BACK

        root = ET.Element("mujoco", model="cao-hierarchical-factory")
        ET.SubElement(root, "compiler", angle="radian")
        ET.SubElement(root, "option", timestep=str(scene.step_seconds), gravity="0 0 0")
        bodies = ET.SubElement(root, "worldbody")
        _add_scene_visuals(root, bodies, scene)
        if duck is not None:
            duck.add_assets(root.find("asset"))
        no_contact = {"contype": "0", "conaffinity": "0"}
        for name, robot in scene.robots.items():
            color = ROBOT_COLORS[zone_index[robot.zone] % len(ROBOT_COLORS)]
            _add_robot(bodies, name, robot, color, duck)
        for name in self.part_names:
            body = ET.SubElement(bodies, "body", name=f"part/{name}", mocap="true", pos="0 0 0")
            ET.SubElement(
                body,
                "geom",
                name=f"part/{name}/geom",
                type="box",
                size=f"{PART_HALF} {PART_HALF} {PART_HALF}",
                rgba=" ".join(str(value) for value in STAGE_COLORS["raw"]),
                **no_contact,
            )
        self.model = mujoco.MjModel.from_xml_string(ET.tostring(root, encoding="unicode"))
        self.data = mujoco.MjData(self.model)
        self.robot_mocap = {
            name: self.model.body(f"robot/{name}").mocapid[0] for name in scene.robots
        }
        self.part_mocap = {name: self.model.body(f"part/{name}").mocapid[0] for name in self.parts}
        self.robot_lamp = {name: self.model.geom(f"robot/{name}/lamp").id for name in scene.robots}
        # The shell geoms turn red when the robot is blocked; keep their colors.
        self.robot_shell = {name: [] for name in scene.robots}
        for index in range(self.model.ngeom):
            parts = self.model.geom(index).name.split("/")
            if parts[0] == "robot" and (
                parts[2:3] == ["shell"]
                or (parts[2:3] == ["mesh"] and parts[3] in robot_model.SHELL_MESHES)
            ):
                self.robot_shell[parts[1]].append((index, tuple(self.model.geom_rgba[index])))
        self.robot_joints = {
            name: robot_model.joint_addresses(self.model, f"robot/{name}") for name in scene.robots
        }
        self.part_geom = {name: self.model.geom(f"part/{name}/geom").id for name in self.parts}
        self._restack(scene.raw_store)
        self._push()
        self.forward()

    # ----- geometry and rendering state -------------------------------------------------

    def forward(self) -> None:
        mujoco.mj_forward(self.model, self.data)

    def now(self) -> float:
        return float(self.data.time)

    def _slot_xyz(self, place: str, index: int) -> list[float]:
        x, y = self.scene.places[place].xy
        if self.scene.places[place].capacity:
            # A buffer stacks its parts.
            return [x, y, PART_HALF + index * (2 * PART_HALF + 0.01)]
        # A store or a dock lays its parts out in a grid of three columns.
        rows = math.ceil(self.scene.parts / 3)
        column, row = index % 3, index // 3
        return [x + (column - 1) * 0.15, y + (row - (rows - 1) / 2) * 0.15, PART_HALF]

    def _restack(self, place: str) -> None:
        for index, part in enumerate(self.contents[place]):
            self.parts[part].xyz = self._slot_xyz(place, index)
            self.parts[part].at = place

    def _push(self) -> None:
        for name, robot in self.robots.items():
            self._animate(name, robot)
        for name, part in self.parts.items():
            if part.at is not None and part.at.startswith("robot:"):
                part.xyz = self._carry_xyz(part.at.split(":", 1)[1])
            self.data.mocap_pos[self.part_mocap[name]] = part.xyz
            self.model.geom_rgba[self.part_geom[name]] = STAGE_COLORS[part.stage]

    def _animate(self, name: str, robot: RobotState) -> None:
        """Set the rendered pose, the walking legs, the nod, and the colors of a robot."""
        if robot.shown is not None:
            moved = math.dist(robot.shown[:2], (robot.x, robot.y))
            turned = abs((robot.heading - robot.shown[2] + 180) % 360 - 180)
            # A robot steps when it walks, and steps in place when it turns.
            robot.gait += 2 * math.pi * (moved / STRIDE_M + turned / (4 * STEP_DEGREES * 3))
        robot.shown = (robot.x, robot.y, robot.heading)
        stepping = robot.label not in WAITING_LABELS + ("blocked", "working")
        working = robot.label == "working"
        # A working robot nods over its part and moves its antennas.
        nod = 0.3 * math.sin(self.now() * 7) if working else 0.0
        wiggle = 0.6 * math.sin(self.now() * 9) if working else 0.0
        robot_model.pose(
            self.data.qpos,
            self.robot_joints[name],
            robot.gait,
            1.0 if stepping else 0.0,
            nod,
            wiggle,
        )
        mocap = self.robot_mocap[name]
        self.data.mocap_pos[mocap] = [robot.x, robot.y, 0.0]
        self.data.mocap_quat[mocap] = _quat(robot.heading)
        for index, color in self.robot_shell[name]:
            self.model.geom_rgba[index] = BLOCKED_COLOR if robot.label == "blocked" else color
        if robot.label == "blocked":
            light = LAMP_COLORS["blocked"]
        elif robot.label in WAITING_LABELS:
            light = LAMP_COLORS["waiting"]
        else:
            light = LAMP_COLORS["working"]
        self.model.geom_rgba[self.robot_lamp[name]] = light

    def _carry_xyz(self, robot: str) -> list[float]:
        """Where a robot carries a part: on its back, behind the head."""
        state = self.robots[robot]
        dx, dy = _rotate(self.robot_back[:2], state.heading)
        return [state.x + dx, state.y + dy, self.robot_back[2] + PART_HALF]

    def _xy(self, robot: str) -> XY:
        pose = self.data.body(f"robot/{robot}").xpos
        return float(pose[0]), float(pose[1])

    # ----- commands ---------------------------------------------------------------------

    def _new(
        self, actor: str, command_id: str, operation: str, *arguments: str
    ) -> tuple[Command, bool]:
        previous = self._commands.get((actor, command_id))
        if previous:
            if (previous.operation, previous.arguments) == (operation, arguments):
                return previous, False
            return (
                Command(
                    actor, command_id, operation, arguments, 0, "rejected", "command_id_conflict"
                ),
                False,
            )
        command = Command(
            actor, command_id, operation, arguments, self.now() + self.scene.action_timeout_seconds
        )
        self._commands[actor, command_id] = command
        if not command_id.strip():
            self._set(command, "rejected", "empty_command_id")
            return command, False
        return command, True

    def _set(self, command: Command, status: str, reason: str | None = None) -> dict:
        command.status, command.reason, command.updated_at = status, reason, utc_now()
        LOGGER.info(
            "run=%s actor=%s command=%s operation=%s status=%s reason=%s",
            self.run_id,
            command.actor,
            command.command_id,
            command.operation,
            status,
            reason,
        )
        return command.report()

    def _common_error(self, actor: str) -> str | None:
        if self.stopped:
            return "stopped"
        if not self.allow_motion:
            return "motion_not_approved"
        if actor not in self.scene.teams:
            return "no_team_in_zone"
        return None

    def _cycle_error(self, team: Team, cycle_seconds: float) -> str | None:
        if team.kind == "formation":
            return "wrong_team_kind"
        if not team.min_cycle_seconds <= cycle_seconds <= team.max_cycle_seconds:
            return "pace_out_of_range"
        return None

    def form_team(self, actor: str, command_id: str, formation: str) -> dict:
        """Drive every robot of the team of ``actor`` to its formation slot."""
        with self.lock:
            command, new = self._new(actor, command_id, "form_team", formation)
            if not new:
                return command.report()
            reason = self._common_error(actor)
            state = self.teams.get(actor)
            if reason is None:
                team = self.scene.teams[actor]
                if formation != team.formation:
                    reason = "formation_unavailable"
                elif state.program is not None and not state.program.done:
                    reason = "busy"
                elif state.forming is not None:
                    reason = "busy"
            if reason:
                return self._set(command, "rejected", reason)
            team = self.scene.teams[actor]
            state.formed, state.forming, state.program = False, command, None
            state.centre, state.angle, state.spin = team.centre(), 0.0, 0.0
            for robot, slot in team.slots.items():
                self._go(robot, slot[:2], face=slot[2])
                self.robots[robot].label = "moving"
            return self._set(command, "accepted")

    def start_line(self, actor: str, command_id: str, parts: int, cycle_seconds: float) -> dict:
        """Start the program of a serial or a parallel team for ``parts`` parts."""
        with self.lock:
            command, new = self._new(
                actor, command_id, "start_line", str(parts), f"{cycle_seconds:g}"
            )
            if not new:
                return command.report()
            reason = self._start_error(actor, parts)
            if reason is None:
                reason = self._cycle_error(self.scene.teams[actor], cycle_seconds)
            if reason is None and self.scene.teams[actor].kind == "serial":
                if parts > len(self.contents[self.scene.raw_store]):
                    reason = "not_enough_raw_parts"
            if reason:
                return self._set(command, "rejected", reason)
            state = self.teams[actor]
            state.cycle_seconds = cycle_seconds
            state.program = Program(command=command, target=parts)
            team = self.scene.teams[actor]
            if team.kind == "serial":
                state.stations = [
                    {"robot": robot, "part": None, "timer": None, "ready": False}
                    for robot in team.slots
                ]
            else:
                state.lines = {
                    robot: {"phase": "home", "part": None, "timer": 0.0} for robot in team.slots
                }
            return self._set(command, "running")

    def start_shipping(
        self, actor: str, command_id: str, units: int, batch: int, routine: list[str]
    ) -> dict:
        """Start the program of a formation team: ship ``units`` units in batches."""
        with self.lock:
            command, new = self._new(
                actor, command_id, "start_shipping", str(units), str(batch), *routine
            )
            if not new:
                return command.report()
            reason = self._start_error(actor, units)
            team = self.scene.teams.get(actor)
            if reason is None:
                if team.kind != "formation":
                    reason = "wrong_team_kind"
                elif not 1 <= batch <= team.max_batch:
                    reason = "batch_out_of_range"
                elif len(routine) > team.max_routine_moves:
                    reason = "routine_too_long"
                elif any(move not in MOVES for move in routine) or routine.count("run") != 1:
                    reason = "routine_needs_one_run"
            if reason:
                return self._set(command, "rejected", reason)
            state = self.teams[actor]
            state.program = Program(
                command=command, target=units, batch=batch, routine=tuple(routine)
            )
            state.phase, state.step, state.loading = "waiting", 0, []
            return self._set(command, "running")

    def _start_error(self, actor: str, count: int) -> str | None:
        reason = self._common_error(actor)
        if reason:
            return reason
        state = self.teams[actor]
        if state.forming is not None or (state.program is not None and not state.program.done):
            return "busy"
        if not state.formed:
            return "formation_not_ready"
        if not 1 <= count <= self.scene.parts:
            return "count_out_of_range"
        return None

    def set_pace(self, actor: str, command_id: str, cycle_seconds: float) -> dict:
        """Change the cycle time of a serial or a parallel team; new work uses it."""
        with self.lock:
            command, new = self._new(actor, command_id, "set_pace", f"{cycle_seconds:g}")
            if not new:
                return command.report()
            reason = self._common_error(actor)
            if reason is None:
                reason = self._cycle_error(self.scene.teams[actor], cycle_seconds)
            if reason:
                return self._set(command, "rejected", reason)
            self.teams[actor].cycle_seconds = cycle_seconds
            return self._set(command, "finished")

    def command(self, actor: str, command_id: str) -> dict:
        with self.lock:
            command = self._commands.get((actor, command_id))
            return (
                command.report()
                if command
                else {"actor": actor, "command_id": command_id, "status": "unknown"}
            )

    # ----- motion -----------------------------------------------------------------------

    def _drive(self, robot: RobotState, dt: float, speed: float) -> bool:
        """Move a robot toward its goal; return True when it is there."""
        if robot.goal is None:
            return True
        gx, gy, gh = robot.goal
        distance = math.dist((robot.x, robot.y), (gx, gy))
        step = speed * dt
        # Within the arrival tolerance of the scene, the robot is at its goal.
        if distance <= max(step, self.scene.arrival_tolerance_m):
            robot.x, robot.y = gx, gy
        else:
            robot.x += (gx - robot.x) * step / distance
            robot.y += (gy - robot.y) * step / distance
        turn = (gh - robot.heading + 180) % 360 - 180
        limit = self.scene.robot_turn_deg_s * dt
        robot.heading = gh if abs(turn) <= limit else robot.heading + math.copysign(limit, turn)
        if (robot.x, robot.y) == (gx, gy) and robot.heading == gh:
            if robot.face is not None and robot.face != gh:
                robot.goal, robot.face = (gx, gy, robot.face), None
                return False
            robot.goal = robot.face = None
            return True
        return False

    def _go(self, robot: str, xy: XY, face: float | None = None) -> None:
        """Walk a robot to ``xy`` facing the way it walks; then turn it to ``face``."""
        state = self.robots[robot]
        heading = state.heading
        if math.dist((state.x, state.y), xy) > self.scene.arrival_tolerance_m:
            heading = math.degrees(math.atan2(xy[1] - state.y, xy[0] - state.x))
        state.goal, state.face = (xy[0], xy[1], heading), face

    def _arrived(self, robot: str) -> bool:
        return self.robots[robot].goal is None

    def _send(self, part: str, kind: str, target: str, on_arrival=None) -> None:
        """Start a part moving to a robot or a place."""
        state = self.parts[part]
        if state.at is not None and not state.at.startswith("robot:"):
            place = state.at
            self.contents[place].remove(part)
            self._restack(place)
        elif state.at is not None:
            self.robots[state.at.split(":", 1)[1]].carrying = None
        state.at, state.goal, state.on_arrival = None, (kind, target), on_arrival
        if kind == "place":
            # Each part in flight to a place gets its own slot, after the parts
            # there and the parts already on their way.
            state.slot = len(self.contents[target]) + self.incoming[target]
            self.incoming[target] += 1
        else:
            self.robots[target].carrying = part

    def _move_parts(self, dt: float) -> None:
        for name, part in self.parts.items():
            if part.goal is None:
                continue
            kind, target = part.goal
            if kind == "robot":
                goal = self._carry_xyz(target)
            else:
                goal = self._slot_xyz(target, part.slot)
            distance = math.dist(part.xyz, goal)
            step = self.scene.part_speed_m_s * dt
            if distance > step:
                part.xyz = [v + (g - v) * step / distance for v, g in zip(part.xyz, goal)]
                continue
            part.xyz, part.goal = goal, None
            if kind == "robot":
                part.at = f"robot:{target}"
            else:
                self.incoming[target] -= 1
                self.contents[target].append(name)
                part.at = target
                # A part can leave the place while another is in flight, so the
                # parts there close up into the first slots.
                self._restack(target)
            callback, part.on_arrival = part.on_arrival, None
            if callback:
                callback()

    def _space(self, place: str) -> bool:
        capacity = self.scene.places[place].capacity
        return capacity is None or len(self.contents[place]) + self.incoming[place] < capacity

    def _available(self, place: str, reserved: set[str]) -> list[str]:
        return [part for part in self.contents[place] if part not in reserved]

    def _delivered(self, program: Program) -> Callable[[], None]:
        def count() -> None:
            program.output += 1
            program.output_times.append(self.now())

        return count

    # ----- team programs ----------------------------------------------------------------

    def _run_serial(self, name: str, team: Team, state: TeamState, dt: float) -> None:
        program, stations = state.program, state.stations
        blocked = False
        for index in reversed(range(len(stations))):
            station = stations[index]
            if station["part"] is None or not station["ready"]:
                continue
            part = station["part"]
            if index == len(stations) - 1:
                if not self._space(team.output):
                    blocked = True
                    continue
                self.parts[part].stage = "machined"
                self._send(part, "place", team.output, self._delivered(program))
            elif stations[index + 1]["part"] is None:
                following = stations[index + 1]
                following.update(part=part, timer=None, ready=False)
                self._send(part, "robot", following["robot"])
            else:
                continue
            station.update(part=None, timer=None, ready=False)
            self.robots[station["robot"]].carrying = None
        first = stations[0]
        if first["part"] is None and program.started < program.target and self.contents[team.input]:
            part = self.contents[team.input][0]
            first.update(part=part, timer=None, ready=False)
            self._send(part, "robot", first["robot"])
            program.started += 1
        for station in stations:
            robot = self.robots[station["robot"]]
            part = station["part"]
            if part is None:
                robot.label = "idle"
            elif self.parts[part].goal is not None:
                robot.label = "receiving"
            elif not station["ready"]:
                if station["timer"] is None:
                    station["timer"] = state.cycle_seconds
                station["timer"] -= dt
                station["ready"] = station["timer"] <= 0
                robot.label = "working"
            else:
                # A finished part that cannot move on: the queue backs up behind a
                # full output buffer.
                robot.label = "blocked" if blocked else "waiting"
        state.blocked = blocked
        state.starved = False
        program.done = program.output >= program.target

    def _run_parallel(self, name: str, team: Team, state: TeamState, dt: float) -> None:
        program = state.program
        reserved = {line["part"] for line in state.lines.values() if line["part"]}
        source, target = self.scene.places[team.input], self.scene.places[team.output]
        blocked = False
        for robot_name, line in state.lines.items():
            robot = self.robots[robot_name]
            slot = team.slots[robot_name]
            side = _sign(slot[1] - source.xy[1]) * 0.25
            pick = (source.xy[0] + _sign(slot[0] - source.xy[0]) * 0.3, source.xy[1] + side)
            drop = (target.xy[0] + _sign(slot[0] - target.xy[0]) * 0.3, target.xy[1] + side)
            phase = line["phase"]
            if phase == "home":
                available = self._available(team.input, reserved)
                if program.started < program.target and available:
                    line["part"] = available[0]
                    reserved.add(available[0])
                    program.started += 1
                    line["phase"] = "to_pick"
                    self._go(robot_name, pick)
            elif phase == "to_pick" and self._arrived(robot_name):
                self._send(line["part"], "robot", robot_name)
                line["phase"] = "picking"
            elif phase == "picking" and self.parts[line["part"]].goal is None:
                line["phase"] = "to_line"
                self._go(robot_name, slot[:2], face=slot[2])
            elif phase == "to_line" and self._arrived(robot_name):
                line.update(phase="assembling", timer=state.cycle_seconds)
            elif phase == "assembling":
                line["timer"] -= dt
                if line["timer"] <= 0:
                    self.parts[line["part"]].stage = "assembled"
                    line["phase"] = "to_drop"
                    self._go(robot_name, drop)
            elif phase == "to_drop" and self._arrived(robot_name):
                line["phase"] = "dropping"
            if line["phase"] == "dropping":
                if self._space(team.output):
                    self._send(line["part"], "place", team.output, self._delivered(program))
                    line.update(phase="to_home", part=None)
                    self._go(robot_name, slot[:2], face=slot[2])
                else:
                    blocked = True
            elif line["phase"] == "to_home" and self._arrived(robot_name):
                line["phase"] = "home"
            robot.label = {
                "home": "idle",
                "assembling": "working",
                "dropping": "blocked",
            }.get(line["phase"], "moving")
        state.blocked = blocked
        state.starved = (
            program.started < program.target
            and all(line["phase"] == "home" for line in state.lines.values())
            and not self._available(team.input, reserved)
        )
        program.done = program.output >= program.target

    def _place_formation(self, team: Team, state: TeamState) -> None:
        home = team.centre()
        for robot_name, slot in team.slots.items():
            dx, dy = _rotate((slot[0] - home[0], slot[1] - home[1]), state.angle)
            robot = self.robots[robot_name]
            robot.x, robot.y = state.centre[0] + dx, state.centre[1] + dy
            robot.heading = (slot[2] + state.angle + state.spin) % 360
            robot.goal = robot.face = None

    def _run_formation(self, name: str, team: Team, state: TeamState, dt: float) -> None:
        program = state.program
        robots = list(team.slots)
        remaining = program.target - program.output
        size = min(program.batch, remaining)
        label = state.phase
        if state.phase == "waiting":
            label = "waiting"
            if size and len(self.contents[team.input]) >= size:
                state.phase, state.loading = "loading", []
                program.started += size
        if state.phase == "loading":
            label = "loading"
            moving = any(self.parts[part].goal is not None for part in state.loading)
            if not moving and len(state.loading) < size:
                part = self.contents[team.input][0]
                state.loading.append(part)
                self._send(part, "robot", robots[len(state.loading) - 1])
            elif not moving:
                state.phase, state.step = "routine", 0
        if state.phase == "routine":
            move = program.routine[state.step]
            finished = False
            if move == "spin":
                label = "spinning"
                state.spin += team.spin_deg_s * dt
                if state.spin >= SPIN_DEGREES:
                    state.spin, finished = 0.0, True
            elif move == "turn":
                label = "turning"
                state.angle += team.turn_deg_s * dt
                state.turned += team.turn_deg_s * dt
                if state.turned >= TURN_DEGREES:
                    state.angle -= state.turned - TURN_DEGREES
                    state.turned, finished = 0.0, True
            else:
                label = "running"
                finished = self._run_leg(team, state, program, dt)
            if finished:
                state.step += 1
                if state.step >= len(program.routine):
                    state.phase = "waiting"
                    state.loading = []
            self._place_formation(team, state)
        for robot_name in robots:
            self.robots[robot_name].label = label
        state.blocked = False
        state.starved = state.phase == "waiting" and remaining > 0
        # Done only after the last routine, so that the formation is home again.
        program.done = program.output >= program.target and state.phase == "waiting"
        if program.done:
            for robot_name in robots:
                self.robots[robot_name].label = "idle"

    def _run_leg(self, team: Team, state: TeamState, program: Program, dt: float) -> bool:
        """Run out to the dock, unload the batch, and run back. True when home again."""
        home = team.centre()
        if state.leg == "out":
            if self._advance_centre(state, team.run_to, team.run_speed_m_s * dt):
                for part in state.loading:
                    self._send(part, "place", team.output, self._delivered(program))
                state.leg = "unloading"
        elif state.leg == "unloading":
            if all(self.parts[part].goal is None for part in state.loading):
                state.leg = "back"
        elif self._advance_centre(state, home, team.run_speed_m_s * dt):
            state.leg = "out"
            return True
        return False

    @staticmethod
    def _advance_centre(state: TeamState, goal: XY, step: float) -> bool:
        distance = math.dist(state.centre, goal)
        if distance <= step:
            state.centre = goal
            return True
        state.centre = tuple(c + (g - c) * step / distance for c, g in zip(state.centre, goal))
        return False

    # ----- the controller loop ----------------------------------------------------------

    def tick(self) -> None:
        with self.lock:
            if self.stopped:
                return
            dt = self.scene.step_seconds
            for name, robot in self.robots.items():
                self._drive(robot, dt, self.scene.robots[name].speed_m_s)
            for name, state in self.teams.items():
                team = self.scene.teams[name]
                if state.forming is not None:
                    if all(self._arrived(robot) for robot in team.slots):
                        state.formed = True
                        self._set(state.forming, "finished")
                        state.forming = None
                        for robot in team.slots:
                            self.robots[robot].label = "idle"
                    elif self.now() >= state.forming.deadline:
                        self._set(state.forming, "failed", "action_timeout")
                        state.forming = None
                program = state.program
                if program is None or program.done:
                    # A robot can still walk home after the last part of its program.
                    if program is not None:
                        for robot in team.slots:
                            if self._arrived(robot):
                                self.robots[robot].label = "idle"
                    continue
                runner = {
                    "serial": self._run_serial,
                    "parallel": self._run_parallel,
                    "formation": self._run_formation,
                }[team.kind]
                runner(name, team, state, dt)
                if state.blocked:
                    program.blocked_for += dt
                    program.blocked_seconds += dt
                else:
                    program.blocked_for = 0.0
                if state.starved:
                    program.starved_seconds += dt
                if program.done:
                    state.blocked = state.starved = False
                    program.blocked_for = 0.0
                    self._set(program.command, "finished")
            self._move_parts(dt)
            self._push()
            mujoco.mj_step(self.model, self.data)
            self.forward()

    # ----- observation ------------------------------------------------------------------

    def team_status(self, zone: str) -> dict:
        with self.lock:
            team, state = self.scene.teams[zone], self.teams[zone]
            program = state.program
            if self.stopped and (state.forming is not None or (program and not program.done)):
                # The operator stopped the run: the program is interrupted for good.
                status = "stopped"
            elif state.forming is not None:
                status = "forming"
            elif program is None:
                status = "ready" if state.formed else "idle"
            elif program.done:
                status = "done"
            elif state.blocked:
                status = "blocked"
            elif state.starved:
                status = "starved"
            else:
                status = "running"
            # The mean time between outputs, over all outputs. Two parallel lines
            # can deliver their first units close together, so the value waits
            # for three outputs.
            times = program.output_times if program else []
            interval = (
                round((times[-1] - times[0]) / (len(times) - 1), 2)
                if len(times) >= MIN_INTERVAL_SAMPLES
                else None
            )
            report = {
                "kind": team.kind,
                "formation": team.formation,
                "formation_ready": state.formed,
                "status": status,
                "input": team.input,
                "output": team.output,
                "cycle_seconds": state.cycle_seconds,
                "robots": {
                    name: {
                        "xy": [round(value, 3) for value in self._xy(name)],
                        "heading_deg": round(self.robots[name].heading % 360, 1),
                        "state": self.robots[name].label,
                        "carrying": self.robots[name].carrying,
                    }
                    for name in team.slots
                },
                "program": None,
            }
            if program:
                report["program"] = {
                    "command_id": program.command.command_id,
                    "target": program.target,
                    "started": program.started,
                    "output": program.output,
                    "batch": program.batch,
                    "routine": list(program.routine),
                    "avg_output_interval_seconds": interval,
                    "blocked_for_seconds": round(program.blocked_for, 2),
                    "blocked_seconds": round(program.blocked_seconds, 2),
                    "starved_seconds": round(program.starved_seconds, 2),
                }
            return report

    def observe(self) -> dict:
        with self.lock:
            scene = self.scene.model_dump(mode="json")
            # Drop the start poses: only the live blocks below say where robots are.
            for robot in scene["robots"].values():
                del robot["xy"], robot["heading_deg"]
            return {
                "run_id": self.run_id,
                "observed_at": utc_now(),
                "simulation_seconds": round(self.now(), 2),
                "model": "MuJoCo kinematic robot proxies; idealized part transfer; no collisions",
                "position_units": "m",
                "motion_approved": self.allow_motion,
                "stopped": self.stopped,
                "scene": scene,
                "teams": {zone: self.team_status(zone) for zone in self.scene.teams},
                "places": {
                    name: {
                        "zones": sorted(place.zones),
                        "capacity": place.capacity,
                        "parts": list(self.contents[name]),
                        "incoming": self.incoming[name],
                    }
                    for name, place in self.scene.places.items()
                },
                "parts": {
                    name: {
                        "stage": part.stage,
                        "at": part.at or "moving",
                        "xy": [round(value, 3) for value in part.xyz[:2]],
                    }
                    for name, part in self.parts.items()
                },
                "commands": [command.report() for command in self._commands.values()],
            }

    # ----- life cycle -------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None or self.stopped:
            raise RuntimeError("a world can be started only once")

        def run() -> None:
            try:
                while not self._stop.wait(self.scene.step_seconds):
                    self.tick()
            finally:
                if not self.stopped:
                    LOGGER.error("Simulation controller exited unexpectedly")
                    self.stop(reason="controller_failed", status="failed")

        self._thread = threading.Thread(target=run, name="factory-simulation", daemon=True)
        self._thread.start()

    def stop(self, *, reason: str = "operator_stop", status: str = "interrupted") -> dict:
        with self.lock:
            self.stopped = True
            self._stop.set()
            for command in self._commands.values():
                if command.status in ACTIVE:
                    self._set(command, status, reason)
            return self.observe()

    def close(self) -> dict:
        state = self.stop(reason="controller_shutdown")
        if self._thread is not None:
            self._thread.join(timeout=2)
            if self._thread.is_alive():
                raise RuntimeError("simulation controller shutdown was not confirmed")
        return state
