"""The Open Duck Mini v2 robot model, for the pictures of the factory floor.

Open Duck Mini (https://github.com/apirrone/Open_Duck_Mini, Apache-2.0) is the
small open-source biped that Strands Robots drives in its demos. Its meshes are
about 18 MB, so the example does not commit them. ``fetch`` downloads the files
of one pinned commit once, and checks the SHA-256 of each file against
``open_duck_mini.json``. ``load`` reads them, checks them again, and returns a
``DuckModel`` that adds one copy of the robot to a MuJoCo world for each robot
of the scene.

The robot only shows the motion: the world moves its root as a mocap body and
sets its joint angles. No geom of the robot has contact.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import mujoco
import numpy as np

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "open_duck_mini.json"
DEFAULT_DIR = HERE / ".robot-models" / "open_duck_mini_v2"
# The joints that the world animates, by role, with the name in the model.
JOINTS = {
    "hip_left": "left_hip_pitch",
    "knee_left": "left_knee",
    "ankle_left": "left_ankle",
    "hip_right": "right_hip_pitch",
    "knee_right": "right_knee",
    "ankle_right": "right_ankle",
    "neck": "neck_pitch",
    "head": "head_pitch",
    "antenna_left": "left_antenna",
    "antenna_right": "right_antenna",
}
# Geoms of these meshes get the color of the team; the shell can turn red.
TEAM_MESHES = ("battery_pack_lid", "bms", "body_middle_top")
SHELL_MESHES = ("body_front", "body_back", "body_middle_bottom", "head")
FOOT_MESHES = ("foot_bottom_tpu", "foot_bottom_pla")
# The robots stand 1.6 times the size of the real robot (about 0.45 m), so that
# they show on a floor of 9 m in the overview camera.
SCALE = 1.6
# A crouched standing pose of the left leg, in radians, as the robot stands in
# its demos. The right hip turns the other way.
STAND_HIP, STAND_KNEE, STAND_ANKLE = -0.5, 1.0, -0.5


class ModelError(RuntimeError):
    """The robot model files are missing, incomplete, or changed."""


def manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def directory() -> Path:
    """The model directory: FACTORY_ROBOT_MODEL_DIR, or .robot-models in the example."""
    return Path(os.environ.get("FACTORY_ROBOT_MODEL_DIR") or DEFAULT_DIR)


def fetch(target: Path | None = None) -> Path:
    """Download the pinned model files that are missing, and check every file."""
    target = Path(target or directory())
    spec = manifest()
    target.mkdir(parents=True, exist_ok=True)
    for name, digest in spec["files"].items():
        path = target / name
        if path.is_file() and _sha256(path) == digest:
            continue
        source = spec["license_path"] if name == "LICENSE" else f"{spec['path']}/{name}"
        url = f"https://raw.githubusercontent.com/{spec['repository']}/{spec['commit']}/{source}"
        with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - fixed https URL
            data = response.read()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ModelError(f"{name} from {url} does not match its SHA-256 in {MANIFEST.name}")
        temporary = path.with_name(f".{name}.{uuid.uuid4().hex}.tmp")
        temporary.write_bytes(data)
        temporary.replace(path)
    return target


def available(target: Path | None = None) -> bool:
    target = Path(target or directory())
    return all((target / name).is_file() for name in manifest()["files"])


@dataclass
class DuckModel:
    """The parsed model: its meshes, and the subtree of one robot."""

    folder: Path
    meshes: list[tuple[str, str]]
    trunk: ET.Element
    height: float
    # Where the lamp and a carried part go, relative to the robot root, facing +x.
    lamp: tuple[float, float, float] = (0.0, 0.0, 0.0)
    back: tuple[float, float, float] = (0.0, 0.0, 0.0)

    def add_assets(self, asset: ET.Element) -> None:
        for name, file in self.meshes:
            ET.SubElement(
                asset,
                "mesh",
                name=f"duck/{name}",
                file=str(self.folder / file),
                scale=f"{SCALE} {SCALE} {SCALE}",
            )

    def add_robot(self, parent: ET.Element, prefix: str, team_rgba: str | None = None) -> None:
        """Add one robot under ``parent``, with every name prefixed, and no contact.

        The model faces -x, so the copy turns half a turn to face +x, the
        heading of the world. Geoms of TEAM_MESHES get ``team_rgba``.
        """
        trunk = copy.deepcopy(self.trunk)
        trunk.set("pos", f"0 0 {self.height}")
        trunk.set("quat", "0 0 0 1")
        count = 0
        for element in trunk.iter():
            if "name" in element.attrib:
                element.set("name", f"{prefix}/{element.get('name')}")
            if element is not trunk and "pos" in element.attrib:
                scaled = (SCALE * float(value) for value in element.get("pos").split())
                element.set("pos", " ".join(f"{value:.6g}" for value in scaled))
            if element.tag == "geom":
                element.set("contype", "0")
                element.set("conaffinity", "0")
                if "mesh" in element.attrib:
                    mesh = element.get("mesh")
                    element.set("mesh", f"duck/{mesh}")
                    element.set("name", f"{prefix}/mesh/{mesh}/{count}")
                    count += 1
                    if team_rgba and mesh in TEAM_MESHES:
                        element.set("rgba", team_rgba)
            if element.tag == "joint":
                # The world sets the angles; no limit or damping may move them back.
                for key in ("range", "damping", "frictionloss", "armature"):
                    element.attrib.pop(key, None)
                element.set("limited", "false")
        parent.append(trunk)


def load(target: Path | None = None) -> DuckModel:
    """Read and check the model files, and measure the standing height of the robot."""
    folder = Path(target or directory())
    spec = manifest()
    for name, digest in spec["files"].items():
        path = folder / name
        if not path.is_file():
            raise ModelError(f"{path} is missing. Run: python demo.py fetch-robot")
        if _sha256(path) != digest:
            raise ModelError(f"{path} does not match its SHA-256. Run: python demo.py fetch-robot")
    root = ET.parse(folder / "robot.xml").getroot()
    meshes = [(mesh.get("name"), mesh.get("file")) for mesh in root.iter("mesh")]
    base = next(body for body in root.iter("body") if body.get("name") == "base")
    trunk = next(child for child in base if child.tag == "body")
    model = DuckModel(folder, meshes, trunk, 0.0)
    model.height, model.lamp, model.back = _measure(model)
    return model


def _measure(model: DuckModel):
    """Measure the standing robot: its trunk height, the top of its head, and its back."""
    root = ET.Element("mujoco", model="duck-height")
    ET.SubElement(root, "compiler", angle="radian")
    model.add_assets(ET.SubElement(root, "asset"))
    world = ET.SubElement(root, "worldbody")
    holder = ET.SubElement(world, "body", name="holder")
    model.add_robot(holder, "duck")
    mj = mujoco.MjModel.from_xml_string(ET.tostring(root, encoding="unicode"))
    data = mujoco.MjData(mj)
    pose(data.qpos, joint_addresses(mj, "duck"), 0.0, 0.0, 0.0, 0.0)
    mujoco.mj_forward(mj, data)

    def points(meshes) -> np.ndarray:
        found = []
        for index in range(mj.ngeom):
            mesh = mj.geom_dataid[index]
            if mesh >= 0 and mj.mesh(mesh).name.split("/", 1)[1] in meshes:
                start, count = mj.mesh_vertadr[mesh], mj.mesh_vertnum[mesh]
                vertices = mj.mesh_vert[start : start + count]
                found.append(
                    vertices @ data.geom_xmat[index].reshape(3, 3).T + data.geom_xpos[index]
                )
        return np.concatenate(found)

    lowest = float(points(FOOT_MESHES)[:, 2].min())
    head = points(("head",))
    top = head[head[:, 2].argmax()]
    shell = points(("body_back", "body_middle_top"))
    rear = shell[:, 0].min()
    lamp = (float(head[:, 0].mean()), 0.0, float(top[2] - lowest + 0.035))
    back = (float(rear + 0.06), 0.0, float(shell[:, 2].max() - lowest))
    return -lowest, lamp, back


def joint_addresses(mj: mujoco.MjModel, prefix: str) -> dict[str, int]:
    return {role: mj.joint(f"{prefix}/{name}").qposadr[0] for role, name in JOINTS.items()}


def _leg(phase: float, step: float) -> tuple[float, float, float]:
    """Hip pitch, knee, and ankle of a left leg in a crouched walk, in radians."""
    stride = 0.3 * step * np.cos(phase)
    lift = 0.6 * step * max(0.0, np.sin(phase))
    return STAND_HIP + stride - lift, STAND_KNEE + 2 * lift, STAND_ANKLE - stride - lift


def pose(qpos, address: dict[str, int], swing: float, step: float, nod: float, wiggle: float):
    """Set a walking pose: ``swing`` is the gait phase, and ``step`` its amplitude."""
    hip, knee, ankle = _leg(swing, step)
    qpos[address["hip_left"]], qpos[address["knee_left"]], qpos[address["ankle_left"]] = (
        hip,
        knee,
        ankle,
    )
    hip, knee, ankle = _leg(swing + np.pi, step)
    # The hip of the right leg turns the other way; the knee and the ankle do not.
    qpos[address["hip_right"]], qpos[address["knee_right"]], qpos[address["ankle_right"]] = (
        -hip,
        knee,
        ankle,
    )
    qpos[address["neck"]] = nod
    qpos[address["head"]] = -0.5 * nod
    qpos[address["antenna_left"]] = wiggle
    qpos[address["antenna_right"]] = -wiggle
