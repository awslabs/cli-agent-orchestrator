"""Optional pictures of the shared MuJoCo world, for people who watch a run.

The recorder only reads the world. It renders the ``overview`` camera that
``simulation.py`` adds. It writes a PNG frame each time the measured state
changes (robot or payload positions, custody, offers, or command records). When
the controller stops, it also writes a page that steps through all the frames,
and an animated PNG. The animation holds at most ``MAX_ANIMATION_FRAMES``
frames: the first ones, and always the last one.
"""

from __future__ import annotations

import html
import json
import logging
import struct
import threading
import zlib
from pathlib import Path
from typing import Any, Callable

import mujoco
import numpy as np
from simulation import World

LOGGER = logging.getLogger("transport.recorder")
RendererFactory = Callable[[mujoco.MjModel, int, int], Any]
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MOTION_FRAME_MS = 400
EVENT_FRAME_MS = 1500
MAX_ANIMATION_FRAMES = 400


def _chunk(kind: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


def _header(width: int, height: int) -> bytes:
    return _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))


def compress_pixels(rgb: np.ndarray) -> bytes:
    """Return the zlib stream of an H x W x 3 uint8 image, with PNG filter 0 rows."""
    if rgb.dtype != np.uint8 or rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError("an image must be an H x W x 3 uint8 array")
    height, width, _ = rgb.shape
    rows = np.ascontiguousarray(rgb).reshape(height, width * 3)
    return zlib.compress(b"".join(b"\x00" + row.tobytes() for row in rows), 9)


def encode_png(rgb: np.ndarray) -> bytes:
    """Encode an H x W x 3 uint8 image as a PNG file, with the standard library only."""
    data = compress_pixels(rgb)
    height, width, _ = rgb.shape
    return PNG_SIGNATURE + _header(width, height) + _chunk(b"IDAT", data) + _chunk(b"IEND", b"")


def encode_apng(width: int, height: int, frames: list[tuple[bytes, int]]) -> bytes:
    """Encode an animated PNG from (compressed pixels, delay in milliseconds) pairs.

    Viewers without APNG support show the first frame as a still PNG.
    """
    if not frames:
        raise ValueError("an animation needs at least one frame")
    parts = [PNG_SIGNATURE, _header(width, height)]
    parts.append(_chunk(b"acTL", struct.pack(">II", len(frames), 0)))
    sequence = 0
    for index, (data, delay_ms) in enumerate(frames):
        control = struct.pack(
            ">IIIIIHHBB", sequence, width, height, 0, 0, max(1, delay_ms), 1000, 0, 0
        )
        parts.append(_chunk(b"fcTL", control))
        sequence += 1
        if index == 0:
            parts.append(_chunk(b"IDAT", data))
        else:
            parts.append(_chunk(b"fdAT", struct.pack(">I", sequence) + data))
            sequence += 1
    parts.append(_chunk(b"IEND", b""))
    return b"".join(parts)


class RecordingError(RuntimeError):
    """The recording stopped early, so its files do not show the full run."""


def ensure_empty_directory(directory: Path) -> None:
    """Refuse a directory with files in it, so that two runs never mix their frames."""
    directory = Path(directory)
    if directory.exists() and (not directory.is_dir() or any(directory.iterdir())):
        raise FileExistsError(f"the frames directory must be new or empty: {directory}")


def _state_key(state: dict) -> tuple:
    """The parts of an observation that a new frame must show (positions to 1 cm)."""
    robots = tuple(
        (name, round(robot["xy"][0], 2), round(robot["xy"][1], 2))
        for name, robot in sorted(state["robots"].items())
    )
    payloads = tuple(
        (
            name,
            round(payload["xy"][0], 2),
            round(payload["xy"][1], 2),
            payload["owner"],
            (payload["offer"] or {}).get("offer_id"),
        )
        for name, payload in sorted(state["payloads"].items())
    )
    commands = tuple(
        (c["actor"], c["command_id"], c["status"], c["reason"]) for c in state["commands"]
    )
    return robots, payloads, commands, state["stopped"]


def _caption(state: dict, event: str | None) -> str:
    parts = []
    for name, payload in sorted(state["payloads"].items()):
        place = f"at {payload['at']}" if payload["at"] else "between locations"
        offer = payload["offer"]
        text = f"{name} {place}, owner {payload['owner']}"
        if offer:
            text += f", offer to {offer['to_zone']} pending"
        parts.append(text)
    if state["stopped"]:
        parts.append("run stopped")
    if event:
        parts.append(event)
    return "; ".join(parts)


class Recorder:
    """Write a PNG frame of the world each time its measured state changes.

    The recorder thread owns the OpenGL context of the renderer. If the
    renderer cannot start (for example, no OpenGL backend on a headless Linux
    host), recording stops with a warning and the controller keeps running.
    """

    def __init__(
        self,
        world: World,
        directory: Path,
        *,
        interval: float = 0.5,
        width: int = 960,
        height: int = 540,
        renderer_factory: RendererFactory | None = None,
    ):
        self.world = world
        self.directory = Path(directory)
        self.interval = interval
        self.width = width
        self.height = height
        self.renderer_factory = renderer_factory or (
            lambda model, height, width: mujoco.Renderer(model, height, width)
        )
        self.frames: list[dict] = []
        self.error: str | None = None
        self._rendering = False
        self._animation: list[tuple[bytes, int]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        ensure_empty_directory(self.directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._run, name="transport-recorder", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 15) -> list[dict]:
        """Write a last frame if the state changed, write the outputs, and return the frames.

        Raises TimeoutError when the outputs are not complete within ``timeout``,
        and RecordingError when rendering or writing failed after the renderer
        started. A renderer that cannot start is not an error: it returns no frames.
        """
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                raise TimeoutError(
                    f"the recorder did not finish writing {self.directory} in {timeout} s"
                )
        if self._rendering and self.error is not None:
            raise RecordingError(f"the recording stopped early ({self.error})")
        return self.frames

    def _run(self) -> None:
        try:
            renderer = self.renderer_factory(self.world.model, self.height, self.width)
        except Exception as error:  # noqa: BLE001 - any renderer failure disables recording
            self.error = f"{type(error).__name__}: {error}"
            LOGGER.warning(
                "Recording is off: the MuJoCo renderer did not start (%s). See the README "
                "section 'See the robots move' for the OpenGL backend.",
                self.error,
            )
            return
        self._rendering = True
        last_key: tuple | None = None
        last_commands: dict = {}
        try:
            while True:
                stopping = self._stop.wait(0 if last_key is None else self.interval)
                with self.world.lock:
                    state = self.world.observe()
                    key = _state_key(state)
                    changed = key != last_key
                    if changed:
                        renderer.update_scene(self.world.data, camera="overview")
                if changed:
                    event = None
                    for command in state["commands"]:
                        name = (command["actor"], command["command_id"])
                        status = (command["status"], command["reason"])
                        if last_commands.get(name) != status:
                            reason = f" ({command['reason']})" if command["reason"] else ""
                            event = (
                                f"{command['actor']} {command['operation']} "
                                f"{command['command_id']}: {command['status']}{reason}"
                            )
                            last_commands[name] = status
                    self._write_frame(renderer.render(), state, event)
                    last_key = key
                if stopping:
                    break
        except Exception as error:  # noqa: BLE001 - report it; the controller keeps running
            self.error = f"{type(error).__name__}: {error}"
            LOGGER.error("Recording stopped: %s", self.error, exc_info=True)
        finally:
            close = getattr(renderer, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - the outputs matter more than the context
                    LOGGER.warning("The MuJoCo renderer did not close cleanly", exc_info=True)
            try:
                self._write_outputs()
            except Exception as error:  # noqa: BLE001
                self.error = self.error or f"{type(error).__name__}: {error}"
                LOGGER.error("Could not write the recording files: %s", error, exc_info=True)

    def _write_frame(self, pixels: np.ndarray, state: dict, event: str | None) -> None:
        number = len(self.frames) + 1
        name = f"frame-{number:04d}.png"
        data = compress_pixels(pixels)
        height, width, _ = pixels.shape
        png = PNG_SIGNATURE + _header(width, height) + _chunk(b"IDAT", data) + _chunk(b"IEND", b"")
        (self.directory / name).write_bytes(png)
        (self.directory / "latest.png").write_bytes(png)
        pause = event is not None and not event.endswith(": running")
        frame = (data, EVENT_FRAME_MS if pause else MOTION_FRAME_MS)
        if len(self._animation) < MAX_ANIMATION_FRAMES:
            self._animation.append(frame)
        else:
            # Keep the newest frame in the last place, so that a long animation
            # still ends on the final state.
            self._animation[-1] = frame
        self.frames.append(
            {
                "file": name,
                "observed_at": state["observed_at"],
                "simulation_seconds": round(state["simulation_seconds"], 2),
                "robots": {n: r["xy"] for n, r in state["robots"].items()},
                "payloads": {
                    n: {"xy": p["xy"], "at": p["at"], "owner": p["owner"], "offer": p["offer"]}
                    for n, p in state["payloads"].items()
                },
                "event": event,
                "caption": _caption(state, event),
            }
        )

    def _write_outputs(self) -> None:
        if self._animation:
            first, _ = self._animation[0]
            self._animation[0] = (first, EVENT_FRAME_MS)
            last, _ = self._animation[-1]
            self._animation[-1] = (last, 3 * EVENT_FRAME_MS)
            (self.directory / "animation.png").write_bytes(
                encode_apng(self.width, self.height, self._animation)
            )
        (self.directory / "frames.json").write_text(
            json.dumps({"run_id": self.world.run_id, "frames": self.frames}, indent=2) + "\n",
            encoding="utf-8",
        )
        items = json.dumps(
            [{"file": f["file"], "caption": html.escape(f["caption"])} for f in self.frames]
        )
        page = _INDEX_HTML.replace("__RUN_ID__", html.escape(self.world.run_id)).replace(
            "__FRAMES__", items
        )
        (self.directory / "index.html").write_text(page, encoding="utf-8")


_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Transport run __RUN_ID__</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 1.5rem; max-width: 980px; }
  img { width: 100%; height: auto; border: 1px solid #ccc; }
  .controls { display: flex; gap: .5rem; align-items: center; margin: .5rem 0; }
  input[type=range] { flex: 1; }
</style>
</head>
<body>
<h1>Transport run <code>__RUN_ID__</code></h1>
<p>Each colored area is one ownership zone. A robot is a box in the strong color
of its zone. The pink box is the payload. The amber disc is the shared dock,
and the grey discs are the other locations.</p>
<figure>
  <img id="frame" alt="">
  <figcaption id="caption" aria-live="polite"></figcaption>
</figure>
<div class="controls">
  <button id="prev" type="button">Previous</button>
  <button id="play" type="button">Play</button>
  <button id="next" type="button">Next</button>
  <label for="slider">Frame</label>
  <input id="slider" type="range" min="0" value="0">
  <span id="counter"></span>
</div>
<script>
const frames = __FRAMES__;
let index = 0, timer = null;
const img = document.getElementById("frame"), caption = document.getElementById("caption");
const slider = document.getElementById("slider"), counter = document.getElementById("counter");
const play = document.getElementById("play");
slider.max = Math.max(frames.length - 1, 0);
function show(i) {
  if (!frames.length) { caption.textContent = "No frames were recorded."; return; }
  index = (i + frames.length) % frames.length;
  const f = frames[index], text = new DOMParser().parseFromString(f.caption, "text/html").body.textContent;
  img.src = f.file; img.alt = text; caption.textContent = text;
  slider.value = index; counter.textContent = (index + 1) + " / " + frames.length;
}
document.getElementById("prev").onclick = () => show(index - 1);
document.getElementById("next").onclick = () => show(index + 1);
slider.oninput = () => show(Number(slider.value));
play.onclick = () => {
  if (timer) { clearInterval(timer); timer = null; play.textContent = "Play"; return; }
  if (index === frames.length - 1) { show(0); }
  play.textContent = "Pause";
  timer = setInterval(() => { if (index === frames.length - 1) { play.onclick(); } else { show(index + 1); } }, 500);
};
show(0);
</script>
</body>
</html>
"""
