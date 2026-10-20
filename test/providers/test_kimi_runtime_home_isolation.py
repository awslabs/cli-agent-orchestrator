"""Security isolation tests for the per-worker Kimi runtime home.

Two independently reproduced findings live here:

**Finding 1 (P2).** ``_copy_tree`` passes ``symlinks=True`` so an *internal*
symlink under ``credentials/`` (a member of :data:`SECRET_DIR_NAMES`) was
reproduced verbatim into the runtime home. Writing through the runtime copy then
wrote through the link back into shared/source state. Secret credential state
must never contain a writable path back out of the runtime home, so the secret
copy policy is deliberately not the ordinary ``skills``/``plugins`` policy.

**Finding 2 (P3).** ``_copy_trust_tree`` did ``entries = sorted(scan, ...)``,
which materialised the *entire* source directory before :data:`MAX_TRUST_ENTRIES`
was applied. The record count was bounded but the enumeration, allocation and
scandir consumption were not.

All tests use ``tmp_path`` only; the real ``~/.kimi-code`` is never touched.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any, Dict

from cli_agent_orchestrator.providers import kimi_runtime_home as mod
from cli_agent_orchestrator.providers.kimi_runtime_home import (
    MAX_TRUST_ENTRIES,
    TRUST_DIR_NAME,
    KimiCodeRuntimeHomeBuilder,
)


def _build(source: Path, temp: Path):
    return KimiCodeRuntimeHomeBuilder(source, temp).build()


def _assert_no_escape(root: Path) -> None:
    """Every entry under ``root`` must be a real entry inside ``root``.

    This is the core credential invariant: no symlink, and nothing whose real
    path resolves outside the runtime home (i.e. no writable path back into the
    source home or any shared target).
    """

    assert not root.is_symlink(), f"{root} is a symlink"
    resolved_root = root.resolve()
    for path in root.rglob("*"):
        assert not path.is_symlink(), f"{path} is a symlink"
        real = Path(os.path.realpath(path))
        assert real.is_relative_to(resolved_root), f"{path} escapes to {real}"


class TestCredentialSymlinkIsolation:
    """Finding 1 — secret trees never retain a writable path out."""

    def test_internal_relative_file_symlink_is_not_reproduced(self, tmp_path, caplog):
        source = tmp_path / "src"
        creds = source / "credentials"
        creds.mkdir(parents=True)
        (creds / "real.json").write_text("real")
        (creds / "alias.json").symlink_to("real.json")

        result = _build(source, tmp_path / "temp")

        runtime = result.home / "credentials"
        _assert_no_escape(runtime)
        assert (runtime / "real.json").read_text() == "real"
        assert not (runtime / "alias.json").is_symlink()

    def test_internal_directory_symlink_is_not_followed(self, tmp_path, caplog):
        source = tmp_path / "src"
        creds = source / "credentials"
        creds.mkdir(parents=True)
        external = tmp_path / "external-creds"
        (external / "deep").mkdir(parents=True)
        (external / "deep" / "token.json").write_text("external-token")
        (creds / "linked-dir").symlink_to(external, target_is_directory=True)

        external_before = {
            str(p.relative_to(external)): p.read_bytes() for p in external.rglob("*") if p.is_file()
        }

        result = _build(source, tmp_path / "temp")

        runtime = result.home / "credentials"
        _assert_no_escape(runtime)
        assert not (runtime / "linked-dir").exists()
        assert not (runtime / "linked-dir" / "deep" / "token.json").exists()
        after = {
            str(p.relative_to(external)): p.read_bytes() for p in external.rglob("*") if p.is_file()
        }
        assert after == external_before

    def test_relative_symlink_to_target_outside_source_home(self, tmp_path):
        source = tmp_path / "src"
        creds = source / "credentials"
        creds.mkdir(parents=True)
        shared = tmp_path / "shared-rel-token.json"
        shared.write_text("shared-original")
        relative = os.path.relpath(shared, creds)
        (creds / "rel.json").symlink_to(relative)

        result = _build(source, tmp_path / "temp")

        runtime = result.home / "credentials"
        _assert_no_escape(runtime)
        assert not (runtime / "rel.json").is_symlink()
        assert shared.read_text() == "shared-original"

    def test_absolute_symlink_to_target_outside_source_home(self, tmp_path):
        source = tmp_path / "src"
        creds = source / "credentials"
        creds.mkdir(parents=True)
        shared = tmp_path / "shared-abs-token.json"
        shared.write_text("shared-original")
        (creds / "abs.json").symlink_to(shared)

        result = _build(source, tmp_path / "temp")

        runtime = result.home / "credentials"
        _assert_no_escape(runtime)
        assert not (runtime / "abs.json").is_symlink()
        assert shared.read_text() == "shared-original"

    def test_dangling_symlink_is_skipped_without_aborting(self, tmp_path):
        source = tmp_path / "src"
        creds = source / "credentials"
        creds.mkdir(parents=True)
        (creds / "kept.json").write_text("kept")
        (creds / "dangling.json").symlink_to(creds / "missing.json")

        result = _build(source, tmp_path / "temp")

        runtime = result.home / "credentials"
        _assert_no_escape(runtime)
        assert (runtime / "kept.json").read_text() == "kept"

    def test_ordinary_credential_file_is_a_real_private_copy(self, tmp_path):
        source = tmp_path / "src"
        creds = source / "credentials"
        creds.mkdir(parents=True)
        plain = creds / "plain.json"
        plain.write_text("plain")
        os.chmod(plain, 0o644)

        result = _build(source, tmp_path / "temp")

        runtime = result.home / "credentials"
        copied = runtime / "plain.json"
        assert copied.is_file()
        assert not copied.is_symlink()
        assert copied.read_text() == "plain"
        assert stat.S_IMODE(os.stat(copied).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(runtime).st_mode) == 0o700

    def test_writing_the_runtime_copy_cannot_mutate_source_or_target(self, tmp_path):
        """The exact reproduction: a shared target must stay unchanged."""

        source = tmp_path / "src"
        creds = source / "credentials"
        creds.mkdir(parents=True)
        shared = tmp_path / "shared-token.json"
        shared.write_text("shared-original")
        link = creds / "token.json"
        link.symlink_to(shared)
        (creds / "plain.json").write_text("plain-original")

        result = _build(source, tmp_path / "temp")
        runtime = result.home / "credentials"

        # Simulate Kimi writing credentials during a real run: overwrite the
        # runtime plain copy and re-create the linked name as a regular file.
        (runtime / "plain.json").write_text("plain-tampered")
        (runtime / "token.json").write_text("tampered")

        assert shared.read_text() == "shared-original"
        assert link.is_symlink()
        assert os.readlink(link) == str(shared)
        assert (creds / "plain.json").read_text() == "plain-original"

    def test_source_credential_tree_is_not_mutated_by_the_build(self, tmp_path):
        source = tmp_path / "src"
        creds = source / "credentials"
        creds.mkdir(parents=True)
        (creds / "real.json").write_text("real")
        (creds / "alias.json").symlink_to("real.json")
        external = tmp_path / "external"
        external.mkdir()
        (external / "t").write_text("t")
        (creds / "dir-link").symlink_to(external, target_is_directory=True)

        def snapshot(root: Path) -> Dict[str, Any]:
            out: Dict[str, Any] = {}
            for path in sorted(root.rglob("*")):
                rel = str(path.relative_to(root))
                if path.is_symlink():
                    out[rel] = ("link", os.readlink(path))
                elif path.is_dir():
                    out[rel] = ("dir", stat.S_IMODE(os.stat(path).st_mode))
                else:
                    out[rel] = ("file", path.read_bytes())
            return out

        before = snapshot(source)
        external_before = snapshot(external)

        _build(source, tmp_path / "temp")

        assert snapshot(source) == before
        assert snapshot(external) == external_before


class _CountingScandir:
    """Wrap a real ``os.scandir`` result and count pulled entries."""

    def __init__(self, inner, counter: Dict[str, int]) -> None:
        self._inner = inner
        self._iter = iter(inner)
        self._counter = counter

    def __iter__(self) -> "_CountingScandir":
        return self

    def __next__(self):
        entry = next(self._iter)
        self._counter["pulled"] += 1
        return entry

    def __enter__(self) -> "_CountingScandir":
        return self

    def __exit__(self, *exc: object) -> bool:
        self.close()
        return False

    def close(self) -> None:
        self._inner.close()


class TestTrustTraversalBudget:
    """Finding 2 — the budget must bound enumeration, not just processing."""

    def test_scandir_iterator_is_not_drained_past_the_budget(self, tmp_path, monkeypatch):
        """The exact reproduction: 10,000 entries, budget 4,096.

        Pre-fix the record count was bounded but ``sorted(scan)`` drained the
        whole directory, consuming all 10,000 iterator entries. The iterator
        itself must stop at the budget.
        """

        source = tmp_path / "src"
        source.mkdir()
        trust = source / TRUST_DIR_NAME
        trust.mkdir()
        total = 10_000
        for i in range(total):
            (trust / f"wd_{i:05d}").write_text("{}")

        counter = {"pulled": 0}
        real_scandir = os.scandir

        def counting_scandir(path, *args, **kwargs):
            return _CountingScandir(real_scandir(path, *args, **kwargs), counter)

        monkeypatch.setattr(mod.os, "scandir", counting_scandir)

        result = _build(source, tmp_path / "temp")

        assert result.trust_truncated is True
        assert len(result.trust_records) == MAX_TRUST_ENTRIES
        # The bound is the budget plus a single look-ahead entry used only to
        # detect that more work remains; it is emphatically not `total`.
        assert counter["pulled"] <= MAX_TRUST_ENTRIES + 1, counter["pulled"]

    def test_many_entries_are_not_fully_enumerated(self, tmp_path, monkeypatch):
        source = tmp_path / "src"
        source.mkdir()
        trust = source / TRUST_DIR_NAME
        trust.mkdir()
        total = MAX_TRUST_ENTRIES * 3 + 7
        for i in range(total):
            (trust / f"d{i:05d}").mkdir()

        counter: Dict[str, int] = {"pulled": 0}
        real_scandir = os.scandir

        def counting_scandir(path, *args, **kwargs):
            return _CountingScandir(real_scandir(path, *args, **kwargs), counter)

        monkeypatch.setattr(mod.os, "scandir", counting_scandir)

        result = _build(source, tmp_path / "temp")

        assert result.trust_truncated is True
        assert counter["pulled"] <= MAX_TRUST_ENTRIES + 1, counter["pulled"]

    def test_exact_budget_is_not_reported_as_truncated(self, tmp_path, monkeypatch):
        """Boundary preserved: exactly MAX entries is complete, not truncated."""

        source = tmp_path / "src"
        source.mkdir()
        trust = source / TRUST_DIR_NAME
        trust.mkdir()
        for i in range(MAX_TRUST_ENTRIES):
            (trust / f"f{i:05d}").write_text("{}")

        counter: Dict[str, int] = {"pulled": 0}
        real_scandir = os.scandir

        def counting_scandir(path, *args, **kwargs):
            return _CountingScandir(real_scandir(path, *args, **kwargs), counter)

        monkeypatch.setattr(mod.os, "scandir", counting_scandir)

        result = _build(source, tmp_path / "temp")

        assert result.trust_truncated is False
        assert len(result.trust_records) == MAX_TRUST_ENTRIES
        assert counter["pulled"] == MAX_TRUST_ENTRIES


class TestPreservedTreeSymlinkPolicy:
    """Finding 3 (P2) — a preserved tree keeps a user's links *and* their meaning.

    ``skills/`` and ``plugins/`` are ordinary preserved trees, so their symlinks
    are user semantics and are reproduced verbatim. That is correct only while the
    link still names the same target after the tree moves: the runtime home is a
    different directory, so a *relative* link that left the source home left the
    runtime home too and dangled — reproduced, a linked shared skills directory
    existed in the real home and did not exist for the worker. Absolute links are
    stable, and relative links inside the tree keep their text because the
    preserved structure resolves them the same way.
    """

    def test_external_relative_link_keeps_its_target(self, tmp_path):
        """The linked-in skill is readable from the runtime home."""

        source = tmp_path / "src"
        skills = source / "skills"
        skills.mkdir(parents=True)
        shared = tmp_path / "shared-skills"
        shared.mkdir()
        (shared / "SKILL.md").write_text("shared skill")
        # A link that leaves the source home, exactly as an operator would make it.
        (skills / "shared").symlink_to(os.path.relpath(shared, skills))

        result = _build(source, tmp_path / "temp")

        link = result.home / "skills" / "shared"
        assert link.is_symlink()
        assert link.exists(), "the linked skill must still resolve in the runtime home"
        assert (link / "SKILL.md").read_text() == "shared skill"

    def test_internal_relative_link_is_preserved_verbatim(self, tmp_path):
        """A link inside the tree keeps its relative text and still resolves."""

        source = tmp_path / "src"
        skills = source / "skills"
        skills.mkdir(parents=True)
        (skills / "SKILL.md").write_text("internal skill")
        (skills / "alias").symlink_to("SKILL.md")

        result = _build(source, tmp_path / "temp")

        link = result.home / "skills" / "alias"
        assert os.readlink(link) == "SKILL.md"
        assert link.read_text() == "internal skill"

    def test_absolute_link_keeps_its_target(self, tmp_path):
        source = tmp_path / "src"
        skills = source / "skills"
        skills.mkdir(parents=True)
        shared = tmp_path / "shared-skills"
        shared.mkdir()
        (shared / "SKILL.md").write_text("absolute skill")
        (skills / "shared").symlink_to(shared)

        result = _build(source, tmp_path / "temp")

        link = result.home / "skills" / "shared"
        assert os.readlink(link) == str(shared)
        assert link.exists()

    def test_a_link_that_already_dangled_does_not_abort_the_build(self, tmp_path):
        """Rebasing must not turn a source-side dangling link into a failure."""

        source = tmp_path / "src"
        skills = source / "skills"
        skills.mkdir(parents=True)
        (skills / "gone").symlink_to("nested/missing")

        result = _build(source, tmp_path / "temp")

        link = result.home / "skills" / "gone"
        assert result.home.is_dir()
        assert link.is_symlink()
        assert not link.exists()
