"""Store tests — atomic publish, install-record round-trip, PLUGIN_DATA survival."""

from __future__ import annotations

import errno
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cli_agent_orchestrator.agent_plugins.models import (
    Finding,
    PluginRecord,
    PluginSource,
    Severity,
)
from cli_agent_orchestrator.agent_plugins.store import InstalledPluginStore, PluginStoreError

from .conftest import PLUGIN_SCHEMA_ID, build_plugin


def make_record(name: str = "demo", **overrides) -> PluginRecord:
    defaults = dict(
        name=name,
        version="1.2.3",
        source=PluginSource(kind="path", location="/somewhere"),
        resolved_ref=None,
        installed_at=datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc),
        schema_id="https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
        skill_names=("alpha", "beta"),
        projected_skill_names=("alpha",),
        findings=(
            Finding(
                severity=Severity.WARNING,
                code="manifest.unknown_field",
                spec_ref="§5.2",
                message="ignored",
                path="plugin.json",
            ),
        ),
    )
    defaults.update(overrides)
    return PluginRecord(**defaults)  # type: ignore[arg-type]


class TestRecordRoundTrip:
    def test_write_then_read_preserves_every_field(self, store):
        record = make_record()
        store.write_record(record)

        loaded = store.get("demo")
        assert loaded == record

    def test_list_installed_is_sorted_by_name(self, store):
        for name in ("zulu", "alpha", "mike"):
            store.write_record(make_record(name))
        assert [r.name for r in store.list_installed()] == ["alpha", "mike", "zulu"]

    def test_unknown_plugin_reads_as_none(self, store):
        assert store.get("nope") is None

    def test_corrupt_record_is_skipped_not_raised(self, store):
        store.write_record(make_record("good"))
        (store.state_dir / "broken.json").write_text("{ not json", encoding="utf-8")

        # Listing feeds `cao plugin list`, the panel, and every rebuild. One
        # corrupt record must not take all of them down.
        assert [r.name for r in store.list_installed()] == ["good"]
        assert store.get("broken") is None

    def test_state_dir_is_never_mistaken_for_a_plugin(self, store, tmp_path):
        source = build_plugin(tmp_path / "src", "demo")
        store.publish(source, make_record("demo"))

        assert store.state_dir.name.startswith(".")
        assert [r.name for r in store.list_installed()] == ["demo"]


class TestPublish:
    def test_publish_copies_the_tree_and_writes_the_record(self, store, tmp_path):
        source = build_plugin(tmp_path / "src", "demo", skills=["alpha"])
        store.publish(source, make_record("demo"))

        root = store.plugin_root("demo")
        assert (root / "plugin.json").is_file()
        assert (root / "skills" / "alpha" / "SKILL.md").is_file()
        assert store.get("demo") is not None

    def test_publish_refuses_an_existing_name_without_force(self, store, tmp_path):
        source = build_plugin(tmp_path / "src", "demo")
        store.publish(source, make_record("demo"))

        with pytest.raises(PluginStoreError, match="already installed"):
            store.publish(source, make_record("demo"))

    def test_force_replaces_the_previous_bytes(self, store, tmp_path):
        first = build_plugin(tmp_path / "v1", "demo", skills=["alpha"])
        store.publish(first, make_record("demo"))

        second = build_plugin(tmp_path / "v2", "demo", skills=["beta"])
        store.publish(second, make_record("demo"), force=True)

        root = store.plugin_root("demo")
        assert (root / "skills" / "beta").is_dir()
        assert not (root / "skills" / "alpha").exists()

    def test_publish_preserves_symlinks_rather_than_following_them(self, store, tmp_path):
        """Following links during the copy would launder an escape into content.

        A link pointing outside the source tree would be materialized as real
        bytes *inside* the published root, where containment would then accept
        it — because by then it genuinely is inside.
        """
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret", encoding="utf-8")

        source = build_plugin(tmp_path / "src", "demo")
        (source / "escape").symlink_to(outside, target_is_directory=True)

        store.publish(source, make_record("demo"))
        assert (store.plugin_root("demo") / "escape").is_symlink()

    def test_staging_directories_are_dot_prefixed_and_cleaned_up(self, store, tmp_path):
        source = build_plugin(tmp_path / "src", "demo")
        store.publish(source, make_record("demo"))

        leftovers = [p.name for p in store.plugins_dir.iterdir() if p.name.startswith(".demo.")]
        assert leftovers == []


class TestUnpublish:
    def test_unpublish_removes_root_and_record(self, store, tmp_path):
        store.publish(build_plugin(tmp_path / "src", "demo"), make_record("demo"))

        assert store.unpublish("demo") is True
        assert not store.plugin_root("demo").exists()
        assert store.get("demo") is None

    def test_unpublish_retains_plugin_data_by_default(self, store, tmp_path):
        store.publish(build_plugin(tmp_path / "src", "demo"), make_record("demo"))
        data = store.plugin_data_dir("demo", create=True)
        (data / "state.db").write_text("persisted", encoding="utf-8")

        store.unpublish("demo")

        assert (data / "state.db").read_text(encoding="utf-8") == "persisted"

    def test_purge_data_deletes_plugin_data(self, store, tmp_path):
        store.publish(build_plugin(tmp_path / "src", "demo"), make_record("demo"))
        data = store.plugin_data_dir("demo", create=True)
        (data / "state.db").write_text("persisted", encoding="utf-8")

        store.unpublish("demo", purge_data=True)

        assert not data.exists()

    def test_unpublish_of_an_absent_plugin_reports_nothing_removed(self, store):
        assert store.unpublish("never-installed") is False


class TestPluginDataSurvivesUpdates:
    def test_data_survives_a_publish_over_an_existing_name(self, store, tmp_path):
        """§9.1: PLUGIN_DATA contents must be preserved across a plugin update.

        This is why AGENT_PLUGIN_DATA_DIR lives *outside* AGENT_PLUGINS_DIR — an
        update replaces the plugin root wholesale.
        """
        store.publish(build_plugin(tmp_path / "v1", "demo"), make_record("demo"))
        data = store.plugin_data_dir("demo", create=True)
        (data / "keepme").write_text("v1 state", encoding="utf-8")

        store.publish(build_plugin(tmp_path / "v2", "demo"), make_record("demo"), force=True)

        assert (data / "keepme").read_text(encoding="utf-8") == "v1 state"

    def test_data_dir_is_outside_the_plugins_dir(self, store):
        data = str(store.plugin_data_dir("demo"))
        plugins = str(store.plugins_dir)
        assert not data.startswith(plugins + os.sep)


class TestPermissions:
    @pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
    def test_store_directories_are_owner_only(self, store, tmp_path):
        store.publish(build_plugin(tmp_path / "src", "demo"), make_record("demo"))
        store.plugin_data_dir("demo", create=True)

        for path in (store.plugins_dir, store.data_dir, store.state_dir):
            mode = stat.S_IMODE(path.stat().st_mode)
            assert mode == 0o700, f"{path} is {oct(mode)}"


class TestNameGuard:
    """The store never trusts a name to be safe just because a caller passed it."""

    @pytest.mark.parametrize("bad", ["../escape", "a/b", "..", ".hidden", "", "nul\x00byte"])
    def test_traversal_names_are_rejected(self, store, bad):
        with pytest.raises(ValueError):
            store.plugin_root(bad)

    def test_get_returns_none_rather_than_raising_for_a_bad_name(self, store):
        assert store.get("../escape") is None


class TestAFailedReplaceNeverDestroysAWorkingPlugin:
    """WP4.4 — the double-rename failure story, ported from impl.

    ``publish(force=True)`` cannot be atomic end to end: POSIX will not rename a
    directory over a non-empty one, so the old root is moved aside, the new one is
    renamed in, and the old one is then deleted. Two renames means two failure
    points, and the second one is where a working plugin can be lost.

    These are injected-failure tests because the window cannot be reached any other
    way — every one of these renames succeeds on a healthy filesystem, and the
    scenario that matters (a full disk, a revoked permission, an unmounted volume
    mid-operation) is precisely the one no functional test produces.
    """

    @staticmethod
    def _record(name: str = "demo") -> PluginRecord:
        return PluginRecord(
            name=name,
            version="1.0.0",
            source=PluginSource(kind="path", location="/src"),
            resolved_ref=None,
            installed_at=datetime.now(timezone.utc),
            schema_id=PLUGIN_SCHEMA_ID,
            skill_names=(),
        )

    @pytest.fixture
    def installed(self, tmp_path):
        """A store with ``demo`` published and a marker file inside it."""
        store = InstalledPluginStore(tmp_path / "plugins", tmp_path / "data")
        source = build_plugin(tmp_path / "src", "demo", skills=["alpha"])
        store.publish(source, self._record())
        marker = store.plugin_root("demo") / "MARKER.txt"
        marker.write_text("the operator's working bytes", encoding="utf-8")
        return {"store": store, "tmp_path": tmp_path, "marker": marker}

    @staticmethod
    def _fail_renames_after(count: int, monkeypatch):
        """Let the first ``count`` renames succeed, then fail every one after."""
        real = Path.rename
        seen = {"n": 0}

        def flaky(self, target):
            seen["n"] += 1
            if seen["n"] <= count:
                return real(self, target)
            raise OSError(errno.EIO, "simulated I/O error")

        monkeypatch.setattr(Path, "rename", flaky)
        return seen

    def test_a_failed_swap_restores_the_previous_tree(self, installed, monkeypatch):
        """Aside-move succeeds, swap fails, restore succeeds — the ordinary case."""
        store = installed["store"]
        replacement = build_plugin(installed["tmp_path"] / "src2", "demo", skills=["beta"])

        # 1 = the aside-move, 2 = the swap (fails), 3 = the restore.
        real = Path.rename
        seen = {"n": 0}

        def flaky(self, target):
            seen["n"] += 1
            if seen["n"] == 2:
                raise OSError(errno.EIO, "simulated I/O error")
            return real(self, target)

        monkeypatch.setattr(Path, "rename", flaky)

        with pytest.raises(OSError):
            store.publish(replacement, self._record(), force=True)

        monkeypatch.undo()
        assert installed["marker"].is_file()
        assert installed["marker"].read_text(encoding="utf-8") == "the operator's working bytes"

    def test_a_failed_restore_leaves_the_bytes_recoverable(self, installed, monkeypatch):
        """The data-loss case: swap fails **and** the restore fails too.

        Before this fix the ``finally`` clause swept the aside-moved directory on
        the way out, so ``destination`` did not exist, the backup was deleted, and
        the plugin was gone with no copy anywhere. The bytes must survive.
        """
        store = installed["store"]
        replacement = build_plugin(installed["tmp_path"] / "src2", "demo", skills=["beta"])

        self._fail_renames_after(1, monkeypatch)  # aside-move only

        with pytest.raises(PluginStoreError) as excinfo:
            store.publish(replacement, self._record(), force=True)

        monkeypatch.undo()
        preserved = [
            path
            for path in (installed["tmp_path"] / "plugins").iterdir()
            if "replaced" in path.name
        ]
        assert preserved, "the previous version was deleted with no copy remaining"
        assert (preserved[0] / "MARKER.txt").read_text(encoding="utf-8") == (
            "the operator's working bytes"
        )
        assert "Nothing was deleted" in str(excinfo.value)

    def test_the_error_names_both_paths_so_recovery_is_possible(self, installed, monkeypatch):
        """Manual recovery is one ``mv``; an operator not told where cannot do it."""
        store = installed["store"]
        replacement = build_plugin(installed["tmp_path"] / "src2", "demo", skills=["beta"])

        self._fail_renames_after(1, monkeypatch)

        with pytest.raises(PluginStoreError) as excinfo:
            store.publish(replacement, self._record(), force=True)

        monkeypatch.undo()
        message = str(excinfo.value)
        preserved = next(
            path
            for path in (installed["tmp_path"] / "plugins").iterdir()
            if "replaced" in path.name
        )
        assert str(preserved) in message
        assert str(store.plugin_root("demo")) in message

    def test_a_successful_replace_still_deletes_the_old_tree(self, installed):
        """The cleanup must keep working — the fix must not leak a backup per install.

        The failure this excludes is a "safe" implementation that never deletes:
        every forced reinstall would leave another full copy of the plugin behind
        and the store would grow without bound.
        """
        store = installed["store"]
        replacement = build_plugin(installed["tmp_path"] / "src2", "demo", skills=["beta"])

        store.publish(replacement, self._record(), force=True)

        leftovers = [
            path.name
            for path in (installed["tmp_path"] / "plugins").iterdir()
            if "replaced" in path.name
        ]
        assert leftovers == []
        assert (store.plugin_root("demo") / "skills" / "beta").is_dir()

    def test_a_failed_aside_move_leaves_everything_untouched(self, installed, monkeypatch):
        """Failing before the window opens is the easy case; assert it anyway.

        Nothing has moved yet, so there is nothing to restore — and the operator's
        plugin must be exactly as it was, not merely recoverable.
        """
        store = installed["store"]
        replacement = build_plugin(installed["tmp_path"] / "src2", "demo", skills=["beta"])

        self._fail_renames_after(0, monkeypatch)  # even the aside-move fails

        with pytest.raises(OSError):
            store.publish(replacement, self._record(), force=True)

        monkeypatch.undo()
        assert installed["marker"].is_file()
        assert (store.plugin_root("demo") / "skills" / "alpha").is_dir()
