"""Unit tests for AguiStreamReader using a fake SSE transport."""

from __future__ import annotations

from typing import Iterator
from unittest.mock import MagicMock, patch

import pytest
import requests

from cli_agent_orchestrator.services.agui.stream_reader import AguiStreamReader


class _FakeResponse:
    """Simulates a requests.Response with iter_lines for SSE testing."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self.status_code = 200

    def raise_for_status(self) -> None:
        pass

    def iter_lines(self, decode_unicode: bool = False) -> Iterator[str]:
        for line in self._lines:
            yield line


def _make_reader(
    lines: list[str],
    base_url: str = "http://localhost:8420",
    since: str | None = None,
    access_token: str | None = None,
) -> tuple[AguiStreamReader, list]:
    """Create a reader and collect all frames from the given SSE lines."""
    reader = AguiStreamReader(base_url, since=since, access_token=access_token)
    fake_resp = _FakeResponse(lines)
    with patch("cli_agent_orchestrator.services.agui.stream_reader.requests.get") as mock_get:
        mock_get.return_value = fake_resp
        frames = list(reader.frames())
    return reader, frames


class TestFrameParsing:
    """Basic SSE frame parsing."""

    def test_parses_complete_event(self):
        lines = [
            "id: evt-1",
            "event: RUN_STARTED",
            'data: {"thread_id": "s-1"}',
            "",
        ]
        reader, frames = _make_reader(lines)
        assert len(frames) == 1
        event_id, agui_type, data = frames[0]
        assert event_id == "evt-1"
        assert agui_type == "RUN_STARTED"
        assert data == {"thread_id": "s-1"}

    def test_parses_multiple_events(self):
        lines = [
            "id: e1",
            "event: STEP_STARTED",
            'data: {"step_id": "t-1"}',
            "",
            "id: e2",
            "event: STEP_FINISHED",
            'data: {"step_id": "t-1"}',
            "",
        ]
        _, frames = _make_reader(lines)
        assert len(frames) == 2
        assert frames[0][1] == "STEP_STARTED"
        assert frames[1][1] == "STEP_FINISHED"

    def test_multiline_data(self):
        lines = [
            "id: e1",
            "event: RAW",
            'data: {"a":',
            'data: "b"}',
            "",
        ]
        _, frames = _make_reader(lines)
        assert len(frames) == 1
        # Multi-line data is joined with newline, then parsed as JSON.
        assert frames[0][2] == {"a": "b"}


class TestIdLessFrames:
    """Frames without an id: field."""

    def test_frame_without_id(self):
        lines = [
            "event: STATE_SNAPSHOT",
            'data: {"snapshot": {}}',
            "",
        ]
        reader, frames = _make_reader(lines)
        assert len(frames) == 1
        event_id, agui_type, data = frames[0]
        assert event_id is None
        assert agui_type == "STATE_SNAPSHOT"
        assert data == {"snapshot": {}}
        # last_event_id should not be updated for id-less frames.
        assert reader.last_event_id is None

    def test_mixed_id_and_no_id(self):
        lines = [
            "id: first",
            "event: A",
            'data: {"x": 1}',
            "",
            "event: B",
            'data: {"x": 2}',
            "",
        ]
        reader, frames = _make_reader(lines)
        assert frames[0][0] == "first"
        assert frames[1][0] is None
        # last_event_id should be "first" since B had no id.
        assert reader.last_event_id == "first"


class TestCursorPropagation:
    """last_event_id tracks the most recent id."""

    def test_last_event_id_updated_sequentially(self):
        lines = [
            "id: a",
            "event: X",
            "data: {}",
            "",
            "id: b",
            "event: Y",
            "data: {}",
            "",
            "id: c",
            "event: Z",
            "data: {}",
            "",
        ]
        reader, frames = _make_reader(lines)
        assert reader.last_event_id == "c"


class TestReconnectResume:
    """On reconnect, the reader sends Last-Event-ID header."""

    def test_reconnect_sends_last_event_id(self):
        reader = AguiStreamReader("http://localhost:8420")
        # Simulate first connection that yields one event.
        lines1 = ["id: evt-5", "event: A", "data: {}", ""]
        fake_resp1 = _FakeResponse(lines1)
        with patch("cli_agent_orchestrator.services.agui.stream_reader.requests.get") as mock_get:
            mock_get.return_value = fake_resp1
            list(reader.frames())

        assert reader.last_event_id == "evt-5"

        # Simulate reconnect; verify Last-Event-ID header is sent.
        lines2 = ["id: evt-6", "event: B", "data: {}", ""]
        fake_resp2 = _FakeResponse(lines2)
        with patch("cli_agent_orchestrator.services.agui.stream_reader.requests.get") as mock_get:
            mock_get.return_value = fake_resp2
            list(reader.frames())
            # Check the headers passed to requests.get
            call_kwargs = mock_get.call_args[1]
            assert call_kwargs["headers"]["Last-Event-ID"] == "evt-5"

    def test_since_passed_as_param_not_in_url(self):
        reader = AguiStreamReader("http://localhost:8420", since="2024-01-01T00:00:00Z")
        lines = ["id: e1", "event: A", "data: {}", ""]
        fake_resp = _FakeResponse(lines)
        with patch("cli_agent_orchestrator.services.agui.stream_reader.requests.get") as mock_get:
            mock_get.return_value = fake_resp
            list(reader.frames())
            url_called = mock_get.call_args[0][0]
            params = mock_get.call_args[1]["params"]
            # ``since`` is delegated to requests via ``params`` (which URL-encodes
            # it) rather than hand-concatenated into the URL string.
            assert "since" not in url_called
            assert params == {"since": "2024-01-01T00:00:00Z"}

    def test_since_offset_timestamp_is_url_encoded(self):
        # ISO-8601 offsets contain ``+00:00``; a raw ``?since=`` string sends
        # ``+`` as a literal space and the server rejects it (HTTP 400). Passing
        # via ``params`` lets requests percent-encode ``+`` to ``%2B``.
        since = "2024-01-01T00:00:00+00:00"
        reader = AguiStreamReader("http://localhost:8420", since=since)
        lines = ["id: e1", "event: A", "data: {}", ""]
        fake_resp = _FakeResponse(lines)
        with patch("cli_agent_orchestrator.services.agui.stream_reader.requests.get") as mock_get:
            mock_get.return_value = fake_resp
            list(reader.frames())
            url_called = mock_get.call_args[0][0]
            params = mock_get.call_args[1]["params"]
            assert params == {"since": since}

        # Prove requests encodes the offset correctly (no server needed): the
        # prepared URL must carry ``%2B`` and never a raw ``+`` or space.
        prepared = requests.models.PreparedRequest()
        prepared.prepare_url(url_called, params)
        assert "since=2024-01-01T00%3A00%3A00%2B00%3A00" in prepared.url
        assert "+00:00" not in prepared.url


class TestMalformedLineTolerance:
    """Malformed lines are skipped without crashing."""

    def test_skips_comment_lines(self):
        lines = [
            ": this is a comment",
            "id: e1",
            "event: A",
            'data: {"ok": true}',
            "",
        ]
        _, frames = _make_reader(lines)
        assert len(frames) == 1
        assert frames[0][2] == {"ok": True}

    def test_skips_lines_without_colon(self):
        lines = [
            "id: e1",
            "event: A",
            "this-line-has-no-colon",
            'data: {"ok": true}',
            "",
        ]
        _, frames = _make_reader(lines)
        assert len(frames) == 1

    def test_skips_event_with_invalid_json_data(self):
        lines = [
            "id: bad",
            "event: A",
            "data: not-json{{{",
            "",
            "id: good",
            "event: B",
            'data: {"v": 1}',
            "",
        ]
        _, frames = _make_reader(lines)
        # Only the second event with valid JSON is yielded.
        assert len(frames) == 1
        assert frames[0][0] == "good"

    def test_incomplete_event_no_data(self):
        """An event with event: but no data: is not dispatched."""
        lines = [
            "id: e1",
            "event: A",
            "",
        ]
        _, frames = _make_reader(lines)
        assert len(frames) == 0

    def test_incomplete_event_no_event_field(self):
        """An event with data: but no event: field is not dispatched."""
        lines = [
            "id: e1",
            'data: {"x": 1}',
            "",
        ]
        _, frames = _make_reader(lines)
        assert len(frames) == 0

    def test_stream_ends_without_blank_line(self):
        """If stream ends mid-event, still dispatch if complete."""
        lines = [
            "id: e1",
            "event: A",
            'data: {"x": 1}',
            # No trailing blank line
        ]
        _, frames = _make_reader(lines)
        assert len(frames) == 1
        assert frames[0][0] == "e1"

    def test_non_dict_data_wrapped(self):
        """Non-dict JSON (e.g. a list) gets wrapped in {_raw: ...}."""
        lines = [
            "event: A",
            "data: [1, 2, 3]",
            "",
        ]
        _, frames = _make_reader(lines)
        assert len(frames) == 1
        assert frames[0][2] == {"_raw": [1, 2, 3]}

    def test_access_token_sent_in_header(self):
        lines = ["id: e1", "event: A", "data: {}", ""]
        fake_resp = _FakeResponse(lines)
        with patch("cli_agent_orchestrator.services.agui.stream_reader.requests.get") as mock_get:
            mock_get.return_value = fake_resp
            reader = AguiStreamReader("http://localhost:8420", access_token="my-secret-token")
            list(reader.frames())
            headers = mock_get.call_args[1]["headers"]
            assert headers["Authorization"] == "Bearer my-secret-token"


# ---------------------------------------------------------------------------
# Item 2 — Connect/read timeout tests
# ---------------------------------------------------------------------------


class TestTimeoutNegotiation:
    """Tests for the (connect, read) timeout tuple in AguiStreamReader."""

    def test_tuple_timeout_passed_to_requests(self):
        """A (connect, read) tuple is passed through to requests.get."""
        reader = AguiStreamReader("http://localhost:8420", timeout=(5.0, 30.0))
        lines = ["id: e1", "event: A", "data: {}", ""]
        fake_resp = _FakeResponse(lines)
        with patch("cli_agent_orchestrator.services.agui.stream_reader.requests.get") as mock_get:
            mock_get.return_value = fake_resp
            list(reader.frames())
            kwargs = mock_get.call_args[1]
            assert kwargs["timeout"] == (5.0, 30.0)

    def test_float_compat_uses_default_connect(self):
        """A single float is interpreted as read timeout with 10s connect default."""
        from cli_agent_orchestrator.services.agui.stream_reader import _DEFAULT_CONNECT_TIMEOUT

        reader = AguiStreamReader("http://localhost:8420", timeout=45.0)
        lines = ["id: e1", "event: A", "data: {}", ""]
        fake_resp = _FakeResponse(lines)
        with patch("cli_agent_orchestrator.services.agui.stream_reader.requests.get") as mock_get:
            mock_get.return_value = fake_resp
            list(reader.frames())
            kwargs = mock_get.call_args[1]
            assert kwargs["timeout"] == (_DEFAULT_CONNECT_TIMEOUT, 45.0)

    def test_none_timeout_uses_defaults(self):
        """None/absent timeout uses the module defaults (10s connect, 60s read)."""
        from cli_agent_orchestrator.services.agui.stream_reader import (
            _DEFAULT_CONNECT_TIMEOUT,
            _DEFAULT_READ_TIMEOUT,
        )

        reader = AguiStreamReader("http://localhost:8420")
        lines = ["id: e1", "event: A", "data: {}", ""]
        fake_resp = _FakeResponse(lines)
        with patch("cli_agent_orchestrator.services.agui.stream_reader.requests.get") as mock_get:
            mock_get.return_value = fake_resp
            list(reader.frames())
            kwargs = mock_get.call_args[1]
            assert kwargs["timeout"] == (_DEFAULT_CONNECT_TIMEOUT, _DEFAULT_READ_TIMEOUT)

    def test_default_read_exceeds_heartbeat(self):
        """The default read timeout (60s) must exceed the server heartbeat (15s)."""
        from cli_agent_orchestrator.services.agui.stream_reader import _DEFAULT_READ_TIMEOUT

        # Server heartbeat is 15s (CAO_AGUI_HEARTBEAT_SECONDS default)
        assert _DEFAULT_READ_TIMEOUT > 15.0, "Read timeout must exceed 15s heartbeat"
