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
release_commit = _load("release_commit")

FEATURE = Path(".devcontainer") / "features" / "cao" / "devcontainer-feature.json"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"


def test_repository_fragments_are_valid():
    bv.read_fragments()
    bv.promoted_changelog("0.0.0")


@pytest.mark.parametrize(
    ("name", "valid"),
    [
        ("1469.Added.md", True),
        ("3.Changed.md", True),
        ("25.Deprecated.md", True),
        ("1.Removed.md", True),
        ("7.Fixed.md", True),
        ("10.Security.md", True),
        ("0.Fixed.md", False),
        ("07.Fixed.md", False),
        ("3.fixed.md", False),
        ("3.Other.md", False),
        ("3.Fixed.txt", False),
        ("not-a-fragment.md", False),
    ],
)
def test_fragment_name_rules_agree(name, valid):
    assert (bv.FRAGMENT_NAME.fullmatch(name) is not None) is valid
    assert (release_commit.FRAGMENT_PATH.fullmatch(f"changelog.d/{name}") is not None) is valid


@pytest.fixture
def tree(tmp_path: Path, monkeypatch) -> Path:
    """A copy of the version carriers plus a safe changelog, with the module pointed at it."""
    root = tmp_path / "tree"
    (root / FEATURE).parent.mkdir(parents=True)
    for rel in (Path("pyproject.toml"), Path("uv.lock"), FEATURE):
        shutil.copyfile(REPO_ROOT / rel, root / rel)
    (root / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n- Hand-written release note.\n", encoding="utf-8"
    )
    monkeypatch.setattr(bv, "ROOT", root)
    monkeypatch.setattr(bv, "PYPROJECT", root / "pyproject.toml")
    monkeypatch.setattr(bv, "UV_LOCK", root / "uv.lock")
    monkeypatch.setattr(bv, "DEVCONTAINER_FEATURE", root / FEATURE)
    monkeypatch.setattr(bv, "CHANGELOG", root / "CHANGELOG.md")
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


class TestGenerateChangelog:
    def _generate(
        self, tmp_path: Path, monkeypatch, content: bytes, version: str = "1.2.3"
    ) -> bytes:
        changelog = tmp_path / "CHANGELOG.md"
        changelog.write_bytes(content)
        monkeypatch.setattr(bv, "ROOT", tmp_path)
        monkeypatch.setattr(bv, "CHANGELOG", changelog)
        monkeypatch.setattr(bv, "_release_date", lambda: "2030-04-05")
        bv.generate_changelog(version)
        return changelog.read_bytes()

    def test_promotes_the_hand_written_section_without_rewriting_any_other_bytes(
        self, tmp_path, monkeypatch
    ):
        before = (
            b"# Changelog\r\n"
            b"\r\n"
            b"## [Unreleased]\r\n"
            b"\r\n"
            b"### Fixed\r\n"
            b"- A hand-written explanation that is not a commit subject (#1).\r\n"
            b"\r\n"
            b"## [1.2.2] - 2029-12-31\r\n"
            b"\r\n"
            b"- Older release text stays byte-for-byte.\r\n"
        )
        assert self._generate(tmp_path, monkeypatch, before) == (
            b"# Changelog\r\n"
            b"\r\n"
            b"## [Unreleased]\r\n"
            b"\r\n"
            b"## [1.2.3] - 2030-04-05\r\n"
            b"\r\n"
            b"### Fixed\r\n"
            b"- A hand-written explanation that is not a commit subject (#1).\r\n"
            b"\r\n"
            b"## [1.2.2] - 2029-12-31\r\n"
            b"\r\n"
            b"- Older release text stays byte-for-byte.\r\n"
        )

    def test_missing_unreleased_heading_refuses(self, tmp_path, monkeypatch):
        with pytest.raises(bv.ChangelogError, match="exactly one.*found 0"):
            self._generate(
                tmp_path,
                monkeypatch,
                b"# Changelog\n\n## [1.2.2] - 2029-12-31\n\n- Existing notes.\n",
            )

    def test_two_unreleased_headings_refuse(self, tmp_path, monkeypatch):
        with pytest.raises(bv.ChangelogError, match="exactly one.*found 2"):
            self._generate(
                tmp_path,
                monkeypatch,
                b"# Changelog\n\n## [Unreleased]\n\nText\n\n## [Unreleased]\n",
            )

    def test_empty_unreleased_section_is_allowed(self, tmp_path, monkeypatch):
        before = b"# Changelog\n\n## [Unreleased]\n\n## [1.2.2] - 2029-12-31\n\n- Old.\n"
        assert self._generate(tmp_path, monkeypatch, before) == (
            b"# Changelog\n\n"
            b"## [Unreleased]\n\n"
            b"## [1.2.3] - 2030-04-05\n\n"
            b"## [1.2.2] - 2029-12-31\n\n"
            b"- Old.\n"
        )

    def test_release_workflow_has_no_git_cliff_install_or_call(self):
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        assert "git-cliff" not in workflow
        assert "taiki-e/install-action" not in workflow


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
            rel: (tree / rel).read_text("utf-8")
            for rel in ("pyproject.toml", "uv.lock", FEATURE, "CHANGELOG.md")
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
            rel: (tree / rel).read_text("utf-8")
            for rel in ("pyproject.toml", "uv.lock", FEATURE, "CHANGELOG.md")
        }
        runner = _RaisingRunner(exc)
        real = bv.update_uv_lock
        monkeypatch.setattr(bv, "update_uv_lock", lambda old, new: real(old, new, run=runner))
        monkeypatch.setattr(sys, "argv", ["bump_version.py", "patch"])
        with pytest.raises(type(exc)):
            bv.main()
        for rel, text in originals.items():
            assert (tree / rel).read_text("utf-8") == text, rel
        assert runner.calls == [["uv", "lock"]], "the second uv call never runs"


class TestMainRestoresTheLockWhenWritingTheChangelogFails:
    def test_every_version_carrier_is_restored(self, tree, monkeypatch):
        originals = {
            rel: (tree / rel).read_bytes()
            for rel in ("pyproject.toml", "uv.lock", FEATURE, "CHANGELOG.md")
        }
        new_changelog = bv.promoted_changelog(bv.bump("patch", _current(tree)))
        write_changelog = bv._write_changelog

        def fail_only_the_new_changelog(data):
            if data == new_changelog:
                raise OSError("disk full")
            return write_changelog(data)

        monkeypatch.setattr(bv, "_write_changelog", fail_only_the_new_changelog)
        uv = FakeUv(tree / "uv.lock", _moves_version(bv.bump("patch", _current(tree))))
        real = bv.update_uv_lock
        monkeypatch.setattr(bv, "update_uv_lock", lambda old, new: real(old, new, run=uv))
        monkeypatch.setattr(sys, "argv", ["bump_version.py", "patch"])

        with pytest.raises(OSError, match="disk full"):
            bv.main()

        for rel, before in originals.items():
            assert (tree / rel).read_bytes() == before, rel


class TestFragmentCollection:
    def _run(self, tree, monkeypatch):
        new = bv.bump("patch", _current(tree))
        uv = FakeUv(tree / "uv.lock", _moves_version(new))
        real = bv.update_uv_lock
        with monkeypatch.context() as patch:
            patch.setattr(bv, "update_uv_lock", lambda old, version: real(old, version, run=uv))
            patch.setattr(sys, "argv", ["bump_version.py", "patch"])
            bv.main()

    def test_real_bump_collects_all_types_and_preserves_handwritten_notes(self, tree, monkeypatch):
        kinds = ("Added", "Changed", "Deprecated", "Removed", "Fixed", "Security")
        version = bv.bump("patch", _current(tree))
        monkeypatch.setattr(bv, "_release_date", lambda: "2030-04-05")
        (tree / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [Unreleased]\n\n- Hand-written release note.\n\n"
            "## [1.0.0] - 2029-01-01\n\n- Old release.\n"
        )
        directory = tree / "changelog.d"
        directory.mkdir()
        (directory / "README.md").write_text("Documentation.\n")
        for kind in kinds:
            for number in (20, 3):
                (directory / f"{number}.{kind}.md").write_text(f"- {kind} #{number}.\n")
        self._run(tree, monkeypatch)
        text = (tree / "CHANGELOG.md").read_text()
        assert text.count("- Hand-written release note.") == 1
        positions = [text.index(f"- {kind} #{number}.") for kind in kinds for number in (3, 20)]
        assert positions == sorted(positions)
        assert text.count("## [Unreleased]") == 1
        assert sorted(path.name for path in directory.iterdir()) == ["README.md"]
        assert "## [1.0.0] - 2029-01-01\n\n- Old release.\n" in text
        released = text.split("## [Unreleased]\n\n", 1)[1].split("## [1.0.0]", 1)[0]
        assert released.index("- Hand-written release note.") < released.index("### Added")
        for kind in kinds:
            assert released.count(f"### {kind}\n\n") == 1
        assert released.index("### Added") < released.index("### Changed")
        assert released.endswith("### Security\n\n- Security #3.\n- Security #20.\n\n")
        expected = (
            f"# Changelog\n\n## [Unreleased]\n\n## [{version}] - 2030-04-05\n\n"
            "- Hand-written release note.\n\n"
            + "".join(f"### {kind}\n\n- {kind} #3.\n- {kind} #20.\n\n" for kind in kinds)
            + "## [1.0.0] - 2029-01-01\n\n- Old release.\n"
        )
        assert text == expected

        self._run(tree, monkeypatch)
        text = (tree / "CHANGELOG.md").read_text()
        assert text.count("- Added #3.") == 1
        assert text.count("- Hand-written release note.") == 1

    def test_merges_matching_heading_without_duplicate_and_inserts_new_heading_in_order(
        self, tree, monkeypatch
    ):
        (tree / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [Unreleased]\n\n### Fixed\n\n- Hand-written fix.\n\n"
            "### Security\n\n- Hand-written security note.\n\n"
            "### Other\n\n- Keep other notes.\n\n## [1.0.0] - 2029-01-01\n\n- Old.\n"
        )
        directory = tree / "changelog.d"
        directory.mkdir()
        (directory / "5.Fixed.md").write_text("- Fragment fix.\n")
        (directory / "6.Added.md").write_text("- Fragment addition.\n")
        self._run(tree, monkeypatch)
        text = (tree / "CHANGELOG.md").read_text()
        released = text.split("## [Unreleased]\n\n", 1)[1].split("## [1.0.0]", 1)[0]
        assert released.count("### Fixed") == 1
        assert released.index("### Added") < released.index("### Fixed")
        assert released.index("- Hand-written fix.") < released.index("- Fragment fix. (#5)")
        assert released.index("- Fragment fix. (#5)") < released.index("### Other")
        assert released.count("### Security\n\n") == 1
        assert "- Hand-written security note." in released
        assert text.endswith("## [1.0.0] - 2029-01-01\n\n- Old.\n")

    def test_duplicate_hand_written_headings_collapse_and_exact_pr_number_is_cited(
        self, tree, monkeypatch
    ):
        (tree / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [Unreleased]\n\n### Changed\n\n- First change.\n\n"
            "### Changed\n\n- Second change.\n\n## [1.0.0] - 2029-01-01\n\n- Old.\n"
        )
        directory = tree / "changelog.d"
        directory.mkdir()
        (directory / "5.Changed.md").write_text("- Mentions #50 but not this PR.\n")
        self._run(tree, monkeypatch)
        text = (tree / "CHANGELOG.md").read_text()
        released = text.split("## [Unreleased]\n\n", 1)[1].split("## [1.0.0]", 1)[0]
        assert released.count("### Changed") == 1
        assert released.index("- First change.") < released.index("- Second change.")
        assert released.index("- Second change.") < released.index(
            "- Mentions #50 but not this PR. (#5)"
        )

    def test_empty_handwritten_section_has_one_blank_line_after_release_heading(
        self, tree, monkeypatch
    ):
        (tree / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n\n")
        directory = tree / "changelog.d"
        directory.mkdir()
        (directory / "5.Fixed.md").write_text("- Fix.\n")
        monkeypatch.setattr(bv, "_release_date", lambda: "2030-04-05")
        version = bv.bump("patch", _current(tree))
        self._run(tree, monkeypatch)
        assert (tree / "CHANGELOG.md").read_text() == (
            f"# Changelog\n\n## [Unreleased]\n\n## [{version}] - 2030-04-05\n\n"
            "### Fixed\n\n- Fix. (#5)\n\n"
        )

    @pytest.mark.parametrize(
        "name", ["not-a-fragment.md", "14.Other.md", "14.Fixed.txt", "0.Fixed.md", "07.Fixed.md"]
    )
    def test_invalid_name_or_type_refuses_without_changes(self, tree, monkeypatch, name):
        directory = tree / "changelog.d"
        directory.mkdir()
        (directory / name).write_text("- Note.\n")
        before = {
            path: path.read_bytes()
            for path in (
                tree / "CHANGELOG.md",
                tree / "uv.lock",
                tree / "pyproject.toml",
                tree / FEATURE,
                directory / name,
            )
        }
        with pytest.raises(bv.ChangelogError, match="expected <PR>.<type>.md"):
            self._run(tree, monkeypatch)
        assert all(path.read_bytes() == content for path, content in before.items())

    def test_invalid_content_refuses_without_changes(self, tree, monkeypatch):
        directory = tree / "changelog.d"
        directory.mkdir()
        fragment = directory / "3.Fixed.md"
        fragment.write_text("- First.\n- Second.\n")
        before = {
            path: path.read_bytes()
            for path in (
                tree / "CHANGELOG.md",
                tree / "uv.lock",
                tree / "pyproject.toml",
                tree / FEATURE,
                fragment,
            )
        }
        with pytest.raises(bv.ChangelogError, match="expected one '- ' entry"):
            self._run(tree, monkeypatch)
        assert all(path.read_bytes() == content for path, content in before.items())

    @pytest.mark.parametrize(
        ("content", "message"),
        [
            (b"Note.\n", "expected one '- ' entry"),
            (b"-Note.\n", "expected one '- ' entry"),
            (b"\xff", "expected UTF-8"),
            (b"\xef\xbb\xbf- Note.\n", "UTF-8 BOM is not allowed"),
        ],
    )
    def test_malformed_fragment_is_refused_before_any_carrier_changes(
        self, tree, monkeypatch, content, message
    ):
        directory = tree / "changelog.d"
        directory.mkdir()
        fragment = directory / "5.Fixed.md"
        fragment.write_bytes(content)
        before = {
            path: path.read_bytes()
            for path in (
                tree / "CHANGELOG.md",
                tree / "uv.lock",
                tree / "pyproject.toml",
                tree / FEATURE,
                fragment,
            )
        }
        with pytest.raises(bv.ChangelogError, match=message):
            self._run(tree, monkeypatch)
        assert all(path.read_bytes() == data for path, data in before.items())

    @pytest.mark.parametrize("kind", ["directory", "symlink"])
    def test_fragment_that_is_not_regular_file_is_refused(self, tree, monkeypatch, kind):
        directory = tree / "changelog.d"
        directory.mkdir()
        fragment = directory / "5.Fixed.md"
        if kind == "directory":
            fragment.mkdir()
        else:
            target = tree / "note.txt"
            target.write_text("- Note.\n")
            fragment.symlink_to(target)
        before = (tree / "CHANGELOG.md").read_bytes()
        with pytest.raises(bv.ChangelogError, match="expected <PR>.<type>.md"):
            self._run(tree, monkeypatch)
        assert (tree / "CHANGELOG.md").read_bytes() == before
        if kind == "directory":
            assert fragment.is_dir()
        else:
            assert fragment.is_symlink()

    def test_link_references_stay_after_generated_sections(self, tree, monkeypatch):
        changelog = tree / "CHANGELOG.md"
        changelog.write_text(
            "# Changelog\n\n## [Unreleased]\n\n[latest]: https://example.invalid/latest\n"
        )
        directory = tree / "changelog.d"
        directory.mkdir()
        (directory / "5.Fixed.md").write_text("- Fix.\n")
        self._run(tree, monkeypatch)
        text = changelog.read_text()
        assert text.index("### Fixed") < text.index("[latest]:")
        assert text.endswith("[latest]: https://example.invalid/latest\n")

    def test_failed_restore_does_not_hide_original_error_or_skip_other_restores(
        self, tree, monkeypatch, capsys
    ):
        directory = tree / "changelog.d"
        directory.mkdir()
        first = directory / "3.Added.md"
        second = directory / "4.Fixed.md"
        first.write_text("- First.\n")
        second.write_text("- Second.\n")
        before = {
            path: path.read_bytes()
            for path in (tree / "CHANGELOG.md", tree / "uv.lock", tree / FEATURE, first, second)
        }
        unlink = Path.unlink
        write_bytes = Path.write_bytes
        unlink_count = 0

        def fail_second_unlink(path, *args, **kwargs):
            nonlocal unlink_count
            if path in (first, second):
                unlink_count += 1
                if unlink_count == 2:
                    raise OSError("original delete failure")
            return unlink(path, *args, **kwargs)

        def fail_pyproject_restore(path, content):
            if path == bv.PYPROJECT:
                raise OSError("restore failure")
            return write_bytes(path, content)

        monkeypatch.setattr(Path, "unlink", fail_second_unlink)
        monkeypatch.setattr(Path, "write_bytes", fail_pyproject_restore)
        with pytest.raises(OSError, match="original delete failure"):
            self._run(tree, monkeypatch)
        assert "restore failure" in capsys.readouterr().err
        assert all(path.read_bytes() == content for path, content in before.items())

    @pytest.mark.parametrize("failure", ["write", "chmod", "replace", "delete"])
    def test_midway_failure_restores_changelog_and_every_fragment(self, tree, monkeypatch, failure):
        directory = tree / "changelog.d"
        directory.mkdir()
        fragments = [directory / "3.Added.md", directory / "4.Fixed.md"]
        for fragment in fragments:
            fragment.write_text("- Note.\n")
        before = {
            path: path.read_bytes()
            for path in (
                tree / "CHANGELOG.md",
                tree / "uv.lock",
                tree / "pyproject.toml",
                tree / FEATURE,
                *fragments,
            )
        }
        if failure == "write":

            def fail_write(content):
                raise OSError("disk full")

            monkeypatch.setattr(bv, "_write_changelog", fail_write)
        elif failure == "chmod":

            def fail_chmod(path, mode):
                raise OSError("cannot set mode")

            monkeypatch.setattr(bv.os, "chmod", fail_chmod)
        elif failure == "replace":

            def fail_replace(source, destination):
                raise OSError("cannot replace changelog")

            monkeypatch.setattr(bv.os, "replace", fail_replace)
        else:
            unlink = Path.unlink
            unlink_count = 0

            def fail_second_fragment(path, *args, **kwargs):
                nonlocal unlink_count
                if path in fragments:
                    unlink_count += 1
                    if unlink_count == 2:
                        raise OSError("cannot delete fragment")
                return unlink(path, *args, **kwargs)

            monkeypatch.setattr(Path, "unlink", fail_second_fragment)
        with pytest.raises(OSError):
            self._run(tree, monkeypatch)
        assert all(path.read_bytes() == content for path, content in before.items())
        assert not list(tree.glob(".CHANGELOG-*"))


class TestMainRefusesInvalidChangelogBeforeWriting:
    @pytest.mark.parametrize(
        "changelog",
        [
            "# Changelog\n\n## [1.2.2] - 2029-12-31\n\n- Existing notes.\n",
            "# Changelog\n\n## [Unreleased]\n\nText\n\n## [Unreleased]\n",
        ],
        ids=["missing-unreleased", "two-unreleased"],
    )
    def test_no_version_carrier_changes_when_changelog_is_invalid(
        self, tree, monkeypatch, changelog
    ):
        (tree / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
        originals = {
            rel: (tree / rel).read_bytes()
            for rel in ("pyproject.toml", FEATURE, "uv.lock", "CHANGELOG.md")
        }
        uv = FakeUv(tree / "uv.lock", _moves_version(bv.bump("patch", _current(tree))))
        real = bv.update_uv_lock
        monkeypatch.setattr(bv, "update_uv_lock", lambda old, new: real(old, new, run=uv))
        monkeypatch.setattr(sys, "argv", ["bump_version.py", "patch"])

        with pytest.raises(bv.ChangelogError, match="exactly one"):
            bv.main()

        for rel, before in originals.items():
            assert (tree / rel).read_bytes() == before, rel
