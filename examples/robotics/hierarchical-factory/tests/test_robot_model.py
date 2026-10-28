from __future__ import annotations

import hashlib
import io
import json
import re
from pathlib import Path

import pytest
import robot_model
from simulation import Scene, World

EXAMPLE = Path(__file__).resolve().parents[1]
SCENE = Scene.model_validate_json((EXAMPLE / "factory.json").read_text())


def test_the_manifest_pins_one_commit_and_a_hash_for_every_file():
    spec = robot_model.manifest()
    assert spec["repository"] == "apirrone/Open_Duck_Mini"
    assert re.fullmatch(r"[0-9a-f]{40}", spec["commit"])
    assert spec["license"] == "Apache-2.0"
    assert {"robot.xml", "LICENSE"} <= set(spec["files"])
    assert all(re.fullmatch(r"[0-9a-f]{64}", digest) for digest in spec["files"].values())
    assert all(re.fullmatch(r"[A-Za-z0-9_.-]+", name) for name in spec["files"])


def fake_manifest(monkeypatch, files: dict[str, bytes]) -> dict:
    spec = {
        **robot_model.manifest(),
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
    }
    monkeypatch.setattr(robot_model, "manifest", lambda: spec)
    return spec


def test_fetch_downloads_only_the_pinned_files_and_checks_each_hash(tmp_path, monkeypatch):
    files = {"robot.xml": b"<mujoco/>", "LICENSE": b"Apache License", "a.stl": b"solid"}
    spec = fake_manifest(monkeypatch, files)
    urls = []

    def urlopen(url, timeout):
        urls.append(url)
        return io.BytesIO(files[url.rsplit("/", 1)[1]])

    monkeypatch.setattr(robot_model.urllib.request, "urlopen", urlopen)
    robot_model.fetch(tmp_path)
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == files
    prefix = f"https://raw.githubusercontent.com/{spec['repository']}/{spec['commit']}/"
    assert all(url.startswith(prefix) for url in urls)
    assert prefix + "LICENSE" in urls
    assert prefix + f"{spec['path']}/a.stl" in urls
    # A second fetch keeps the checked files and downloads nothing.
    urls.clear()
    robot_model.fetch(tmp_path)
    assert urls == []
    assert robot_model.available(tmp_path)


def test_fetch_refuses_a_file_that_does_not_match_its_hash(tmp_path, monkeypatch):
    fake_manifest(monkeypatch, {"robot.xml": b"<mujoco/>"})
    monkeypatch.setattr(
        robot_model.urllib.request, "urlopen", lambda url, timeout: io.BytesIO(b"<changed/>")
    )
    with pytest.raises(robot_model.ModelError, match="does not match its SHA-256"):
        robot_model.fetch(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_load_refuses_a_missing_or_changed_file(tmp_path, monkeypatch):
    fake_manifest(monkeypatch, {"robot.xml": b"<mujoco/>"})
    with pytest.raises(robot_model.ModelError, match="fetch-robot"):
        robot_model.load(tmp_path)
    (tmp_path / "robot.xml").write_bytes(b"<changed/>")
    with pytest.raises(robot_model.ModelError, match="SHA-256"):
        robot_model.load(tmp_path)


def test_without_the_model_the_world_uses_simple_bipeds_with_the_same_joints():
    world = World(SCENE)
    assert world.robot_look == "simple"
    for name in SCENE.robots:
        assert set(world.robot_joints[name]) == set(robot_model.JOINTS)
    json.dumps(world.observe(), allow_nan=False)


@pytest.mark.skipif(
    not robot_model.available(robot_model.DEFAULT_DIR),
    reason="the Open Duck Mini model is not fetched: python demo.py fetch-robot",
)
def test_with_the_fetched_model_every_robot_is_an_open_duck_mini(monkeypatch):
    monkeypatch.setenv("FACTORY_ROBOT_MODEL_DIR", str(robot_model.DEFAULT_DIR))
    world = World(SCENE, allow_motion=True)
    assert world.robot_look == "open_duck_mini_v2"
    meshes = {world.model.mesh(index).name for index in range(world.model.nmesh)}
    assert {"duck/head", "duck/body_front", "duck/foot_top"} <= meshes
    # No geom of a robot has contact, and the feet stand on the floor.
    assert all(
        world.model.geom(index).contype[0] == 0 and world.model.geom(index).conaffinity[0] == 0
        for index in range(world.model.ngeom)
    )
    world.form_team("west", "form-1", "queue")
    for _ in range(300):
        world.tick()
    assert world.command("west", "form-1")["status"] == "finished"
