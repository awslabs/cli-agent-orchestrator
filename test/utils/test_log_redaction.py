"""Bearer credentials must never land in log lines.

``/agui/v1/stream?access_token=<JWT>`` carries the token in the query string
because browser ``EventSource`` cannot set an ``Authorization`` header. The
main app's uvicorn access log (and any app-level log that echoes a request
path) would otherwise persist the full JWT, replayable until ``exp``.

``RedactQueryTokenFilter`` scrubs ``access_token`` (and ``ticket``, reserved
for the planned short-lived-ticket handshake) values from every record that
passes through it, including uvicorn's percent-style access records where the
path arrives via ``record.args``.
"""

from __future__ import annotations

import logging

from cli_agent_orchestrator.utils.logging import REDACTED, RedactQueryTokenFilter


def _record(msg: str, args: tuple = ()) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args or None,
        exc_info=None,
    )


def _rendered(record: logging.LogRecord) -> str:
    assert RedactQueryTokenFilter().filter(record) is True  # never drops records
    return record.getMessage()


class TestRedactQueryTokenFilter:
    def test_scrubs_access_token_in_plain_message(self):
        out = _rendered(_record("GET /agui/v1/stream?access_token=eyJhbGciOi.abc.def HTTP/1.1"))
        assert "eyJhbGciOi" not in out
        assert f"access_token={REDACTED}" in out

    def test_scrubs_uvicorn_style_args(self):
        # uvicorn.access logs '%s - "%s %s HTTP/%s" %d' with the path in args.
        rec = _record(
            '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1:5", "GET", "/agui/v1/stream?since=t0&access_token=SECRET.J.WT", "1.1", 200),
        )
        out = _rendered(rec)
        assert "SECRET" not in out
        assert "since=t0" in out  # only the credential is scrubbed

    def test_scrubs_token_present_in_both_msg_and_args(self):
        # A pre-rendered msg AND a format-args path can each carry the token;
        # one filter pass must scrub both (a regression that fixed only one
        # branch would leak through the other).
        rec = _record(
            "retrying GET /agui/v1/stream?access_token=SECRET.J.WT after %s",
            ("GET /agui/v1/stream?access_token=SECRET.J.WT",),
        )
        out = _rendered(rec)
        assert "SECRET" not in out
        assert out.count(f"access_token={REDACTED}") == 2

    def test_scrubs_ticket_param_and_preserves_other_params(self):
        out = _rendered(_record("GET /agui/v1/stream?ticket=TKT123&since=x HTTP/1.1"))
        assert "TKT123" not in out
        assert "since=x" in out

    def test_plain_lines_untouched(self):
        msg = "GET /agui/v1/stream?since=2026-07-04T00:00:00Z HTTP/1.1"
        assert _rendered(_record(msg)) == msg

    def test_uvicorn_access_logger_is_wired(self):
        # The filter must be attached by install_access_log_redaction().
        from cli_agent_orchestrator.utils.logging import install_access_log_redaction

        install_access_log_redaction()
        access_logger = logging.getLogger("uvicorn.access")
        assert any(isinstance(f, RedactQueryTokenFilter) for f in access_logger.filters)
        # Idempotent: calling twice must not stack duplicate filters.
        install_access_log_redaction()
        count = sum(isinstance(f, RedactQueryTokenFilter) for f in access_logger.filters)
        assert count == 1
