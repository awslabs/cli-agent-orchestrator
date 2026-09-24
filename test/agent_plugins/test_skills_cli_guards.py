"""``cao skills`` against the agent-plugin lifecycle and projection (R6, R7).

Two defects this PR introduced, both in ``cli/commands/skills.py``:

**R6 (G4).** ``_lifecycle_guard`` took the lock with ``store.lifecycle_lock`` and
caught ``installer.PluginBusyError``. The store raises its OWN busy error, and the
two classes are *siblings* — the installer's subclasses ``PluginInstallError``, the
store's subclasses ``RuntimeError`` — so the ``except`` never matched and the
operator never saw the "nothing was changed, retry" message the handler exists to
produce. The same round also left the ``copytree`` outside the guard, so the bytes
landed on disk while no lock was held.

**R7 (G3).** ``remove()`` asked ``exists()`` then ``is_dir()`` — both follow
symlinks — and then called ``shutil.rmtree``, which refuses a symbolic link. A
symlink-mode projection therefore crashed the command with ``OSError``.

Both suites drive the real CLI and the real ``flock``; nothing here asserts on a
mocked lock. The one thing that is monkeypatched is the guard's *timeout*, so a
contended acquisition fails in milliseconds instead of the production 60 seconds.
"""

from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Callable, Iterator

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.agent_plugins import installer as installer_module
from cli_agent_orchestrator.agent_plugins.installer import install
from cli_agent_orchestrator.agent_plugins.models import PluginSource
from cli_agent_orchestrator.agent_plugins.projection import (
    MARKER_FILENAME,
    rebuild_projection,
)
from cli_agent_orchestrator.agent_plugins.store import InstalledPluginStore
from cli_agent_orchestrator.cli.commands import skills as skills_module
from cli_agent_orchestrator.cli.main import cli

from .conftest import build_plugin

USER_MARKER = "# the user's own version"


def _write_user_skill(folder: Path, name: str) -> Path:
    """A minimal valid skill folder whose frontmatter name matches ``name``."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: A user-authored skill.\n---\n\n{USER_MARKER}\n",
        encoding="utf-8",
    )
    return folder


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A tmp-backed skill store, plugin store, and silenced agent refresh.

    ``SKILLS_DIR`` is patched on all three modules that read it, and the default
    ``InstalledPluginStore()`` the CLI builds for itself is already redirected by
    this package's autouse ``_never_touch_the_real_plugin_store`` fixture — that
    default store is precisely what ``_lifecycle_guard`` constructs, so the lock
    under test is the one in the scratch tree.
    """
    plugins_dir = tmp_path / "agent-plugins"
    data_dir = tmp_path / "agent-plugin-data"
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    monkeypatch.setattr("cli_agent_orchestrator.agent_plugins.store.AGENT_PLUGINS_DIR", plugins_dir)
    monkeypatch.setattr(
        "cli_agent_orchestrator.agent_plugins.store.AGENT_PLUGIN_DATA_DIR", data_dir
    )
    monkeypatch.setattr("cli_agent_orchestrator.agent_plugins.projection.SKILLS_DIR", skills_dir)
    monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", skills_dir)
    monkeypatch.setattr("cli_agent_orchestrator.cli.commands.skills.SKILLS_DIR", skills_dir)
    monkeypatch.setattr(
        "cli_agent_orchestrator.agent_plugins.installer._refresh_agent_artifacts", lambda: None
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.cli.commands.skills._refresh_installed_agents", lambda: None
    )

    return {
        "store": InstalledPluginStore(plugins_dir, data_dir),
        "skills_dir": skills_dir,
        "tmp_path": tmp_path,
    }


@pytest.fixture
def impatient_lock(monkeypatch):
    """Make every lifecycle acquisition give up after 200ms, keeping real ``flock``.

    The guard hardcodes a 60-second production timeout, which is right for an
    operator waiting behind a real install and useless in a test that deliberately
    contends the lock. Wrapping the store method rather than replacing the lock
    keeps the genuine ``flock`` contention, the genuine store-side
    ``PluginBusyError``, and therefore the genuine sibling-class translation this
    file is about — only the deadline changes.
    """
    real = InstalledPluginStore.lifecycle_lock

    def impatient(self, timeout: float = 60.0, *, blocking: bool = True):
        return real(self, min(timeout, 0.2), blocking=blocking)

    monkeypatch.setattr(InstalledPluginStore, "lifecycle_lock", impatient)


@pytest.fixture
def lock_holder() -> Iterator[Callable[[], None]]:
    """Hold the DEFAULT store's lifecycle lock on a background thread.

    Yields a callable that takes the lock and returns once it is provably held, so
    no test has to guess at a sleep. The lock is released on teardown even if the
    body fails.
    """
    acquired = threading.Event()
    release = threading.Event()
    threads: list[threading.Thread] = []

    def hold() -> None:
        def body() -> None:
            # No arguments, deliberately: the same default store `_lifecycle_guard`
            # builds, so this contends the exact lock file the CLI will reach for.
            with InstalledPluginStore().lifecycle_lock(5.0):
                acquired.set()
                release.wait(timeout=10.0)

        thread = threading.Thread(target=body, daemon=True)
        thread.start()
        threads.append(thread)
        assert acquired.wait(timeout=5.0), "the holder thread never took the lock"

    try:
        yield hold
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=5.0)


def _install_donor(
    world, skill_name: str, *, name: str = "donor", src: str = "plugin-src"
) -> object:
    """Install a one-skill plugin so ``skill_name`` becomes a projection."""
    source = build_plugin(
        world["tmp_path"] / src, name, skills=[skill_name], version="1.0.0", with_mcp=False
    )
    return install(
        PluginSource(kind="path", location=str(source)),
        store=world["store"],
        skills_dir=world["skills_dir"],
        force=True,
        refresh_agents=False,
    )


class TestABusyStoreIsReportedNotLeaked:
    """R6 — the handler that could never fire.

    The completion predicate from requirements R6: a test holding the lifecycle
    lock and running ``cao skills add --force`` sees *"Refusing to replace the
    skill … Nothing was changed"*.
    """

    def test_add_force_on_a_busy_store_says_nothing_was_changed(
        self, world, lock_holder, impatient_lock
    ):
        """The operator-facing message, end to end through the real CLI.

        Before the fix this reported the store's bare "another agent-plugin
        operation is in progress" through the command's catch-all, with no
        indication that the skill was left untouched — which is the one fact an
        operator needs before deciding whether to retry.
        """
        installed = _write_user_skill(world["skills_dir"] / "shared-skill", "shared-skill")
        before = (installed / "SKILL.md").read_bytes()
        user_src = _write_user_skill(world["tmp_path"] / "mine" / "shared-skill", "shared-skill")

        lock_holder()
        result = CliRunner().invoke(cli, ["skills", "add", str(user_src), "--force"])

        assert result.exit_code != 0
        assert "Refusing to replace the skill" in result.output
        assert "Nothing was changed" in result.output
        # The claim the message makes must actually be true.
        assert (installed / "SKILL.md").read_bytes() == before

    def test_an_idle_store_still_installs(self, world, impatient_lock):
        """The guard must not break the path it wraps."""
        _write_user_skill(world["skills_dir"] / "shared-skill", "shared-skill")
        user_src = _write_user_skill(world["tmp_path"] / "mine" / "shared-skill", "shared-skill")
        (user_src / "extra.txt").write_text("new bytes", encoding="utf-8")

        result = CliRunner().invoke(cli, ["skills", "add", str(user_src), "--force"])

        assert result.exit_code == 0, result.output
        assert (world["skills_dir"] / "shared-skill" / "extra.txt").is_file()


class TestTheLifecycleGuardItself:
    """R6.1 — the first direct test of ``_lifecycle_guard``; there was none.

    Asserted on the exception *chain* rather than only the message, because the
    message alone passes just as happily with the broken sibling-class ``except``
    replaced by a catch-all ``RuntimeError`` — which would swallow unrelated
    failures. What must be true is that the busy condition arrives as the
    installer's class.
    """

    def test_a_busy_store_is_translated_to_the_installers_busy_error(
        self, world, lock_holder, impatient_lock
    ):
        lock_holder()

        with pytest.raises(RuntimeError) as caught:
            with skills_module._lifecycle_guard():  # pragma: no branch
                pytest.fail("the guard yielded while another holder had the lock")

        assert "Refusing to replace the skill" in str(caught.value)
        assert "Nothing was changed" in str(caught.value)
        cause = caught.value.__cause__
        assert isinstance(cause, installer_module.PluginBusyError), (
            "the guard must route through installer._lifecycle: the store's busy error "
            f"is a sibling of the installer's, not a subclass. Got {cause!r}"
        )
        assert isinstance(cause, installer_module.PluginInstallError)

    def test_an_idle_store_yields(self, world, impatient_lock):
        entered = False
        with skills_module._lifecycle_guard():
            entered = True
        assert entered

    def test_the_copy_runs_inside_the_guard(self, world, impatient_lock, monkeypatch):
        """R6.3 — the round-5 residual: ``copytree`` executed outside the lock.

        Detected by trying to take the lifecycle lock from inside ``copytree``.
        ``flock`` is held per open file description, not per process, so a second
        acquisition in this same process conflicts exactly as another process would
        — no second process needed to prove the lock is held.
        """
        _write_user_skill(world["skills_dir"] / "shared-skill", "shared-skill")
        user_src = _write_user_skill(world["tmp_path"] / "mine" / "shared-skill", "shared-skill")

        observed: dict[str, bool] = {}
        real_copytree = shutil.copytree

        def probing_copytree(src, dst, *args, **kwargs):
            try:
                with InstalledPluginStore().lifecycle_lock(0.05):
                    observed["locked"] = False
            except Exception:
                observed["locked"] = True
            return real_copytree(src, dst, *args, **kwargs)

        monkeypatch.setattr(skills_module.shutil, "copytree", probing_copytree)

        result = CliRunner().invoke(cli, ["skills", "add", str(user_src), "--force"])

        assert result.exit_code == 0, result.output
        assert observed.get("locked") is True, (
            "the copy ran with the lifecycle lock free, so a concurrent plugin "
            "operation can interleave with the bytes landing on disk"
        )


class TestRemovingAProjectedSkill:
    """R7 — ``remove()`` crashed on a symlink and silently lost to the next rebuild.

    Semantics implemented: a plugin-owned projection is **refused** with a pointer
    to ``cao plugin remove`` (design.md §3.2). Unlinking it would let
    ``cao skills remove`` delete plugin-owned content, which is the category
    confusion the projection-ownership work exists to prevent — and in copy mode it
    would be undone by the next ``rebuild_projection`` anyway.
    """

    def test_a_symlink_projection_is_refused_not_rmtreed(self, world):
        """The reported crash: ``rmtree`` refuses a symbolic link.

        ``shutil`` raises ``OSError('Cannot call rmtree on a symbolic link')`` and
        then re-raises it through its ``onexc`` hook with ``filename`` attached, so
        ``str(exc)`` — which is all the command surfaces — collapses to
        ``[Errno None] None: PosixPath(...)``. The explanation is lost; asserting
        that rendering is gone is what makes this a regression guard rather than a
        vacuous absence check.
        """
        _install_donor(world, "shared-skill")
        projected = world["skills_dir"] / "shared-skill"
        if not projected.is_symlink():
            pytest.skip("symlink projection unavailable in this environment")

        result = CliRunner().invoke(cli, ["skills", "remove", "shared-skill"])

        assert "[Errno None]" not in result.output
        assert "Cannot call rmtree on a symbolic link" not in result.output
        assert result.exit_code != 0
        assert "cao plugin remove" in result.output
        assert "donor" in result.output
        assert projected.is_symlink(), "the projection was removed despite the refusal"

    def test_a_copy_mode_projection_is_refused_so_no_rebuild_can_undo_it(self, world, monkeypatch):
        """R7.2 in copy mode.

        ``rmtree`` succeeds on a copy-mode projection, so the old code reported
        success and the next ``rebuild_projection`` put the directory straight back
        — a removal silently undone. Refusing makes the resurrection unreachable
        because nothing is ever removed, and it keeps the claim intact rather than
        leaving the records describing a projection that is not on disk.
        """
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_skill_projection_mode",
            lambda: "copy",
        )
        _install_donor(world, "shared-skill")
        projected = world["skills_dir"] / "shared-skill"
        assert projected.is_dir() and not projected.is_symlink()
        assert (projected / MARKER_FILENAME).is_file()

        result = CliRunner().invoke(cli, ["skills", "remove", "shared-skill"])

        assert result.exit_code != 0
        assert "cao plugin remove" in result.output
        assert projected.is_dir(), "a copy-mode projection was deleted"
        assert world["store"].get("donor").projected_skill_names == ("shared-skill",)

        rebuilt = rebuild_projection(world["store"], skills_dir=world["skills_dir"], mode="copy")
        assert "shared-skill" in rebuilt.projected
        assert (projected / MARKER_FILENAME).is_file()

    def test_a_user_owned_skill_is_still_removed_and_stays_removed(self, world):
        """The contrast case: nothing claims this name, so removal is the user's call.

        Also the honest form of "a rebuild does not resurrect a removed skill" —
        with plugin-owned projections refused, this is the only skill a removal can
        actually delete, and the rebuild must leave it deleted.
        """
        _write_user_skill(world["skills_dir"] / "mine-alone", "mine-alone")

        result = CliRunner().invoke(cli, ["skills", "remove", "mine-alone"])

        assert result.exit_code == 0, result.output
        assert "removed successfully" in result.output
        assert not (world["skills_dir"] / "mine-alone").exists()

        rebuild_projection(world["store"], skills_dir=world["skills_dir"])
        assert not (world["skills_dir"] / "mine-alone").exists()

    def test_a_dangling_symlink_no_plugin_claims_is_unlinked(self, world):
        """No claim, so no plugin to point the operator at — but still never ``rmtree``.

        This is the case ``sweep_dangling_projections`` cleans up in the background;
        an operator asking for it explicitly must not get a traceback.
        """
        target = world["tmp_path"] / "gone" / "orphan-skill"
        _write_user_skill(target, "orphan-skill")
        link: Path = world["skills_dir"] / "orphan-skill"
        link.symlink_to(target)
        shutil.rmtree(world["tmp_path"] / "gone")

        result = CliRunner().invoke(cli, ["skills", "remove", "orphan-skill"])

        assert result.exit_code == 0, result.output
        assert not link.is_symlink()

    def test_a_user_directory_under_a_stale_claim_is_still_removable(self, world):
        """Refusal is gated on structural proof, not on the record's name claim.

        The poisoned state ``test_claim_transfer`` describes: the record still
        claims the name while what is on disk is the user's own directory. A refusal
        driven by the claim alone would strand the user with a skill they cannot
        remove — so ownership here means "a symlink into the plugin store, or a copy
        whose marker still verifies", the same predicate the sweep trusts.
        """
        _install_donor(world, "shared-skill")
        projected = world["skills_dir"] / "shared-skill"
        if projected.is_symlink():
            projected.unlink()
        else:
            shutil.rmtree(projected)
        _write_user_skill(projected, "shared-skill")

        result = CliRunner().invoke(cli, ["skills", "remove", "shared-skill"])

        assert result.exit_code == 0, result.output
        assert not projected.exists()
