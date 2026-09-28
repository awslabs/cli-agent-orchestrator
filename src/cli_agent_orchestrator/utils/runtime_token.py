"""Single owner-only source for the runtime-channel token (#745).

The runtime channel authenticates with a shared token. After startup that
token's VALUE lives only in an owner-only file — never in an environment
variable a child process could inherit, in argv, or in a persisted config.
Everything that needs it reads the file named by ``CAO_RUNTIME_TOKEN_FILE``.

``load_runtime_token`` accepts either configured source and normalizes both to
that shape:

- ``CAO_RUNTIME_TOKEN_FILE`` — already a path; it is read and left in place.
- ``CAO_RUNTIME_TOKEN`` — a raw value; it is written to the owner-only file,
  ``CAO_RUNTIME_TOKEN_FILE`` is pointed at it, and the value is dropped from the
  environment so no child inherits it.

Rotation means restarting the process: the token is read and cached once.
"""

import logging
import os
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

RUNTIME_TOKEN_ENV = "CAO_RUNTIME_TOKEN"
RUNTIME_TOKEN_FILE_ENV = "CAO_RUNTIME_TOKEN_FILE"

_LOCK = threading.Lock()
_LOADED = False
_TOKEN: Optional[str] = None


def load_runtime_token() -> Optional[str]:
    """Resolve the runtime token once and cache it. Idempotent and thread-safe.

    Returns the token value, or ``None`` when neither variable is set or the
    configured file is unreadable — in which case callers fail closed.
    """
    global _LOADED, _TOKEN
    with _LOCK:
        if _LOADED:
            return _TOKEN
        _TOKEN = _resolve()
        _LOADED = True
        return _TOKEN


def _resolve() -> Optional[str]:
    file_path = os.environ.get(RUNTIME_TOKEN_FILE_ENV, "").strip()
    if file_path:
        # Already a path: read it and leave the file where it is.
        try:
            return Path(file_path).read_text(encoding="utf-8").strip() or None
        except OSError:
            logger.warning(
                "could not read %s=%s; the runtime token is unavailable",
                RUNTIME_TOKEN_FILE_ENV,
                file_path,
                exc_info=True,
            )
            return None

    value = os.environ.get(RUNTIME_TOKEN_ENV, "").strip()
    if not value:
        return None

    # A raw value from the env: move it into an owner-only file and out of the
    # environment, so it survives startup only as a path.
    path = _write_token_file(value)
    if path:
        os.environ[RUNTIME_TOKEN_FILE_ENV] = path
        os.environ.pop(RUNTIME_TOKEN_ENV, None)
    return value


def _write_token_file(token: str) -> Optional[str]:
    """Write ``token`` to ``CAO_HOME_DIR/tmp/runtime-token`` (0600) and return its path."""
    try:
        from cli_agent_orchestrator.constants import CAO_HOME_DIR
        from cli_agent_orchestrator.utils.atomic_file import write_owner_only

        tmp_dir = Path(CAO_HOME_DIR) / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = tmp_dir / "runtime-token"
        write_owner_only(target, token)
        return str(target)
    except Exception:  # noqa: BLE001
        logger.warning("could not write the runtime token file", exc_info=True)
        return None


def runtime_token() -> Optional[str]:
    """The runtime token value, loading it on first use."""
    return load_runtime_token()


def runtime_token_file() -> Optional[str]:
    """Path to the owner-only token file, loading on first use, or ``None``."""
    load_runtime_token()
    path = os.environ.get(RUNTIME_TOKEN_FILE_ENV, "").strip()
    return path or None


def _reset_cache_for_tests() -> None:
    """Clear the process cache so a test can re-resolve under a fresh environment."""
    global _LOADED, _TOKEN
    with _LOCK:
        _LOADED = False
        _TOKEN = None
