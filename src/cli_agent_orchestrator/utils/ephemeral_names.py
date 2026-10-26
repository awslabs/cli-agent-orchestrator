"""Pure ephemeral namespace matching, safe for HTTP-only MCP callers."""

import re

_RESERVED_EPHEMERAL_PATTERN = re.compile(
    r"^[A-Z][A-Za-z0-9]{1,23}-[a-z][a-z0-9_]{2,31}-[0-9a-f]{4}$"
)


def routes_to_ephemeral_store(name: str) -> bool:
    """Route reserved names exclusively to the server-owned live store."""
    return _RESERVED_EPHEMERAL_PATTERN.fullmatch(name) is not None
