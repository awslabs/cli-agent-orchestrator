"""The shared runner-channel token, read once and removed from the environment.

Configured as ``CAO_RUNNER_TOKEN_FILE`` (the path of a file holding the token,
e.g. a mounted Kubernetes Secret) or ``CAO_RUNNER_TOKEN``; the file wins when
both are set. Both variables are removed from this process's environment once
read, on the server and on a runner alike: tmux copies ``CAO_*`` variables into
every pane it starts, so an agent would otherwise inherit the token, or the
path to it. Read once per process; rotating it means a restart.

The hub's optional TLS material (``CAO_RUNNER_TLS_CERT``/``CAO_RUNNER_TLS_KEY``)
is read here too, as part of the same channel configuration.

The file is the preferred form. An environment value is visible in
``/proc/<pid>/environ`` for the life of the process even after it is removed
from ``os.environ``; a file can be readable by the runner's user alone.
"""

import logging
import os
import ssl
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

TOKEN_ENV = "CAO_RUNNER_TOKEN"
TOKEN_FILE_ENV = "CAO_RUNNER_TOKEN_FILE"
TLS_CERT_ENV = "CAO_RUNNER_TLS_CERT"
TLS_KEY_ENV = "CAO_RUNNER_TLS_KEY"

_lock = threading.Lock()
_loaded = False
_token: Optional[str] = None


def runner_token() -> Optional[str]:
    """The configured token, or None when none is configured or it is unreadable."""
    global _loaded, _token
    with _lock:
        if not _loaded:
            _token = _read()
            _loaded = True
        return _token


def _read() -> Optional[str]:
    value = os.environ.pop(TOKEN_ENV, "").strip()
    # Removed too: a later child need not learn where the token file is.
    path = os.environ.pop(TOKEN_FILE_ENV, "").strip()
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip() or None
        except OSError as exc:
            # Fail closed: a configured file that cannot be read is not
            # silently replaced by the environment value.
            logger.error("cannot read %s=%s: %s", TOKEN_FILE_ENV, path, exc)
            return None
    return value or None


def runner_hub_ssl_context() -> Optional[ssl.SSLContext]:
    """The hub's TLS context from ``CAO_RUNNER_TLS_CERT``/``_KEY``; None for ws://.

    A certificate without a key (or the reverse) is a configuration error, not
    a silent fall back to plaintext.
    """
    cert = os.environ.get(TLS_CERT_ENV, "").strip()
    key = os.environ.get(TLS_KEY_ENV, "").strip()
    if not cert and not key:
        return None
    if not cert:
        raise ValueError(f"{TLS_KEY_ENV} is set but {TLS_CERT_ENV} is not")
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(cert, key or None)
    return context


def _reset_for_tests() -> None:
    global _loaded, _token
    with _lock:
        _loaded = False
        _token = None
