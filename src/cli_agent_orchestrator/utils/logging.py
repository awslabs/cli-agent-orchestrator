import logging
import re
import sys
from datetime import datetime

from cli_agent_orchestrator.constants import LOG_DIR
from cli_agent_orchestrator.services.config_service import ConfigService

# Query parameters that carry bearer credentials. `access_token` is the AG-UI
# SSE pattern (browser EventSource cannot set an Authorization header);
# `ticket` is reserved for the planned short-lived-ticket handshake.
_CREDENTIAL_PARAMS = ("access_token", "ticket")
REDACTED = "[REDACTED]"
_CREDENTIAL_RE = re.compile(
    r"\b(" + "|".join(_CREDENTIAL_PARAMS) + r")=([^&\s\"']+)",
)


class RedactQueryTokenFilter(logging.Filter):
    """Scrub credential-bearing query parameters from log records.

    Attached to ``uvicorn.access`` so ``GET /agui/v1/stream?access_token=<JWT>``
    lines never persist the token (uvicorn logs the raw path+query, and a JWT in
    an access log is replayable until ``exp``). Mutates the record in place and
    always returns True — this filter redacts, it never drops.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str) and _CREDENTIAL_RE.search(record.msg):
            record.msg = _CREDENTIAL_RE.sub(rf"\1={REDACTED}", record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(
                _CREDENTIAL_RE.sub(rf"\1={REDACTED}", arg) if isinstance(arg, str) else arg
                for arg in record.args
            )
        return True


def install_access_log_redaction() -> None:
    """Attach the credential filter to uvicorn's access logger (idempotent)."""
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactQueryTokenFilter) for f in access_logger.filters):
        access_logger.addFilter(RedactQueryTokenFilter())


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
