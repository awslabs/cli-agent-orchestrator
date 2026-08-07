"""HTTP surface for the issue tracker.

These tests exercise the boundary the dashboard and `conduct` actually talk
to: route ordering (a literal path must beat a parameterised one), the mapping
from a service refusal to an HTTP status a client can branch on, and PATCH
semantics where "field absent" and "field sent empty" mean different things.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import Base
from cli_agent_orchestrator.services import issue_tracker as tracker


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/tracker-api.db")
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(tracker, "SessionLocal", sessionmaker(bind=engine))
    yield
    engine.dispose()


@pytest.fixture
def project(client, tmp_path):
    repo = tmp_path / "cao-conductor"
    repo.mkdir()
    response = client.post(
        "/tracker/projects",
        json={
            "name": "CAO System",
            "id": "cao-system",
            "issue_prefix": "cond",
            "scopes": [
                {"kind": "path", "value": str(repo)},
                {"kind": "session", "value": "cao-p1-closure"},
            ],
        },
    )
    assert response.status_code == 201, response.text
    return {"body": response.json(), "repo": repo}


def _issue(client, **overrides):
    payload = {"project_id": "cao-system", "title": "a defect"}
    payload.update(overrides)
    response = client.post("/tracker/issues", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


class TestProjectRoutes:
    def test_a_created_project_is_listed(self, client, project):
        rows = client.get("/tracker/projects").json()
        assert [r["id"] for r in rows] == ["cao-system"]

    def test_project_detail_carries_scopes_and_counts(self, client, project):
        _issue(client)
        body = client.get("/tracker/projects/cao-system").json()
        assert body["counts"]["open"] == 1
        assert {s["kind"] for s in body["scopes"]} == {"path", "session"}

    def test_a_missing_project_is_404_not_400(self, client):
        assert client.get("/tracker/projects/nope").status_code == 404

    def test_a_duplicate_project_is_409(self, client, project):
        response = client.post("/tracker/projects", json={"name": "x", "id": "cao-system"})
        assert response.status_code == 409

    def test_an_invalid_slug_is_400(self, client):
        response = client.post("/tracker/projects", json={"name": "x", "id": "Not A Slug"})
        assert response.status_code == 400

    def test_archiving_hides_the_project_from_the_default_list(self, client, project):
        client.patch("/tracker/projects/cao-system", json={"status": "archived"})
        assert client.get("/tracker/projects").json() == []
        assert len(client.get("/tracker/projects?include_archived=true").json()) == 1

    def test_deleting_a_project_with_issues_is_409_without_force(self, client, project):
        _issue(client)
        assert client.delete("/tracker/projects/cao-system").status_code == 409
        assert client.delete("/tracker/projects/cao-system?force=true").status_code == 200


class TestResolveRouting:
    def test_resolve_is_not_swallowed_by_the_project_id_route(self, client, project):
        # `resolve` is a legal slug. If the parameterised route were declared
        # first, this would 404 as "no such project: resolve" and the CLI's
        # only way to ask "where would this file?" would silently break.
        body = client.get("/tracker/projects/resolve", params={"session": "cao-p1-closure"}).json()
        assert body == {
            "project_id": "cao-system",
            "matched_by": "session",
            "matched_value": "cao-p1-closure",
        }

    def test_resolve_reports_no_match_as_a_null_project_not_an_error(
        self, client, project, tmp_path
    ):
        body = client.get(
            "/tracker/projects/resolve", params={"cwd": str(tmp_path / "elsewhere")}
        ).json()
        assert body["project_id"] is None

    def test_resolve_with_an_unknown_explicit_project_is_404(self, client):
        assert (
            client.get("/tracker/projects/resolve", params={"project": "nope"}).status_code == 404
        )


class TestScopeRoutes:
    def test_a_scope_claimed_by_another_project_is_409(self, client, project):
        client.post("/tracker/projects", json={"name": "Other", "id": "other"})
        response = client.post(
            "/tracker/projects/other/scopes",
            json={"kind": "path", "value": str(project["repo"])},
        )
        assert response.status_code == 409

    def test_a_scope_can_be_removed(self, client, project):
        detail = client.get("/tracker/projects/cao-system").json()
        scope_id = detail["scopes"][0]["id"]
        assert client.delete(f"/tracker/projects/cao-system/scopes/{scope_id}").status_code == 200
        assert len(client.get("/tracker/projects/cao-system").json()["scopes"]) == 1

    def test_removing_another_projects_scope_is_404(self, client, project):
        client.post("/tracker/projects", json={"name": "Other", "id": "other"})
        scope_id = client.get("/tracker/projects/cao-system").json()["scopes"][0]["id"]
        assert client.delete(f"/tracker/projects/other/scopes/{scope_id}").status_code == 404


class TestIssueRoutes:
    def test_filing_by_cwd_resolves_the_project(self, client, project):
        nested = project["repo"] / "conduct"
        nested.mkdir()
        body = _issue(client, project_id=None, cwd=str(nested))
        assert (body["project_id"], body["resolved_by"]) == ("cao-system", "path")

    def test_an_unresolvable_filing_site_is_422(self, client, project, tmp_path):
        response = client.post(
            "/tracker/issues", json={"title": "orphan", "cwd": str(tmp_path / "nowhere")}
        )
        assert response.status_code == 422

    def test_stats_is_not_swallowed_by_the_issue_key_route(self, client, project):
        _issue(client, severity="P1")
        body = client.get("/tracker/issues/stats", params={"project_id": "cao-system"}).json()
        assert body["by_severity"]["P1"] == 1

    def test_an_unknown_issue_is_404(self, client, project):
        assert client.get("/tracker/issues/cond-9999").status_code == 404

    def test_an_invalid_severity_is_400(self, client, project):
        response = client.post(
            "/tracker/issues", json={"project_id": "cao-system", "title": "x", "severity": "P9"}
        )
        assert response.status_code == 400

    def test_listing_filters_compose(self, client, project):
        _issue(client, title="one", severity="P1", component="conduct")
        _issue(client, title="two", severity="P3", component="conduct")
        _issue(client, title="three", severity="P1", component="fork")
        body = client.get(
            "/tracker/issues",
            params={"project_id": "cao-system", "severity": "P1", "component": "conduct"},
        ).json()
        assert [i["title"] for i in body["issues"]] == ["one"]

    def test_repeated_status_params_are_an_or(self, client, project):
        a = _issue(client, title="one")
        _issue(client, title="two")
        client.patch(f"/tracker/issues/{a['key']}", json={"status": "blocked"})
        body = client.get("/tracker/issues?status=blocked&status=open").json()
        assert body["total"] == 2


class TestPatchSemantics:
    def test_an_absent_field_is_untouched(self, client, project):
        issue = _issue(client, assignee="terra", severity="P2")
        body = client.patch(
            f"/tracker/issues/{issue['key']}", json={"status": "in-progress"}
        ).json()
        assert (body["assignee"], body["severity"]) == ("terra", "P2")

    def test_an_empty_string_clears_a_field(self, client, project):
        # This is the distinction that lets the dashboard unassign an issue.
        # Without model_fields_set, "" and "absent" would be indistinguishable.
        issue = _issue(client, assignee="terra")
        body = client.patch(f"/tracker/issues/{issue['key']}", json={"assignee": ""}).json()
        assert body["assignee"] is None

    def test_labels_are_replaced_wholesale(self, client, project):
        issue = _issue(client, labels=["a", "b"])
        body = client.patch(f"/tracker/issues/{issue['key']}", json={"labels": ["c"]}).json()
        assert body["labels"] == ["c"]

    def test_the_actor_is_recorded_and_not_treated_as_a_field(self, client, project):
        issue = _issue(client)
        client.patch(
            f"/tracker/issues/{issue['key']}", json={"status": "blocked", "actor": "colin"}
        )
        events = client.get(f"/tracker/issues/{issue['key']}").json()["events"]
        field_events = [e for e in events if e["kind"] == "field"]
        assert [(e["field"], e["actor"]) for e in field_events] == [("status", "colin")]


class TestCommentsAndLinksRoutes:
    def test_a_comment_round_trips(self, client, project):
        issue = _issue(client)
        assert (
            client.post(
                f"/tracker/issues/{issue['key']}/comments",
                json={"body": "reproduced", "author": "colin"},
            ).status_code
            == 201
        )
        detail = client.get(f"/tracker/issues/{issue['key']}").json()
        assert [c["body"] for c in detail["comments"]] == ["reproduced"]

    def test_an_empty_comment_is_400(self, client, project):
        issue = _issue(client)
        response = client.post(f"/tracker/issues/{issue['key']}/comments", json={"body": "  "})
        assert response.status_code == 400

    def test_a_link_round_trips_and_can_be_removed(self, client, project):
        a, b = _issue(client, title="a"), _issue(client, title="b")
        created = client.post(
            f"/tracker/issues/{a['key']}/links", json={"to_key": b["key"], "kind": "blocks"}
        )
        assert created.status_code == 201
        link_id = created.json()["id"]
        assert client.delete(f"/tracker/issues/{a['key']}/links/{link_id}").status_code == 200
        assert client.get(f"/tracker/issues/{b['key']}").json()["links"] == []

    def test_an_unknown_link_kind_is_400(self, client, project):
        a, b = _issue(client, title="a"), _issue(client, title="b")
        response = client.post(
            f"/tracker/issues/{a['key']}/links", json={"to_key": b["key"], "kind": "supersedes"}
        )
        assert response.status_code == 400


class TestVocabularyAndExport:
    def test_the_vocabulary_is_served_rather_than_duplicated_client_side(self, client):
        body = client.get("/tracker/vocabulary").json()
        assert body["statuses"] == list(tracker.STATUSES)
        assert body["severities"] == list(tracker.SEVERITIES)

    def test_the_export_returns_markdown(self, client, project):
        _issue(client, title="event-mirror traceback", severity="P2")
        response = client.get("/tracker/projects/cao-system/export")
        assert response.headers["content-type"].startswith("text/markdown")
        assert "## cond-0001 — [P2] event-mirror traceback" in response.text


class TestUnknownFieldsAreRefused:
    """An unknown field is a 422 naming it, never a silently ignored 200.

    Pydantic drops what it does not recognise by default, so
    `PATCH {"project_id": "other"}` looked like it moved an issue between
    projects and did nothing at all — a 200 for an operation that never
    happened.
    """

    def test_patching_a_non_editable_field_is_refused(self, client, project):
        issue = _issue(client)
        response = client.patch(f"/tracker/issues/{issue['key']}", json={"project_id": "other"})
        assert response.status_code == 422
        assert "project_id" in response.text

    def test_a_misspelled_field_is_refused(self, client, project):
        issue = _issue(client)
        assert (
            client.patch(f"/tracker/issues/{issue['key']}", json={"assigne": "x"}).status_code
            == 422
        )

    def test_an_unknown_field_on_create_is_refused(self, client, project):
        response = client.post(
            "/tracker/issues", json={"project_id": "cao-system", "title": "x", "sevrity": "P1"}
        )
        assert response.status_code == 422

    def test_an_out_of_range_limit_is_refused_not_truncated(self, client, project):
        # Silently returning 500 of 100000 requested rows reads as "that was
        # everything".
        assert client.get("/tracker/issues?limit=100000").status_code == 422

    def test_a_prefix_already_in_use_is_409(self, client, project):
        response = client.post(
            "/tracker/projects", json={"name": "Other", "id": "other", "issue_prefix": "cond"}
        )
        assert response.status_code == 409
        assert "cao-system" in response.json()["detail"]
