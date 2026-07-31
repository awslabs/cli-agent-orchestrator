"""Tests for the §5 macro store: CRUD, ordering, quarantine, atomicity.

Every test redirects ``macro_store.MACROS_PATH`` into a tmp directory; all
derived paths (lock, quarantine glob) follow from it.
"""

import json
import os
import stat

import pytest

from cli_agent_orchestrator.services import macro_store
from cli_agent_orchestrator.services.macro_store import (
    builtin_macro_id,
    builtin_macros_for_provider,
    resolve_builtin,
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "macros.json"
    monkeypatch.setattr(macro_store, "MACROS_PATH", path)
    return path


def _write_store(path, document):
    path.write_text(json.dumps(document))


def _record(record_id, name, scope=None, favorite=False, events=None):
    return {
        "id": record_id,
        "name": name,
        "description": None,
        "scope": scope or {"kind": "global"},
        "events": events or [{"type": "key", "key": "Enter"}],
        "favorite": favorite,
        "created_at": "2026-07-28T00:00:00Z",
        "updated_at": "2026-07-28T00:00:00Z",
    }


class TestCreateAndList:
    def test_create_mints_uuid_timestamps_and_persists(self, store):
        record = macro_store.create_macro(
            name="Model K2.7",
            description="pick the model",
            scope={"kind": "provider", "provider": "kimi_cli"},
            notation='"/model" enter up*3 enter',
            favorite=True,
        )
        assert record["id"]
        assert record["created_at"].endswith("Z")
        assert record["updated_at"].endswith("Z")
        on_disk = json.loads(store.read_text())
        assert on_disk["schema_version"] == 1
        assert on_disk["macros"][0]["id"] == record["id"]
        # Notation never touches disk: only the resolved events persist.
        assert "notation" not in on_disk["macros"][0]
        assert on_disk["macros"][0]["events"][-1] == {"type": "key", "key": "Enter"}

    def test_create_with_raw_events(self, store):
        record = macro_store.create_macro(
            name="Interrupt",
            scope={"kind": "global"},
            events=[{"type": "key", "key": "C-c"}],
        )
        assert record["events"] == [{"type": "key", "key": "C-c"}]
        assert record["favorite"] is False

    def test_create_requires_exactly_one_of_events_or_notation(self, store):
        with pytest.raises(macro_store.MacroValidationError):
            macro_store.create_macro(name="x", scope={"kind": "global"})
        with pytest.raises(macro_store.MacroValidationError):
            macro_store.create_macro(
                name="x",
                scope={"kind": "global"},
                events=[{"type": "key", "key": "Enter"}],
                notation="enter",
            )

    def test_create_notation_error_carries_offset(self, store):
        with pytest.raises(macro_store.MacroValidationError) as excinfo:
            macro_store.create_macro(name="x", scope={"kind": "global"}, notation="up*0")
        assert excinfo.value.errors == [
            {
                "offset": 3,
                "message": "a repeat count is a positive integer written [1-9][0-9]* "
                "(zero and empty counts are malformed, not no-ops)",
            }
        ]

    def test_create_validates_name_scope_favorite(self, store):
        with pytest.raises(macro_store.MacroValidationError) as excinfo:
            macro_store.create_macro(name="", scope={"kind": "sideways"}, notation="enter")
        messages = [error["message"] for error in excinfo.value.errors]
        assert any("name" in message for message in messages)
        assert any("scope kind" in message for message in messages)

    def test_written_file_is_mode_0600(self, store):
        macro_store.create_macro(name="x", scope={"kind": "global"}, notation="enter")
        mode = stat.S_IMODE(os.stat(store).st_mode)
        assert mode == 0o600

    def test_list_empty_store_has_no_quarantine(self, store):
        assert macro_store.list_macros() == {"macros": []}


class TestVisibilityAndOrdering:
    def test_scope_visibility(self, store):
        _write_store(
            store,
            {
                "schema_version": 1,
                "macros": [
                    _record("1", "global one"),
                    _record("2", "kimi one", {"kind": "provider", "provider": "kimi_cli"}),
                    _record("3", "claude one", {"kind": "provider", "provider": "claude_code"}),
                    _record("4", "profile one", {"kind": "profile", "profile": "p1"}),
                ],
            },
        )
        names = [
            m["name"] for m in macro_store.list_macros(provider="kimi_cli", profile="p1")["macros"]
        ]
        assert "global one" in names and "kimi one" in names and "profile one" in names
        assert "claude one" not in names
        names = [m["name"] for m in macro_store.list_macros()["macros"]]
        assert names == ["global one"]

    def test_pinned_ordering_favorites_scope_rank_name(self, store):
        _write_store(
            store,
            {
                "schema_version": 1,
                "macros": [
                    _record("1", "zebra", favorite=True),
                    _record("2", "Apple", favorite=True),
                    _record("3", "kimi fav", {"kind": "provider", "provider": "kimi_cli"}, True),
                    _record("4", "prof fav", {"kind": "profile", "profile": "p1"}, True),
                    _record("5", "plain", favorite=False),
                    _record("6", "another plain", {"kind": "profile", "profile": "p1"}, False),
                ],
            },
        )
        names = [
            m["name"] for m in macro_store.list_macros(provider="kimi_cli", profile="p1")["macros"]
        ]
        # Favorites first: global by name (Apple < zebra), then the provider
        # group including the synthesized built-ins (Compact < kimi fav <
        # Stop), then profile; non-favorites after, global before profile.
        assert names == [
            "Apple",
            "zebra",
            "Compact",
            "kimi fav",
            "Stop",
            "prof fav",
            "plain",
            "another plain",
        ]

    def test_case_insensitive_name_ties_keep_file_order(self, store):
        _write_store(
            store,
            {
                "schema_version": 1,
                "macros": [
                    _record("1", "Beta"),
                    _record("2", "beta"),
                ],
            },
        )
        ids = [m["id"] for m in macro_store.list_macros()["macros"]]
        assert ids == ["1", "2"]


class TestBuiltins:
    def test_deterministic_ids_and_synthesis(self):
        builtins = builtin_macros_for_provider("kimi_cli")
        assert [b["id"] for b in builtins] == [
            "builtin:kimi_cli:compact",
            "builtin:kimi_cli:stop",
        ]
        assert all(b["origin"] == "builtin" and b["mutable"] is False for b in builtins)
        assert all(b["favorite"] is True for b in builtins)
        compact = builtins[0]
        assert compact["events"] == [
            {"type": "text", "text": "/compact"},
            {"type": "key", "key": "Enter"},
        ]
        assert builtins[1]["events"] == [{"type": "key", "key": "Escape"}]

    def test_no_registry_entry_synthesizes_nothing(self):
        # The provider is looked up rather than named, so this keeps asserting
        # what it means. It named "codex" when Codex had no registry row; the
        # evidence-pinned `_codex_entry` (compact + stop) later gave it one,
        # and the assertion then failed on a provider that is behaving
        # correctly. Resolving an absent provider from the registry itself
        # cannot drift that way — and if every known provider ever advertises
        # controls, this skips loudly instead of silently asserting nothing.
        from cli_agent_orchestrator.models.provider import ProviderType
        from cli_agent_orchestrator.services.provider_controls import (
            advertised_provider_controls,
        )

        advertised = advertised_provider_controls()
        unregistered = next((p.value for p in ProviderType if p.value not in advertised), None)
        if unregistered is None:
            pytest.skip("every known provider now advertises controls")

        assert builtin_macros_for_provider(unregistered) == []
        assert builtin_macros_for_provider(None) == []

    def test_builtins_merge_into_the_visible_set(self, store):
        names = [m["name"] for m in macro_store.list_macros(provider="kimi_cli")["macros"]]
        assert names == ["Compact", "Stop"]
        # Built-ins are annotated; user records carry origin/mutable too.
        for macro in macro_store.list_macros(provider="kimi_cli")["macros"]:
            assert macro["origin"] == "builtin" and macro["mutable"] is False

    def test_resolve_builtin_round_trips(self):
        record = resolve_builtin("builtin:claude_code:stop")
        assert record is not None and record["name"] == "Stop"
        assert resolve_builtin("builtin:claude_code:nope") is None
        assert resolve_builtin("builtin:unknown_provider:stop") is None
        assert resolve_builtin("not-a-builtin") is None

    def test_user_record_cannot_claim_builtin_prefix_on_load(self, store):
        _write_store(
            store,
            {
                "schema_version": 1,
                "macros": [_record("builtin:kimi_cli:stop", "impostor")],
            },
        )
        response = macro_store.list_macros()
        assert response["macros"] == []
        assert response["quarantine"]["count"] == 1

    def test_duplicate_builtin_mints_user_record(self, store):
        copy = macro_store.duplicate_macro("builtin:kimi_cli:compact", name="My Compact")
        assert copy["name"] == "My Compact"
        assert not copy["id"].startswith("builtin:")
        assert copy["scope"] == {"kind": "provider", "provider": "kimi_cli"}
        listed = macro_store.list_macros(provider="kimi_cli")["macros"]
        assert any(m["id"] == copy["id"] and m["origin"] == "user" for m in listed)
        # The built-in itself is untouched and still synthesized.
        assert resolve_builtin("builtin:kimi_cli:compact") is not None


class TestUpdateDeleteDuplicate:
    def test_update_full_replace_bumps_updated_at(self, store):
        created = macro_store.create_macro(
            name="before", scope={"kind": "global"}, notation="enter", favorite=False
        )
        updated = macro_store.update_macro(
            created["id"],
            name="after",
            description="d",
            scope={"kind": "profile", "profile": "p1"},
            notation="escape",
            favorite=True,
        )
        assert updated["id"] == created["id"]
        assert updated["created_at"] == created["created_at"]
        assert updated["updated_at"] >= created["updated_at"]
        assert updated["events"] == [{"type": "key", "key": "Escape"}]
        assert updated["scope"] == {"kind": "profile", "profile": "p1"}
        assert updated["favorite"] is True

    def test_update_builtin_conflicts(self, store):
        with pytest.raises(macro_store.BuiltinMacroConflictError):
            macro_store.update_macro(
                "builtin:kimi_cli:stop", name="x", scope={"kind": "global"}, notation="enter"
            )

    def test_delete_builtin_conflicts(self, store):
        with pytest.raises(macro_store.BuiltinMacroConflictError):
            macro_store.delete_macro("builtin:kimi_cli:stop")

    def test_update_and_delete_unknown_id_are_not_found(self, store):
        with pytest.raises(macro_store.MacroNotFoundError):
            macro_store.update_macro(
                "missing", name="x", scope={"kind": "global"}, notation="enter"
            )
        with pytest.raises(macro_store.MacroNotFoundError):
            macro_store.delete_macro("missing")
        with pytest.raises(macro_store.MacroNotFoundError):
            macro_store.duplicate_macro("missing")

    def test_delete_removes_the_record(self, store):
        created = macro_store.create_macro(name="x", scope={"kind": "global"}, notation="enter")
        macro_store.delete_macro(created["id"])
        assert macro_store.list_macros()["macros"] == []
        assert json.loads(store.read_text())["macros"] == []

    def test_duplicate_user_record_copies_and_renames(self, store):
        created = macro_store.create_macro(
            name="original", scope={"kind": "global"}, notation="enter", favorite=True
        )
        copy = macro_store.duplicate_macro(created["id"])
        assert copy["id"] != created["id"]
        assert copy["name"] == "original"
        assert copy["favorite"] is True
        assert copy["events"] == created["events"]


class TestQuarantine:
    def test_unparseable_json_moves_the_whole_file_aside(self, store):
        store.write_text("{not json")
        response = macro_store.list_macros()
        assert response["macros"] == []
        quarantine = response["quarantine"]
        assert quarantine["count"] is None  # raw bytes cannot be record-counted
        assert "macros.quarantine-" in quarantine["path"]
        assert not store.exists()
        # The moved file keeps the original bytes — nothing is lost.
        with open(quarantine["path"]) as handle:
            assert handle.read() == "{not json"

    def test_newer_schema_version_moves_the_whole_file_aside(self, store):
        _write_store(store, {"schema_version": 99, "macros": [_record("1", "x")]})
        response = macro_store.list_macros()
        assert response["macros"] == []
        assert response["quarantine"]["count"] == 1

    def test_top_level_shape_violation_moves_the_whole_file_aside(self, store):
        _write_store(store, {"schema_version": 1, "macros": {"not": "a list"}})
        response = macro_store.list_macros()
        assert response["macros"] == []
        assert "quarantine" in response

    def test_per_record_failures_quarantine_the_record_only(self, store):
        _write_store(
            store,
            {
                "schema_version": 1,
                "macros": [
                    _record("good", "kept"),
                    {
                        "id": "bad-events",
                        "name": "dropped",
                        "scope": {"kind": "global"},
                        "events": [{"type": "text"}],
                        "favorite": False,
                        "created_at": None,
                        "updated_at": None,
                    },
                    {
                        "name": "missing id",
                        "scope": {"kind": "global"},
                        "events": [{"type": "key", "key": "Enter"}],
                        "favorite": False,
                    },
                    _record("good-2", "also kept"),
                ],
            },
        )
        response = macro_store.list_macros()
        assert [m["name"] for m in response["macros"]] == ["also kept", "kept"]
        assert response["quarantine"]["count"] == 2
        quarantine_doc = json.loads(open(response["quarantine"]["path"]).read())
        assert [r.get("id", r.get("name")) for r in quarantine_doc["records"]] == [
            "bad-events",
            "missing id",
        ]

    def test_quarantine_reports_until_the_operator_deletes_the_file(self, store):
        store.write_text("{not json")
        first = macro_store.list_macros()
        assert "quarantine" in first
        second = macro_store.list_macros()
        assert second["quarantine"]["path"] == first["quarantine"]["path"]
        os.unlink(first["quarantine"]["path"])
        assert "quarantine" not in macro_store.list_macros()

    def test_store_recovers_and_keeps_working_after_quarantine(self, store):
        store.write_text("{not json")
        macro_store.list_macros()
        created = macro_store.create_macro(name="x", scope={"kind": "global"}, notation="enter")
        listed = macro_store.list_macros()
        assert [m["id"] for m in listed["macros"]] == [created["id"]]
        assert "quarantine" in listed  # still reported until deleted


class TestAtomicityAndLocking:
    def test_lock_file_is_created_alongside(self, store):
        macro_store.create_macro(name="x", scope={"kind": "global"}, notation="enter")
        assert (store.parent / "macros.json.lock").exists()

    def test_no_part_files_remain_after_writes(self, store):
        macro_store.create_macro(name="x", scope={"kind": "global"}, notation="enter")
        macro_store.create_macro(name="y", scope={"kind": "global"}, notation="escape")
        leftovers = [p.name for p in store.parent.glob(".macros-*.part")]
        assert leftovers == []

    def test_invalid_update_leaves_the_file_untouched(self, store):
        created = macro_store.create_macro(name="x", scope={"kind": "global"}, notation="enter")
        before = store.read_text()
        with pytest.raises(macro_store.MacroValidationError):
            macro_store.update_macro(
                created["id"], name="", scope={"kind": "global"}, notation="enter"
            )
        assert store.read_text() == before
