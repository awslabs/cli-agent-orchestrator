"""A shared MuJoCo world, not a planner or a physical-robot controller."""

from __future__ import annotations

import logging
import math
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Annotated

import mujoco
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Identifier = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]*$", max_length=64)]
Positive = Annotated[float, Field(gt=0)]
XY = tuple[float, float]
LOGGER = logging.getLogger("transport")
ACTIVE = ("accepted", "running")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Zone(Model):
    bounds: tuple[float, float, float, float]

    def contains(self, xy: XY) -> bool:
        x0, y0, x1, y1 = self.bounds
        return x0 <= xy[0] <= x1 and y0 <= xy[1] <= y1


class Location(Model):
    xy: XY
    zones: set[Identifier] = Field(min_length=1)
    handoff: bool = False


class Robot(Model):
    zone: Identifier
    at: Identifier
    locations: set[Identifier] = Field(min_length=1)
    payload_kg: Positive
    fixtures: set[Identifier] = Field(min_length=1)
    speed_m_s: Positive


class Payload(Model):
    at: Identifier
    owner: Identifier
    mass_kg: Positive
    fixture: Identifier


class Scene(Model):
    zones: dict[Identifier, Zone] = Field(min_length=1)
    locations: dict[Identifier, Location] = Field(min_length=1)
    robots: dict[Identifier, Robot] = Field(min_length=1)
    payloads: dict[Identifier, Payload] = Field(min_length=1)
    step_seconds: Positive = 0.02
    arrival_tolerance_m: Positive = 0.01
    action_timeout_seconds: Positive = 15

    @model_validator(mode="after")
    def validate_geometry(self) -> Scene:
        for zone in self.zones.values():
            x0, y0, x1, y1 = zone.bounds
            if not (x0 < x1 and y0 < y1):
                raise ValueError("zone bounds must describe a nonempty rectangle")
        for location in self.locations.values():
            if not location.zones <= self.zones.keys():
                raise ValueError("every location must belong to declared zones")
            if any(not self.zones[zone].contains(location.xy) for zone in location.zones):
                raise ValueError("locations must lie inside every owning zone")
            if location.handoff and len(location.zones) < 2:
                raise ValueError("a handoff location must be shared by multiple zones")
        locations = list(self.locations.values())
        for index, location in enumerate(locations):
            if any(
                math.dist(location.xy, other.xy) <= 2 * self.arrival_tolerance_m
                for other in locations[index + 1 :]
            ):
                raise ValueError("location arrival regions must not overlap")
        for robot in self.robots.values():
            if robot.zone not in self.zones or not robot.locations <= self.locations.keys():
                raise ValueError("robots must reference declared zones and locations")
            if robot.at not in robot.locations or any(
                robot.zone not in self.locations[location].zones for location in robot.locations
            ):
                raise ValueError("a robot may serve only its own zone's locations")
        for payload in self.payloads.values():
            if (
                payload.at not in self.locations
                or payload.owner not in self.locations[payload.at].zones
            ):
                raise ValueError("a payload must start at a location belonging to its owner")
        if self.step_seconds > self.action_timeout_seconds:
            raise ValueError("the simulation step must not exceed the action timeout")
        return self


# Render colors, in scene order of the zones. Robots use the strong shade of
# their zone; the payload is pink; a shared dock is amber.
ZONE_COLORS = ("0.56 0.7 0.95 1", "0.6 0.84 0.6 1", "0.95 0.74 0.55 1", "0.78 0.64 0.95 1")
ROBOT_COLORS = ("0.12 0.32 0.9 1", "0.08 0.55 0.2 1", "0.88 0.42 0.08 1", "0.5 0.25 0.85 1")
HANDOFF_COLOR = "1 0.72 0.1 1"
PAYLOAD_COLOR = "0.9 0.12 0.5 1"
LOCATION_COLOR = "0.3 0.3 0.34 1"
CAMERA_TILT_DEGREES = 32.0
CAMERA_FOVY_DEGREES = 45.0
CAMERA_ASPECT = 16 / 9


def _robot_color(zone_index: int) -> str:
    """A strong shade of the tile color of the zone, so a robot shows its owner."""
    return ROBOT_COLORS[zone_index % len(ROBOT_COLORS)]


def _add_scene_visuals(root: ET.Element, world: ET.Element, scene: Scene) -> None:
    """Add a floor, zone tiles, location markers, a light, and an overview camera.

    These elements exist only so that the world can be rendered (see
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
    ET.SubElement(asset, "material", name="floor", texture="floor", texrepeat="12 12")

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
        # A small inset shows a gap between neighbouring tiles. It is at most a
        # quarter of the tile, so a narrow but valid zone keeps a positive size.
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
    for name, location in scene.locations.items():
        x, y = location.xy
        ET.SubElement(
            world,
            "geom",
            name=f"visual/location/{name}",
            type="cylinder",
            size="0.22 0.004",
            pos=f"{x} {y} -0.005",
            rgba=HANDOFF_COLOR if location.handoff else LOCATION_COLOR,
            **no_contact,
        )

    # Look at the centre of the zones from the -y side, tilted from vertical,
    # far enough back that every zone fits a 16:9 frame with a margin.
    tilt = math.radians(CAMERA_TILT_DEGREES)
    tan_v = math.tan(math.radians(CAMERA_FOVY_DEGREES) / 2)
    distance = 1.05 * max((half_w + 0.3) / (tan_v * CAMERA_ASPECT), (half_h + 0.3) / tan_v)
    ET.SubElement(
        world,
        "camera",
        name="overview",
        fovy=str(CAMERA_FOVY_DEGREES),
        pos=f"{cx} {cy - distance * math.sin(tilt)} {distance * math.cos(tilt)}",
        xyaxes=f"1 0 0 0 {math.cos(tilt)} {math.sin(tilt)}",
    )


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


class World:
    def __init__(self, scene: Scene, *, allow_motion: bool = False, run_id: str | None = None):
        self.scene = scene.model_copy(deep=True)
        self.allow_motion = allow_motion
        self.run_id = run_id or uuid.uuid4().hex
        self.started_at = time.monotonic()
        self.stopped = False
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._commands: dict[tuple[str, str], Command] = {}
        self._owners = {name: payload.owner for name, payload in scene.payloads.items()}
        self._offers: dict[str, dict] = {}

        root = ET.Element("mujoco", model="cao-kinematic-transport")
        ET.SubElement(root, "option", timestep=str(scene.step_seconds), gravity="0 0 0")
        bodies = ET.SubElement(root, "worldbody")
        _add_scene_visuals(root, bodies, scene)
        zone_index = {zone: index for index, zone in enumerate(scene.zones)}
        for kind, entities, height in (
            ("robot", scene.robots, 0.15),
            ("payload", scene.payloads, 0.4),
        ):
            for name, entity in entities.items():
                x, y = scene.locations[entity.at].xy
                # Render color only: a robot has a dark shade of its zone color.
                color = _robot_color(zone_index[entity.zone]) if kind == "robot" else PAYLOAD_COLOR
                body = ET.SubElement(
                    bodies, "body", name=f"{kind}/{name}", mocap="true", pos=f"{x} {y} {height}"
                )
                ET.SubElement(
                    body,
                    "geom",
                    type="box",
                    size="0.12 0.12 0.1",
                    rgba=color,
                    contype="0",
                    conaffinity="0",
                )
        self.model = mujoco.MjModel.from_xml_string(ET.tostring(root, encoding="unicode"))
        self.data = mujoco.MjData(self.model)
        self.robot_mocap = {
            name: self.model.body(f"robot/{name}").mocapid[0] for name in scene.robots
        }
        self.payload_mocap = {
            name: self.model.body(f"payload/{name}").mocapid[0] for name in scene.payloads
        }
        self.forward()

    def forward(self) -> None:
        mujoco.mj_forward(self.model, self.data)

    def _xy(self, kind: str, name: str) -> XY:
        pose = self.data.body(f"{kind}/{name}").xpos
        return float(pose[0]), float(pose[1])

    def _at(self, xy: XY) -> str | None:
        return next(
            (
                name
                for name, location in self.scene.locations.items()
                if math.dist(xy, location.xy) <= self.scene.arrival_tolerance_m
            ),
            None,
        )

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
            actor,
            command_id,
            operation,
            arguments,
            time.monotonic() + self.scene.action_timeout_seconds,
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

    def _busy(self, *, robot: str | None = None, payload: str) -> bool:
        return any(
            command.operation == "move"
            and command.status in ACTIVE
            and (command.arguments[0] == robot or command.arguments[1] == payload)
            for command in self._commands.values()
        )

    def _capability_error(self, robot: Robot, payload: Payload) -> str | None:
        if payload.mass_kg > robot.payload_kg:
            return "payload_too_heavy"
        if payload.fixture not in robot.fixtures:
            return "fixture_unavailable"
        return None

    def move(self, actor: str, command_id: str, robot: str, payload: str, destination: str) -> dict:
        with self.lock:
            busy = self._busy(robot=robot, payload=payload)
            command, new = self._new(actor, command_id, "move", robot, payload, destination)
            if not new:
                return command.report()
            robot_spec = self.scene.robots.get(robot)
            payload_spec = self.scene.payloads.get(payload)
            reason = None
            if self.stopped:
                reason = "stopped"
            elif not self.allow_motion:
                reason = "motion_not_approved"
            elif robot_spec is None:
                reason = "robot_unavailable"
            elif robot_spec.zone != actor:
                reason = "not_robot_owner"
            elif payload_spec is None:
                reason = "payload_unavailable"
            elif self._owners[payload] != actor:
                reason = "not_payload_owner"
            elif destination not in robot_spec.locations:
                reason = "destination_unavailable"
            elif busy:
                reason = "busy"
            elif payload in self._offers:
                reason = "handoff_pending"
            elif self._at(self._xy("robot", robot)) not in robot_spec.locations:
                reason = "robot_position_unknown"
            elif math.dist(self._xy("robot", robot), self._xy("payload", payload)) > (
                self.scene.arrival_tolerance_m
            ):
                reason = "payload_not_at_robot"
            else:
                reason = self._capability_error(robot_spec, payload_spec)
            if reason:
                return self._set(command, "rejected", reason)
            return self._set(command, "accepted")

    def offer(self, actor: str, command_id: str, payload: str, receiver: str) -> dict:
        with self.lock:
            command, new = self._new(actor, command_id, "offer", payload, receiver)
            if not new:
                return command.report()
            reason = None
            location = self._at(self._xy("payload", payload)) if payload in self._owners else None
            if self.stopped:
                reason = "stopped"
            elif not self.allow_motion:
                reason = "motion_not_approved"
            elif payload not in self._owners:
                reason = "payload_unavailable"
            elif self._owners[payload] != actor:
                reason = "not_payload_owner"
            elif self._busy(payload=payload):
                reason = "busy"
            elif payload in self._offers:
                reason = "handoff_pending"
            elif (
                location is None
                or not self.scene.locations[location].handoff
                or actor not in self.scene.locations[location].zones
            ):
                reason = "not_at_handoff"
            elif receiver == actor or receiver not in self.scene.locations[location].zones:
                reason = "receiver_unavailable"
            if reason:
                return self._set(command, "rejected", reason)
            self._offers[payload] = {
                "offer_id": command_id,
                "from_zone": actor,
                "to_zone": receiver,
                "at": location,
            }
            return self._set(command, "finished")

    def accept(self, actor: str, command_id: str, payload: str, robot: str, offer_id: str) -> dict:
        with self.lock:
            command, new = self._new(actor, command_id, "accept", payload, robot, offer_id)
            if not new:
                return command.report()
            offer = self._offers.get(payload)
            robot_spec = self.scene.robots.get(robot)
            reason = None
            if self.stopped:
                reason = "stopped"
            elif not self.allow_motion:
                reason = "motion_not_approved"
            elif offer is None:
                reason = "no_handoff_offer"
            elif offer["to_zone"] != actor:
                reason = "not_handoff_receiver"
            elif offer["offer_id"] != offer_id:
                reason = "offer_mismatch"
            elif robot_spec is None or robot_spec.zone != actor:
                reason = "not_robot_owner"
            elif self._busy(robot=robot, payload=payload):
                reason = "busy"
            elif (
                self._at(self._xy("payload", payload)) != offer["at"]
                or self._at(self._xy("robot", robot)) != offer["at"]
                or offer["at"] not in robot_spec.locations
                or math.dist(self._xy("robot", robot), self._xy("payload", payload))
                > self.scene.arrival_tolerance_m
            ):
                reason = "not_at_handoff"
            else:
                reason = self._capability_error(robot_spec, self.scene.payloads[payload])
            if reason:
                return self._set(command, "rejected", reason)
            self._owners[payload] = actor
            del self._offers[payload]
            return self._set(command, "finished")

    def tick(self, *, now: float | None = None) -> None:
        with self.lock:
            if self.stopped:
                return
            now = time.monotonic() if now is None else now
            for command in self._commands.values():
                if command.operation != "move" or command.status not in ACTIVE:
                    continue
                if now >= command.deadline:
                    self._set(command, "failed", "action_timeout")
                    continue
                robot, payload, destination = command.arguments
                if command.status == "accepted":
                    self._set(command, "running")
                start = self._xy("robot", robot)
                target = self.scene.locations[destination].xy
                distance = math.dist(start, target)
                step = self.scene.robots[robot].speed_m_s * self.scene.step_seconds
                fraction = min(1.0, step / distance) if distance else 1.0
                xy = [value + (end - value) * fraction for value, end in zip(start, target)]
                self.data.mocap_pos[self.robot_mocap[robot], :2] = xy
                # Idealized rigid carrying: no grasp, wheel dynamics, or collision avoidance.
                self.data.mocap_pos[self.payload_mocap[payload], :2] = xy
            mujoco.mj_step(self.model, self.data)
            self.forward()
            for command in self._commands.values():
                if command.operation == "move" and command.status in ACTIVE:
                    robot, payload, destination = command.arguments
                    target = self.scene.locations[destination].xy
                    if all(
                        math.dist(self._xy(kind, name), target) <= self.scene.arrival_tolerance_m
                        for kind, name in (("robot", robot), ("payload", payload))
                    ):
                        self._set(command, "finished")

    def command(self, actor: str, command_id: str) -> dict:
        with self.lock:
            command = self._commands.get((actor, command_id))
            return (
                command.report()
                if command
                else {"actor": actor, "command_id": command_id, "status": "unknown"}
            )

    def observe(self) -> dict:
        with self.lock:
            return {
                "run_id": self.run_id,
                "observed_at": utc_now(),
                "simulation_seconds": float(self.data.time),
                "model": "MuJoCo kinematic cart proxies; rigid carry assistance; no collisions",
                "position_units": "m",
                "motion_approved": self.allow_motion,
                "stopped": self.stopped,
                "scene": self.scene.model_dump(mode="json"),
                "robots": {
                    name: {"xy": list(self._xy("robot", name)), "zone": robot.zone}
                    for name, robot in self.scene.robots.items()
                },
                "payloads": {
                    name: {
                        "xy": list(self._xy("payload", name)),
                        "at": self._at(self._xy("payload", name)),
                        "owner": self._owners[name],
                        "offer": dict(self._offers[name]) if name in self._offers else None,
                    }
                    for name in self.scene.payloads
                },
                "commands": [command.report() for command in self._commands.values()],
            }

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

        self._thread = threading.Thread(target=run, name="transport-simulation", daemon=True)
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
