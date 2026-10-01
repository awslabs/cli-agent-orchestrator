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

import pytest

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

    def test_scrubs_websocket_token_param(self):
        # The terminal PTY WebSocket reads its bearer from ``?token=`` (browser
        # WebSocket cannot set an Authorization header). uvicorn logs the
        # handshake with the raw path+query -- on ``uvicorn.error``, not the
        # access logger; the wiring tests below cover that half.
        rec = _record(
            '%s - "WebSocket %s" [accepted]',
            ("127.0.0.1:5", "/terminals/abcdef12/ws?token=eyJhbGciOi.abc.def"),
        )
        out = _rendered(rec)
        assert "eyJhbGciOi" not in out
        assert f"token={REDACTED}" in out

    def test_access_token_is_not_double_scrubbed_by_token(self):
        # ``token`` is a suffix of ``access_token``. The ``\b`` anchor keeps the
        # match on the whole parameter name, so one credential is scrubbed once
        # (a second substitution would leave a mangled ``access_token=`` prefix).
        out = _rendered(_record("GET /agui/v1/stream?access_token=SECRET.J.WT HTTP/1.1"))
        assert out.count(REDACTED) == 1
        assert f"access_token={REDACTED}" in out

    @pytest.mark.parametrize(
        "encoded_name",
        ["%61ccess_token", "access%5Ftoken", "%61%63%63%65%73%73%5F%74%6F%6B%65%6E"],
    )
    def test_scrubs_percent_encoded_parameter_names(self, encoded_name):
        # Starlette percent-decodes query NAMES before lookup, so an encoded
        # spelling authenticates exactly like the plain one; uvicorn logs the
        # raw bytes. Found by the #706 review: ``?%61ccess_token=`` walked past
        # a literal-name pattern.
        out = _rendered(_record(f"GET /agui/v1/stream?{encoded_name}=SECRET.J.WT&since=x HTTP/1.1"))
        assert "SECRET" not in out
        assert f"{encoded_name}={REDACTED}" in out
        assert "since=x" in out

    def test_scrubs_percent_encoded_websocket_token_name(self):
        rec = _record(
            '%s - "WebSocket %s" [accepted]',
            ("127.0.0.1:5", "/terminals/abcdef12/ws?%74oken=eyJhbGciOi.SECRET.WS"),
        )
        out = _rendered(rec)
        assert "SECRET" not in out
        assert f"%74oken={REDACTED}" in out

    def test_unrelated_percent_encoded_name_is_left_alone(self):
        msg = "GET /agui/v1/stream?%73ince=2026-07-04 HTTP/1.1"
        assert _rendered(_record(msg)) == msg

    def test_scrubs_ticket_param_and_preserves_other_params(self):
        out = _rendered(_record("GET /agui/v1/stream?ticket=TKT123&since=x HTTP/1.1"))
        assert "TKT123" not in out
        assert "since=x" in out

    def test_plain_lines_untouched(self):
        msg = "GET /agui/v1/stream?since=2026-07-04T00:00:00Z HTTP/1.1"
        assert _rendered(_record(msg)) == msg

    @pytest.mark.parametrize("logger_name", ["uvicorn.access", "uvicorn.error"])
    def test_uvicorn_request_loggers_are_wired(self, logger_name):
        # The filter must be attached by install_access_log_redaction() to BOTH
        # loggers uvicorn writes request lines on: HTTP requests on
        # ``uvicorn.access``, WebSocket handshakes on ``uvicorn.error``.
        from cli_agent_orchestrator.utils.logging import install_access_log_redaction

        install_access_log_redaction()
        logger_ = logging.getLogger(logger_name)
        assert any(isinstance(f, RedactQueryTokenFilter) for f in logger_.filters)
        # Idempotent: calling twice must not stack duplicate filters.
        install_access_log_redaction()
        count = sum(isinstance(f, RedactQueryTokenFilter) for f in logger_.filters)
        assert count == 1

    def test_websocket_handshake_logged_on_uvicorn_error_is_redacted(self):
        # Exercises the WIRING, not the regex: log uvicorn's exact handshake
        # record through the ``uvicorn.error`` logger with a capture handler
        # and assert the token never reaches the handler. A filter attached
        # only to ``uvicorn.access`` passes this class's other tests and fails
        # this one, because a logging.Filter never sees a sibling logger's
        # records.
        from cli_agent_orchestrator.utils.logging import install_access_log_redaction

        install_access_log_redaction()
        logger_ = logging.getLogger("uvicorn.error")
        captured: list[str] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(record.getMessage())

        handler = _Capture()
        logger_.addHandler(handler)
        previous_level = logger_.level
        logger_.setLevel(logging.INFO)
        try:
            logger_.info(
                '%s - "WebSocket %s" [accepted]',
                "127.0.0.1:5",
                "/terminals/abcdef12/ws?token=eyJhbGciOi.SECRET.WS",
            )
        finally:
            logger_.removeHandler(handler)
            logger_.setLevel(previous_level)

        assert captured, "the handler saw no record; the filter must redact, never drop"
        assert "SECRET" not in captured[0]
        assert f"token={REDACTED}" in captured[0]


class TestRedactCapabilityPath:
    """PR #453 review (haofeif): the job_id is a PATH segment, not a query param.

    ``GET /handoff-results/<job_id>`` puts the sole retrieval capability for a
    row of raw worker output directly in the path uvicorn logs verbatim, where
    ``_CREDENTIAL_RE`` -- which only matches ``name=value`` query pairs -- cannot
    reach it. Truncating the id in the application's own warning is not enough
    while the access log still records it in full.
    """

    JOB_ID = "cafe1234cafe1234cafe1234cafe1234"

    def test_scrubs_job_id_in_plain_message(self):
        out = _rendered(_record(f"GET /handoff-results/{self.JOB_ID} HTTP/1.1"))
        assert self.JOB_ID not in out
        assert f"/handoff-results/{REDACTED}" in out

    def test_scrubs_job_id_in_uvicorn_style_args(self):
        # The real access-log shape: path arrives via record.args, not msg.
        rec = _record(
            '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1:5", "GET", f"/handoff-results/{self.JOB_ID}", "1.1", 200),
        )
        out = _rendered(rec)
        assert self.JOB_ID not in out
        assert f"/handoff-results/{REDACTED}" in out

    def test_scrubs_job_id_with_trailing_query_string(self):
        # Stopping at '?' keeps the id redacted without swallowing the query.
        out = _rendered(_record(f"GET /handoff-results/{self.JOB_ID}?x=1 HTTP/1.1"))
        assert self.JOB_ID not in out
        assert "x=1" in out

    def test_route_prefix_itself_is_preserved(self):
        # Operators still need to see WHICH route was called; only the id goes.
        out = _rendered(_record(f"GET /handoff-results/{self.JOB_ID} HTTP/1.1"))
        assert "/handoff-results/" in out

    def test_both_credential_classes_scrubbed_in_one_pass(self):
        # A query credential and a path capability in the same record: fixing
        # only one class would leak the other.
        out = _rendered(
            _record(f"GET /handoff-results/{self.JOB_ID}?access_token=SECRET.J.WT HTTP/1.1")
        )
        assert self.JOB_ID not in out
        assert "SECRET" not in out
