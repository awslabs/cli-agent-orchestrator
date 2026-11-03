"""Tests for ``scripts/bump_version.py``: the bump must move every version carrier together.

``publish-to-pypi.yml``'s ``verify-release-version`` refuses a tag unless ``pyproject.toml``,
``uv.lock`` and the devcontainer feature all carry it. ``release.yml`` bumped only two of
them, so 2.5.3 and 2.5.4 had to be released by hand through bump PRs (#899, #946). These
tests run the real ``assert_release_version.check`` on the bumped tree, so a bump the
publish workflow would refuse fails here first.

``uv`` itself is replaced by a fake that edits the lock the way a given uv would. A real
``uv lock`` needs the network and a uv matching the one that wrote the lock; the point of
the guard is precisely to refuse when that uv does more than move the version.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bv = _load("bump_version")
arv = _load("assert_release_version")

FEATURE = Path(".devcontainer") / "features" / "cao" / "devcontainer-feature.json"


@pytest.fixture
def tree(tmp_path: Path, monkeypatch) -> Path:
    """A copy of this repo's three real version carriers, with the module pointed at it."""
    root = tmp_path / "tree"
    (root / FEATURE).parent.mkdir(parents=True)
    for rel in (Path("pyproject.toml"), Path("uv.lock"), FEATURE):
        shutil.copyfile(REPO_ROOT / rel, root / rel)
    monkeypatch.setattr(bv, "ROOT", root)
    monkeypatch.setattr(bv, "PYPROJECT", root / "pyproject.toml")
    monkeypatch.setattr(bv, "UV_LOCK", root / "uv.lock")
    monkeypatch.setattr(bv, "DEVCONTAINER_FEATURE", root / FEATURE)
    return root


def _set_root_version(text: str, version: str) -> str:
    return re.sub(
        r'(\[\[package\]\]\nname = "cli-agent-orchestrator"\nversion = )"[^"]+"',
        rf'\1"{version}"',
        text,
        count=1,
    )


class FakeUv:
    """Stands in for ``subprocess.run`` on the two uv calls bump_version makes.

    ``relock`` maps the lock text to what this uv would write after the version moved.
    """

    def __init__(
        self,
        lock: Path,
        relock: Callable[[str], str],
        fail_lock: bool = False,
        fail_check: bool = False,
    ) -> None:
        self.lock, self.relock = lock, relock
        self.fail_lock, self.fail_check = fail_lock, fail_check
        self.calls: List[List[str]] = []
        self.envs: List[Optional[dict]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        self.envs.append(kwargs.get("env"))
        if argv == ["uv", "lock"]:
            self.lock.write_text(self.relock(self.lock.read_text("utf-8")), "utf-8")
            if self.fail_lock:
                raise subprocess.CalledProcessError(2, argv)
        elif argv == ["uv", "lock", "--locked"]:
            if self.fail_check:
                raise subprocess.CalledProcessError(1, argv)
        else:  # pragma: no cover - a call the script must not make
            raise AssertionError(f"unexpected call {argv}")
        return subprocess.CompletedProcess(argv, 0, "", "")


def _moves_version(to: str) -> Callable[[str], str]:
    return lambda text: _set_root_version(text, to)


def _current(tree: Path) -> str:
    return arv.pyproject_version(tree / "pyproject.toml")


class TestUpdateUvLock:
    def test_the_lock_moves_with_the_version_and_is_rechecked(self, tree, monkeypatch):
        monkeypatch.setenv("UV_LOCKED", "1")
        old = _current(tree)
        uv = FakeUv(tree / "uv.lock", _moves_version("9.9.9"))
        bv.update_uv_lock(old, "9.9.9", run=uv)
        assert arv.lock_version("cli-agent-orchestrator", tree / "uv.lock") == "9.9.9"
        assert uv.calls == [["uv", "lock"], ["uv", "lock", "--locked"]]

    def test_the_relock_runs_without_the_workflow_lock_flags(self, tree, monkeypatch):
        """release.yml sets UV_LOCKED=1, which would make `uv lock` refuse to write."""
        monkeypatch.setenv("UV_LOCKED", "1")
        monkeypatch.setenv("UV_FROZEN", "1")
        uv = FakeUv(tree / "uv.lock", _moves_version("9.9.9"))
        bv.update_uv_lock(_current(tree), "9.9.9", run=uv)
        relock_env = uv.envs[0]
        assert relock_env is not None
        assert "UV_LOCKED" not in relock_env and "UV_FROZEN" not in relock_env
        assert uv.envs[1] is None, "the --locked re-check keeps the inherited flags"

    def test_a_revision_rewrite_is_undone(self, tree):
        before = (tree / "uv.lock").read_text("utf-8")
        revision = re.search(r"(?m)^revision = \d+$", before)
        assert revision is not None

        def newer_uv(text: str) -> str:
            return re.sub(
                r"(?m)^revision = \d+$", "revision = 99", _set_root_version(text, "9.9.9")
            )

        bv.update_uv_lock(_current(tree), "9.9.9", run=FakeUv(tree / "uv.lock", newer_uv))
        after = (tree / "uv.lock").read_text("utf-8")
        assert revision.group(0) in after and "revision = 99" not in after

    def test_any_other_change_refuses_and_restores_the_lock(self, tree):
        before = (tree / "uv.lock").read_text("utf-8")

        def re_resolves(text: str) -> str:
            return _set_root_version(text, "9.9.9").replace(
                "requires-python", "requires-python-rewritten", 1
            )

        uv = FakeUv(tree / "uv.lock", re_resolves)
        with pytest.raises(bv.LockDrift, match=r"changed 2 line\(s\)"):
            bv.update_uv_lock(_current(tree), "9.9.9", run=uv)
        assert (tree / "uv.lock").read_text("utf-8") == before
        assert uv.calls == [["uv", "lock"]], "a drifted lock is never re-checked or kept"

    def test_a_failing_uv_restores_the_lock(self, tree):
        before = (tree / "uv.lock").read_text("utf-8")
        uv = FakeUv(tree / "uv.lock", lambda text: text + "half-written\n", fail_lock=True)
        with pytest.raises(subprocess.CalledProcessError):
            bv.update_uv_lock(_current(tree), "9.9.9", run=uv)
        assert (tree / "uv.lock").read_text("utf-8") == before

    def test_a_failing_recheck_restores_the_lock(self, tree):
        before = (tree / "uv.lock").read_text("utf-8")
        uv = FakeUv(tree / "uv.lock", _moves_version("9.9.9"), fail_check=True)
        with pytest.raises(subprocess.CalledProcessError):
            bv.update_uv_lock(_current(tree), "9.9.9", run=uv)
        assert (tree / "uv.lock").read_text("utf-8") == before

    def test_a_lock_that_already_lags_refuses_before_running_uv(self, tree):
        uv = FakeUv(tree / "uv.lock", _moves_version("9.9.9"))
        with pytest.raises(bv.LockDrift, match="lags"):
            bv.update_uv_lock("0.0.1", "9.9.9", run=uv)
        assert uv.calls == []


class TestTheBumpPassesThePublishCheck:
    def _bump(self, tree: Path, monkeypatch, part: str, uv: FakeUv) -> int:
        monkeypatch.setattr(bv, "generate_changelog", lambda version: None)
        real = bv.update_uv_lock
        monkeypatch.setattr(bv, "update_uv_lock", lambda old, new: real(old, new, run=uv))
        monkeypatch.setattr(sys, "argv", ["bump_version.py", part])
        try:
            bv.main()
        except SystemExit as exc:
            return int(exc.code or 0)
        return 0

    @pytest.mark.parametrize("part", ["patch", "minor", "major"])
    def test_all_three_carriers_agree_with_the_new_tag(self, tree, monkeypatch, part):
        old = _current(tree)
        new = bv.bump(part, old)
        uv = FakeUv(tree / "uv.lock", _moves_version(new))
        assert self._bump(tree, monkeypatch, part, uv) == 0
        report = arv.check(
            f"v{new}",
            pyproject=tree / "pyproject.toml",
            lock=tree / "uv.lock",
            devcontainer=tree / FEATURE,
        )
        assert report.ok, report.problems
        feature = json.loads((tree / FEATURE).read_text("utf-8"))
        assert feature["version"] == new

    def test_without_the_lock_step_the_publish_check_refuses(self, tree):
        """The gap this closes: the old bump moved pyproject and the feature, not uv.lock."""
        old = _current(tree)
        new = bv.bump("patch", old)
        bv.update_pyproject(new)
        bv.update_devcontainer_feature(new)
        report = arv.check(
            f"v{new}",
            pyproject=tree / "pyproject.toml",
            lock=tree / "uv.lock",
            devcontainer=tree / FEATURE,
        )
        assert not report.ok
        assert any("uv.lock records" in p for p in report.problems)

    def test_a_drifting_lock_leaves_the_whole_tree_as_it_was(self, tree, monkeypatch, capsys):
        originals = {
            rel: (tree / rel).read_text("utf-8") for rel in ("pyproject.toml", "uv.lock", FEATURE)
        }

        def re_resolves(text: str) -> str:
            return text.replace("requires-python", "requires-python-rewritten", 1)

        uv = FakeUv(tree / "uv.lock", re_resolves)
        assert self._bump(tree, monkeypatch, "patch", uv) == 1
        for rel, text in originals.items():
            assert (tree / rel).read_text("utf-8") == text, rel
        assert "relock main in a reviewed PR" in capsys.readouterr().err


class _RaisingRunner:
    """Stands in for ``subprocess.run`` when ``uv`` itself cannot run at all."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.calls: List[List[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        raise self.exc


class TestMainRestoresOnAnyException:
    """Review on #652, P3-2: ``main`` restored ``pyproject.toml`` and the devcontainer
    feature only on ``LockDrift``; ``update_uv_lock`` restores only ``uv.lock``. A missing
    or failing ``uv``, or Ctrl-C, left the tree half-bumped and a re-run did not repair it.
    """

    @pytest.mark.parametrize(
        "exc",
        [
            FileNotFoundError(2, "No such file or directory: 'uv'"),
            subprocess.CalledProcessError(2, ["uv", "lock"]),
            KeyboardInterrupt(),
        ],
        ids=["missing-uv", "failing-uv", "interrupt"],
    )
    def test_every_snapshotted_file_is_restored_on_any_exception(self, tree, monkeypatch, exc):
        originals = {
            rel: (tree / rel).read_text("utf-8") for rel in ("pyproject.toml", "uv.lock", FEATURE)
        }
        runner = _RaisingRunner(exc)
        monkeypatch.setattr(bv, "generate_changelog", lambda version: None)
        real = bv.update_uv_lock
        monkeypatch.setattr(bv, "update_uv_lock", lambda old, new: real(old, new, run=runner))
        monkeypatch.setattr(sys, "argv", ["bump_version.py", "patch"])
        with pytest.raises(type(exc)):
            bv.main()
        for rel, text in originals.items():
            assert (tree / rel).read_text("utf-8") == text, rel
        assert runner.calls == [["uv", "lock"]], "the second uv call never runs"
