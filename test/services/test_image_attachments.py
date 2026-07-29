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
import threading
import zlib
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


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def _png_ihdr(width: int = 120, height: int = 80, interlace: int = 0) -> bytes:
    return _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, interlace))


def _valid_raw(width: int = 120, height: int = 80, filter_byte: int = 0) -> bytes:
    return b"".join(bytes([filter_byte]) + b"\x7f" * (width * 3) for _ in range(height))


def png_bytes(width=120, height=80):
    """A genuine, fully decodable 8-bit RGB PNG: signature, IHDR, one IDAT
    whose zlib stream carries exactly the declared scanlines (filter byte 0
    per row), and IEND — every chunk with a valid CRC."""
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_ihdr(width, height)
        + _png_chunk(b"IDAT", zlib.compress(_valid_raw(width, height)))
        + _png_chunk(b"IEND", b"")
    )


def png_interlaced_bytes(width=9, height=9):
    """A genuine Adam7-interlaced PNG (all seven passes non-empty at 9×9)."""
    raw = b""
    for x0, y0, dx, dy in (
        (0, 0, 8, 8),
        (4, 0, 8, 8),
        (0, 4, 4, 8),
        (2, 0, 4, 4),
        (0, 2, 2, 4),
        (1, 0, 2, 2),
        (0, 1, 1, 2),
    ):
        pass_width = 0 if width <= x0 else (width - x0 + dx - 1) // dx
        pass_height = 0 if height <= y0 else (height - y0 + dy - 1) // dy
        if pass_width and pass_height:
            raw += b"".join(b"\x00" + b"\x7f" * (pass_width * 3) for _ in range(pass_height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_ihdr(width, height, interlace=1)
        + _png_chunk(b"IDAT", zlib.compress(raw))
        + _png_chunk(b"IEND", b"")
    )


def png_header_only():
    """The pre-r1 fixture: signature + IHDR + four zero bytes — a header
    that claims to be an image, with no IDAT and no IEND."""
    return b"\x89PNG\r\n\x1a\n" + _png_ihdr() + b"\x00\x00\x00\x00"


def png_crc_broken():
    """A genuine PNG with one IDAT payload bit flipped: CRC mismatch."""
    data = bytearray(png_bytes())
    idat_at = bytes(data).find(b"IDAT")
    data[idat_at + 4] ^= 0x01
    return bytes(data)


def png_no_idat():
    return b"\x89PNG\r\n\x1a\n" + _png_ihdr() + _png_chunk(b"IEND", b"")


def png_no_iend():
    return b"\x89PNG\r\n\x1a\n" + _png_ihdr() + _png_chunk(b"IDAT", zlib.compress(_valid_raw()))


def png_truncated_idat():
    """The IDAT's zlib stream is cut short; the chunk itself is CRC-sound."""
    payload = zlib.compress(_valid_raw())[:5]
    return (
        b"\x89PNG\r\n\x1a\n" + _png_ihdr() + _png_chunk(b"IDAT", payload) + _png_chunk(b"IEND", b"")
    )


def png_garbage_idat():
    """A CRC-sound IDAT whose payload is not a zlib stream at all."""
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_ihdr()
        + _png_chunk(b"IDAT", b"\xff\xff\xff\xff")
        + _png_chunk(b"IEND", b"")
    )


def png_short_image_data():
    """A clean zlib stream that decodes to half the declared scanlines."""
    raw = _valid_raw()[: len(_valid_raw()) // 2]
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_ihdr()
        + _png_chunk(b"IDAT", zlib.compress(raw))
        + _png_chunk(b"IEND", b"")
    )


def png_bad_filter_byte():
    """The right declared byte count, but a scanline opens with filter 5."""
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_ihdr()
        + _png_chunk(b"IDAT", zlib.compress(_valid_raw(filter_byte=5)))
        + _png_chunk(b"IEND", b"")
    )


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

    def test_interlaced_png_decodes_dimensions(self):
        assert image_attachments.sniff_image(png_interlaced_bytes(9, 9)) == ("png", 9, 9)

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


class TestRemovalRace:
    """The submitted-state check and the removal transition are one
    manifest-locked mutation (Lane C r1): a bind racing a removal can never
    delete the staged file out from under a submitted binding, in either
    commit order.  The interleavings are forced deterministically by
    gating the named mutator inside ``_mutate`` until the other thread's
    mutation has committed."""

    def _stage_one(self):
        return image_attachments.stage_upload(
            TERMINAL,
            display_filename="a.png",
            content=png_bytes(),
            allowed_formats=ALLOWED_PNG_ONLY,
        )

    def test_remove_starting_first_still_conflicts_after_a_bind_commits(self, store, monkeypatch):
        record = self._stage_one()
        staged = image_attachments.staged_absolute_path(record)
        real_mutate = image_attachments._mutate
        remove_parked = threading.Event()
        bind_committed = threading.Event()

        def gated(mutator):
            # mark_removed is parked *before* its lock acquisition until the
            # bind has committed — the exact pre-r1 interleave, which now
            # must conflict instead of deleting.
            if getattr(mutator, "__name__", "") == "mark_removed":
                remove_parked.set()
                assert bind_committed.wait(timeout=10)
            return real_mutate(mutator)

        monkeypatch.setattr(image_attachments, "_mutate", gated)
        outcomes = {}

        def do_remove():
            try:
                outcomes["remove"] = image_attachments.remove_attachment(
                    TERMINAL, record["attachment_id"]
                )
            except image_attachments.AttachmentBindingError as exc:
                outcomes["remove_error"] = exc

        thread = threading.Thread(target=do_remove)
        thread.start()
        assert remove_parked.wait(timeout=10)
        image_attachments.bind_for_submit(TERMINAL, "op-race", [record["attachment_id"]])
        bind_committed.set()
        thread.join(timeout=10)

        assert "remove_error" in outcomes, outcomes
        final = image_attachments.get_attachment(TERMINAL, record["attachment_id"])
        assert final["state"] == "submitted"
        assert final["bound_operation_id"] == "op-race"
        assert staged.exists()

    def test_bind_starting_first_is_not_ready_after_a_remove_commits(self, store, monkeypatch):
        record = self._stage_one()
        staged = image_attachments.staged_absolute_path(record)
        real_mutate = image_attachments._mutate
        bind_parked = threading.Event()
        remove_committed = threading.Event()

        def gated(mutator):
            if getattr(mutator, "__name__", "") == "bind":
                bind_parked.set()
                assert remove_committed.wait(timeout=10)
            result = real_mutate(mutator)
            if getattr(mutator, "__name__", "") == "mark_removed":
                remove_committed.set()
            return result

        monkeypatch.setattr(image_attachments, "_mutate", gated)
        outcomes = {}

        def do_bind():
            try:
                outcomes["bind"] = image_attachments.bind_for_submit(
                    TERMINAL, "op-race", [record["attachment_id"]]
                )
            except image_attachments.AttachmentBindingError as exc:
                outcomes["bind_error"] = exc

        thread = threading.Thread(target=do_bind)
        thread.start()
        assert bind_parked.wait(timeout=10)
        image_attachments.remove_attachment(TERMINAL, record["attachment_id"])
        thread.join(timeout=10)

        assert "bind_error" in outcomes, outcomes
        assert outcomes["bind_error"].reason_code == REASON_ATTACHMENT_NOT_READY
        assert not staged.exists()


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
