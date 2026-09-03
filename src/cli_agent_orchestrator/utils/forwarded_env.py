"""Validation for operator-forwarded session environment variables.

The same constraints are enforced by every client entry point that forwards
env vars into a launched session -- ``cao launch --env``
(``cli/commands/launch.py``) and the ops-MCP ``launch_session`` tool -- and
mirror the server-side filtering in ``TmuxClient._merge_extra_env``. Keeping the
canonical constants and the validator here stops the two client paths from
drifting apart. See issue #248.

The server silently *drops* a var that violates these rules
(``SessionEnvStore._merge_extra_env``), so each client validates at its own
boundary and surfaces a clear error instead of letting a forwarded var vanish.

Error messages name the offending KEY and the violated rule only; they never
echo the VALUE, so a secret passed as a value cannot leak into an error string.
"""

from typing import Dict, Mapping

# Prefixes reserved for provider-managed env; forwarding them is rejected so an
# operator cannot clobber the provider's own auth/config vars. Mirrored in
# ``TmuxClient._merge_extra_env`` server-side.
FORWARDED_ENV_BLOCKED_PREFIXES = ("CLAUDE", "CODEX_", "__MISE_")

# Explicit exceptions to the blocked prefixes: the documented Claude Code
# auth-routing flags an operator legitimately needs to forward.
FORWARDED_ENV_PREFIX_ALLOWLIST = frozenset(
    {
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
        "CLAUDE_CODE_SKIP_VERTEX_AUTH",
        "CLAUDE_CODE_SKIP_FOUNDRY_AUTH",
    }
)

# Per-value byte cap. Forwarded vars ride the ``tmux new-session -e`` argv, so an
# oversized value risks the kernel "command too long" limit (see PR #246).
FORWARDED_ENV_MAX_VALUE_BYTES = 2048


class ForwardedEnvError(ValueError):
    """A forwarded env var violates the forwarding constraints.

    Subclasses ``ValueError`` so callers that only catch ``ValueError`` still
    work; the message names the key and rule but never the value.
    """


def _is_valid_env_key(key: str) -> bool:
    """POSIX env-name shape: leading letter/underscore, then ASCII alnum/underscore.

    Stricter than ``str.isidentifier`` only in forbidding non-ASCII.
    """
    return bool(
        key
        and (key[0].isalpha() or key[0] == "_")
        and all(c.isalnum() or c == "_" for c in key)
        and key.isascii()
    )


def _uses_blocked_prefix(key: str) -> bool:
    if key in FORWARDED_ENV_PREFIX_ALLOWLIST:
        return False
    return any(key.startswith(p) for p in FORWARDED_ENV_BLOCKED_PREFIXES)


def validate_forwarded_env(mapping: Mapping[str, str]) -> Dict[str, str]:
    """Validate an already-parsed env mapping; return it as a plain dict.

    Raises ``ForwardedEnvError`` on the first offending entry:
      * a key that is not a ``[A-Za-z_][A-Za-z0-9_]*`` ASCII identifier,
      * a key using a blocked provider prefix (outside the allowlist),
      * a value whose UTF-8 encoding is >= ``FORWARDED_ENV_MAX_VALUE_BYTES``.
    """
    validated: Dict[str, str] = {}
    for key, value in mapping.items():
        if not _is_valid_env_key(key):
            raise ForwardedEnvError(f"env key must match [A-Za-z_][A-Za-z0-9_]* (got {key!r})")
        if _uses_blocked_prefix(key):
            raise ForwardedEnvError(
                f"env key {key!r} uses a blocked prefix "
                f"({', '.join(FORWARDED_ENV_BLOCKED_PREFIXES)}) reserved for provider env"
            )
        if len(value.encode("utf-8")) >= FORWARDED_ENV_MAX_VALUE_BYTES:
            raise ForwardedEnvError(
                f"env value for {key!r} exceeds {FORWARDED_ENV_MAX_VALUE_BYTES} bytes "
                "(tmux argv limit, PR #246)"
            )
        validated[key] = value
    return validated
