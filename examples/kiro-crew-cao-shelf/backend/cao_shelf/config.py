"""Settings stored in ``data/config.json`` (mode 0600) and the one atomic file writer."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .cao import validate_cao_url
from .errors import ShelfError
from .validation import Field, validate_object

DEFAULTS: Dict[str, Any] = {
    "cao_url": "http://127.0.0.1:9889",
    "poll_seconds": 5,
    "warm_hours": 1.0,
    "stale_hours": 48.0,
    "persist_history": True,
}

SPEC = {
    "cao_url": Field("str", min_len=1, max_len=200),
    "poll_seconds": Field("int", minimum=3, maximum=60),
    "warm_hours": Field("number", minimum=0.25, maximum=24),
    "stale_hours": Field("number", minimum=2, maximum=720),
    "persist_history": Field("bool"),
}

ENV_CAO_URL = "CAO_SHELF_CAO_URL"


def atomic_write_json(path: Path, obj: Any) -> None:
    """Write ``obj`` as JSON to ``path`` with mode 0600, replacing the file in one step.

    Every file this backend writes goes through here, and every one is under ``data/``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(obj, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(str(tmp), 0o600)
        os.replace(str(tmp), str(path))
    finally:
        if tmp.exists():
            tmp.unlink()


def move_aside(path: Path, stamp: str) -> Path:
    """Rename ``path`` to ``<name>.<stamp>.aside`` (a counter is added if that exists) and return
    the new path. The file is kept, never deleted."""
    target = path.with_name(f"{path.name}.{stamp}.aside")
    n = 1
    while target.exists():
        n += 1
        target = path.with_name(f"{path.name}.{stamp}-{n}.aside")
    os.replace(str(path), str(target))
    return target


class ConfigStore:
    """Holds the settings in memory and on disk.

    ``CAO_SHELF_CAO_URL`` is honoured only when ``KIROCREW_SPAWNED`` is unset (tests and the dev
    harness); inside Kiro Crew the environment is fixed by the gateway and the setting is the file.
    """

    def __init__(self, path: Path, environ: Optional[Mapping[str, str]] = None) -> None:
        self._path = path
        self._lock = threading.Lock()
        env = environ if environ is not None else os.environ
        self._env_url = env.get(ENV_CAO_URL) if "KIROCREW_SPAWNED" not in env else None
        self._values = self._load()

    def _load(self) -> Dict[str, Any]:
        values = dict(DEFAULTS)
        try:
            stored = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                checked = validate_object(
                    {k: v for k, v in stored.items() if k in SPEC}, SPEC, "config file"
                )
                checked["cao_url"] = validate_cao_url(checked.get("cao_url", values["cao_url"]))
                values.update(checked)
        except (OSError, ValueError, ShelfError):
            values = dict(DEFAULTS)  # a missing or damaged file means defaults
        if values["warm_hours"] >= values["stale_hours"]:
            values = dict(DEFAULTS)
        return values

    def get(self) -> Dict[str, Any]:
        with self._lock:
            out = dict(self._values)
        if self._env_url:
            try:
                out["cao_url"] = validate_cao_url(self._env_url)
            except ShelfError:
                pass
        return out

    def update(self, body: Mapping[str, Any]) -> Dict[str, Any]:
        """Apply a (possibly partial) change. Unknown fields, bad ranges and non-loopback URLs are
        refused and nothing is saved."""
        checked = validate_object(dict(body), SPEC, "config")
        if "cao_url" in checked:
            checked["cao_url"] = validate_cao_url(checked["cao_url"])
        with self._lock:
            merged = {**self._values, **checked}
            if merged["warm_hours"] >= merged["stale_hours"]:
                raise ShelfError("invalid_request", "warm_hours must be below stale_hours")
            atomic_write_json(self._path, merged)
            self._values = merged
        return self.get()
