from __future__ import annotations

import json
import secrets
import socket
import threading
import time
from pathlib import Path

import httpx2
import numpy as np
import pytest
import uvicorn
from cell import Cell, FakeBackend, Scene
from cell_mcp import make_server

EXAMPLE = Path(__file__).resolve().parents[1]


class PicturedFake(FakeBackend):
    """The fake backend plus pictures: a flat image and fixed label pixels."""

    def frame(self, world: str):
        return np.full((40, 64, 3), 90, dtype=np.uint8)

    def project(self, world: str, points: dict) -> dict:
        return {name: [10, 10] for name in points}


def serve(tmp_path, backend, allowed_hosts=None):
    scene = Scene.model_validate_json((EXAMPLE / "cell.json").read_text())
    cell = Cell(scene, backend, run_id="view-test", allow_motion=True)
    token = secrets.token_urlsafe(32)
    app = make_server(
        cell, {token: {"client_id": "supervisor", "scopes": ["observe"]}}, tmp_path / "s.json"
    ).http_app(stateless_http=True, host_origin_protection=True, allowed_hosts=allowed_hosts)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", ws="none")
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "controller did not start"
        time.sleep(0.01)
    return f"http://127.0.0.1:{port}", token, server, thread, listener


@pytest.fixture
def pictured(tmp_path):
    base, token, server, thread, listener = serve(tmp_path, PicturedFake())
    try:
        yield base, token
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()


def get(url: str, **kwargs) -> httpx2.Response:
    return httpx2.get(url, timeout=5, trust_env=False, **kwargs)


def test_the_page_and_state_need_no_credential_and_show_none(pictured):
    base, token = pictured
    page = get(base + "/")
    assert page.status_code == 200 and "policy-driven cell" in page.text
    state = get(base + "/state")
    assert state.status_code == 200
    body = state.json()
    assert body["cell"]["run_id"] == "view-test"
    assert token not in state.text
    assert "token" not in json.dumps(body).lower()


def test_pictures_arrive_for_each_robot_world(pictured):
    base, _ = pictured
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        worlds = get(base + "/state").json()["view"]["worlds"]
        if {"g1", "arm"} <= set(worlds):
            break
        time.sleep(0.1)
    assert set(worlds["g1"]["labels"]) == {"bay", "inspection", "dock", "arm arm (not drawn)"}
    assert set(worlds["arm"]["labels"]) == {"pass_bin", "rework_bin"}
    picture = get(base + "/frame/g1")
    assert picture.status_code == 200 and picture.headers["content-type"] == "image/jpeg"
    assert picture.content[:2] == b"\xff\xd8"


def test_unknown_world_has_no_picture(pictured):
    base, _ = pictured
    assert get(base + "/frame/nobody").status_code == 404


def test_a_foreign_host_name_is_refused(pictured):
    base, _ = pictured
    assert get(base + "/state", headers={"Host": "attacker.example"}).status_code == 421


def test_an_allowed_forward_host_is_accepted(tmp_path):
    base, _, server, thread, listener = serve(
        tmp_path, PicturedFake(), allowed_hosts=["*.cloudfront.net"]
    )
    try:
        assert get(base + "/state", headers={"Host": "d111.cloudfront.net"}).status_code == 200
        assert get(base + "/state", headers={"Host": "attacker.example"}).status_code == 421
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()


def test_without_pictures_the_view_says_so(tmp_path):
    base, _, server, thread, listener = serve(tmp_path, FakeBackend())
    try:
        view = get(base + "/state").json()["view"]
        assert view["enabled"] is False and view["worlds"] == {}
        assert get(base + "/frame/g1").status_code == 404
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
