"""Tests for the §8.4 image-attachment store: content validation, the
staging state machine, manifest discipline, binding, and the sweep.

Every test redirects ``image_attachments.attachments_root`` and
``manifest_path`` into a tmp directory; all derived paths (lock,
quarantine, staged files) follow from them.
"""

import json
import os
import stat
import struct
from datetime import datetime, timedelta, timezone

import pytest

from cli_agent_orchestrator.services import image_attachments
from cli_agent_orchestrator.services.image_attachments import (
    REASON_ATTACHMENT_NOT_READY,
    REASON_ATTACHMENT_TOO_LARGE,
    REASON_ATTACHMENT_TYPE_UNSUPPORTED,
    REASON_ATTACHMENT_UNKNOWN,
)

TERMINAL = "a1b2c3d4"
ALLOWED_PNG_ONLY = frozenset({"png"})
ALLOWED_ALL = frozenset({"png", "jpeg", "gif", "webp"})


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "attachments"
    manifest = tmp_path / "attachments.json"
    monkeypatch.setattr(image_attachments, "attachments_root", lambda: root)
    monkeypatch.setattr(image_attachments, "manifest_path", lambda: manifest)
    return tmp_path


# --- Minimal image fixtures (structure the decoders actually parse) ---------


def png_bytes(width=120, height=80):
    ihdr = (
        struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height) + bytes([8, 2, 0, 0, 0])
    )
    return b"\x89PNG\r\n\x1a\n" + ihdr + b"\x00\x00\x00\x00"


def gif_bytes(width=64, height=48):
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00\x00\x00"


def jpeg_bytes(width=640, height=480):
    app0 = b"\xff\xe0" + struct.pack(">H", 4) + b"\x00\x00"
    sof0 = (
        b"\xff\xc0"
        + struct.pack(">H", 9)
        + b"\x08"
        + struct.pack(">H", height)
        + struct.pack(">H", width)
        + b"\x00"
    )
    return b"\xff\xd8" + app0 + sof0


def webp_bytes(width=300, height=200):
    payload = (
        b"\x00\x00\x00\x00" + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    )
    return (
        b"RIFF"
        + (4 + 8 + len(payload)).to_bytes(4, "little")
        + b"WEBP"
        + b"VP8X"
        + len(payload).to_bytes(4, "little")
        + payload
    )


def _aged(ts: str, **delta) -> str:
    return (
        (datetime.fromisoformat(ts.replace("Z", "+00:00")) - timedelta(**delta))
        .isoformat()
        .replace("+00:00", "Z")
    )


def _seed_manifest(tmp_path, records):
    (tmp_path / "attachments.json").write_text(
        json.dumps({"schema_version": 1, "attachments": records})
    )


def _ready_record(attachment_id="att-1", terminal=TERMINAL, **overrides):
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    record = {
        "attachment_id": attachment_id,
        "terminal_id": terminal,
        "state": "ready",
        "format": "png",
        "content_type": "image/png",
        "width": 120,
        "height": 80,
        "size_bytes": 33,
        "sha256": "0" * 64,
        "display_filename": "shot.png",
        "staged_path": f"attachments/{terminal}/{attachment_id}.png",
        "bound_operation_id": None,
        "error": None,
        "created_at": now,
        "updated_at": now,
    }
    record.update(overrides)
    return record


class TestContentValidation:
    def test_png_decodes_dimensions(self):
        assert image_attachments.sniff_image(png_bytes(120, 80)) == ("png", 120, 80)

    def test_gif_decodes_dimensions(self):
        assert image_attachments.sniff_image(gif_bytes(64, 48)) == ("gif", 64, 48)

    def test_jpeg_decodes_dimensions_via_sof_scan(self):
        assert image_attachments.sniff_image(jpeg_bytes(640, 480)) == ("jpeg", 640, 480)

    def test_webp_decodes_dimensions_via_vp8x(self):
        assert image_attachments.sniff_image(webp_bytes(300, 200)) == ("webp", 300, 200)

    def test_png_bad_signature_rejected(self):
        data = b"\x89PNG\r\n\x1a\r" + png_bytes()[8:]
        with pytest.raises(ValueError):
            image_attachments.sniff_image(data)

    def test_png_truncated_ihdr_rejected(self):
        with pytest.raises(ValueError):
            image_attachments.sniff_image(png_bytes()[:20])

    def test_jpeg_magic_without_sof_rejected(self):
        with pytest.raises(ValueError):
            image_attachments.sniff_image(b"\xff\xd8" + b"\xff\xd9" + b"\x00" * 64)

    def test_random_bytes_match_no_format(self):
        with pytest.raises(ValueError):
            image_attachments.sniff_image(b"\x00\x01\x02\x03" * 32)

    def test_empty_upload_is_type_unsupported(self):
        with pytest.raises(image_attachments.AttachmentValidationError) as excinfo:
            image_attachments.validate_image(b"")
        assert excinfo.value.reason_code == REASON_ATTACHMENT_TYPE_UNSUPPORTED

    def test_over_byte_limit_is_too_large(self):
        big = png_bytes() + b"\x00" * (image_attachments.MAX_IMAGE_BYTES)
        with pytest.raises(image_attachments.AttachmentValidationError) as excinfo:
            image_attachments.validate_image(big)
        assert excinfo.value.reason_code == REASON_ATTACHMENT_TOO_LARGE

    def test_over_dimension_limit_is_too_large(self):
        with pytest.raises(image_attachments.AttachmentValidationError) as excinfo:
            image_attachments.validate_image(png_bytes(8001, 10))
        assert excinfo.value.reason_code == REASON_ATTACHMENT_TOO_LARGE
        with pytest.raises(image_attachments.AttachmentValidationError):
            image_attachments.validate_image(png_bytes(10, 9000))

    def test_zero_dimensions_refused(self):
        with pytest.raises(image_attachments.AttachmentValidationError):
            image_attachments.validate_image(png_bytes(0, 10))

    def test_corrupt_content_is_type_unsupported(self):
        with pytest.raises(image_attachments.AttachmentValidationError) as excinfo:
            image_attachments.validate_image(b"\x89PNG\r\n\x1a\n" + b"garbage")
        assert excinfo.value.reason_code == REASON_ATTACHMENT_TYPE_UNSUPPORTED


class TestDisplayFilename:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("../../etc/passwd", "passwd"),
            ("C:\\Users\\op\\Desktop\\shot.png", "shot.png"),
            ("shot\x1b[0m.png", "shot[0m.png"),
            ("", "image"),
            (None, "image"),
            ("...", "image"),
            ("screenshot final.png", "screenshot final.png"),
        ],
    )
    def test_sanitization(self, raw, expected):
        assert image_attachments.sanitize_display_filename(raw) == expected


class TestStagingLifecycle:
    def test_upload_stages_file_and_ready_record(self, store):
        record = image_attachments.stage_upload(
            TERMINAL,
            display_filename="shot.png",
            content=png_bytes(),
            allowed_formats=ALLOWED_PNG_ONLY,
        )
        assert record["state"] == "ready"
        assert record["format"] == "png"
        assert (record["width"], record["height"]) == (120, 80)
        assert record["content_type"] == "image/png"
        staged = image_attachments.staged_absolute_path(record)
        assert staged.read_bytes() == png_bytes()
        assert stat.S_IMODE(staged.stat().st_mode) == 0o600
        # The manifest itself obeys the D5 0600 discipline.
        manifest = store / "attachments.json"
        assert stat.S_IMODE(manifest.stat().st_mode) == 0o600
        on_disk = json.loads(manifest.read_text())
        assert on_disk["schema_version"] == 1
        assert on_disk["attachments"][0]["attachment_id"] == record["attachment_id"]

    def test_mime_spoof_is_decided_by_content(self, store):
        # Named .jpg, bytes are PNG: content wins, format is png.
        record = image_attachments.stage_upload(
            TERMINAL,
            display_filename="photo.jpg",
            content=png_bytes(),
            allowed_formats=ALLOWED_PNG_ONLY,
        )
        assert record["format"] == "png"
        assert record["display_filename"] == "photo.jpg"

    def test_disallowed_format_fails_with_failed_record_and_no_file(self, store):
        with pytest.raises(image_attachments.AttachmentValidationError) as excinfo:
            image_attachments.stage_upload(
                TERMINAL,
                display_filename="photo.jpg",
                content=jpeg_bytes(),
                allowed_formats=ALLOWED_PNG_ONLY,
            )
        assert excinfo.value.reason_code == REASON_ATTACHMENT_TYPE_UNSUPPORTED
        records = image_attachments.list_attachments(TERMINAL)
        assert len(records) == 1
        assert records[0]["state"] == "failed"
        assert records[0]["error"]["reason_code"] == REASON_ATTACHMENT_TYPE_UNSUPPORTED
        assert not (store / "attachments" / TERMINAL).exists()

    def test_invalid_content_fails_with_failed_record_and_no_file(self, store):
        with pytest.raises(image_attachments.AttachmentValidationError):
            image_attachments.stage_upload(
                TERMINAL,
                display_filename="x.png",
                content=b"not an image",
                allowed_formats=ALLOWED_ALL,
            )
        records = image_attachments.list_attachments(TERMINAL)
        assert len(records) == 1 and records[0]["state"] == "failed"
        assert not (store / "attachments" / TERMINAL).exists()


class TestBinding:
    def test_ready_binds_to_submitted_under_operation(self, store):
        record = image_attachments.stage_upload(
            TERMINAL,
            display_filename="a.png",
            content=png_bytes(),
            allowed_formats=ALLOWED_PNG_ONLY,
        )
        bound = image_attachments.bind_for_submit(TERMINAL, "op-1", [record["attachment_id"]])
        assert bound[0]["state"] == "submitted"
        assert bound[0]["bound_operation_id"] == "op-1"

    def test_identical_replay_reads_existing_binding(self, store):
        record = image_attachments.stage_upload(
            TERMINAL,
            display_filename="a.png",
            content=png_bytes(),
            allowed_formats=ALLOWED_PNG_ONLY,
        )
        image_attachments.bind_for_submit(TERMINAL, "op-1", [record["attachment_id"]])
        replayed = image_attachments.bind_for_submit(TERMINAL, "op-1", [record["attachment_id"]])
        assert replayed[0]["state"] == "submitted"
        assert replayed[0]["bound_operation_id"] == "op-1"

    def test_different_operation_on_submitted_is_not_ready(self, store):
        record = image_attachments.stage_upload(
            TERMINAL,
            display_filename="a.png",
            content=png_bytes(),
            allowed_formats=ALLOWED_PNG_ONLY,
        )
        image_attachments.bind_for_submit(TERMINAL, "op-1", [record["attachment_id"]])
        with pytest.raises(image_attachments.AttachmentBindingError) as excinfo:
            image_attachments.bind_for_submit(TERMINAL, "op-2", [record["attachment_id"]])
        assert excinfo.value.reason_code == REASON_ATTACHMENT_NOT_READY

    def test_unknown_attachment_is_unknown(self, store):
        with pytest.raises(image_attachments.AttachmentBindingError) as excinfo:
            image_attachments.bind_for_submit(TERMINAL, "op-1", ["does-not-exist"])
        assert excinfo.value.reason_code == REASON_ATTACHMENT_UNKNOWN

    def test_cross_terminal_reference_is_unknown(self, store):
        record = image_attachments.stage_upload(
            TERMINAL,
            display_filename="a.png",
            content=png_bytes(),
            allowed_formats=ALLOWED_PNG_ONLY,
        )
        with pytest.raises(image_attachments.AttachmentBindingError) as excinfo:
            image_attachments.bind_for_submit("ffffffff", "op-1", [record["attachment_id"]])
        assert excinfo.value.reason_code == REASON_ATTACHMENT_UNKNOWN

    def test_failed_attachment_is_not_ready(self, store):
        with pytest.raises(image_attachments.AttachmentValidationError):
            image_attachments.stage_upload(
                TERMINAL,
                display_filename="x.png",
                content=b"junk",
                allowed_formats=ALLOWED_ALL,
            )
        failed = image_attachments.list_attachments(TERMINAL)[0]
        with pytest.raises(image_attachments.AttachmentBindingError) as excinfo:
            image_attachments.bind_for_submit(TERMINAL, "op-1", [failed["attachment_id"]])
        assert excinfo.value.reason_code == REASON_ATTACHMENT_NOT_READY

    def test_failed_bind_persists_no_partial_transition(self, store):
        first = image_attachments.stage_upload(
            TERMINAL,
            display_filename="a.png",
            content=png_bytes(),
            allowed_formats=ALLOWED_PNG_ONLY,
        )
        with pytest.raises(image_attachments.AttachmentBindingError):
            image_attachments.bind_for_submit(TERMINAL, "op-1", [first["attachment_id"], "missing"])
        # All-or-nothing: the valid attachment stayed ready.
        assert (
            image_attachments.get_attachment(TERMINAL, first["attachment_id"])["state"] == "ready"
        )


class TestRemoval:
    def test_remove_ready_deletes_file_and_record_state(self, store):
        record = image_attachments.stage_upload(
            TERMINAL,
            display_filename="a.png",
            content=png_bytes(),
            allowed_formats=ALLOWED_PNG_ONLY,
        )
        staged = image_attachments.staged_absolute_path(record)
        assert staged.exists()
        removed = image_attachments.remove_attachment(TERMINAL, record["attachment_id"])
        assert removed["state"] == "removed"
        assert not staged.exists()
        assert image_attachments.list_attachments(TERMINAL) == []

    def test_remove_submitted_conflicts(self, store):
        record = image_attachments.stage_upload(
            TERMINAL,
            display_filename="a.png",
            content=png_bytes(),
            allowed_formats=ALLOWED_PNG_ONLY,
        )
        image_attachments.bind_for_submit(TERMINAL, "op-1", [record["attachment_id"]])
        with pytest.raises(image_attachments.AttachmentBindingError):
            image_attachments.remove_attachment(TERMINAL, record["attachment_id"])
        # Retained for the provider mid-turn.
        assert image_attachments.staged_absolute_path(record).exists()

    def test_remove_unknown_raises(self, store):
        with pytest.raises(image_attachments.AttachmentNotFoundError):
            image_attachments.remove_attachment(TERMINAL, "nope")


class TestManifestDiscipline:
    def test_corrupt_manifest_quarantines_and_starts_empty(self, store):
        (store / "attachments.json").write_text("{not json")
        record = image_attachments.stage_upload(
            TERMINAL,
            display_filename="a.png",
            content=png_bytes(),
            allowed_formats=ALLOWED_PNG_ONLY,
        )
        assert record["state"] == "ready"
        quarantines = list(store.glob("attachments.quarantine-*.json"))
        assert len(quarantines) == 1
        assert quarantines[0].read_text() == "{not json"

    def test_future_schema_version_quarantines(self, store):
        _seed_manifest(store, [])
        document = json.loads((store / "attachments.json").read_text())
        document["schema_version"] = 99
        (store / "attachments.json").write_text(json.dumps(document))
        assert image_attachments.list_attachments(TERMINAL) == []
        assert len(list(store.glob("attachments.quarantine-*.json"))) == 1


class TestSweep:
    def test_orphan_files_deleted(self, store):
        record = image_attachments.stage_upload(
            TERMINAL,
            display_filename="a.png",
            content=png_bytes(),
            allowed_formats=ALLOWED_PNG_ONLY,
        )
        orphan = store / "attachments" / TERMINAL / ".crashed-upload.part"
        orphan.write_bytes(b"partial")
        counts = image_attachments.sweep_attachments()
        assert counts["orphans_deleted"] == 1
        assert not orphan.exists()
        assert image_attachments.staged_absolute_path(record).exists()

    def test_expired_submitted_purged_with_file(self, store):
        record = _ready_record(
            state="submitted",
            bound_operation_id="op-1",
            updated_at=_aged(datetime.now(timezone.utc).isoformat(), hours=25),
        )
        _seed_manifest(store, [record])
        staged = store / record["staged_path"]
        staged.parent.mkdir(parents=True)
        staged.write_bytes(png_bytes())
        counts = image_attachments.sweep_attachments()
        assert counts["records_purged"] == 1
        assert counts["files_deleted"] == 1
        assert not staged.exists()

    def test_fresh_submitted_retained(self, store):
        record = _ready_record(state="submitted", bound_operation_id="op-1")
        _seed_manifest(store, [record])
        staged = store / record["staged_path"]
        staged.parent.mkdir(parents=True)
        staged.write_bytes(png_bytes())
        counts = image_attachments.sweep_attachments()
        assert counts["records_purged"] == 0
        assert staged.exists()

    def test_stale_staging_purged(self, store):
        record = _ready_record(
            state="staging",
            staged_path=None,
            updated_at=_aged(datetime.now(timezone.utc).isoformat(), hours=2),
        )
        _seed_manifest(store, [record])
        counts = image_attachments.sweep_attachments()
        assert counts["records_purged"] == 1

    def test_old_failed_purged(self, store):
        record = _ready_record(
            state="failed",
            staged_path=None,
            updated_at=_aged(datetime.now(timezone.utc).isoformat(), hours=25),
        )
        _seed_manifest(store, [record])
        assert image_attachments.sweep_attachments()["records_purged"] == 1

    def test_old_ready_never_touched(self, store):
        record = _ready_record(updated_at=_aged(datetime.now(timezone.utc).isoformat(), days=30))
        _seed_manifest(store, [record])
        staged = store / record["staged_path"]
        staged.parent.mkdir(parents=True)
        staged.write_bytes(png_bytes())
        counts = image_attachments.sweep_attachments()
        assert counts["records_purged"] == 0
        assert staged.exists()
        assert image_attachments.list_attachments(TERMINAL)[0]["state"] == "ready"

    def test_removed_records_purged(self, store):
        record = _ready_record(state="removed", staged_path=None)
        _seed_manifest(store, [record])
        assert image_attachments.sweep_attachments()["records_purged"] == 1
