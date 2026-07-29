"""HTTP-boundary tests for the Lane C operator-message + attachments routes.

The service suites prove what happens to a message; these prove the wire
says so — the 200-with-typed-outcome discipline, 422 for shape, 404/409
where those are the honest statuses, and the additive capability blocks
an old client simply ignores.
"""

from __future__ import annotations

import io
import uuid

import pytest

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.services import operator_message_service as oms

TERMINAL = "a1b2c3d4"
OP = str(uuid.uuid4())


def _result(**overrides):
    fields = dict(
        operation_id=OP,
        outcome="accepted",
        reason_code="delivered",
        detail="typed and submitted",
        replayed=False,
        record_state="posted",
    )
    fields.update(overrides)
    return oms.OperatorMessageResult(**fields)


class TestSubmitRoute:
    def test_a_typed_outcome_travels_as_200(self, client, monkeypatch):
        monkeypatch.setattr(oms, "submit_operator_message", lambda *a, **k: _result())
        response = client.post(
            f"/terminals/{TERMINAL}/operator-message",
            json={
                "operation_id": OP,
                "text": "hello",
                "attachments": [],
                "token_map": {},
                "expected_identity": {"terminal_id": TERMINAL},
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["outcome"] == "accepted"
        assert body["operation_id"] == OP
        assert body["record_state"] == "posted"

    def test_a_shape_error_is_422_not_a_typed_200(self, client, monkeypatch):
        def _invalid(*a, **k):
            raise oms.OperatorMessageRequestInvalid("operation_id must be a uuid")

        monkeypatch.setattr(oms, "submit_operator_message", _invalid)
        response = client.post(
            f"/terminals/{TERMINAL}/operator-message",
            json={"operation_id": "nope", "text": "hello", "expected_identity": {}},
        )
        assert response.status_code == 422

    def test_the_real_service_422s_a_bad_uuid(self, client):
        response = client.post(
            f"/terminals/{TERMINAL}/operator-message",
            json={"operation_id": "not-a-uuid", "text": "hello", "expected_identity": {}},
        )
        assert response.status_code == 422


class TestReconcileRoute:
    def test_a_typed_reconcile_travels_as_200(self, client, monkeypatch):
        monkeypatch.setattr(
            oms,
            "reconcile_operator_message",
            lambda operation_id: _result(
                outcome="refused", reason_code="owner-lost-before-write", record_state=None
            ),
        )
        response = client.get(f"/operator-message/{OP}")
        assert response.status_code == 200
        assert response.json()["reason_code"] == "owner-lost-before-write"

    def test_a_bad_uuid_is_422(self, client):
        response = client.get("/operator-message/not-a-uuid")
        assert response.status_code == 422


class TestAttachmentRoutes:
    def _png(self):
        import struct

        ihdr = struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", 4, 4) + bytes([8, 2, 0, 0, 0])
        return b"\x89PNG\r\n\x1a\n" + ihdr + b"\x00\x00\x00\x00"

    def test_upload_returns_201_with_the_ready_record(self, client, monkeypatch):
        monkeypatch.setattr(
            oms,
            "upload_attachment",
            lambda terminal_id, *, display_filename, content: {
                "attachment_id": "att-1",
                "state": "ready",
                "format": "png",
                "width": 4,
                "height": 4,
                "size_bytes": len(content),
                "display_filename": display_filename,
            },
        )
        response = client.post(
            f"/terminals/{TERMINAL}/attachments",
            files={"file": ("shot.png", io.BytesIO(self._png()), "image/png")},
        )
        assert response.status_code == 201
        body = response.json()["attachment"]
        assert body["state"] == "ready"
        assert body["display_filename"] == "shot.png"

    def test_upload_validation_failure_is_a_typed_422(self, client, monkeypatch):
        def _refuse(*a, **k):
            raise oms.AttachmentRefusal(
                422,
                "attachment-type-unsupported",
                "the uploaded content is not a valid image",
                record={"attachment_id": "att-x", "state": "failed"},
            )

        monkeypatch.setattr(oms, "upload_attachment", _refuse)
        response = client.post(
            f"/terminals/{TERMINAL}/attachments",
            files={"file": ("junk.png", io.BytesIO(b"junk"), "image/png")},
        )
        assert response.status_code == 422
        body = response.json()
        assert body["outcome"] == "refused"
        assert body["reason_code"] == "attachment-type-unsupported"
        assert body["attachment"]["state"] == "failed"

    def test_upload_to_an_unknown_terminal_is_404(self, client, monkeypatch):
        def _missing(*a, **k):
            raise LookupError("no terminal is known")

        monkeypatch.setattr(oms, "upload_attachment", _missing)
        response = client.post(
            f"/terminals/{TERMINAL}/attachments",
            files={"file": ("shot.png", io.BytesIO(self._png()), "image/png")},
        )
        assert response.status_code == 404

    def test_upload_without_a_file_is_a_shape_422(self, client):
        response = client.post(f"/terminals/{TERMINAL}/attachments")
        assert response.status_code == 422

    def test_list_returns_the_records(self, client, monkeypatch):
        monkeypatch.setattr(
            oms,
            "list_terminal_attachments",
            lambda terminal_id: [{"attachment_id": "att-1", "state": "ready"}],
        )
        response = client.get(f"/terminals/{TERMINAL}/attachments")
        assert response.status_code == 200
        assert response.json()["attachments"][0]["attachment_id"] == "att-1"

    def test_list_on_an_unknown_terminal_is_404(self, client, monkeypatch):
        def _missing(terminal_id):
            raise LookupError("no terminal is known")

        monkeypatch.setattr(oms, "list_terminal_attachments", _missing)
        assert client.get(f"/terminals/{TERMINAL}/attachments").status_code == 404

    def test_delete_returns_the_removed_record(self, client, monkeypatch):
        monkeypatch.setattr(
            oms,
            "delete_terminal_attachment",
            lambda terminal_id, attachment_id: {
                "attachment_id": attachment_id,
                "state": "removed",
            },
        )
        response = client.delete(f"/terminals/{TERMINAL}/attachments/att-1")
        assert response.status_code == 200
        assert response.json()["deleted"] is True
        assert response.json()["attachment"]["state"] == "removed"

    def test_delete_an_unknown_attachment_is_404(self, client, monkeypatch):
        def _missing(terminal_id, attachment_id):
            raise LookupError("no image attachment")

        monkeypatch.setattr(oms, "delete_terminal_attachment", _missing)
        assert client.delete(f"/terminals/{TERMINAL}/attachments/nope").status_code == 404

    def test_delete_a_submitted_attachment_is_a_typed_409(self, client, monkeypatch):
        def _conflict(terminal_id, attachment_id):
            raise oms.AttachmentRefusal(
                409,
                "attachment-not-ready",
                "submitted images are retained read-only for 24h",
            )

        monkeypatch.setattr(oms, "delete_terminal_attachment", _conflict)
        response = client.delete(f"/terminals/{TERMINAL}/attachments/att-1")
        assert response.status_code == 409
        body = response.json()
        assert body["outcome"] == "refused"
        assert body["reason_code"] == "attachment-not-ready"
