"""Lifecycle serialization for agent-plugin install/uninstall/projection.

Reported by review 5222539218 on #584 (item 1): an uninstall could snapshot the
empty installed set, a same-name reinstall could then complete and materialize
its skill, and the stale uninstall's sweep would remove that new skill. Root and
record survive, the record claims the projected skill, and the projection is
absent -- so ownership checks cannot detect it, because ownership was never the
question. The snapshot being *current* was.

These tests make that interleaving deterministic with a ``threading.Event`` pair
rather than hoping a sleep lands in the window.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from cli_agent_orchestrator.agent_plugins import projection as projection_mod
from cli_agent_orchestrator.agent_plugins.installer import (
    PluginBusyError,
    PluginInstallError,
    install,
    uninstall,
)
from cli_agent_orchestrator.agent_plugins.models import PluginSource
from cli_agent_orchestrator.agent_plugins.store import InstalledPluginStore
from cli_agent_orchestrator.agent_plugins.store import PluginBusyError as StoreBusyError

from .conftest import build_plugin


def _source(path: Path) -> PluginSource:
    return PluginSource(kind="local", location=str(path))


def _store(tmp_path: Path) -> InstalledPluginStore:
    """A store with BOTH directories redirected.

    ``data_dir`` is passed explicitly and deliberately: omitting it defaults to
    the operator's real ``~/.aws/cli-agent-orchestrator/agent-plugin-data``, which
    is how a test in this suite reached the live home once already.
    """
    return InstalledPluginStore(plugins_dir=tmp_path / "plugins", data_dir=tmp_path / "plugin-data")


@pytest.fixture
def demo(tmp_path):
    """A one-skill plugin named ``demo`` providing skill ``shared``."""
    return build_plugin(tmp_path / "src-demo", "demo", skills=["shared"])


class TestPluginBusyError:
    """The error an operator sees when two lifecycle calls collide."""

    def test_it_is_an_install_error_so_existing_handlers_still_catch_it(self):
        """Subclassing keeps every ``except PluginInstallError`` site correct.

        The API's 409 mapping is added *before* the existing 400 branch, so the
        subclass is what lets the narrower status win without duplicating the
        handler.
        """
        assert issubclass(PluginBusyError, PluginInstallError)

    def test_its_message_tells_the_operator_what_to_do(self, tmp_path):
        exc = PluginBusyError("another agent-plugin operation is in progress; retry")
        assert "retry" in str(exc)


class TestTheLifecycleLockIsSeparateFromTheStoreLock:
    """Two locks, because they serialize different spans.

    ``_store_lock`` guards one whole-store *write* and is non-reentrant --
    ``_write_back`` takes it. The lifecycle lock has to span snapshot ->
    publish -> rebuild, which *contains* several such writes. Holding the store
    lock across that span would deadlock on its own writeback, which is why
    "just hold the existing lock", as the reviewer anticipated, is not
    sufficient.
    """

    def test_the_two_lock_files_are_distinct(self, store):
        """Asserted on the constants, so it cannot pass by coincidence.

        Two names, therefore two ``flock`` descriptors, therefore no chance of the
        lifecycle lock accidentally satisfying or blocking a store write.
        """
        from cli_agent_orchestrator.agent_plugins.store import (
            _LIFECYCLE_LOCK_FILENAME,
            _LOCK_FILENAME,
        )

        assert _LOCK_FILENAME != _LIFECYCLE_LOCK_FILENAME

        with store.lifecycle_lock(timeout=1.0):
            pass
        names = sorted(p.name for p in store.state_dir.iterdir())
        assert _LIFECYCLE_LOCK_FILENAME in names, "created on first use"

    def test_the_store_lock_is_still_takeable_while_the_lifecycle_lock_is_held(self, store):
        """The declared lock ORDER: lifecycle first, then store. Never the reverse.

        This is the property that makes the nesting safe. If a store write could
        not proceed under the lifecycle lock, every lifecycle operation would
        deadlock at its first write.
        """
        with store.lifecycle_lock(timeout=1.0):
            store.list_installed()  # takes and releases the store lock internally

    def test_a_second_holder_times_out_rather_than_blocking_forever(self, store):
        """A wedged holder must not hang the caller indefinitely."""
        acquired = threading.Event()
        release = threading.Event()
        result = {}

        def holder():
            with store.lifecycle_lock(timeout=5.0):
                acquired.set()
                release.wait(timeout=5.0)

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        assert acquired.wait(timeout=5.0)
        try:
            second = InstalledPluginStore(plugins_dir=store.plugins_dir, data_dir=store.data_dir)
            # The STORE's class here, deliberately: this calls the store API
            # directly. `installer` adapts it to its own subclass at its boundary,
            # which the next test asserts -- that is what keeps every
            # `except PluginInstallError` handler and the API's 409 correct.
            with pytest.raises(StoreBusyError):
                with second.lifecycle_lock(timeout=0.2):
                    pass
        finally:
            release.set()
            thread.join(timeout=5.0)


class TestTheBusyErrorReachesTheRightHandler:
    """The installer must not leak the store's class to its callers."""

    def test_install_on_a_busy_store_raises_the_installers_busy_error(
        self, tmp_path, demo, skills_dir
    ):
        """Which is a ``PluginInstallError``, so the CLI and API keep working.

        The store's own class is that base's *sibling*, not its subclass, so an
        unadapted raise would escape every `except PluginInstallError` site as an
        unhandled 500 instead of a 409.
        """
        store = _store(tmp_path)
        acquired = threading.Event()
        release = threading.Event()

        def holder():
            with store.lifecycle_lock(timeout=5.0):
                acquired.set()
                release.wait(timeout=5.0)

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        assert acquired.wait(timeout=5.0)
        try:
            with pytest.raises(PluginBusyError) as caught:
                install(
                    _source(demo),
                    store=_store(tmp_path),
                    skills_dir=skills_dir,
                    refresh_agents=False,
                    lock_timeout=0.2,
                )
            assert isinstance(caught.value, PluginInstallError)
        finally:
            release.set()
            thread.join(timeout=5.0)


class TestTheReportedInterleaving:
    """The reviewer's exact sequence, forced to happen."""

    def test_a_stale_uninstall_sweep_cannot_delete_a_newer_reinstalls_skill(
        self, tmp_path, demo, skills_dir, monkeypatch
    ):
        """Thread A uninstalls; thread B reinstalls inside A's snapshot window.

        Without serialization A's rebuild runs against an installed set that no
        longer describes the store, and its sweep deletes the skill B just
        projected. The assertion is on the END STATE the reviewer named: the
        record claims the skill, so the projection must exist and resolve into
        the store.
        """
        store = _store(tmp_path)
        install(_source(demo), store=store, skills_dir=skills_dir, refresh_agents=False)
        assert (skills_dir / "shared").exists()

        snapshot_taken = threading.Event()
        reinstall_done = threading.Event()
        real_list_installed = InstalledPluginStore.list_installed
        armed = {"yes": True}

        def instrumented(self):
            """Pause the FIRST in-rebuild snapshot until the reinstall lands.

            Wrapping ``list_installed`` rather than sleeping makes the window
            exact: the pause is inside ``rebuild_projection``, which is where the
            stale snapshot is read.
            """
            result = real_list_installed(self)
            if armed["yes"] and snapshot_taken.is_set() is False:
                armed["yes"] = False
                snapshot_taken.set()
                reinstall_done.wait(timeout=1.5)
            return result

        errors = {}

        def reinstaller():
            reinstall_done.clear()
            try:
                snapshot_taken.wait(timeout=5.0)
                other = _store(tmp_path)
                install(
                    _source(demo),
                    force=True,
                    store=other,
                    skills_dir=skills_dir,
                    refresh_agents=False,
                    lock_timeout=5.0,
                )
            except Exception as exc:  # pragma: no cover - surfaced by the assert
                errors["reinstall"] = exc
            finally:
                reinstall_done.set()

        thread = threading.Thread(target=reinstaller, daemon=True)
        thread.start()
        monkeypatch.setattr(InstalledPluginStore, "list_installed", instrumented)
        try:
            uninstall("demo", store=store, skills_dir=skills_dir, refresh_agents=False)
        finally:
            reinstall_done.set()
            thread.join(timeout=10.0)
        monkeypatch.undo()

        assert "reinstall" not in errors, errors.get("reinstall")

        fresh = _store(tmp_path)
        record = fresh.get("demo")
        assert record is not None, "the reinstall committed, so the record must survive"
        assert record.projected_skill_names == ("shared",)
        projected = skills_dir / "shared"
        assert projected.exists(), (
            "the record claims 'shared', so its projection must exist -- this is the "
            "exact end state review 5222539218 item 1 describes as reachable"
        )
        assert projected.resolve().is_relative_to(fresh.plugin_root("demo").resolve())


class TestTheSweepYieldsRatherThanWaits:
    """``cao plugin list`` must not block behind an install."""

    def test_the_sweep_reports_that_it_skipped_a_busy_store(self, store, skills_dir):
        """Non-blocking by design: listing is a read and must stay responsive.

        It also must not lie by returning an empty swept list that looks like
        "nothing to do", so the outcome is a structure carrying both facts.
        """
        acquired = threading.Event()
        release = threading.Event()

        def holder():
            with store.lifecycle_lock(timeout=5.0):
                acquired.set()
                release.wait(timeout=5.0)

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        assert acquired.wait(timeout=5.0)
        try:
            other = InstalledPluginStore(plugins_dir=store.plugins_dir, data_dir=store.data_dir)
            sweep = projection_mod.sweep_dangling_projections(other, skills_dir=skills_dir)
            assert sweep.skipped_busy is True
            assert sweep.swept == ()
        finally:
            release.set()
            thread.join(timeout=5.0)

    def test_an_idle_store_is_swept_normally(self, store, skills_dir):
        sweep = projection_mod.sweep_dangling_projections(store, skills_dir=skills_dir)
        assert sweep.skipped_busy is False

    def test_the_result_is_iterable_for_the_names_it_swept(self, store, skills_dir):
        """Kept ergonomic: callers that only want the names still read cleanly."""
        sweep = projection_mod.sweep_dangling_projections(store, skills_dir=skills_dir)
        assert list(sweep.swept) == []
