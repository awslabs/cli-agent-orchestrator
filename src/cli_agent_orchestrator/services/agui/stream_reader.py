"""AguiStreamReader: requests-based SSE reader for AG-UI streams.

Connects to ``GET /agui/v1/stream`` and parses the named SSE fields
(``id:``, ``event:``, ``data:``) into typed tuples that a construct can
consume via :meth:`frames`.

Timeout behaviour: the reader uses a ``(connect, read)`` timeout tuple
passed to ``requests.get``. The read timeout must comfortably exceed the
server's SSE heartbeat interval (default 15s, ``CAO_AGUI_HEARTBEAT_SECONDS``)
to avoid premature disconnection during idle periods — the default 60s read
timeout provides a 4× margin. A single float is accepted for backward
compatibility (interpreted as the read timeout, with a 10s connect default).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Generator, Optional, Tuple, Union

import requests

logger = logging.getLogger(__name__)

# Default connect/read timeouts (seconds). Read must exceed the server's 15s
# SSE heartbeat (CAO_AGUI_HEARTBEAT_SECONDS) to avoid spurious timeouts.
_DEFAULT_CONNECT_TIMEOUT = 10.0
_DEFAULT_READ_TIMEOUT = 60.0


class AguiStreamReader:
    """Parse a ``GET /agui/v1/stream`` SSE connection into typed frames.

    Usage::

        reader = AguiStreamReader("http://localhost:8420")
        for event_id, agui_type, data in reader.frames():
            construct.handle_frame(agui_type, data, event_id)

    On reconnect the reader sends the ``Last-Event-ID`` header set to the
    last successfully yielded event id, so the server replays missed events.

    Timeout interplay with the server heartbeat:
        The server emits ``:keep-alive`` SSE comments every 15s (configurable
        via ``CAO_AGUI_HEARTBEAT_SECONDS``). The read timeout must exceed this
        interval — otherwise a quiet fleet triggers a read timeout before the
        next heartbeat arrives. The default (connect=10s, read=60s) is safe for
        heartbeat intervals up to ~55s.

    Args:
        base_url: The CAO server base URL (e.g. ``http://localhost:8420``).
        since: Optional ISO-8601 timestamp for the ``since`` query param.
        access_token: Optional bearer token for authentication.
        timeout: Connect/read timeout. Accepts a ``(connect, read)`` tuple or a
            single float (interpreted as read timeout with a 10s connect default).
    """

    def __init__(
        self,
        base_url: str,
        since: Optional[str] = None,
        access_token: Optional[str] = None,
        timeout: Union[float, Tuple[float, float], None] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._since = since
        self._access_token = access_token
        # Normalize timeout to a (connect, read) tuple.
        if timeout is None:
            self._timeout: Tuple[float, float] = (_DEFAULT_CONNECT_TIMEOUT, _DEFAULT_READ_TIMEOUT)
        elif isinstance(timeout, tuple):
            self._timeout = timeout
        else:
            # Single float: backward compat — use as read timeout.
            self._timeout = (_DEFAULT_CONNECT_TIMEOUT, timeout)
        self._last_event_id: Optional[str] = None

    @property
    def last_event_id(self) -> Optional[str]:
        """The id of the most recently yielded frame (for reconnect)."""
        return self._last_event_id

    def _build_url(self) -> str:
        return f"{self._base_url}/agui/v1/stream"

    def _build_params(self) -> Dict[str, str]:
        # Pass ``since`` via ``params`` so requests URL-encodes it. ISO-8601
        # timestamps commonly contain ``+00:00``; a raw ``?since=`` string
        # would send ``+`` as a literal space, which the server-side ISO-8601
        # validation then rejects with HTTP 400.
        params: Dict[str, str] = {}
        if self._since:
            params["since"] = self._since
        return params

    def _build_headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {"Accept": "text/event-stream"}
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        if self._last_event_id:
            headers["Last-Event-ID"] = self._last_event_id
        return headers

    def frames(self) -> Generator[Tuple[Optional[str], str, Dict[str, Any]], None, None]:
        """Yield ``(event_id_or_None, agui_type, parsed_data_dict)`` tuples.

        Iterates the SSE stream line-by-line. Each complete event (terminated by
        a blank line) is emitted as a tuple. Malformed lines (missing colon,
        unparseable JSON in ``data:``) are logged and skipped -- they never crash
        the reader.
        """
        url = self._build_url()
        params = self._build_params()
        headers = self._build_headers()

        resp = requests.get(url, params=params, headers=headers, stream=True, timeout=self._timeout)
        resp.raise_for_status()

        # Accumulate fields for the current event.
        current_id: Optional[str] = None
        current_event: Optional[str] = None
        current_data: Optional[str] = None

        for raw_line in resp.iter_lines(decode_unicode=True):
            # iter_lines strips the trailing newline; a blank line signals
            # end-of-event dispatch.
            if raw_line is None:
                continue

            line: str = raw_line

            if line == "":
                # Dispatch the accumulated event if we have the required fields.
                if current_event is not None and current_data is not None:
                    try:
                        parsed = json.loads(current_data)
                    except (json.JSONDecodeError, ValueError):
                        logger.warning(
                            "Skipping frame with unparseable data: %s", current_data[:200]
                        )
                    else:
                        if not isinstance(parsed, dict):
                            parsed = {"_raw": parsed}
                        if current_id is not None:
                            self._last_event_id = current_id
                        yield (current_id, current_event, parsed)

                # Reset for the next event.
                current_id = None
                current_event = None
                current_data = None
                continue

            # SSE comment lines start with ':'
            if line.startswith(":"):
                continue

            # Parse field: value (first colon is the separator per SSE spec).
            colon_idx = line.find(":")
            if colon_idx < 0:
                # Malformed line with no colon -- skip.
                logger.debug("Skipping malformed SSE line: %s", line[:200])
                continue

            field = line[:colon_idx]
            # Per SSE spec, if value starts with a space after colon, strip it.
            value = line[colon_idx + 1 :]
            if value.startswith(" "):
                value = value[1:]

            if field == "id":
                current_id = value
            elif field == "event":
                current_event = value
            elif field == "data":
                # SSE allows multi-line data; concatenate with newline.
                if current_data is None:
                    current_data = value
                else:
                    current_data = f"{current_data}\n{value}"
            # Other fields (retry, etc.) are ignored.

        # If the stream ends without a trailing blank line, dispatch any
        # accumulated event.
        if current_event is not None and current_data is not None:
            try:
                parsed = json.loads(current_data)
            except (json.JSONDecodeError, ValueError):
                pass
            else:
                if not isinstance(parsed, dict):
                    parsed = {"_raw": parsed}
                if current_id is not None:
                    self._last_event_id = current_id
                yield (current_id, current_event, parsed)


__all__ = ["AguiStreamReader"]
