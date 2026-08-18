"""GET /communications — bounded catalog reader (design §7).

The route reads a fixed, conductor-owned projection and resolves every
identifier through the published index.  These tests publish real-looking
indexes and content objects into a scratch conductor state root and assert the
confinement, paging, digest, quarantine, and coverage contracts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Dict, List
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.services import communications_catalog as catalog


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _doc(
    attachment_id: str,
    content: bytes = b"hello",
    role: str = "body",
    state: str = "present",
    quarantine: bool = False,
) -> Dict:
    blob_id = _sha256(content)
    entry = {
        "attachment_id": attachment_id,
        "document_id": f"doc-{attachment_id}",
        "role": role,
        "display_name": "note.txt",
        "media_type": "text/plain",
        "sha256": blob_id,
        "byte_size": len(content),
        "blob_id": blob_id,
        "content_state": state,
        "capture_kind": "report-body",
        "redaction_applied": False,
    }
    if quarantine:
        entry["content_state"] = "content-quarantined"
        entry["quarantine"] = {
            "reason": "operator-request",
            "actor": "operator",
            "quarantined_at": "2026-01-01T00:00:00Z",
            "receipt_sha256": "a" * 64,
        }
    return entry


def _comm(
    communication_id: str,
    task_occurrence_id: str,
    recorded_at: str,
    body: Dict | None = None,
    attachments: List[Dict] | None = None,
    **overrides: object,
) -> Dict:
    entry = {
        "communication_id": communication_id,
        "project_id": "project",
        "session_id": "session",
        "lane_id": "lane",
        "task_occurrence_id": task_occurrence_id,
        "goal_version": "1",
        "kind": "report",
        "report_scope": "task",
        "authored_by_type": "agent",
        "authored_by_id": "agent-1",
        "authored_at": recorded_at,
        "recorded_at": recorded_at,
        "title": "title",
        "delivery_state": "delivered",
        "visibility": "internal",
        "request_key": None,
        "supersedes_communication_id": None,
        "superseded_by": None,
        "body": body,
        "documents": attachments or [],
    }
    entry.update(overrides)
    return entry


def _publish(
    root: str,
    project: str,
    communications: List[Dict],
    blobs: Dict[str, bytes] | None = None,
    coverage: str = "complete",
) -> str:
    directory = os.path.join(root, project)
    os.makedirs(directory, exist_ok=True)
    content_dir = os.path.join(directory, "communications", "content")
    os.makedirs(content_dir, exist_ok=True)

    content_objects: Dict[str, Dict] = {}
    for blob_id, data in (blobs or {}).items():
        path = os.path.join(content_dir, blob_id)
        with open(path, "wb") as fh:
            fh.write(data)
        content_objects[blob_id] = {
            "byte_size": len(data),
            "sha256": blob_id,
            "content_state": "present",
        }

    index = {
        "schema": "cao-communications-index-v1",
        "envelope": {
            "project": project,
            "produced_at": "2026-08-18T00:00:00Z",
            "valid_until": "2026-08-18T01:00:00Z",
            "producer": "cao-conductor-catalog-projection",
            "producer_version": 1,
            "coverage": coverage,
            "content_object_prefix": "communications/content/",
            "communications_count": len(communications),
            "content_objects_count": len(content_objects),
        },
        "communications": communications,
        "content_objects": content_objects,
    }
    path = os.path.join(directory, "communications.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(index, fh)
    return directory


@pytest.fixture
def root(tmp_path):
    """A scratch conductor state root, patched in for the whole request."""
    scratch = tmp_path / "cao-conductor"
    scratch.mkdir()
    with patch.object(catalog, "catalog_root", return_value=str(scratch)):
        yield str(scratch)


class TestFixedLocation:
    """The route reads one place, and filesystem overrides are refused."""

    def test_the_list_route_has_only_expected_query_params(self):
        route = next(r for r in app.routes if getattr(r, "path", None) == "/communications")
        caller_supplied = [p.name for p in route.dependant.query_params]
        assert set(caller_supplied) == {"task_occurrence_id", "cursor"}

    def test_the_location_is_not_configurable_from_the_environment(self, monkeypatch):
        for name in (
            "CAO_STATE_ROOT",
            "CAO_COMMUNICATIONS_ROOT",
            "CONDUCTOR_STATE_ROOT",
            "XDG_STATE_HOME",
        ):
            monkeypatch.setenv(name, "/tmp/attacker-controlled")
        assert catalog.catalog_root() == os.path.expanduser("~/.local/state/cao-conductor")

    def test_path_root_and_project_dir_parameters_are_refused(self, client, root):
        _publish(root, "project", [])
        for param in ("path", "root", "project_dir"):
            response = client.get(f"/communications?task_occurrence_id=t1&{param}=/etc/passwd")
            assert response.status_code == 400
            assert param in response.json()["detail"]

    def test_detail_route_also_refuses_filesystem_override_query_params(self, client, root):
        comm = _comm("c1", "t1", "2026-08-18T00:00:00Z")
        _publish(root, "project", [comm])
        response = client.get("/communications/c1?root=/etc")
        assert response.status_code == 400


class TestListEndpoint:
    def test_happy_path_returns_metadata_and_never_bodies(self, client, root):
        body = _doc("att-1", b"body-content")
        comm = _comm("c1", "t1", "2026-08-18T00:00:00Z", body=body)
        _publish(root, "project", [comm], blobs={body["blob_id"]: b"body-content"})

        response = client.get("/communications?task_occurrence_id=t1")
        assert response.status_code == 200
        payload = response.json()
        assert payload["schema"] == "cao-communications-index-v1"
        assert payload["coverage"] == "complete"
        assert payload["reasons"] == []
        assert payload["total"] == 1
        assert payload["next_cursor"] is None
        assert len(payload["communications"]) == 1
        item = payload["communications"][0]
        assert item["communication_id"] == "c1"
        assert item["task_occurrence_id"] == "t1"
        assert item["body"]["attachment_id"] == "att-1"
        assert "content" not in item
        assert "body-content" not in response.text

    def test_unknown_task_occurrence_is_complete_vacuously(self, client, root):
        comm = _comm("c1", "t1", "2026-08-18T00:00:00Z")
        _publish(root, "project", [comm])
        response = client.get("/communications?task_occurrence_id=unknown")
        assert response.status_code == 200
        payload = response.json()
        assert payload["coverage"] == "complete"
        assert payload["communications"] == []
        assert payload["total"] == 0

    def test_malformed_index_is_a_reason_not_a_500(self, client, root):
        directory = os.path.join(root, "project")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "communications.json"), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        response = client.get("/communications?task_occurrence_id=t1")
        assert response.status_code == 200
        payload = response.json()
        assert payload["coverage"] == "partial"
        assert any(r["reason"] == "malformed" for r in payload["reasons"])

    def test_coverage_unavailable_when_root_missing(self, client, root):
        missing_root = os.path.join(root, "does-not-exist")
        with patch.object(catalog, "catalog_root", return_value=missing_root):
            response = client.get("/communications?task_occurrence_id=t1")
        assert response.status_code == 200
        assert response.json()["coverage"] == "unavailable"

    def test_coverage_partial_when_index_malformed(self, client, root):
        comm = _comm("c1", "t1", "2026-08-18T00:00:00Z")
        _publish(root, "good", [comm])
        bad_dir = os.path.join(root, "bad")
        os.makedirs(bad_dir, exist_ok=True)
        with open(os.path.join(bad_dir, "communications.json"), "w", encoding="utf-8") as fh:
            fh.write("broken")
        response = client.get("/communications?task_occurrence_id=t1")
        assert response.status_code == 200
        payload = response.json()
        assert payload["coverage"] == "partial"
        assert any(r["reason"] == "malformed" for r in payload["reasons"])

    def test_coverage_truncated_at_page_cap(self, client, root):
        many = [
            _comm(f"c{i:03d}", "t1", f"2026-08-18T00:00:{i:02d}.000000Z")
            for i in range(catalog.PAGE_SIZE + 3)
        ]
        _publish(root, "big", many)
        response = client.get("/communications?task_occurrence_id=t1")
        assert response.status_code == 200
        payload = response.json()
        assert payload["coverage"] == "truncated"
        assert len(payload["communications"]) == catalog.PAGE_SIZE
        assert payload["next_cursor"] is not None

    def test_coverage_complete_for_healthy_single_match(self, client, root):
        _publish(root, "clean", [_comm("clean-1", "t2", "2026-08-18T00:00:00Z")])
        response = client.get("/communications?task_occurrence_id=t2")
        assert response.status_code == 200
        assert response.json()["coverage"] == "complete"


class TestCursorPaging:
    def test_keyset_cursor_pages_across_total_order(self, client, root):
        comms = [
            _comm("c1", "t1", "2026-08-18T00:00:03.000000Z"),
            _comm("c2", "t1", "2026-08-18T00:00:02.500000Z"),
            _comm("c3", "t1", "2026-08-18T00:00:02.000000Z"),
            _comm("c4", "t1", "2026-08-18T00:00:01.000000Z"),
        ]
        _publish(root, "project", comms)

        with patch.object(catalog, "PAGE_SIZE", 2):
            response = client.get("/communications?task_occurrence_id=t1")
        assert response.status_code == 200
        payload = response.json()
        assert [c["communication_id"] for c in payload["communications"]] == ["c1", "c2"]
        cursor = payload["next_cursor"]
        assert cursor is not None

        with patch.object(catalog, "PAGE_SIZE", 2):
            response = client.get(f"/communications?task_occurrence_id=t1&cursor={cursor}")
        assert response.status_code == 200
        payload = response.json()
        assert [c["communication_id"] for c in payload["communications"]] == ["c3", "c4"]
        assert payload["next_cursor"] is None

    def test_cursor_normalises_whole_second_stamps(self, client, root):
        # Legacy whole-second stamps must order after microsecond stamps with the
        # same wall-clock second, just as the publisher's CATALOG_RECENCY_ORDER does.
        comms = [
            _comm("whole", "t1", "2026-08-18T00:00:01Z"),
            _comm("micro", "t1", "2026-08-18T00:00:01.123456Z"),
        ]
        _publish(root, "project", comms)
        response = client.get("/communications?task_occurrence_id=t1")
        ids = [c["communication_id"] for c in response.json()["communications"]]
        assert ids == ["micro", "whole"]

    def test_cursor_survives_republication(self, client, root):
        # First publication has the newest row; a client holds a cursor to older rows.
        first = [
            _comm("new", "t1", "2026-08-18T00:00:02.000000Z"),
            _comm("old", "t1", "2026-08-18T00:00:01.000000Z"),
        ]
        _publish(root, "project", first)
        with patch.object(catalog, "PAGE_SIZE", 1):
            response = client.get("/communications?task_occurrence_id=t1")
        cursor = response.json()["next_cursor"]
        assert cursor is not None

        # Republication inserts a newer row at the front.  An offset cursor would
        # shift; a keyset cursor over the total order still points at ``old``.
        second = [
            _comm("newer", "t1", "2026-08-18T00:00:03.000000Z"),
            _comm("new", "t1", "2026-08-18T00:00:02.000000Z"),
            _comm("old", "t1", "2026-08-18T00:00:01.000000Z"),
        ]
        _publish(root, "project", second)
        with patch.object(catalog, "PAGE_SIZE", 1):
            response = client.get(f"/communications?task_occurrence_id=t1&cursor={cursor}")
        assert response.status_code == 200
        payload = response.json()
        assert [c["communication_id"] for c in payload["communications"]] == ["old"]
        assert payload["next_cursor"] is None


class TestDetailEndpoint:
    def test_happy_path_returns_content_and_no_store_header(self, client, root):
        body = _doc("att-1", b"the-body")
        comm = _comm("c1", "t1", "2026-08-18T00:00:00Z", body=body)
        _publish(root, "project", [comm], blobs={body["blob_id"]: b"the-body"})

        response = client.get("/communications/c1")
        assert response.status_code == 200
        assert response.headers.get("cache-control") == "no-store"
        payload = response.json()
        assert payload["communication"]["communication_id"] == "c1"
        assert payload["content"] == "the-body"
        assert payload["reason"] is None

    def test_unknown_communication_id_returns_404_with_bounded_reason(self, client, root):
        _publish(root, "project", [_comm("c1", "t1", "2026-08-18T00:00:00Z")])
        response = client.get("/communications/unknown")
        assert response.status_code == 404
        detail = response.json()["detail"]
        assert "not found" in detail.lower()
        assert root not in response.text

    def test_digest_mismatch_is_refused_not_served(self, client, root):
        body = _doc("att-1", b"the-body")
        comm = _comm("c1", "t1", "2026-08-18T00:00:00Z", body=body)
        # Write wrong bytes that still match the advertised size.
        wrong = b"x" * len(b"the-body")
        _publish(root, "project", [comm], blobs={body["blob_id"]: wrong})

        response = client.get("/communications/c1")
        assert response.status_code == 503
        assert "the-body" not in response.text
        assert "x" * 9 not in response.text

    def test_quarantined_body_returns_tombstone_and_no_bytes(self, client, root):
        body = _doc("att-1", b"the-body", quarantine=True)
        comm = _comm("c1", "t1", "2026-08-18T00:00:00Z", body=body)
        _publish(root, "project", [comm])
        # Do NOT write the content object: a quarantined blob must not be on disk.

        response = client.get("/communications/c1")
        assert response.status_code == 200
        payload = response.json()
        assert payload["content"] is None
        assert payload["reason"] == "content-quarantined"
        assert payload["communication"]["body"]["content_state"] == "content-quarantined"
        assert payload["communication"]["body"]["quarantine"]["reason"] == "operator-request"
        assert "the-body" not in response.text


class TestAttachmentEndpoint:
    def test_happy_path_returns_attachment_content(self, client, root):
        attach = _doc("att-2", b"attachment-content", role="attachment")
        comm = _comm("c1", "t1", "2026-08-18T00:00:00Z", attachments=[attach])
        _publish(root, "project", [comm], blobs={attach["blob_id"]: b"attachment-content"})

        response = client.get("/communications/attachments/att-2")
        assert response.status_code == 200
        assert response.headers.get("cache-control") == "no-store"
        payload = response.json()
        assert payload["document"]["attachment_id"] == "att-2"
        assert payload["content"] == "attachment-content"

    def test_unknown_attachment_id_returns_404(self, client, root):
        _publish(root, "project", [_comm("c1", "t1", "2026-08-18T00:00:00Z")])
        response = client.get("/communications/attachments/unknown")
        assert response.status_code == 404

    def test_symlinked_content_object_is_refused(self, client, root, tmp_path):
        body = _doc("att-1", b"secret")
        comm = _comm("c1", "t1", "2026-08-18T00:00:00Z", body=body)
        _publish(root, "project", [comm], blobs={body["blob_id"]: b"secret"})

        content_dir = os.path.join(root, "project", "communications", "content")
        target = tmp_path / "leaked.txt"
        target.write_bytes(b"secret")
        object_path = os.path.join(content_dir, body["blob_id"])
        os.remove(object_path)
        os.symlink(str(target), object_path)

        response = client.get("/communications/c1")
        assert response.status_code == 200
        payload = response.json()
        assert payload["content"] is None
        assert payload["reason"] in ("symlink-refused", "outside-root")
        assert "secret" not in response.text


class TestConfinementAndBounds:
    def test_invalid_identifiers_return_400(self, client, root):
        _publish(root, "project", [_comm("c1", "t1", "2026-08-18T00:00:00Z")])
        for bad in ("comm with space", "bad@id"):
            encoded = bad.replace(" ", "%20")
            response = client.get(f"/communications/{encoded}")
            assert response.status_code == 400, bad

    def test_reasons_never_echo_paths_or_content_excerpts(self, client, root):
        secret = b"SECRET-EXCERPT-THAT-MUST-NOT-LEAK"
        body = _doc("att-1", secret)
        comm = _comm("c1", "t1", "2026-08-18T00:00:00Z", body=body)
        _publish(root, "project", [comm], blobs={body["blob_id"]: secret})

        # Force an unreadable index so the reason path is exercised.
        bad_dir = os.path.join(root, "bad")
        os.makedirs(bad_dir, exist_ok=True)
        with open(os.path.join(bad_dir, "communications.json"), "w", encoding="utf-8") as fh:
            fh.write("NOT-JSON")

        response = client.get("/communications?task_occurrence_id=t1")
        text = response.text
        assert root not in text
        assert "SECRET-EXCERPT" not in text
        assert "NOT-JSON" not in text

    def test_project_fan_out_is_bounded(self, client, root):
        for i in range(catalog.MAX_PROJECTS + 2):
            _publish(root, f"p{i:03d}", [_comm(f"c{i}", "t1", "2026-08-18T00:00:00Z")])
        response = client.get("/communications?task_occurrence_id=t1")
        assert response.status_code == 200
        payload = response.json()
        assert payload["coverage"] == "truncated"
        assert any(r["reason"] == "project-limit" for r in payload["reasons"])


class TestReadScope:
    def test_routes_declare_a_scope_dependency(self):
        for path in ("/communications", "/communications/{communication_id}"):
            route = next(r for r in app.routes if getattr(r, "path", None) == path)
            stack = list(route.dependant.dependencies)
            names = []
            while stack:
                dep = stack.pop()
                call = getattr(dep, "call", None)
                if call is not None:
                    names.append(getattr(call, "__qualname__", ""))
                stack.extend(dep.dependencies)
            assert any("require_any_scope" in name for name in names), path

    def test_routes_are_readable_with_no_auth_configured(self, client, root):
        comm = _comm("c1", "t1", "2026-08-18T00:00:00Z")
        _publish(root, "project", [comm])
        assert client.get("/communications?task_occurrence_id=t1").status_code == 200
        assert client.get("/communications/c1").status_code == 200
