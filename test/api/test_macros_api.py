"""Tests for the §5.4 /macros API routes.

Scope discipline mirrors the settings routes (READ for list/parse, WRITE for
mutations); the store itself is redirected into a tmp directory per test.
"""

import json

import pytest

from cli_agent_orchestrator.services import macro_store


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(macro_store, "MACROS_PATH", tmp_path / "macros.json")
    return tmp_path / "macros.json"


class TestListEndpoint:
    def test_empty(self, client, store):
        response = client.get("/macros")
        assert response.status_code == 200
        assert response.json() == {"macros": []}

    def test_builtin_synthesis_and_annotation(self, client, store):
        response = client.get("/macros?provider=kimi_cli")
        assert response.status_code == 200
        macros = response.json()["macros"]
        assert [m["id"] for m in macros] == [
            "builtin:kimi_cli:compact",
            "builtin:kimi_cli:stop",
        ]
        assert all(m["origin"] == "builtin" and m["mutable"] is False for m in macros)

    def test_provider_filter_hides_other_provider_records(self, client, store):
        client.post(
            "/macros",
            json={
                "name": "kimi only",
                "scope": {"kind": "provider", "provider": "kimi_cli"},
                "notation": "enter",
            },
        )
        names = [m["name"] for m in client.get("/macros?provider=claude_code").json()["macros"]]
        assert "kimi only" not in names

    def test_quarantine_block_reported(self, client, store):
        store.write_text("{not json")
        response = client.get("/macros")
        assert response.status_code == 200
        quarantine = response.json()["quarantine"]
        assert "macros.quarantine-" in quarantine["path"]


class TestCreateEndpoint:
    def test_create_via_notation(self, client, store):
        response = client.post(
            "/macros",
            json={
                "name": "Model K2.7",
                "scope": {"kind": "provider", "provider": "kimi_cli"},
                "notation": '"/model" enter up*3 enter',
                "favorite": True,
            },
        )
        assert response.status_code == 201
        record = response.json()
        assert record["origin"] if "origin" in record else True
        assert record["events"][0] == {"type": "text", "text": "/model"}
        listed = client.get("/macros?provider=kimi_cli").json()["macros"]
        assert any(m["id"] == record["id"] and m["mutable"] is True for m in listed)

    def test_create_via_events(self, client, store):
        response = client.post(
            "/macros",
            json={
                "name": "Escape hatch",
                "scope": {"kind": "global"},
                "events": [{"type": "key", "key": "Escape"}],
            },
        )
        assert response.status_code == 201
        assert response.json()["events"] == [{"type": "key", "key": "Escape"}]

    def test_create_422_with_notation_errors(self, client, store):
        response = client.post(
            "/macros",
            json={"name": "bad", "scope": {"kind": "global"}, "notation": "ctrl+shift+x"},
        )
        assert response.status_code == 422
        assert response.json()["errors"] == [
            {
                "offset": 0,
                "message": "multi-modifier combination 'ctrl+shift+x' cannot be "
                "represented: no standard-mode terminal byte encoding exists for it "
                "(tmux would inject the base key or a wrong encoding), so it is "
                "refused, never approximated",
            }
        ]

    def test_create_422_when_both_or_neither_payload(self, client, store):
        both = client.post(
            "/macros",
            json={
                "name": "x",
                "scope": {"kind": "global"},
                "events": [{"type": "key", "key": "Enter"}],
                "notation": "enter",
            },
        )
        neither = client.post("/macros", json={"name": "x", "scope": {"kind": "global"}})
        assert both.status_code == 422 and neither.status_code == 422

    def test_create_422_invalid_events(self, client, store):
        response = client.post(
            "/macros",
            json={"name": "x", "scope": {"kind": "global"}, "events": [{"type": "text"}]},
        )
        assert response.status_code == 422
        assert "errors" in response.json()


class TestUpdateDeleteEndpoints:
    def _create(self, client) -> str:
        response = client.post(
            "/macros",
            json={"name": "x", "scope": {"kind": "global"}, "notation": "enter"},
        )
        return response.json()["id"]

    def test_update_full_replace(self, client, store):
        macro_id = self._create(client)
        response = client.put(
            f"/macros/{macro_id}",
            json={
                "name": "renamed",
                "scope": {"kind": "profile", "profile": "p1"},
                "notation": "escape",
                "favorite": True,
            },
        )
        assert response.status_code == 200
        record = response.json()
        assert record["name"] == "renamed"
        assert record["events"] == [{"type": "key", "key": "Escape"}]

    def test_update_builtin_is_409(self, client, store):
        response = client.put(
            "/macros/builtin:kimi_cli:stop",
            json={"name": "x", "scope": {"kind": "global"}, "notation": "enter"},
        )
        assert response.status_code == 409

    def test_delete_builtin_is_409(self, client, store):
        response = client.delete("/macros/builtin:kimi_cli:stop")
        assert response.status_code == 409

    def test_update_and_delete_unknown_are_404(self, client, store):
        assert (
            client.put(
                "/macros/missing",
                json={"name": "x", "scope": {"kind": "global"}, "notation": "enter"},
            ).status_code
            == 404
        )
        assert client.delete("/macros/missing").status_code == 404

    def test_delete_round_trip(self, client, store):
        macro_id = self._create(client)
        assert client.delete(f"/macros/{macro_id}").status_code == 200
        assert client.get("/macros").json()["macros"] == []


class TestDuplicateEndpoint:
    def test_duplicate_builtin_mints_user_record(self, client, store):
        response = client.post(
            "/macros/builtin:kimi_cli:compact/duplicate", json={"name": "My Compact"}
        )
        assert response.status_code == 201
        record = response.json()
        assert record["name"] == "My Compact"
        assert not record["id"].startswith("builtin:")
        # The duplicate is a user record in the visible set.
        listed = client.get("/macros?provider=kimi_cli").json()["macros"]
        assert any(m["id"] == record["id"] and m["origin"] == "user" for m in listed)
        # The built-in still resolves to the same synthesized record.
        builtins = [m for m in listed if m["origin"] == "builtin"]
        assert [b["id"] for b in builtins] == [
            "builtin:kimi_cli:compact",
            "builtin:kimi_cli:stop",
        ]

    def test_duplicate_unknown_is_404(self, client, store):
        assert client.post("/macros/missing/duplicate", json={}).status_code == 404
        assert client.post("/macros/builtin:nope:compact/duplicate", json={}).status_code == 404


class TestParseNotationEndpoint:
    def test_success_shape(self, client, store):
        response = client.post("/macros/parse-notation", json={"notation": '"/model" enter up*3'})
        assert response.status_code == 200
        body = response.json()
        assert body["events"] == [
            {"type": "text", "text": "/model"},
            {"type": "key", "key": "Enter"},
            {"type": "key", "key": "Up"},
            {"type": "key", "key": "Up"},
            {"type": "key", "key": "Up"},
        ]
        assert body["preview"] == '"/model" [Enter] [Up]×3'

    def test_422_errors_shape(self, client, store):
        response = client.post("/macros/parse-notation", json={"notation": "up*0"})
        assert response.status_code == 422
        assert response.json() == {
            "errors": [
                {
                    "offset": 3,
                    "message": "a repeat count is a positive integer written [1-9][0-9]* "
                    "(zero and empty counts are malformed, not no-ops)",
                }
            ]
        }

    def test_missing_notation_is_422(self, client, store):
        # The field is required by the request model: never a 500.
        response = client.post("/macros/parse-notation", json={})
        assert response.status_code == 422

    def test_absurd_repeat_count_is_the_ordinary_offset_422_not_500(self, client, store):
        """r11 regression: the over-budget repeat fails before the integer
        conversion, so the endpoint answers the ordinary offset-bearing
        422 shape — never HTTP 500, never offset null with a leaked
        conversion message."""
        response = client.post("/macros/parse-notation", json={"notation": "up*" + "9" * 5000})
        assert response.status_code == 422
        errors = response.json()["errors"]
        assert errors[0]["offset"] == 0
        assert errors[0]["message"] == (
            "this event brings the sequence past the 32-event cap; a repeat "
            "expansion counts every event it stands for"
        )
        assert "sys.set_int_max_str_digits" not in errors[0]["message"]


class TestCreateRepeatRegression:
    def test_create_with_absurd_repeat_is_the_ordinary_422(self, client, store):
        response = client.post(
            "/macros",
            json={
                "name": "x",
                "scope": {"kind": "global"},
                "notation": "up*" + "9" * 5000,
            },
        )
        assert response.status_code == 422
        errors = response.json()["errors"]
        assert errors[0]["offset"] == 0
        assert errors[0]["message"] == (
            "this event brings the sequence past the 32-event cap; a repeat "
            "expansion counts every event it stands for"
        )


class TestScopeGating:
    """Scope discipline: the H4 guard (test_scope_coverage.py) mechanically
    rejects any mutating route without a ``require_any_scope`` dependency;
    here we pin the *intended* scopes per route directly on the route table."""

    def _scopes_for(self, method: str, path: str) -> set:
        from cli_agent_orchestrator.api.main import app

        for route in app.routes:
            if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
                seen = set()
                stack = list(route.dependant.dependencies)
                while stack:
                    dep = stack.pop()
                    call = getattr(dep, "call", None)
                    if call is not None and "require_any_scope" in getattr(
                        call, "__qualname__", ""
                    ):
                        # The factory closes over the required scope tuple.
                        for cell in call.__closure__ or ():
                            value = cell.cell_contents
                            if isinstance(value, tuple) and all(
                                isinstance(item, str) for item in value
                            ):
                                seen.update(value)
                    stack.extend(getattr(dep, "dependencies", []))
                return seen
        raise AssertionError(f"no route {method} {path}")

    def test_list_and_parse_are_read_scoped(self):
        from cli_agent_orchestrator.security.auth import SCOPE_READ

        assert SCOPE_READ in self._scopes_for("GET", "/macros")
        assert SCOPE_READ in self._scopes_for("POST", "/macros/parse-notation")

    def test_mutations_are_write_scoped(self):
        from cli_agent_orchestrator.security.auth import SCOPE_WRITE

        assert SCOPE_WRITE in self._scopes_for("POST", "/macros")
        assert SCOPE_WRITE in self._scopes_for("PUT", "/macros/{macro_id}")
        assert SCOPE_WRITE in self._scopes_for("DELETE", "/macros/{macro_id}")
        assert SCOPE_WRITE in self._scopes_for("POST", "/macros/{macro_id}/duplicate")
