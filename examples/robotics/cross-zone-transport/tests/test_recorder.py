from __future__ import annotations

import json
import os
import struct
import sys
import threading
import time
import zlib
from pathlib import Path

import demo
import mujoco
import numpy as np
import pytest
from recorder import (
    Recorder,
    RecordingError,
    compress_pixels,
    encode_apng,
    encode_png,
    ensure_empty_directory,
)
from simulation import Scene, World

EXAMPLE = Path(__file__).resolve().parents[1]


def world(scene_file: str = "site.json", **kwargs) -> World:
    return World(Scene.model_validate_json((EXAMPLE / scene_file).read_text()), **kwargs)


def decode_png(data: bytes) -> tuple[int, int, bytes]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    position, chunks = 8, {}
    while position < len(data):
        (length,) = struct.unpack(">I", data[position : position + 4])
        kind = data[position + 4 : position + 8]
        body = data[position + 8 : position + 8 + length]
        (crc,) = struct.unpack(">I", data[position + 8 + length : position + 12 + length])
        assert crc == zlib.crc32(kind + body) & 0xFFFFFFFF
        chunks[kind] = body
        position += 12 + length
    width, height, depth, color, *_ = struct.unpack(">IIBBBBB", chunks[b"IHDR"])
    assert (depth, color) == (8, 2)
    return width, height, zlib.decompress(chunks[b"IDAT"])


def test_encode_png_round_trips_pixels():
    pixels = np.arange(4 * 5 * 3, dtype=np.uint8).reshape(4, 5, 3)
    width, height, raw = decode_png(encode_png(pixels))
    assert (width, height) == (5, 4)
    rows = [raw[i * 16 : (i + 1) * 16] for i in range(4)]
    assert all(row[0] == 0 for row in rows)
    assert b"".join(row[1:] for row in rows) == pixels.tobytes()
    with pytest.raises(ValueError):
        encode_png(np.zeros((2, 2), dtype=np.uint8))


@pytest.mark.parametrize("scene_file", ["site.json", "return-site.json"])
def test_render_elements_cannot_touch_anything(scene_file):
    w = world(scene_file)
    assert w.model.camera("overview").id == 0
    visual = [
        w.model.geom(i) for i in range(w.model.ngeom) if w.model.geom(i).name.startswith("visual/")
    ]
    assert visual, "the scene should have render-only geoms"
    assert all(g.contype[0] == 0 and g.conaffinity[0] == 0 for g in visual)
    assert all(w.model.body(g.bodyid[0]).name == "world" for g in visual)


class FakeRenderer:
    def __init__(self, model, height, width):
        self.shape = (height, width, 3)
        self.cameras: list[str] = []
        self.closed = False

    def update_scene(self, data, camera):
        self.cameras.append(camera)

    def render(self):
        return np.full(self.shape, 128, dtype=np.uint8)

    def close(self):
        self.closed = True


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_recorder_writes_a_frame_only_when_the_measured_state_changes(tmp_path):
    w = world(allow_motion=True)
    renderers: list[FakeRenderer] = []

    def factory(model, height, width):
        renderers.append(FakeRenderer(model, height, width))
        return renderers[-1]

    recorder = Recorder(
        w, tmp_path / "frames", interval=0.02, width=32, height=18, renderer_factory=factory
    )
    recorder.start()
    assert wait_for(lambda: len(recorder.frames) == 1)
    time.sleep(0.2)
    assert len(recorder.frames) == 1, "an unchanged world must not produce new frames"

    assert w.move("west", "leg-1", "cart-west", "parcel", "dock")["status"] == "accepted"
    for _ in range(200):
        w.tick()
    assert w.command("west", "leg-1")["status"] == "finished"
    assert wait_for(lambda: any("finished" in (f["event"] or "") for f in recorder.frames))
    frames = recorder.stop()

    assert frames[0]["payloads"]["parcel"]["at"] == "stock"
    assert frames[-1]["payloads"]["parcel"]["at"] == "dock"
    assert renderers[0].cameras and set(renderers[0].cameras) == {"overview"}
    assert renderers[0].closed
    directory = tmp_path / "frames"
    names = sorted(p.name for p in directory.glob("frame-*.png"))
    assert names == [f["file"] for f in frames]
    assert decode_png((directory / names[-1]).read_bytes())[:2] == (32, 18)
    assert (directory / "latest.png").read_bytes() == (directory / names[-1]).read_bytes()
    index = json.loads((directory / "frames.json").read_text())
    assert index["run_id"] == w.run_id and len(index["frames"]) == len(frames)
    page = (directory / "index.html").read_text()
    assert w.run_id in page and frames[-1]["file"] in page
    chunks = png_chunks((directory / "animation.png").read_bytes())
    assert struct.unpack(">II", chunks[b"acTL"][0]) == (len(frames), 0)
    assert len(chunks[b"fcTL"]) == len(frames) and len(chunks[b"fdAT"]) == len(frames) - 1


def test_recorder_stop_records_the_stopped_state(tmp_path):
    w = world(allow_motion=True)
    recorder = Recorder(
        w, tmp_path, interval=0.02, width=8, height=8, renderer_factory=FakeRenderer
    )
    recorder.start()
    assert wait_for(lambda: len(recorder.frames) == 1)
    w.stop()
    frames = recorder.stop()
    assert "run stopped" in frames[-1]["caption"]


def test_a_renderer_that_cannot_start_disables_recording_only(tmp_path, caplog):
    def broken(model, height, width):
        raise RuntimeError("no OpenGL backend")

    w = world()
    recorder = Recorder(w, tmp_path, interval=0.02, renderer_factory=broken)
    recorder.start()
    assert recorder.stop() == []
    assert recorder.error == "RuntimeError: no OpenGL backend"
    assert "Recording is off" in caplog.text
    assert not list(tmp_path.glob("frame-*.png"))
    assert w.observe()["stopped"] is False


def test_stop_reports_a_writer_that_does_not_finish(tmp_path):
    release = threading.Event()

    class SlowRenderer(FakeRenderer):
        def render(self):
            release.wait(5)
            return super().render()

    w = world()
    recorder = Recorder(
        w, tmp_path, interval=0.02, width=8, height=8, renderer_factory=SlowRenderer
    )
    recorder.start()
    try:
        with pytest.raises(TimeoutError, match="did not finish"):
            recorder.stop(timeout=0.1)
    finally:
        release.set()
        recorder._thread.join(5)


def test_a_render_failure_after_start_is_reported_not_hidden(tmp_path, caplog):
    class FailingRenderer(FakeRenderer):
        def render(self):
            if len(self.cameras) > 1:
                raise OSError("the disk is full")
            return super().render()

    w = world()
    recorder = Recorder(
        w, tmp_path, interval=0.02, width=8, height=8, renderer_factory=FailingRenderer
    )
    recorder.start()
    assert wait_for(lambda: len(recorder.frames) == 1)
    w.stop()  # A state change: the recorder renders again, and the render fails.
    with pytest.raises(RecordingError, match="the disk is full"):
        recorder.stop()
    assert len(recorder.frames) == 1
    assert "Recording stopped" in caplog.text
    assert (tmp_path / "frames.json").exists(), "the frames before the failure stay readable"


def test_a_long_animation_still_ends_on_the_last_frame(tmp_path, monkeypatch):
    monkeypatch.setattr("recorder.MAX_ANIMATION_FRAMES", 3)
    w = world()
    recorder = Recorder(w, tmp_path, width=4, height=2)
    state = w.observe()
    for value in range(5):
        recorder._write_frame(np.full((2, 4, 3), value, dtype=np.uint8), state, None)
    recorder._write_outputs()

    assert len(recorder.frames) == 5, "frames.json and index.html keep every frame"
    chunks = png_chunks((tmp_path / "animation.png").read_bytes())
    assert struct.unpack(">II", chunks[b"acTL"][0]) == (3, 0)
    last = compress_pixels(np.full((2, 4, 3), 4, dtype=np.uint8))
    assert chunks[b"fdAT"][-1][4:] == last


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("the recorder did not finish writing frames in 15 s"),
        RecordingError("the recording stopped early (OSError: the disk is full)"),
    ],
)
def test_serve_does_not_claim_an_incomplete_recording(tmp_path, monkeypatch, caplog, error):
    run_dir = tmp_path / "run"
    demo.prepare(run_dir, EXAMPLE / "site.json", port=8766, provider="copilot_cli")

    class Server:
        def run(self, **kwargs):
            raise KeyboardInterrupt

    class StuckRecorder:
        def __init__(self, world, directory):
            pass

        def start(self):
            pass

        def stop(self):
            raise error

    monkeypatch.setattr(demo, "make_server", lambda *args: Server())
    monkeypatch.setattr(demo, "Recorder", StuckRecorder)
    with caplog.at_level("INFO", logger="transport"):
        demo.serve(run_dir, allow_motion=False, record=tmp_path / "frames")
    assert "Recording is not complete" in caplog.text
    assert "Recorded" not in caplog.text


def test_real_renderer_draws_the_overview_camera(tmp_path):
    """Runs only where MuJoCo can create an OpenGL context (macOS, or MUJOCO_GL set)."""
    if sys.platform != "darwin" and not os.environ.get("MUJOCO_GL"):
        pytest.skip("needs an OpenGL backend for MuJoCo: set MUJOCO_GL (for example egl)")
    w = world()
    result: dict = {}

    def attempt():
        try:
            renderer = mujoco.Renderer(w.model, 90, 160)
            renderer.update_scene(w.data, camera="overview")
            result["pixels"] = renderer.render()
            renderer.close()
        except Exception as error:  # noqa: BLE001
            result["error"] = error

    # A daemon thread: if the renderer hangs, the skip below still lets pytest exit.
    thread = threading.Thread(target=attempt, daemon=True)
    thread.start()
    thread.join(timeout=60)
    if "pixels" not in result:
        pytest.skip(f"no OpenGL backend for MuJoCo here: {result.get('error')}")
    pixels = result["pixels"]
    assert pixels.shape == (90, 160, 3)
    assert len(np.unique(pixels.reshape(-1, 3), axis=0)) > 20, "the frame should not be blank"


def png_chunks(data: bytes) -> dict[bytes, list[bytes]]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    position, chunks = 8, {}
    while position < len(data):
        (length,) = struct.unpack(">I", data[position : position + 4])
        kind = data[position + 4 : position + 8]
        body = data[position + 8 : position + 8 + length]
        (crc,) = struct.unpack(">I", data[position + 8 + length : position + 12 + length])
        assert crc == zlib.crc32(kind + body) & 0xFFFFFFFF
        chunks.setdefault(kind, []).append(body)
        position += 12 + length
    return chunks


def test_encode_apng_numbers_frames_and_delays():
    frames = [(zlib.compress(b"\x00" + bytes(6)), 400), (zlib.compress(b"\x00" + bytes(6)), 1500)]
    chunks = png_chunks(encode_apng(2, 1, frames))
    assert struct.unpack(">II", chunks[b"acTL"][0]) == (2, 0)
    controls = [struct.unpack(">IIIIIHHBB", c) for c in chunks[b"fcTL"]]
    assert [c[0] for c in controls] == [0, 1] and [c[5] for c in controls] == [400, 1500]
    assert struct.unpack(">I", chunks[b"fdAT"][0][:4]) == (2,)
    with pytest.raises(ValueError):
        encode_apng(2, 1, [])


def test_a_frames_directory_with_files_is_refused(tmp_path):
    ensure_empty_directory(tmp_path / "new")
    ensure_empty_directory(tmp_path)
    (tmp_path / "frame-0001.png").write_bytes(b"old")
    with pytest.raises(FileExistsError):
        ensure_empty_directory(tmp_path)


def test_serve_refuses_a_used_frames_directory_without_consuming_the_run(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    demo.prepare(run_dir, EXAMPLE / "site.json", port=8766, provider="copilot_cli")
    frames = tmp_path / "frames"
    frames.mkdir()
    (frames / "frame-0001.png").write_bytes(b"old")
    monkeypatch.setattr(demo, "make_server", lambda *args: pytest.fail("must not start"))
    with pytest.raises(FileExistsError):
        demo.serve(run_dir, allow_motion=True, record=frames)
    assert not (run_dir / "started.json").exists()
