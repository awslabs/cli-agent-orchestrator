"""Store integrity under injected failure — the record and the bytes never disagree.

Both properties here were reproduced by review on #584 and each test is
mutation-verified: reverting the corresponding fix makes it fail.

The invariant under test is not "an exception surfaced" but the *state left
behind* after one. A store that raises and leaves a v1 record describing v2
bytes has still corrupted itself, and a ``remove`` that returns success with the
plugin root still on disk has lied to its caller — neither is observable by
asserting on the exception alone, so every test below asserts on disk contents
and on the store's own read API.
"""

from __future__ import annotations

import shutil
import threading

import pytest

from cli_agent_orchestrator.agent_plugins.store import PluginStoreError

from .conftest import build_plugin
from .test_store import make_record


class TestPublishCommitsTheRecordOrRollsBack:
    """A failed record write must leave the store exactly as it was.

    **Validates: the failed-install isolation guarantee** — the store's claim
    that "a process interruption at any point before the rename leaves the store
    byte-identical to its pre-publish state" is worth nothing if the *record*
    write is outside that protection.
    """

    def test_a_failed_force_update_restores_the_previous_bytes_and_record(
        self, store, tmp_path, monkeypatch
    ):
        # `build_plugin(skills=...)` takes skill *names*, so the two versions are
        # distinguished by a marker file written into each staged tree — content
        # that actually differs, rather than two identical default packages that
        # would make the assertion below pass no matter what publish() did.
        v1 = build_plugin(tmp_path / "v1", name="demo", skills=("alpha",))
        (v1 / "VERSION.txt").write_text("v1", encoding="utf-8")
        store.publish(v1, make_record("demo", version="1.0.0"))
        installed = store.plugin_root("demo")
        assert (installed / "VERSION.txt").read_text(encoding="utf-8") == "v1"

        v2 = build_plugin(tmp_path / "v2", name="demo", skills=("alpha",))
        (v2 / "VERSION.txt").write_text("v2", encoding="utf-8")

        monkeypatch.setattr(
            type(store),
            "write_record",
            lambda self, record: (_ for _ in ()).throw(OSError(28, "No space left on device")),
        )

        with pytest.raises(OSError):
            store.publish(v2, make_record("demo", version="2.0.0"), force=True)

        monkeypatch.undo()

        # The bytes are v1 again, not v2.
        assert (installed / "VERSION.txt").read_text(encoding="utf-8") == "v1"
        # And the record still describes v1 — never v2 bytes under a v1 record,
        # nor a v2 record over restored v1 bytes.
        assert store.get("demo").version == "1.0.0"

    def test_a_failed_first_install_leaves_no_orphan_root(self, store, tmp_path, monkeypatch):
        fresh = build_plugin(tmp_path / "fresh", name="solo", skills=("alpha",))

        monkeypatch.setattr(
            type(store),
            "write_record",
            lambda self, record: (_ for _ in ()).throw(OSError(13, "Permission denied")),
        )

        with pytest.raises(OSError):
            store.publish(fresh, make_record("solo"))

        monkeypatch.undo()

        # No untracked bytes: an orphan root would block a later non-force add
        # while `get()` reported the plugin as absent.
        assert not store.plugin_root("solo").exists()
        assert store.get("solo") is None
        assert store.is_installed("solo") is False

    def test_a_successful_publish_still_commits_the_record(self, store, tmp_path):
        """The rollback path must not have broken the happy path."""
        pkg = build_plugin(tmp_path / "ok", name="fine", skills=("alpha",))
        store.publish(pkg, make_record("fine", version="9.9.9"))

        assert store.plugin_root("fine").is_dir()
        assert store.get("fine").version == "9.9.9"

    def test_no_replaced_backup_directory_survives_a_rollback(self, store, tmp_path, monkeypatch):
        """The rollback must not leak the dot-prefixed aside-copy into the store."""
        v1 = build_plugin(tmp_path / "b1", name="demo", skills=("alpha",))
        store.publish(v1, make_record("demo"))
        v2 = build_plugin(tmp_path / "b2", name="demo", skills=("alpha",))

        monkeypatch.setattr(
            type(store),
            "write_record",
            lambda self, record: (_ for _ in ()).throw(OSError(28, "full")),
        )
        with pytest.raises(OSError):
            store.publish(v2, make_record("demo"), force=True)
        monkeypatch.undo()

        leftovers = [p.name for p in store.plugin_root("demo").parent.glob(".demo.replaced.*")]
        assert leftovers == []


class TestConcurrentPublishCannotSilentlyReplace:
    """Two installs of the same new name: one wins, the other must not overwrite it.

    Reported in review of revision 1. The pre-publish
    ``destination.exists() and not force`` guard ran once, before staging, while
    the swap re-checked existence **without** re-consulting ``force``. Both
    callers therefore passed the first guard for a name neither had installed,
    and the loser took the *replace* path: it backed up the winner's freshly
    published tree, renamed over it, and deleted the backup. Nothing raised,
    because the ``EEXIST``/``ENOTEMPTY`` concurrency guard only fires on an actual
    ``rename`` failure, which that interleaving never produces.
    """

    def test_the_loser_raises_instead_of_replacing_the_winners_bytes(self, store, tmp_path):
        first = build_plugin(tmp_path / "first", name="raced", skills=("alpha",))
        (first / "WHO.txt").write_text("first", encoding="utf-8")
        second = build_plugin(tmp_path / "second", name="raced", skills=("alpha",))
        (second / "WHO.txt").write_text("second", encoding="utf-8")

        barrier = threading.Barrier(2, timeout=10)
        outcomes: dict[str, BaseException | str] = {}

        def publish(tag: str, source):
            barrier.wait()
            try:
                store.publish(source, make_record("raced", version=tag), force=False)
                outcomes[tag] = "published"
            except BaseException as exc:  # noqa: BLE001 - recorded, asserted below
                outcomes[tag] = exc

        threads = [
            threading.Thread(target=publish, args=("first", first)),
            threading.Thread(target=publish, args=("second", second)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        published = [tag for tag, r in outcomes.items() if r == "published"]
        failed = [tag for tag, r in outcomes.items() if isinstance(r, BaseException)]

        assert len(published) == 1, f"expected exactly one winner, got {outcomes}"
        assert len(failed) == 1, f"expected the loser to raise, got {outcomes}"
        assert isinstance(outcomes[failed[0]], PluginStoreError)

        # The winner's bytes survive, and the record agrees with them — no
        # metadata/bytes mismatch.
        on_disk = (store.plugin_root("raced") / "WHO.txt").read_text(encoding="utf-8")
        assert on_disk == published[0]
        assert store.get("raced").version == published[0]

    def test_a_concurrent_force_update_still_leaves_record_and_bytes_agreeing(
        self, store, tmp_path
    ):
        """Two racing force updates may both succeed, but not disagree."""
        base = build_plugin(tmp_path / "base", name="forced", skills=("alpha",))
        (base / "WHO.txt").write_text("base", encoding="utf-8")
        store.publish(base, make_record("forced", version="base"))

        sources = {}
        for tag in ("a", "b"):
            src = build_plugin(tmp_path / tag, name="forced", skills=("alpha",))
            (src / "WHO.txt").write_text(tag, encoding="utf-8")
            sources[tag] = src

        barrier = threading.Barrier(2, timeout=10)

        def publish(tag):
            barrier.wait()
            store.publish(sources[tag], make_record("forced", version=tag), force=True)

        threads = [threading.Thread(target=publish, args=(t,)) for t in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        on_disk = (store.plugin_root("forced") / "WHO.txt").read_text(encoding="utf-8")
        assert (
            store.get("forced").version == on_disk
        ), "the install record describes a different version than the bytes on disk"


class TestUnpublishReportsFailureInsteadOfLying:
    """``unpublish`` must not return success while the root is still installed.

    **Validates: the remove idempotence property (P5)** — decidable only if a
    reported removal actually happened.
    """

    def test_an_undeletable_root_raises_and_keeps_the_record(self, store, tmp_path, monkeypatch):
        pkg = build_plugin(tmp_path / "stuck", name="stuck", skills=("alpha",))
        store.publish(pkg, make_record("stuck"))

        def busy(path, *args, **kwargs):
            raise OSError(16, "Device or resource busy")

        monkeypatch.setattr(shutil, "rmtree", busy)

        with pytest.raises(PluginStoreError, match="Could not remove"):
            store.unpublish("stuck")

        monkeypatch.undo()

        # The installation is still *tracked* — the untracked-install trap the
        # reviewer reproduced (root present, `get()` None, later add blocked).
        assert store.plugin_root("stuck").is_dir()
        assert store.get("stuck") is not None

    def test_purge_data_failure_is_reported_too(self, store, tmp_path, monkeypatch):
        pkg = build_plugin(tmp_path / "withdata", name="withdata", skills=("alpha",))
        store.publish(pkg, make_record("withdata"))
        data = store.plugin_data_dir("withdata")
        data.mkdir(parents=True, exist_ok=True)
        (data / "state.txt").write_text("keep", encoding="utf-8")

        real_rmtree = shutil.rmtree

        def busy_for_data(path, *args, **kwargs):
            if str(path) == str(data):
                raise OSError(16, "Device or resource busy")
            return real_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(shutil, "rmtree", busy_for_data)

        with pytest.raises(PluginStoreError, match="Could not remove"):
            store.unpublish("withdata", purge_data=True)

        monkeypatch.undo()
        assert (data / "state.txt").read_text(encoding="utf-8") == "keep"

    def test_a_normal_remove_still_succeeds_and_is_idempotent(self, store, tmp_path):
        pkg = build_plugin(tmp_path / "gone", name="gone", skills=("alpha",))
        store.publish(pkg, make_record("gone"))

        assert store.unpublish("gone") is True
        assert store.plugin_root("gone").exists() is False
        assert store.get("gone") is None
        # Second removal reports "nothing to do" rather than raising.
        assert store.unpublish("gone") is False
