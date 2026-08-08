"""HTTP surface for feature requests (the typed feature aliases, D4).

The feature endpoints share storage with issues but enforce a ``kind`` guard:
an issue key is not a feature and vice versa. These tests pin that boundary,
the CRUD round-trips, comment/link ownership checks, and the markdown export —
behavior the project/issue API tests do not reach.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import Base
from cli_agent_orchestrator.services import issue_tracker as tracker


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/feat-api.db")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)
    monkeypatch.setattr(tracker, "SessionLocal", session_factory)
    # remove_feature_link's ownership check imports SessionLocal directly from
    # the database module; patch both so the test DB is authoritative.
    import cli_agent_orchestrator.clients.database as dbmod

    monkeypatch.setattr(dbmod, "SessionLocal", session_factory)
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
            "scopes": [{"kind": "path", "value": str(repo)}],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _feature(client, **overrides):
    payload = {"project_id": "cao-system", "title": "ship dark mode"}
    payload.update(overrides)
    response = client.post("/tracker/features", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


class TestFeatureRoutes:
    def test_create_returns_201_with_key(self, client, project):
        body = _feature(client, severity="P2", component="ui")
        assert body["key"].startswith("cond-")
        assert body["kind"] == "feature"
        assert body["severity"] == "P2"

    def test_unknown_field_on_create_is_refused(self, client, project):
        response = client.post(
            "/tracker/features",
            json={"project_id": "cao-system", "title": "x", "bogus": 1},
        )
        assert response.status_code == 422

    def test_list_filters_by_severity(self, client, project):
        _feature(client, severity="P1")
        _feature(client, title="second", severity="P3")
        response = client.get("/tracker/features?project_id=cao-system&severity=P1")
        assert response.status_code == 200
        page = response.json()
        assert page["total"] == 1
        assert page["issues"][0]["severity"] == "P1"

    def test_stats_reports_feature_counts(self, client, project):
        _feature(client, severity="P1")
        response = client.get("/tracker/features/stats?project_id=cao-system")
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 1
        assert body["by_severity"]["P1"] == 1


class TestFeatureKindGuards:
    def test_get_feature_on_an_issue_is_404(self, client, project):
        issue = client.post(
            "/tracker/issues", json={"project_id": "cao-system", "title": "a defect"}
        ).json()
        response = client.get(f"/tracker/features/{issue['key']}")
        assert response.status_code == 404

    def test_delete_feature_on_an_issue_is_404(self, client, project):
        issue = client.post(
            "/tracker/issues", json={"project_id": "cao-system", "title": "a defect"}
        ).json()
        response = client.delete(f"/tracker/features/{issue['key']}")
        assert response.status_code == 404


class TestFeatureUpdatePatch:
    def test_only_set_fields_are_applied(self, client, project):
        feature = _feature(client)
        response = client.patch(
            f"/tracker/features/{feature['key']}",
            json={"title": "renamed", "actor": "colin"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["title"] == "renamed"
        # severity was absent from the patch -> unchanged.
        assert body["severity"] == "unset"

    def test_close_via_status_and_resolution(self, client, project):
        feature = _feature(client)
        response = client.patch(
            f"/tracker/features/{feature['key']}",
            json={"status": "closed", "resolution": "shipped"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "closed"


class TestFeatureCommentsAndLinks:
    def test_comment_round_trip(self, client, project):
        feature = _feature(client)
        created = client.post(
            f"/tracker/features/{feature['key']}/comments",
            json={"body": "looks good", "author": "reviewer"},
        )
        assert created.status_code == 201
        comment_id = created.json()["id"]
        deleted = client.delete(f"/tracker/features/{feature['key']}/comments/{comment_id}")
        assert deleted.status_code == 200

    def test_link_round_trip_and_ownership(self, client, project):
        a = _feature(client, title="a")
        b = _feature(client, title="b")
        created = client.post(
            f"/tracker/features/{a['key']}/links",
            json={"to_key": b["key"], "kind": "relates", "actor": "colin"},
        )
        assert created.status_code == 201
        link_id = created.json()["id"]
        # Removing via the correct feature succeeds.
        ok = client.delete(f"/tracker/features/{a['key']}/links/{link_id}")
        assert ok.status_code == 200

    def test_removing_a_link_owned_by_another_feature_is_404(self, client, project):
        a = _feature(client, title="a")
        b = _feature(client, title="b")
        c = _feature(client, title="c")  # uninvolved in the a->b link
        created = client.post(
            f"/tracker/features/{a['key']}/links",
            json={"to_key": b["key"], "kind": "relates"},
        )
        link_id = created.json()["id"]
        # c is neither endpoint of the link, so the ownership guard refuses.
        response = client.delete(f"/tracker/features/{c['key']}/links/{link_id}")
        assert response.status_code == 404


class TestFeatureExport:
    def test_export_returns_markdown(self, client, project):
        _feature(client, title="ship dark mode")
        response = client.get("/tracker/projects/cao-system/features/export?open_only=false")
        assert response.status_code == 200
        assert "text/markdown" in response.headers["content-type"]
        assert "ship dark mode" in response.text
