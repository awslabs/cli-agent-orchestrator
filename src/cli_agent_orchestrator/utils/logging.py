import logging
import re
import sys
from datetime import datetime
from urllib.parse import unquote

from cli_agent_orchestrator.constants import LOG_DIR
from cli_agent_orchestrator.services.config_service import ConfigService

# Query parameters that carry bearer credentials. `access_token` is the AG-UI
# SSE pattern (browser EventSource cannot set an Authorization header);
# `token` is the terminal WebSocket's equivalent (browser WebSocket cannot set
# one either, and uvicorn logs the upgrade request's path+query the same way);
# `ticket` is reserved for the planned short-lived-ticket handshake.
_CREDENTIAL_PARAMS = frozenset({"access_token", "token", "ticket"})
REDACTED = "[REDACTED]"
# Every ``name=value`` pair as the server would parse it. The NAME is matched as
# a whole token (so ``access_token`` is one name, never ``token`` inside it) and
# is percent-decoded before the credential check: Starlette decodes query names,
# so ``?%61ccess_token=<JWT>`` authenticates exactly like ``?access_token=`` --
# and uvicorn logs the raw, still-encoded bytes, which a literal-name pattern
# would walk past (found by the #706 review).
_QUERY_PAIR_RE = re.compile(r"(?<![\w%.\-])([A-Za-z0-9_%.\-]+)=([^&\s\"']+)")


def _redact_pair(match: "re.Match[str]") -> str:
    name = match.group(1)
    if unquote(name).lower() in _CREDENTIAL_PARAMS:
        return f"{name}={REDACTED}"
    return match.group(0)


# Route families where a PATH SEGMENT is itself the credential, not a query
# parameter carrying one -- so ``_CREDENTIAL_RE`` above cannot reach it.
# ``GET /handoff-results/<job_id>`` (issue #447): job_id is the sole retrieval
# capability for a row that can hold raw worker output, and uvicorn logs the raw
# path, so an access-log reader would otherwise hold the full capability for
# every result fetched. Anchored on the route prefix and stopping at the next
# ``/``, ``?`` or whitespace so a longer path or a query string still redacts.
_CAPABILITY_PATH_RE = re.compile(r"(/handoff-results/)[^\s/?\"']+")


def _scrub(text: str) -> str:
    """Redact both credential query params and capability-bearing path segments."""
    text = _QUERY_PAIR_RE.sub(_redact_pair, text)
    return _CAPABILITY_PATH_RE.sub(rf"\g<1>{REDACTED}", text)


class RedactQueryTokenFilter(logging.Filter):
    """Scrub credentials from log records, whether in a query param or a path.

    Attached to ``uvicorn.access`` so ``GET /agui/v1/stream?access_token=<JWT>``
    lines never persist the token (uvicorn logs the raw path+query, and a JWT in
    an access log is replayable until ``exp``), to ``uvicorn.error`` so the
    ``"WebSocket /terminals/<id>/ws?token=<JWT>" [accepted]`` handshake line
    uvicorn emits there does not either, and so
    ``GET /handoff-results/<job_id>`` never persists the id that retrieves that
    job's worker output. Mutates the record in place and always returns True —
    this filter redacts, it never drops.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _scrub(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_scrub(arg) if isinstance(arg, str) else arg for arg in record.args)
        return True


#: Loggers uvicorn writes request lines to. HTTP requests go to ``uvicorn.access``;
#: WebSocket handshakes (``'%s - "WebSocket %s" [accepted]'`` and the ``403`` /
#: ``rejected`` variants) go to ``uvicorn.error`` from every websocket
#: implementation, because ``h11_impl`` hands the upgrade off before its
#: access-log call. A ``logging.Filter`` sees only records emitted on the logger
#: it is attached to, never a sibling's, so both need the filter.
_UVICORN_REQUEST_LOGGERS = ("uvicorn.access", "uvicorn.error")


def install_access_log_redaction() -> None:
    """Attach the credential filter to every uvicorn request logger (idempotent)."""
    for name in _UVICORN_REQUEST_LOGGERS:
        logger_ = logging.getLogger(name)
        if not any(isinstance(f, RedactQueryTokenFilter) for f in logger_.filters):
            logger_.addFilter(RedactQueryTokenFilter())


def setup_logging() -> None:
    """Setup logging configuration."""
    log_level = str(ConfigService.get("logging.level", default="INFO")).upper()

    # Ensure log directory exists
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_file = LOG_DIR / f"cao_{timestamp}.log"

    fmt = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

    # Stream handler: WARNING+ always goes to stderr so operationally-relevant
    # events surface on the console (and in a subprocess's captured stdout/stderr,
    # which the e2e harness asserts on) rather than being buried in the log file.
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.WARNING)
    stderr_handler.setFormatter(logging.Formatter(fmt))

    logging.basicConfig(
        level=log_level,
        format=fmt,
        handlers=[logging.FileHandler(log_file), stderr_handler],
    )

    print(f"Server logs: {log_file}")
    print("For debug logs: export CAO_LOG_LEVEL=DEBUG && cao-server")
    logging.info(f"Logging to: {log_file}")
