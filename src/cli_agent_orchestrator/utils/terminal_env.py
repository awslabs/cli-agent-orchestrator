"""Environment policy shared by every terminal backend.

A backend (tmux, herdr) builds the environment of a terminal it launches from
layers merged in a fixed order:

1. operator-forwarded env (``cao launch --env`` / the API's ``env_vars``),
   which is also the channel the runtime routing ids ride on
   (``CAO_WORKFLOW_*``, ``CAO_CALLBACK_*``);
2. the agent profile's own ``env:`` declaration (:func:`merge_profile_env`);
3. the terminal's runtime identity (:func:`apply_runtime_identity`).

This module owns the parts of that policy the backends must apply identically,
so there is one implementation to test rather than one copy per backend: the
per-value byte cap and the one warning every channel logs when it drops a
value (:func:`within_value_cap`), and layers 2 and 3.

Profile env is installed configuration -- a profile can already launch
arbitrary executables through ``mcpServers.command`` -- so, unlike
operator-forwarded env, it is not filtered by the inherited-env prefix
blocklist (``CLAUDE*``, ``CODEX_*``, ...). Two limits still apply: the byte
cap, which protects the backend argv limit rather than a trust boundary, and
the runtime-owned identity keys (``RUNTIME_IDENTITY_ENV_KEYS``), which a
profile can neither replace nor invent.
"""

import logging
from typing import Dict, List, Mapping, Optional

from cli_agent_orchestrator.constants import (
    RUNTIME_IDENTITY_ENV_KEYS,
    SESSION_NAME_ENV,
    TERMINAL_ID_ENV,
)
from cli_agent_orchestrator.utils.forwarded_env import FORWARDED_ENV_MAX_VALUE_BYTES

logger = logging.getLogger(__name__)

# Per-value byte cap (PR #246). Every value rides the backend's create argv
# (``tmux new-session -e`` / ``new-window -e``, ``herdr ... --env``), so the
# server enforces the same limit the client-side validator rejects at.
MAX_ENV_VALUE_BYTES = FORWARDED_ENV_MAX_VALUE_BYTES


def _log_drop(source: str, key: str, reason: str) -> None:
    """Log a dropped env var: the key and why, never the value (may be a secret)."""
    logger.warning("Dropping %s env var %s — %s", source, key, reason)


def within_value_cap(source: str, key: str, value: str) -> bool:
    """Return True if ``value`` fits the byte cap; otherwise log the drop.

    ``source`` names the channel (``"forwarded"``, ``"profile"``) so every
    backend and channel reports a dropped value with the same words.
    """
    if len(value.encode("utf-8")) < MAX_ENV_VALUE_BYTES:
        return True
    _log_drop(source, key, f"value exceeds {MAX_ENV_VALUE_BYTES} bytes")
    return False


def merge_profile_env(
    environment: Dict[str, str], profile_env: Optional[Mapping[str, str]]
) -> List[str]:
    """Merge a profile's ``env:`` into ``environment`` in place.

    Called after operator env is merged, so the more specific per-agent
    declaration wins on conflict -- except for runtime-owned identity keys,
    which are dropped whether or not the runtime set them. Returns the keys
    actually written, in declaration order, so a caller can undo exactly those.
    """
    applied: List[str] = []
    for key, value in (profile_env or {}).items():
        if key in RUNTIME_IDENTITY_ENV_KEYS:
            _log_drop("profile", key, "name is reserved for CAO's runtime identity")
            continue
        if not within_value_cap("profile", key, value):
            continue
        environment[key] = value
        applied.append(key)
    return applied


def apply_runtime_identity(
    environment: Dict[str, str], terminal_id: str, session_name: str
) -> None:
    """Write the terminal's identity into ``environment``, overriding any value.

    Called last, after every other layer, so neither operator nor profile env
    (nor a ``CAO_*`` var inherited from a cao-server that itself runs inside a
    CAO terminal) can make a terminal report someone else's identity.
    """
    environment[TERMINAL_ID_ENV] = terminal_id
    environment[SESSION_NAME_ENV] = session_name
