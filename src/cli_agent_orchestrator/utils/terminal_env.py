"""Environment policy shared by every terminal backend.

A backend (tmux, herdr) builds the environment of a terminal it launches from
layers merged in a fixed order; this module owns the parts of that policy the
backends must apply identically, so there is one implementation to test rather
than one copy per backend:

- the per-value byte cap, and the one warning every channel logs when it drops
  a value over it (:func:`within_value_cap`);
- the agent profile's own ``env:`` declaration (:func:`merge_profile_env`).

Profile env is installed configuration -- a profile can already launch
arbitrary executables through ``mcpServers.command`` -- so, unlike
operator-forwarded env, it is not filtered by the inherited-env prefix
blocklist (``CLAUDE*``, ``CODEX_*``, ...). The byte cap still applies: it
protects the backend argv limit, not a trust boundary.
"""

import logging
from typing import Dict, List, Mapping, Optional

from cli_agent_orchestrator.utils.forwarded_env import FORWARDED_ENV_MAX_VALUE_BYTES

logger = logging.getLogger(__name__)

# Per-value byte cap (PR #246). Every value rides the backend's create argv
# (``tmux new-session -e`` / ``new-window -e``, ``herdr ... --env``), so the
# server enforces the same limit the client-side validator rejects at.
MAX_ENV_VALUE_BYTES = FORWARDED_ENV_MAX_VALUE_BYTES


def within_value_cap(source: str, key: str, value: str) -> bool:
    """Return True if ``value`` fits the byte cap; otherwise log the drop.

    ``source`` names the channel (``"forwarded"``, ``"profile"``) so every
    backend and channel reports a dropped value with the same words. The
    warning names the key only, never the value, which may be a secret.
    """
    if len(value.encode("utf-8")) < MAX_ENV_VALUE_BYTES:
        return True
    logger.warning(
        "Dropping %s env var %s — value exceeds %d bytes", source, key, MAX_ENV_VALUE_BYTES
    )
    return False


def merge_profile_env(
    environment: Dict[str, str], profile_env: Optional[Mapping[str, str]]
) -> List[str]:
    """Merge a profile's ``env:`` into ``environment`` in place.

    Called after operator env is merged, so the more specific per-agent
    declaration wins on conflict. Returns the keys actually written, in
    declaration order, so a caller can undo exactly those.
    """
    applied: List[str] = []
    for key, value in (profile_env or {}).items():
        if not within_value_cap("profile", key, value):
            continue
        environment[key] = value
        applied.append(key)
    return applied
