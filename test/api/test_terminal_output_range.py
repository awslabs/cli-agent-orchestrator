"""Tests for U5 offset-ranged terminal-log read (#504, FR-4.3).

Covers ``terminal_service.read_output_range`` (the additive on-disk reader) and
``GET /terminals/{terminal_id}/output/range`` (the route over it). The rolling
buffer / ``get_output`` path is a SEPARATE read path and is untouched here.

The reader ranges over the append-only ``{terminal_id}.log`` LogWriter maintains.
Tests point ``terminal_service.TERMINAL_LOG_DIR`` at a tmp dir (the same
monkeypatch attribute existing terminal_service / log_writer tests use) and write
a real log file, so byte-offset behavior is exercised end to end.
"""

from unittest.mock import patch

import pytest

from cli_agent_orchestrator.services import terminal_service
from cli_agent_orchestrator.services.terminal_service import (
    TERMINAL_RANGE_MAX_LENGTH,
    read_output_range,
)

# A valid 8-char-hex terminal id (matches the route's TerminalId pattern AND the
# service's _validate_key_part charset).
TID = "abcd1234"


@pytest.fixture
def log_dir(monkeypatch, tmp_path):
    """Point TERMINAL_LOG_DIR at a temp dir so tests never touch real logs."""
    d = tmp_path / "terminal"
    d.mkdir()
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.TERMINAL_LOG_DIR",
        d,
    )
    return d


def _write_log(log_dir, terminal_id: str, data: bytes) -> None:
    (log_dir / f"{terminal_id}.log").write_bytes(data)


# ---------------------------------------------------------------------------
# read_output_range — service function
# ---------------------------------------------------------------------------


class TestReadOutputRange:
    def test_happy_mid_file_range_returns_exact_bytes(self, log_dir):
        """A mid-file (offset, length) slice returns exactly those bytes (BR-1)."""
        _write_log(log_dir, TID, b"0123456789abcdef")

        # bytes [5, 5+4) == "56789"[:-... -> "56789" is 5 chars; take 4 from 5
        result = read_output_range(TID, offset=5, length=4)

        assert result == "5678"

    def test_read_to_eof_returns_available_tail(self, log_dir):
        """A length past the remaining bytes returns only what is there, no error."""
        _write_log(log_dir, TID, b"hello")

        result = read_output_range(TID, offset=2, length=1000)

        assert result == "llo"

    def test_offset_beyond_eof_returns_empty(self, log_dir):
        """An offset at/beyond EOF yields an empty range, never an error (BR-4)."""
        _write_log(log_dir, TID, b"short")

        assert read_output_range(TID, offset=999, length=10) == ""

    def test_missing_log_file_returns_empty(self, log_dir):
        """A valid terminal that has not logged yet -> empty range, not error (BR-4)."""
        # No file written for TID.
        assert read_output_range(TID, offset=0, length=10) == ""

    def test_length_over_cap_is_clamped(self, log_dir, monkeypatch):
        """length is clamped to TERMINAL_RANGE_MAX_LENGTH (BR-2)."""
        # Shrink the cap so we don't have to write 1 MiB to exercise the clamp.
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_service.TERMINAL_RANGE_MAX_LENGTH",
            4,
        )
        _write_log(log_dir, TID, b"abcdefghij")

        result = read_output_range(TID, offset=0, length=10_000)

        assert result == "abcd"  # capped to 4 bytes, not the requested 10_000

    def test_real_cap_constant_is_one_mib(self):
        """Pin the shipped cap: 1 MiB — a defensible ceiling above any real window."""
        assert TERMINAL_RANGE_MAX_LENGTH == 1024 * 1024

    def test_mid_multibyte_offset_decodes_with_replace(self, log_dir):
        """An offset that splits a UTF-8 sequence decodes with replace, no raise (BR-5)."""
        # "€" is 3 bytes (e2 82 ac). Start the range one byte into it so the first
        # byte is a dangling continuation byte -> replacement char, never UnicodeDecodeError.
        _write_log(log_dir, TID, "A€B".encode("utf-8"))  # 41 e2 82 ac 42

        result = read_output_range(TID, offset=2, length=10)

        # No exception raised; the split multibyte prefix becomes U+FFFD, "B" survives.
        assert isinstance(result, str)
        assert "�" in result
        assert result.endswith("B")

    def test_negative_offset_raises_valueerror(self, log_dir):
        """A negative offset is a caller error (-> 400 at the route)."""
        _write_log(log_dir, TID, b"data")

        with pytest.raises(ValueError, match="offset must be >= 0"):
            read_output_range(TID, offset=-1, length=4)

    def test_path_traversal_id_raises_valueerror(self, log_dir):
        """A traversal id is rejected before the path is built (path-traversal defense)."""
        _write_log(log_dir, TID, b"data")

        with pytest.raises(ValueError):
            read_output_range("../etc/passwd", offset=0, length=4)

    def test_io_error_is_surfaced_not_swallowed(self, log_dir):
        """A genuine I/O error is raised, distinct from 'nothing logged' (BR-4)."""
        _write_log(log_dir, TID, b"data")

        # Simulate a real read failure (permission, etc.): open() raises PermissionError
        # (an OSError subclass), which must propagate, NOT collapse to "".
        with patch(
            "cli_agent_orchestrator.services.terminal_service.open",
            side_effect=PermissionError("denied"),
            create=True,
        ):
            with pytest.raises(OSError):
                read_output_range(TID, offset=0, length=4)


# ---------------------------------------------------------------------------
# GET /terminals/{terminal_id}/output/range — route
# ---------------------------------------------------------------------------


class TestOutputRangeRoute:
    def test_happy_returns_range_and_echoes_params(self, client, log_dir):
        _write_log(log_dir, TID, b"0123456789")

        resp = client.get(f"/terminals/{TID}/output/range", params={"offset": 3, "length": 4})

        assert resp.status_code == 200
        body = resp.json()
        assert body == {"terminal_id": TID, "offset": 3, "length": 4, "data": "3456"}

    def test_unlogged_terminal_returns_200_empty_data(self, client, log_dir):
        """BR-4: a valid-but-unlogged terminal degrades to 200 with empty data, not 404."""
        resp = client.get(f"/terminals/{TID}/output/range", params={"offset": 0, "length": 10})

        assert resp.status_code == 200
        assert resp.json()["data"] == ""

    def test_offset_beyond_eof_returns_200_empty_data(self, client, log_dir):
        _write_log(log_dir, TID, b"tiny")

        resp = client.get(f"/terminals/{TID}/output/range", params={"offset": 500, "length": 10})

        assert resp.status_code == 200
        assert resp.json()["data"] == ""

    def test_length_below_floor_is_422(self, client, log_dir):
        """length ge=1: a 0-length read is rejected at the boundary."""
        resp = client.get(f"/terminals/{TID}/output/range", params={"offset": 0, "length": 0})
        assert resp.status_code == 422

    def test_negative_offset_is_422(self, client, log_dir):
        """offset ge=0: a negative offset is rejected at the boundary."""
        resp = client.get(f"/terminals/{TID}/output/range", params={"offset": -1, "length": 4})
        assert resp.status_code == 422

    def test_length_over_cap_is_422(self, client, log_dir):
        """length le=cap: a request over the cap is rejected at the boundary (BR-2)."""
        resp = client.get(
            f"/terminals/{TID}/output/range",
            params={"offset": 0, "length": TERMINAL_RANGE_MAX_LENGTH + 1},
        )
        assert resp.status_code == 422

    def test_malformed_terminal_id_is_422(self, client, log_dir):
        """A non-hex-8 id fails the TerminalId path pattern before the handler runs."""
        resp = client.get(
            "/terminals/not-a-valid-id/output/range", params={"offset": 0, "length": 4}
        )
        assert resp.status_code == 422

    def test_io_error_surfaces_as_500(self, client, log_dir):
        """A genuine I/O failure is reported (500), not masked as empty data (BR-4)."""
        with patch(
            "cli_agent_orchestrator.api.main.terminal_service.read_output_range",
            side_effect=OSError("disk on fire"),
        ):
            resp = client.get(f"/terminals/{TID}/output/range", params={"offset": 0, "length": 4})

        assert resp.status_code == 500
        assert "Failed to read output range" in resp.json()["detail"]
