"""Post-3.0 AG-UI previews (#1162, #1164, #1194) behind ``CAO_AGUI_POST3_PREVIEW``.

Covers the flag (unset means 404 and no side effect), the team-composition
projection, the tenant-keyed decision log, and the A2UI rendering of one
allow-listed component with off-list components still refused.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Iterator

import pytest
from fastapi.testclient import TestClient

import cli_agent_orchestrator.api.main as main
from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.services.agui import post3_preview as pv
from cli_agent_orchestrator.services.agui.approval_bridge import ApprovalBridge
from cli_agent_orchestrator.services.agui.base import RecordingUiEmitter
from cli_agent_orchestrator.services.agui.handoff_approval import AgentHandoffWithApproval
from cli_agent_orchestrator.services.agui_stream import GENERATIVE_UI_COMPONENTS

client = TestClient(app, base_url="http://localhost")

ROUTES = (
    ("get", "/agui/v1/preview/team-composition", None),
    ("get", "/agui/v1/preview/approvals", None),
    (
        "post",
        "/agui/v1/preview/a2ui",
        {"component": "metric", "props": {"label": "a", "value": "1"}},
    ),
)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.setenv("CAO_AGUI_ENABLED", "true")
    monkeypatch.delenv("CAO_MCP_APPS_ENABLED", raising=False)
    monkeypatch.delenv(pv.PREVIEW_ENV, raising=False)
    monkeypatch.delenv("AUTH0_DOMAIN", raising=False)
    monkeypatch.delenv("CAO_AUTH_JWKS_URI", raising=False)
    monkeypatch.setattr(pv, "get_decision_log", lambda: pv.DecisionLog(tmp_path / "decisions"))
    yield


@pytest.fixture
def preview_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(pv.PREVIEW_ENV, "1")


def _call(method: str, path: str, body: Any) -> Any:
    return client.get(path) if method == "get" else client.post(path, json=body)


# ---------------------------------------------------------------------------
# The flag: unset means absent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method,path,body", ROUTES)
def test_routes_are_404_when_the_flag_is_unset(method: str, path: str, body: Any) -> None:
    r = _call(method, path, body)
    assert r.status_code == 404
    # The same body an unregistered path gets: absent, not "disabled".
    assert r.json() == {"detail": "Not Found"}
    assert r.content == client.get("/agui/v1/no-such-preview-route").content


@pytest.mark.parametrize("method,path,body", ROUTES)
def test_routes_are_404_when_only_the_preview_flag_is_set(
    monkeypatch: pytest.MonkeyPatch, method: str, path: str, body: Any
) -> None:
    monkeypatch.setenv(pv.PREVIEW_ENV, "1")
    monkeypatch.setenv("CAO_AGUI_ENABLED", "false")
    r = _call(method, path, body)
    assert r.status_code == 404
    assert r.content == client.get("/agui/v1/no-such-preview-route").content


@pytest.mark.parametrize("method,path,body", ROUTES)
def test_routes_are_404_when_only_the_agui_surface_is_on(
    monkeypatch: pytest.MonkeyPatch, method: str, path: str, body: Any
) -> None:
    monkeypatch.setenv("CAO_AGUI_ENABLED", "1")
    monkeypatch.delenv(pv.PREVIEW_ENV, raising=False)
    r = _call(method, path, body)
    assert r.status_code == 404
    assert r.content == client.get("/agui/v1/no-such-preview-route").content


@pytest.mark.parametrize("value", ["", "0", "false", "no", " "])
def test_falsy_flag_values_stay_off(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(pv.PREVIEW_ENV, value)
    assert pv.post3_preview_enabled() is False


def test_the_flag_is_read_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    assert pv.post3_preview_enabled() is False
    monkeypatch.setenv(pv.PREVIEW_ENV, "true")
    assert pv.post3_preview_enabled() is True


def test_a_resume_with_the_flag_unset_writes_no_decision_log(tmp_path: Path) -> None:
    bridge = _bridge_with_interrupt()
    iid = bridge["interrupt_id"]
    r = client.post(f"/agui/v1/interrupts/{iid}/resume", json={"decision": "approve"})
    assert r.status_code == 200
    assert not (tmp_path / "decisions").exists()
    assert set(r.json()) == {"ok", "interrupt_id", "resolved", "outcome"}


# ---------------------------------------------------------------------------
# (a) team composition
# ---------------------------------------------------------------------------

SNAPSHOT: Dict[str, Any] = {
    "terminals": [
        {"id": "t1", "provider": "kiro_cli", "agent_profile": "dev", "status": "idle"},
        {"id": "t2", "provider": "claude_code", "agent_profile": None, "status": "processing"},
        {"id": "t3", "provider": "kiro_cli", "agent_profile": "rev", "status": "idle"},
    ]
}
PROFILES = [{"name": "dev", "role": "developer"}, {"name": "rev", "role": "reviewer"}]


def test_team_composition_projects_agent_cards_and_metrics() -> None:
    out = pv.build_team_composition(SNAPSHOT, PROFILES)
    assert [m["component"] for m in out["members"]] == ["agent_card"] * 3
    assert out["members"][0]["props"] == {
        "name": "dev",
        "provider": "kiro_cli",
        "status": "idle",
        "role": "developer",
    }
    assert out["members"][1]["props"]["name"] == "t2"
    assert "role" not in out["members"][1]["props"]
    assert {m["props"]["label"]: m["props"]["value"] for m in out["metrics"]} == {
        "members": "3",
        "providers": "2",
    }
    assert all(m["component"] in GENERATIVE_UI_COMPONENTS for m in out["members"] + out["metrics"])


def test_team_composition_is_bounded_and_metadata_only() -> None:
    big = {"terminals": [{"id": f"t{i}", "provider": "p", "prompt": "SECRET"} for i in range(80)]}
    out = pv.build_team_composition(big, [])
    assert len(out["members"]) == pv.MAX_TEAM_MEMBERS and out["truncated"] is True
    assert "SECRET" not in json.dumps(out)


def test_team_composition_route(preview_on: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "_agui_fleet_snapshot", lambda scopes, profiles=True: SNAPSHOT)
    monkeypatch.setattr(main, "_agui_profile_catalog", lambda: PROFILES)
    r = client.get("/agui/v1/preview/team-composition")
    assert r.status_code == 200
    assert r.json()["preview"] is True and len(r.json()["members"]) == 3


# ---------------------------------------------------------------------------
# (b) decision log
# ---------------------------------------------------------------------------


def _bridge_with_interrupt() -> Dict[str, str]:
    construct = AgentHandoffWithApproval(emitter=RecordingUiEmitter(), answer_delivery=None)
    app.state.approval_bridge = ApprovalBridge(construct=construct)
    construct.on_provider_waiting(
        terminal_id="term-1", provider="kiro_cli", raw_prompt="Allow? (y/n)", session_name="s"
    )
    return {"interrupt_id": next(iter(construct._interrupts))}


@pytest.fixture
def interrupt(request: pytest.FixtureRequest) -> Iterator[Dict[str, str]]:
    yield _bridge_with_interrupt()
    if hasattr(app.state, "approval_bridge"):
        del app.state.approval_bridge


def test_a_decision_is_logged_and_summarised(preview_on: None, interrupt: Dict[str, str]) -> None:
    iid = interrupt["interrupt_id"]
    assert (
        client.post(f"/agui/v1/interrupts/{iid}/resume", json={"decision": "deny"}).status_code
        == 200
    )
    s = client.get("/agui/v1/preview/approvals").json()
    assert s["total"] == 1 and s["by_decision"] == {"approve": 0, "deny": 1, "edit": 0}
    assert s["by_provider"] == {"kiro_cli": 1}
    rec = s["recent"][0]
    assert rec["tenant_id"] == "local" and rec["terminal_id"] == "term-1"
    assert rec["interrupt_id"] == iid


def test_a_repeated_resume_is_not_a_second_decision(
    preview_on: None, interrupt: Dict[str, str]
) -> None:
    iid = interrupt["interrupt_id"]
    for _ in range(2):
        assert (
            client.post(
                f"/agui/v1/interrupts/{iid}/resume", json={"decision": "approve"}
            ).status_code
            == 200
        )
    assert client.get("/agui/v1/preview/approvals").json()["total"] == 1


def test_edited_text_is_never_stored(tmp_path: Path) -> None:
    log = pv.DecisionLog(tmp_path)
    interrupt = type("I", (), {"id": "i1", "metadata": {"terminal_id": "t", "provider": "p"}})()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(pv, "get_decision_log", lambda: log)
    try:
        pv.record_decision(interrupt, "edit", "TOP-SECRET-EDIT", "edited")
    finally:
        monkey.undo()
    raw = (tmp_path / "local.jsonl").read_text()
    assert "TOP-SECRET-EDIT" not in raw
    assert json.loads(raw)["edited"] is True


def test_the_log_is_keyed_by_tenant(tmp_path: Path) -> None:
    log = pv.DecisionLog(tmp_path)
    kw: Dict[str, Any] = dict(
        interrupt_id="i", terminal_id="t", provider="p", edited=False, outcome="approved"
    )
    log.append("local", decision="approve", **kw)
    log.append("other", decision="deny", **kw)
    assert log.summary("local")["by_decision"]["approve"] == 1
    assert log.summary("local")["total"] == 1
    assert log.summary("other")["by_decision"]["deny"] == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == ["local.jsonl", "other.jsonl"]


@pytest.mark.parametrize("bad", ["../x", "a/b", "", "A", "x" * 65, ".."])
def test_a_tenant_id_cannot_escape_the_log_directory(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError):
        pv.DecisionLog(tmp_path).summary(bad)


def test_the_log_file_is_owner_only_and_append_only(tmp_path: Path) -> None:
    log = pv.DecisionLog(tmp_path)
    kw: Dict[str, Any] = dict(
        interrupt_id="i", terminal_id="t", provider="p", edited=False, outcome=None
    )
    log.append("local", decision="approve", **kw)
    log.append("local", decision="deny", **kw)
    path = tmp_path / "local.jsonl"
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert [json.loads(x)["decision"] for x in path.read_text().splitlines()] == ["approve", "deny"]


def test_an_unknown_decision_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        pv.DecisionLog(tmp_path).append(
            "local",
            interrupt_id="i",
            terminal_id=None,
            provider=None,
            decision="maybe",
            edited=False,
            outcome=None,
        )


def test_the_log_rotates_when_the_threshold_is_patched_low(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Threshold patched to 10 bytes; the real 1 MiB constant is exercised separately."""
    monkeypatch.setattr(pv, "_ROTATE_BYTES", 10)
    log = pv.DecisionLog(tmp_path)
    kw: Dict[str, Any] = dict(terminal_id="t", provider="p", edited=False, outcome=None)
    log.append("local", interrupt_id="a", decision="approve", **kw)
    log.append("local", interrupt_id="b", decision="deny", **kw)
    assert (tmp_path / "local.jsonl.1").exists()
    assert log.summary("local")["total"] == 2  # both generations count


# ---------------------------------------------------------------------------
# (c) one allow-listed component as an A2UI surface
# ---------------------------------------------------------------------------


def test_the_allow_list_is_unchanged() -> None:
    assert GENERATIVE_UI_COMPONENTS == {
        "approval_card",
        "choice_prompt",
        "diff_summary",
        "progress",
        "metric",
        "agent_card",
    }
    assert pv.A2UI_RENDERABLE <= GENERATIVE_UI_COMPONENTS


def test_a_metric_renders_as_an_a2ui_surface_on_activity_snapshot(preview_on: None) -> None:
    r = client.post(
        "/agui/v1/preview/a2ui",
        json={"component": "metric", "props": {"label": "Tokens", "value": 1200, "unit": "tok"}},
    )
    assert r.status_code == 200
    frame = r.json()["frame"]
    assert frame["type"] == "ACTIVITY_SNAPSHOT" and frame["activityType"] == "a2ui-surface"
    ops = frame["content"]["a2ui_operations"]
    assert ops[0]["createSurface"]["catalogId"] == pv.A2UI_BASIC_CATALOG_ID
    assert {c["component"] for c in ops[1]["updateComponents"]["components"]} == {
        "Card",
        "Column",
        "Text",
    }
    assert r.json()["rendered"] == "Tokens\n1200 tok"


def test_off_list_components_are_still_refused(preview_on: None) -> None:
    r = client.post("/agui/v1/preview/a2ui", json={"component": "iframe", "props": {}})
    assert r.status_code == 400 and "Unknown UI component" in r.json()["detail"]


def test_an_off_list_name_leaves_the_metadata_only_refusal_record(
    preview_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An off-list name gets the same refusal record ``emit_ui`` leaves, and only
    then the 400; a listed component without an A2UI rendering leaves none."""
    calls = []
    monkeypatch.setattr(main, "_publish_ui_refusal", lambda *a, **k: calls.append(a))
    r = client.post(
        "/agui/v1/preview/a2ui", json={"component": "iframe", "props": {}, "terminal_id": "t9"}
    )
    assert r.status_code == 400
    assert calls == [("iframe", "t9")]
    calls.clear()
    r = client.post("/agui/v1/preview/a2ui", json={"component": "approval_card", "props": {}})
    assert r.status_code == 400 and calls == []


def _scopes(scopes: list) -> Any:
    async def _dep() -> list:
        return list(scopes)

    return _dep


def test_the_a2ui_route_needs_the_write_scope(
    preview_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With auth on, a read-only caller is refused (403); write and admin are let in."""
    from cli_agent_orchestrator.security import auth

    monkeypatch.setenv("CAO_AUTH_JWKS_URI", "https://idp.example/jwks")
    body = {"component": "metric", "props": {"label": "Tokens", "value": 1200, "unit": "tok"}}
    try:
        app.dependency_overrides[auth.get_current_scopes] = _scopes([auth.SCOPE_READ])
        assert client.post("/agui/v1/preview/a2ui", json=body).status_code == 403
        for scope in (auth.SCOPE_WRITE, auth.SCOPE_ADMIN):
            app.dependency_overrides[auth.get_current_scopes] = _scopes([scope])
            assert client.post("/agui/v1/preview/a2ui", json=body).status_code == 200
    finally:
        app.dependency_overrides.pop(auth.get_current_scopes, None)


def test_a_listed_component_without_an_a2ui_rendering_is_refused(preview_on: None) -> None:
    r = client.post("/agui/v1/preview/a2ui", json={"component": "approval_card", "props": {}})
    assert r.status_code == 400 and "no A2UI rendering" in r.json()["detail"]


@pytest.mark.parametrize(
    "value",
    [
        "https://evil.example/x",
        "javascript:alert(1)",
        "//evil.example",
        "data:text/html,x",
        "www.evil.example",
        "[click](x)",
        "<script>alert(1)</script>",
        "![i](x)",
    ],
)
def test_no_urls_scripts_or_links_reach_the_surface(value: str) -> None:
    try:
        ops = pv.to_a2ui_operations("metric", {"label": "L", "value": value})
    except pv.A2uiRefusal:
        return
    blob = json.dumps(ops)
    for needle in ("://", "javascript:", "<", "](", "www."):
        assert needle not in blob.replace(pv.A2UI_BASIC_CATALOG_ID, "")


def test_a_metric_without_a_label_or_value_is_refused() -> None:
    for props in ({}, {"label": "x"}, {"value": "1"}, {"label": "x", "value": True}):
        with pytest.raises(pv.A2uiRefusal):
            pv.to_a2ui_operations("metric", props)


def test_the_reference_renderer_refuses_what_it_does_not_know() -> None:
    ops = pv.to_a2ui_operations("metric", {"label": "a", "value": "1"})
    ops[1]["updateComponents"]["components"][3]["component"] = "Image"
    with pytest.raises(pv.A2uiRefusal):
        pv.render_text(ops)
    ops = pv.to_a2ui_operations("metric", {"label": "a", "value": "1"})
    ops[0]["createSurface"]["catalogId"] = "https://elsewhere.example/catalog.json"
    with pytest.raises(pv.A2uiRefusal):
        pv.render_text(ops)


def test_the_surface_id_is_validated() -> None:
    with pytest.raises(pv.A2uiRefusal):
        pv.to_a2ui_operations("metric", {"label": "a", "value": "1"}, surface_id="../x")


# ---------------------------------------------------------------------------
# Review follow-ups
# ---------------------------------------------------------------------------


def test_a_decision_log_write_failure_does_not_fail_the_resume(
    preview_on: None, interrupt: Dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom() -> pv.DecisionLog:
        raise OSError("disk full")

    monkeypatch.setattr(pv, "get_decision_log", boom)
    iid = interrupt["interrupt_id"]
    r = client.post(f"/agui/v1/interrupts/{iid}/resume", json={"decision": "approve"})
    assert r.status_code == 200 and r.json()["resolved"] is True


def test_props_over_8kb_are_refused_on_the_a2ui_route(preview_on: None) -> None:
    r = client.post(
        "/agui/v1/preview/a2ui",
        json={"component": "metric", "props": {"label": "x", "value": "y" * 9000}},
    )
    assert r.status_code == 400 and "too large" in r.json()["detail"]


def test_an_edit_through_the_route_logs_edited_true_and_no_text(
    preview_on: None, tmp_path: Path
) -> None:
    construct = AgentHandoffWithApproval(emitter=RecordingUiEmitter(), answer_delivery=None)
    app.state.approval_bridge = ApprovalBridge(construct=construct)
    try:
        itr = construct.on_provider_waiting("t-1", "kiro_cli", "Allow this action? [y/n/t]:")
        r = client.post(
            f"/agui/v1/interrupts/{itr.id}/resume",
            json={"decision": "edit", "edited_text": "EDITED-SECRET"},
        )
    finally:
        del app.state.approval_bridge
    assert r.status_code == 200 and r.json()["outcome"] == "edit"
    raw = (tmp_path / "decisions" / "local.jsonl").read_text()
    assert "EDITED-SECRET" not in raw
    rec = json.loads(raw)
    assert rec["decision"] == "edit" and rec["edited"] is True


def test_a_corrupt_log_line_is_skipped(tmp_path: Path) -> None:
    log = pv.DecisionLog(tmp_path)
    kw: Dict[str, Any] = dict(terminal_id="t", provider="p", edited=False, outcome=None)
    log.append("local", interrupt_id="a", decision="approve", **kw)
    with open(tmp_path / "local.jsonl", "a") as fh:
        fh.write("{not json\n[1,2]\n" + json.dumps({"tenant_id": "local", "decision": "x"}) + "\n")
    log.append("local", interrupt_id="b", decision="deny", **kw)
    s = log.summary("local")
    assert s["total"] == 2 and s["by_decision"] == {"approve": 1, "deny": 1, "edit": 0}


def test_the_summary_reads_both_generations(tmp_path: Path) -> None:
    log = pv.DecisionLog(tmp_path)
    kw: Dict[str, Any] = dict(terminal_id="t", provider="p", edited=False, outcome=None)
    log.append("local", interrupt_id="old", decision="approve", **kw)
    os.replace(tmp_path / "local.jsonl", tmp_path / "local.jsonl.1")
    log.append("local", interrupt_id="new", decision="deny", **kw)
    assert [r["interrupt_id"] for r in log.records("local")] == ["old", "new"]


def test_rotation_at_the_real_one_mib_threshold(tmp_path: Path) -> None:
    assert pv._ROTATE_BYTES == 1024 * 1024
    log = pv.DecisionLog(tmp_path)
    kw: Dict[str, Any] = dict(terminal_id="t", provider="p", edited=False, outcome=None)
    log.append("local", interrupt_id="first", decision="approve", **kw)
    with open(tmp_path / "local.jsonl", "a") as fh:  # pad the file past 1 MiB with blanks
        fh.write("\n" * (pv._ROTATE_BYTES + 1))
    log.append("local", interrupt_id="second", decision="deny", **kw)
    assert (tmp_path / "local.jsonl.1").exists()
    assert [r["interrupt_id"] for r in log.records("local")] == ["first", "second"]


def test_a_racing_duplicate_interrupt_id_counts_once(tmp_path: Path) -> None:
    log = pv.DecisionLog(tmp_path)
    kw: Dict[str, Any] = dict(terminal_id="t", provider="p", edited=False, outcome=None)
    log.append("local", interrupt_id="same", decision="approve", **kw)
    log.append("local", interrupt_id="same", decision="deny", **kw)
    s = log.summary("local")
    assert s["total"] == 1 and s["by_decision"]["approve"] == 1


def test_a_looser_existing_log_file_is_tightened(tmp_path: Path) -> None:
    path = tmp_path / "local.jsonl"
    path.write_text("")
    os.chmod(path, 0o644)
    os.chmod(tmp_path, 0o755)
    pv.DecisionLog(tmp_path).append(
        "local",
        interrupt_id="i",
        terminal_id=None,
        provider=None,
        decision="approve",
        edited=False,
        outcome=None,
    )
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert os.stat(tmp_path).st_mode & 0o777 == 0o700


@pytest.mark.parametrize("value", ["http:evil.example", "file:/etc/passwd", "ftp:x", "blob:x"])
def test_scheme_without_slashes_is_refused(value: str) -> None:
    with pytest.raises(pv.A2uiRefusal):
        pv.to_a2ui_operations("metric", {"label": "L", "value": value})


def test_ordinary_values_with_a_colon_still_render() -> None:
    ops = pv.to_a2ui_operations("metric", {"label": "Uptime", "value": "12:30"})
    assert pv.render_text(ops) == "Uptime\n12:30"


# ---------------------------------------------------------------------------
# Review findings on the decision log (local CodeRabbit, 2026-11-23)
# ---------------------------------------------------------------------------


def test_an_unterminated_corrupt_tail_does_not_swallow_the_next_record(tmp_path: Path) -> None:
    """A crash can leave a partial last line with no newline. The next append
    must start on its own line, or that record is joined to the fragment and
    lost when the corrupt line is skipped."""
    log = pv.DecisionLog(tmp_path)
    kw: Dict[str, Any] = dict(terminal_id="t", provider="p", edited=False, outcome=None)
    log.append("local", interrupt_id="a", decision="approve", **kw)
    with open(tmp_path / "local.jsonl", "a") as fh:
        fh.write('{"tenant_id":"local","decis')  # torn write, no newline
    log.append("local", interrupt_id="b", decision="deny", **kw)
    s = log.summary("local")
    assert [r["interrupt_id"] for r in s["recent"]] == ["a", "b"]
    assert s["by_decision"] == {"approve": 1, "deny": 1, "edit": 0}


@pytest.mark.parametrize(
    "bad",
    [
        {"interrupt_id": []},
        {"interrupt_id": {"x": 1}},
        {"interrupt_id": "ok", "provider": ["p"]},
        {"interrupt_id": "ok", "provider": {"p": 1}},
        {"interrupt_id": "ok", "terminal_id": ["t"]},
        {"interrupt_id": "ok", "outcome": {"o": 1}},
    ],
    ids=["list-id", "dict-id", "list-provider", "dict-provider", "list-terminal", "dict-outcome"],
)
def test_a_record_with_unusable_field_types_is_skipped(tmp_path: Path, bad: Dict[str, Any]) -> None:
    """A well-formed JSON line whose id or provider is not a string (or null) is a
    corrupt record: skip it and keep counting the valid ones, instead of failing
    the decisions route with a TypeError."""
    log = pv.DecisionLog(tmp_path)
    kw: Dict[str, Any] = dict(terminal_id="t", provider="p", edited=False, outcome=None)
    log.append("local", interrupt_id="a", decision="approve", **kw)
    with open(tmp_path / "local.jsonl", "a") as fh:
        fh.write(json.dumps({"tenant_id": "local", "decision": "deny", **bad}) + "\n")
    s = log.summary("local")
    assert s["total"] == 1 and [r["interrupt_id"] for r in s["recent"]] == ["a"]


def test_the_log_records_the_committed_decision_not_a_retry_s_request(
    preview_on: None, tmp_path: Path
) -> None:
    """Once a multi-key delivery has started, resume() delivers the pinned original
    decision even if a retry asks for another. The log must record what was
    committed (the outcome), not the retry's request."""
    from cli_agent_orchestrator.services.agui.handoff_approval import ApprovalDecision

    construct = AgentHandoffWithApproval(emitter=RecordingUiEmitter(), answer_delivery=None)
    app.state.approval_bridge = ApprovalBridge(construct=construct)
    try:
        itr = construct.on_provider_waiting("t-1", "kiro_cli", "Allow this action? [y/n/t]:")
        construct._delivery_decisions[itr.id] = ApprovalDecision.APPROVE  # a prefix was sent
        r = client.post(f"/agui/v1/interrupts/{itr.id}/resume", json={"decision": "deny"})
    finally:
        del app.state.approval_bridge
    assert r.status_code == 200 and r.json()["outcome"] == "approve"
    rec = json.loads((tmp_path / "decisions" / "local.jsonl").read_text())
    assert rec["decision"] == "approve" and rec["outcome"] == "approve"
    assert rec["edited"] is False


def test_a_resume_that_expiry_wins_logs_no_decision(
    preview_on: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    """If a concurrent expiry wins, the construct commits ``outcome="expired"``
    and no decision was delivered, so nothing is logged and no failed write is
    reported either."""
    construct = AgentHandoffWithApproval(emitter=RecordingUiEmitter(), answer_delivery=None)
    app.state.approval_bridge = ApprovalBridge(construct=construct)
    try:
        itr = construct.on_provider_waiting("t-1", "kiro_cli", "Allow this action? [y/n/t]:")

        async def _expiry_wins(interrupt_id, decision, edited_text=None):
            itr.resolved, itr.outcome = True, "expired"
            return itr

        monkeypatch.setattr(construct, "resume", _expiry_wins)
        with caplog.at_level("WARNING"):
            r = client.post(f"/agui/v1/interrupts/{itr.id}/resume", json={"decision": "approve"})
    finally:
        del app.state.approval_bridge
    assert r.status_code == 200 and r.json()["outcome"] == "expired"
    assert "decision log write failed" not in caplog.text
    assert not (tmp_path / "decisions" / "local.jsonl").exists()


def test_two_concurrent_contrary_first_resumes_log_the_one_committed_decision(
    preview_on: None, tmp_path: Path
) -> None:
    """Two first resumes race with contrary decisions. Both see the interrupt
    unresolved; the second joins the first's in-flight delivery, so one decision is
    committed. Each request may append a line, but every line records the committed
    decision, and the summary counts the interrupt once."""
    import threading

    entered, release = threading.Event(), threading.Event()

    class _HeldDelivery:
        def send_input(self, terminal_id: str, text: str, **kwargs: Any) -> None:
            entered.set()
            assert release.wait(10)

        def send_special_key(self, terminal_id: str, key: str) -> bool:
            entered.set()
            assert release.wait(10)
            return True

    construct = AgentHandoffWithApproval(
        emitter=RecordingUiEmitter(), answer_delivery=_HeldDelivery()
    )
    results: Dict[str, Any] = {}
    try:
        # One portal, so both requests run on the same event loop and the second
        # can join the first's in-flight delivery task. The app's lifespan installs
        # its own bridge, so this test's bridge is set after it has run.
        with TestClient(app, base_url="http://localhost") as c:
            app.state.approval_bridge = ApprovalBridge(construct=construct)
            itr = construct.on_provider_waiting("t-1", "kiro_cli", "Allow this action? [y/n/t]:")
            url = f"/agui/v1/interrupts/{itr.id}/resume"

            def _post(name: str, decision: str) -> None:
                results[name] = c.post(url, json={"decision": decision})

            first = threading.Thread(target=_post, args=("approve", "approve"))
            first.start()
            assert entered.wait(10), "the first delivery never started"
            second = threading.Thread(target=_post, args=("deny", "deny"))
            second.start()
            second.join(0.5)  # it is parked on the in-flight delivery, not finished
            assert second.is_alive()
            release.set()
            first.join(10)
            second.join(10)
    finally:
        release.set()
        if hasattr(app.state, "approval_bridge"):
            del app.state.approval_bridge
    assert {k: (r.status_code, r.json()["outcome"]) for k, r in results.items()} == {
        "approve": (200, "approve"),
        "deny": (200, "approve"),
    }
    lines = [
        json.loads(x) for x in (tmp_path / "decisions" / "local.jsonl").read_text().splitlines()
    ]
    assert lines and {r["decision"] for r in lines} == {"approve"}
    summary = pv.DecisionLog(tmp_path / "decisions").summary("local")
    assert summary["total"] == 1 and summary["by_decision"]["approve"] == 1
