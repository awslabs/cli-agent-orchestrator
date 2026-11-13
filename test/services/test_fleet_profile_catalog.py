"""Profile catalog on the AG-UI fleet snapshot (#1161).

The fleet snapshot carries a read-only ``profiles`` list so the dashboard can name the
profile behind a terminal. The wire is ``name``, ``source``, ``role``, ``loadable`` and
nothing else: no description (operator free text), path, prompt, tools or MCP servers.
The catalog is a cached scan, so it does not rescan per event and does not churn deltas.
It is read-only, behind the same read scope as ``GET /agents/profiles``, and default-off.
"""

from __future__ import annotations

from test._support import agui_ambient_records as R
from test._support.agui_conformance import parse_sse
from test._support.agui_stream_harness import install
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

import cli_agent_orchestrator.api.main as main
from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.services.agui_stream import GENERATIVE_UI_COMPONENTS

client = TestClient(app, base_url="http://localhost")

CATALOG_KEYS = {"name", "source", "role", "loadable"}

# A listing row exactly as ``list_agent_profiles`` returns it, plus sentinels in every
# field that must NOT reach the wire.
RAW_PROFILES: List[Dict[str, Any]] = [
    {
        "name": "dev",
        "source": "kiro",
        "loadable": True,
        "description": "SENTINEL-DESCRIPTION operator free text",
        "capabilities": ["SENTINEL-CAP"],
        "tags": ["SENTINEL-TAG"],
        "role": "developer",
        "duplicated_in": ["built-in"],
        "path": "/Users/someone/SENTINEL-PATH/dev.md",
        "system_prompt": "SENTINEL-PROMPT",
        "allowedTools": ["SENTINEL-TOOL"],
        "mcpServers": {"SENTINEL-MCP": {}},
    },
    {"name": "sup", "source": "built-in", "loadable": False, "role": None},
]
SENTINELS = (
    "SENTINEL-DESCRIPTION",
    "SENTINEL-CAP",
    "SENTINEL-TAG",
    "SENTINEL-PATH",
    "SENTINEL-PROMPT",
    "SENTINEL-TOOL",
    "SENTINEL-MCP",
)


@pytest.fixture(autouse=True)
def _fresh_catalog_cache():
    # getattr: on the base this helper does not exist, and a missing fixture helper must
    # not turn every test into a setup ERROR (it would hide which assertions really fail).
    reset = getattr(main, "_reset_agui_profile_catalog_cache", lambda: None)
    reset()
    yield
    reset()


@pytest.fixture
def scans(monkeypatch: pytest.MonkeyPatch) -> List[int]:
    calls: List[int] = []

    def _list() -> List[Dict[str, Any]]:
        calls.append(1)
        return [dict(p) for p in RAW_PROFILES]

    monkeypatch.setattr("cli_agent_orchestrator.utils.agent_profiles.list_agent_profiles", _list)
    return calls


def _first_snapshot(body: str) -> Dict[str, Any]:
    for event in parse_sse(body):
        if isinstance(event.get("snapshot"), dict):
            return event["snapshot"]
    raise AssertionError("no STATE_SNAPSHOT")


def test_build_profile_catalog_is_an_allow_list() -> None:
    from cli_agent_orchestrator.services.ui_state_service import build_profile_catalog

    catalog = build_profile_catalog(RAW_PROFILES)
    assert [p["name"] for p in catalog] == ["dev", "sup"]
    assert all(set(p) == CATALOG_KEYS for p in catalog)
    assert catalog[0] == {"name": "dev", "source": "kiro", "role": "developer", "loadable": True}
    assert catalog[1]["role"] is None
    assert not any(s in repr(catalog) for s in SENTINELS)


def test_build_profile_catalog_tolerates_junk_rows() -> None:
    from cli_agent_orchestrator.services.ui_state_service import build_profile_catalog

    assert build_profile_catalog([]) == []
    assert build_profile_catalog([{}, "x", None]) == []  # type: ignore[list-item]
    # Not a list at all (a lister that returned the wrong thing): nothing, not a crash.
    for junk in (None, "dev", {"name": "dev"}, 7):
        assert build_profile_catalog(junk) == []
    # A row with a non-string source/role/loadable is coerced, never passed through.
    (row,) = build_profile_catalog([{"name": "x", "source": 3, "role": 4, "loadable": "yes"}])
    assert row == {"name": "x", "source": "", "role": None, "loadable": False}


def test_snapshot_carries_catalog_on_the_wire_without_sentinels(
    monkeypatch: pytest.MonkeyPatch, scans: List[int]
) -> None:
    install(monkeypatch)
    with client.stream("GET", "/agui/v1/stream") as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())
    profiles = _first_snapshot(body)["profiles"]
    assert [p["name"] for p in profiles] == ["dev", "sup"]
    assert all(set(p) == CATALOG_KEYS for p in profiles)
    assert not any(s in body for s in SENTINELS)


def test_run_plane_snapshot_has_no_catalog_and_never_scans(
    monkeypatch: pytest.MonkeyPatch, scans: List[int]
) -> None:
    """The run plane serves approvals, not the fleet view: no profiles key, no scan."""
    install(monkeypatch)
    body = {
        "threadId": "t",
        "runId": "r",
        "state": {},
        "messages": [],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }
    with client.stream("POST", "/agui/v1/run", json=body) as resp:
        assert resp.status_code == 200
        text = "".join(resp.iter_text())
    assert "profiles" not in _first_snapshot(text)
    assert scans == []


def test_legacy_framing_never_gains_the_catalog(
    monkeypatch: pytest.MonkeyPatch, scans: List[int]
) -> None:
    """``?framing=cao-legacy`` is the deprecated wire; it gains only ``runner_id`` per terminal."""
    install(monkeypatch)
    with client.stream("GET", "/agui/v1/stream", params={"framing": "cao-legacy"}) as resp:
        assert resp.status_code == 200
        text = "".join(resp.iter_text())
    assert "profiles" not in _first_snapshot(text)
    assert scans == []


def test_post_stream_snapshot_carries_catalog(
    monkeypatch: pytest.MonkeyPatch, scans: List[int]
) -> None:
    install(monkeypatch)
    body = {
        "threadId": "t",
        "runId": "r",
        "state": {},
        "messages": [],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }
    with client.stream(
        "POST", "/agui/v1/stream", json=body, headers={"Accept": "text/event-stream"}
    ) as resp:
        assert resp.status_code == 200
        text = "".join(resp.iter_text())
    assert [p["name"] for p in _first_snapshot(text)["profiles"]] == ["dev", "sup"]


def test_catalog_is_scanned_once_not_per_event_and_never_diffed(
    monkeypatch: pytest.MonkeyPatch, scans: List[int]
) -> None:
    """Ten bus events recompute the snapshot ten times; the catalog is scanned at most once
    and contributes no STATE_DELTA op."""
    install(monkeypatch, live=R.PRIMITIVE_RECORDS[:10])
    with client.stream("GET", "/agui/v1/stream") as resp:
        body = "".join(resp.iter_text())
    assert len(scans) == 1  # exactly one: zero would mean the catalog never reached the wire
    paths = [
        op["path"] for e in parse_sse(body) if e.get("type") == "STATE_DELTA" for op in e["delta"]
    ]
    assert not [p for p in paths if p.startswith("/profiles")], paths


def test_the_catalog_is_rescanned_after_the_ttl(
    monkeypatch: pytest.MonkeyPatch, scans: List[int]
) -> None:
    now = [1000.0]
    monkeypatch.setattr(main.time, "monotonic", lambda: now[0])
    main._agui_profile_catalog()
    main._agui_profile_catalog()
    assert len(scans) == 1
    now[0] += main._AGUI_PROFILE_CATALOG_TTL_SECONDS + 1
    main._agui_profile_catalog()
    assert len(scans) == 2


def test_a_ttl_refresh_with_identical_content_emits_no_delta(
    monkeypatch: pytest.MonkeyPatch, scans: List[int]
) -> None:
    """The cache expiring and the scan returning the same catalog is not a change."""
    from cli_agent_orchestrator.services.ui_state_service import diff_snapshot

    now = [1000.0]
    monkeypatch.setattr(main.time, "monotonic", lambda: now[0])
    install(monkeypatch)
    before = main._agui_fleet_snapshot(["cao:read"])
    now[0] += main._AGUI_PROFILE_CATALOG_TTL_SECONDS + 1
    after = main._agui_fleet_snapshot(["cao:read"])
    assert len(scans) == 2, "the refresh really rescanned"
    assert diff_snapshot(before, after) == []


def test_a_failing_terminal_listing_degrades_to_a_smaller_snapshot(
    monkeypatch: pytest.MonkeyPatch, scans: List[int]
) -> None:
    install(monkeypatch, sessions=[{"id": "a", "name": "a", "status": "active"}])

    def _boom(*_a: Any, **_k: Any) -> List[Dict[str, Any]]:
        raise RuntimeError("backend down")

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.session_service.list_current_session_terminals", _boom
    )
    snapshot = main._agui_fleet_snapshot(["cao:read"])
    assert snapshot["terminals"] == []
    assert [s["id"] for s in snapshot["sessions"]] == ["a"]
    assert [p["name"] for p in snapshot["profiles"]] == ["dev", "sup"]


def test_catalog_failure_degrades_to_empty_and_keeps_the_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom() -> List[Dict[str, Any]]:
        raise RuntimeError("scan failed")

    monkeypatch.setattr("cli_agent_orchestrator.utils.agent_profiles.list_agent_profiles", _boom)
    install(monkeypatch)
    with client.stream("GET", "/agui/v1/stream") as resp:
        assert resp.status_code == 200
        snapshot = _first_snapshot("".join(resp.iter_text()))
    assert snapshot["profiles"] == []


# --- auth parity ------------------------------------------------------------------------


def test_catalog_absent_without_a_valid_token_when_auth_is_on(
    monkeypatch: pytest.MonkeyPatch, scans: List[int]
) -> None:
    install(monkeypatch)
    monkeypatch.setattr(main, "is_auth_enabled", lambda: True)
    resp = client.get("/agui/v1/stream")
    assert resp.status_code == 401
    assert "dev" not in resp.text and "profiles" not in resp.text
    assert scans == [], "no token: the catalog must not even be scanned"


def test_catalog_absent_for_a_token_without_read_scope(
    monkeypatch: pytest.MonkeyPatch, scans: List[int]
) -> None:
    install(monkeypatch)
    monkeypatch.setattr(main, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(main, "extract_scopes_from_token", lambda tok: ["cao:other"])
    resp = client.get("/agui/v1/stream", params={"access_token": "x.y.z"})
    assert resp.status_code == 403
    assert "profiles" not in resp.text and scans == []


def test_catalog_present_for_a_read_scope_token(
    monkeypatch: pytest.MonkeyPatch, scans: List[int]
) -> None:
    install(monkeypatch)
    monkeypatch.setattr(main, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(main, "extract_scopes_from_token", lambda tok: ["cao:read"])
    with client.stream("GET", "/agui/v1/stream", params={"access_token": "x.y.z"}) as resp:
        assert resp.status_code == 200
        assert _first_snapshot("".join(resp.iter_text()))["profiles"]


# --- default-off and no write path ------------------------------------------------------


def test_flag_unset_means_404_and_the_catalog_is_never_built(
    monkeypatch: pytest.MonkeyPatch, scans: List[int]
) -> None:
    monkeypatch.delenv("CAO_AGUI_ENABLED", raising=False)
    monkeypatch.delenv("CAO_MCP_APPS_ENABLED", raising=False)
    assert client.get("/agui/v1/stream").status_code == 404
    assert client.post("/agui/v1/run", json={}).status_code == 404
    assert scans == []


def test_no_agui_route_writes_a_profile() -> None:
    agui_routes = [r for r in app.routes if getattr(r, "path", "").startswith("/agui/")]
    assert agui_routes, "the guard would pass vacuously"
    for route in agui_routes:
        assert "profile" not in route.path.lower(), route.path  # type: ignore[attr-defined]
    for method in ("post", "put", "patch", "delete"):
        for path in ("/agui/v1/profiles", "/agui/v1/profiles/dev", "/agui/v1/catalog"):
            assert getattr(client, method)(path).status_code in (404, 405), (method, path)


def test_emit_ui_allow_list_is_frozen() -> None:
    assert GENERATIVE_UI_COMPONENTS == frozenset(
        {"approval_card", "choice_prompt", "diff_summary", "progress", "metric", "agent_card"}
    )


def test_the_catalog_is_returned_as_copies(
    monkeypatch: pytest.MonkeyPatch, scans: List[int]
) -> None:
    """The cache is shared across streams: a caller that edits its result must not edit it."""
    first = main._agui_profile_catalog()
    # A shallow copy per entry is enough because an entry holds only scalars (the wire shape
    # is name, source, role, loadable); this pins that, so a nested value forces a deepcopy.
    assert all(
        not isinstance(v, (dict, list, set, tuple)) for entry in first for v in entry.values()
    )
    first[0]["name"] = "changed"
    first.append({"name": "extra"})
    again = main._agui_profile_catalog()
    assert [p["name"] for p in again] == ["dev", "sup"]
    assert len(scans) == 1
