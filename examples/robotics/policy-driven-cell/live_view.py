"""Live pictures of the cell for the browser view.

A background thread renders each robot's world with the backend's fixed camera
and keeps the latest JPEG of each. A world renders often while its robot has a
running command and rarely while it is idle, so watching costs little CPU when
nothing moves. The thread also projects the named locations and bins into each
picture, so the page can label them.

Rendering only reads a world. It never steps one and never sends a command, so
the view cannot move a robot. A backend without pictures (the fake backend of
the tests) gives an empty view, and the page shows only the map.
"""

from __future__ import annotations

import io
import logging
import threading
import time

from cell import Cell

LOGGER = logging.getLogger("cell")
ACTIVE_FPS = 8.0
IDLE_FPS = 1.0
MAX_BUSY = 0.5  # at most half of one CPU for pictures, so the policy keeps its pace


class LiveView:
    def __init__(self, cell: Cell, *, quality: int = 70) -> None:
        self.cell = cell
        self.quality = quality
        self.enabled = callable(getattr(cell.backend, "frame", None))
        self._frames: dict[str, dict] = {}
        self._labels: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._failed = False

    def start(self) -> None:
        if not self.enabled:
            return
        self._thread = threading.Thread(target=self._loop, name="cell-live-view", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def worlds(self) -> list[str]:
        return [*self.cell.scene.humanoids, *self.cell.scene.arms]

    def jpeg(self, world: str) -> bytes | None:
        with self._lock:
            frame = self._frames.get(world)
            return frame["jpeg"] if frame else None

    def state(self) -> dict:
        """What the page needs besides the cell's own observe(): frame numbers and labels."""
        with self._lock:
            return {
                "enabled": self.enabled and not self._failed,
                "worlds": {
                    world: {
                        "frame": frame["number"],
                        "width": frame["width"],
                        "height": frame["height"],
                        "labels": self._labels.get(world, {}),
                    }
                    for world, frame in self._frames.items()
                },
                "poses": {
                    world: frame["pose"]
                    for world, frame in self._frames.items()
                    if frame.get("pose")
                },
            }

    def _active(self) -> set[str]:
        return {c["robot"] for c in self.cell.observe()["commands"] if c["status"] == "running"}

    def _loop(self) -> None:
        last: dict[str, float] = {}
        while not self._stop.is_set():
            started = time.monotonic()
            active = self._active()
            for world in self.worlds():
                period = 1.0 / (ACTIVE_FPS if world in active else IDLE_FPS)
                if started - last.get(world, 0.0) >= period:
                    last[world] = started
                    if not self._render(world):
                        return
            busy = time.monotonic() - started
            self._stop.wait(max(busy * (1 - MAX_BUSY) / MAX_BUSY, 1.0 / (ACTIVE_FPS * 2)))

    def _render(self, world: str) -> bool:
        from PIL import Image

        backend = self.cell.backend
        try:
            rgb = backend.frame(world)
            buffer = io.BytesIO()
            Image.fromarray(rgb).save(buffer, "JPEG", quality=self.quality)
            pose = None
            if world in self.cell.scene.humanoids:
                p = backend.humanoid_pose(world)
                pose = {
                    "position": [round(p.x, 3), round(p.y, 3)],
                    "yaw": round(p.yaw, 3),
                    "pelvis_height_m": round(p.z, 3),
                }
            if world not in self._labels:
                self._labels[world] = backend.project(world, self._points(world))
        except Exception:
            # Pictures are a convenience. A renderer that cannot start (no OpenGL
            # library, no display) must not stop the run: log once and go on blind.
            LOGGER.exception("LIVE VIEW: no pictures of %s; the map still works", world)
            self._failed = True
            return False
        with self._lock:
            number = self._frames.get(world, {}).get("number", 0) + 1
            self._frames[world] = {
                "jpeg": buffer.getvalue(),
                "number": number,
                "width": rgb.shape[1],
                "height": rgb.shape[0],
                "pose": pose,
            }
        return True

    def _points(self, world: str) -> dict[str, tuple[float, float, float]]:
        scene = self.cell.scene
        if world in scene.humanoids:
            points = {name: (p.x, p.y, 0.0) for name, p in scene.locations.items()}
            # The arm is not in the humanoid's world; label where it stands in the cell.
            points.update(
                {
                    f"arm {name} (not drawn)": (a.base.x, a.base.y, 0.0)
                    for name, a in scene.arms.items()
                }
            )
            return points
        return {name: (p.x, p.y, 0.0) for name, p in scene.bins.items()}
