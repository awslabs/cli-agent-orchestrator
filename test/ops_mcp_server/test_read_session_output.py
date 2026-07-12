"""Tests for the read_session_output operations MCP tool."""

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.ops_mcp_server.server import (
    _read_session_output_impl,
    read_session_output,
)

REQUEST = "cli_agent_orchestrator.ops_mcp_server.server.requests.request"


def _response(*, status_code: int = 200, json_data=None, text: str = ""):
    """Create a mock HTTP response."""
    response = MagicMock()
    response.status_code = status_code
    response.text = text
    response.json.return_value = json_data
    return response


class TestReadSessionOutputImpl:
    """Logic tests for the sync _read_session_output_impl helper."""

    def test_reads_by_terminal_id_full(self) -> None:
        """A direct terminal_id read returns the output and metadata."""
        with patch(
            REQUEST, return_value=_response(json_data={"output": "hello world", "mode": "full"})
        ) as mock_request:
            result = _read_session_output_impl("term-1", None, "full", None)

        assert result == {
            "success": True,
            "terminal_id": "term-1",
            "mode": "full",
            "output": "hello world",
            "truncated": False,
            "total_chars": 11,
        }
        mock_request.assert_called_once_with(
            "get",
            "http://127.0.0.1:9889/terminals/term-1/output",
            params={"mode": "full"},
            json=None,
        )

    def test_resolves_session_with_single_terminal(self) -> None:
        """A session_name with exactly one terminal resolves to that terminal."""
        responses = [
            _response(json_data={"name": "cao-x", "terminals": [{"id": "term-9"}]}),
            _response(json_data={"output": "abc", "mode": "full"}),
        ]
        with patch(REQUEST, side_effect=responses):
            result = _read_session_output_impl(None, "cao-x", "full", None)

        assert result["success"] is True
        assert result["terminal_id"] == "term-9"
        assert result["output"] == "abc"

    def test_ambiguous_session_returns_terminal_list(self) -> None:
        """A session with more than one terminal returns the list and requires terminal_id."""
        payload = {"name": "cao-x", "terminals": [{"id": "term-1"}, {"id": "term-2"}]}
        with patch(REQUEST, return_value=_response(json_data=payload)) as mock_request:
            result = _read_session_output_impl(None, "cao-x", "full", None)

        assert result["success"] is False
        assert "2 terminals" in result["message"]
        assert result["terminals"] == payload["terminals"]
        mock_request.assert_called_once()  # only the resolve call; no output read

    def test_session_with_no_terminals_errors(self) -> None:
        """A session with no terminals returns a clear error."""
        with patch(REQUEST, return_value=_response(json_data={"name": "cao-x", "terminals": []})):
            result = _read_session_output_impl(None, "cao-x", "full", None)

        assert result == {"success": False, "message": "Session 'cao-x' has no terminals"}

    def test_invalid_mode_short_circuits(self) -> None:
        """An invalid mode is rejected before any API call."""
        with patch(REQUEST) as mock_request:
            result = _read_session_output_impl("term-1", None, "weird", None)

        assert result == {
            "success": False,
            "message": "Invalid mode 'weird'; expected 'full' or 'last'",
        }
        mock_request.assert_not_called()

    def test_requires_terminal_id_or_session_name(self) -> None:
        """Calling with neither identifier returns an error before any API call."""
        with patch(REQUEST) as mock_request:
            result = _read_session_output_impl(None, None, "full", None)

        assert result == {
            "success": False,
            "message": "Provide either terminal_id or session_name",
        }
        mock_request.assert_not_called()

    def test_truncates_and_reports_total_chars(self) -> None:
        """max_chars caps the output tail and reports the full pre-truncation length."""
        with patch(
            REQUEST, return_value=_response(json_data={"output": "0123456789", "mode": "full"})
        ):
            result = _read_session_output_impl("term-1", None, "full", 4)

        assert result["output"] == "6789"
        assert result["truncated"] is True
        assert result["total_chars"] == 10

    def test_no_truncation_when_under_cap(self) -> None:
        """max_chars larger than the output leaves it intact."""
        with patch(REQUEST, return_value=_response(json_data={"output": "short", "mode": "full"})):
            result = _read_session_output_impl("term-1", None, "full", 100)

        assert result["output"] == "short"
        assert result["truncated"] is False
        assert result["total_chars"] == 5

    def test_api_error_is_returned(self) -> None:
        """An API error on the output read is surfaced as a failure."""
        with patch(
            REQUEST,
            return_value=_response(status_code=404, json_data={"detail": "Terminal not found"}),
        ):
            result = _read_session_output_impl("missing", None, "full", None)

        assert result["success"] is False
        assert "Terminal not found" in result["message"]

    def test_invalid_output_payload_is_rejected(self) -> None:
        """A payload missing the output field is treated as a failure."""
        with patch(REQUEST, return_value=_response(json_data={"mode": "full"})):
            result = _read_session_output_impl("term-1", None, "full", None)

        assert result == {
            "success": False,
            "message": "Read output failed: invalid response payload",
        }


@pytest.mark.asyncio
async def test_read_session_output_tool_delegates_to_impl() -> None:
    """The async @mcp.tool wrapper passes through to the impl with its defaults."""
    with patch(REQUEST, return_value=_response(json_data={"output": "data", "mode": "full"})):
        result = await read_session_output(terminal_id="term-1")

    assert result["success"] is True
    assert result["output"] == "data"
    assert result["total_chars"] == 4
