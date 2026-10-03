"""The shared runtime-channel token (#745).

Configured as ``CAO_RUNTIME_TOKEN_FILE`` (the path of a file holding the token,
e.g. a mounted Kubernetes Secret) or ``CAO_RUNTIME_TOKEN``. Both are removed
from this process's environment once read, so tmux panes and agent processes
started later do not inherit it. Read once per process; rotating it means a
restart.
"""

import logging
import os
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

TOKEN_ENV = "CAO_RUNTIME_TOKEN"
TOKEN_FILE_ENV = "CAO_RUNTIME_TOKEN_FILE"
#: The WebSocket handshake header a runtime presents the token in.
TOKEN_HEADER = "x-cao-runtime-token"

_lock = threading.Lock()
_loaded = False
_token: Optional[str] = None


def runtime_token() -> Optional[str]:
    """The configured token, or ``None`` when none is configured or it is unreadable."""
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
            logger.error("cannot read %s=%s: %s", TOKEN_FILE_ENV, path, exc)
            return None
    return value or None


def _reset_for_tests() -> None:
    global _loaded, _token
    with _lock:
        _loaded = False
        _token = None
