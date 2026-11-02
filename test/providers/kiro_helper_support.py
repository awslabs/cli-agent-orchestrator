"""Shared fixtures for the Kiro helper tests (#1167 Phase 2).

``operator_home`` gives a test a fake ``HOME`` whose ``~/.kiro`` looks like a
real operator's (agents, settings, steering and installed powers), and arms a
spy for G19b: CAO may only ``stat`` or ``lstat`` anything under that
``~/.kiro``. Opening, listing, creating, writing, chmod-ing, renaming or
removing any path there fails the test at teardown. A test that arranges its
own ``~/.kiro`` does so inside ``operator_home.arranging()``.

Import the fixture into a test module (``from test.providers.kiro_helper_support
import operator_home  # noqa: F401``) so pytest finds it there.
"""

from __future__ import annotations

import builtins
import contextlib
import functools
import io
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

# os and shutil calls that open, list, create, change or remove a path. stat,
# lstat, access and realpath are the reads H5 allows.
_PATH_CALLS = (
    "open",
    "listdir",
    "scandir",
    "mkdir",
    "makedirs",
    "rmdir",
    "removedirs",
    "unlink",
    "remove",
    "rename",
    "replace",
    "renames",
    "chmod",
    "chown",
    "lchown",
    "chflags",
    "lchflags",
    "symlink",
    "link",
    "utime",
    "truncate",
    "mkfifo",
    "mknod",
)
_SHUTIL_CALLS = ("rmtree", "copy", "copy2", "copyfile", "copytree", "move", "chown")


@dataclass
class GlobalKiroSpy:
    """Records every forbidden call that names a path under one ``~/.kiro``."""

    kiro: Path
    touches: list[str] = field(default_factory=list)
    armed: bool = True

    def _roots(self) -> tuple[str, ...]:
        return tuple({os.path.abspath(self.kiro), os.path.realpath(self.kiro)})

    def _under(self, value: Any) -> bool:
        # fsdecode, not fspath: a bytes path (psutil lists /proc that way on
        # Linux) must compare as text, not raise inside the call it wraps.
        try:
            path = os.path.abspath(os.fsdecode(value))
        except TypeError:
            return False  # A file descriptor, or no path at all.
        return any(path == root or path.startswith(root + os.sep) for root in self._roots())

    def _wrap(self, name: str, real: Callable[..., Any]) -> Callable[..., Any]:
        # wraps() keeps the function's attributes, e.g. rmtree.avoids_symlink_attacks.
        @functools.wraps(real)
        def spy(*args: Any, **kwargs: Any) -> Any:
            if self.armed and not kwargs.get("dir_fd"):
                for value in args[:2]:
                    if self._under(value):
                        self.touches.append(f"{name}({os.fsdecode(value)})")
            return real(*args, **kwargs)

        return spy

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in _PATH_CALLS:
            real = getattr(os, name, None)
            if real is not None:
                monkeypatch.setattr(os, name, self._wrap(f"os.{name}", real))
        for name in _SHUTIL_CALLS:
            monkeypatch.setattr(shutil, name, self._wrap(f"shutil.{name}", getattr(shutil, name)))
        opener = self._wrap("open", builtins.open)
        monkeypatch.setattr(builtins, "open", opener)
        monkeypatch.setattr(io, "open", opener)

    @contextlib.contextmanager
    def paused(self) -> Iterator[None]:
        self.armed = False
        try:
            yield
        finally:
            self.armed = True


@dataclass
class OperatorHome:
    """A fake operator ``HOME`` and the spy over its ``~/.kiro``."""

    home: Path
    kiro: Path
    spy: GlobalKiroSpy

    def arranging(self) -> contextlib.AbstractContextManager[None]:
        """Arrange ``~/.kiro`` yourself, with the spy paused."""
        return self.spy.paused()

    def set_entry(self, name: str, kind: str) -> None:
        """``kind``: ``absent``, ``file``, ``dir`` or ``dangling`` (a link to nothing)."""
        path = self.kiro / name
        with self.arranging():
            if os.path.lexists(path):
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            if kind == "file":
                path.write_text("{}")
            elif kind == "dir":
                path.mkdir()
                (path / "installed.json").write_text('{"powers": []}')
            elif kind == "dangling":
                path.symlink_to(self.kiro / "missing-target")


@pytest.fixture
def operator_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[OperatorHome]:
    """This Mac's shape by default: powers and its marker, no secret store (S7)."""
    home = tmp_path / "operator-home"
    kiro = home / ".kiro"
    for sub in ("agents", "settings", "steering", "powers"):
        (kiro / sub).mkdir(parents=True)
    (kiro / "agents" / "mine.json").write_text('{"name": "mine", "tools": ["*"]}')
    (kiro / "settings" / "cli.json").write_text('{"chat.defaultAgent": "mine"}')
    (kiro / "powers" / "installed.json").write_text('{"powers": []}')
    (kiro / ".powers-migrated").write_text("/somewhere/else\n")
    monkeypatch.setenv("HOME", str(home))
    spy = GlobalKiroSpy(kiro)
    spy.install(monkeypatch)
    yield OperatorHome(home, kiro, spy)
    touches = list(spy.touches)
    spy.armed = False
    assert not touches, f"CAO touched the operator's ~/.kiro beyond stat: {touches}"
