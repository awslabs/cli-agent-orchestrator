"""Filesystem-path helpers shared across services and utils."""

import os
from pathlib import Path


def normalized_path(path: "str | Path") -> str:
    """Canonical form for comparing configured directory paths (GH #280/#281).

    Disabled agent-profile directories are stored as the exact strings the UI
    sends, but the same directory can be reached via a different spelling
    (``~``, trailing slash, a symlink; e.g. the local agent-store is also a
    provider default). ``realpath`` + ``expanduser`` canonicalizes all of
    those, so the disable check matches whenever two spellings reach the same
    physical directory.

    Lives here (rather than in ``utils.agent_profiles`` or
    ``services.settings_service``) so both can import it without reaching into
    each other's private API.
    """
    return os.path.realpath(os.path.expanduser(str(path)))
