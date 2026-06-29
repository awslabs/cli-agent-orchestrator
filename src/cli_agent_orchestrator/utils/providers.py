"""Shared provider-installation helpers.

Single source of truth for the provider → CLI-binary mapping and the
``shutil.which`` installation check. Both the ``/agents/providers`` API
endpoint and the install-aware default fallback (``resolve_provider``) consume
this so the binary map never drifts between them.
"""

import shutil
from typing import Dict, List

# Provider name → the executable looked up on PATH to decide "installed".
# Keep in sync with the ProviderType enum; a provider absent here is treated as
# "binary unknown" (reported uninstalled).
PROVIDER_BINARIES: Dict[str, str] = {
    "kiro_cli": "kiro-cli",
    "claude_code": "claude",
    "q_cli": "q",
    "codex": "codex",
    "gemini_cli": "gemini",
    "hermes": "hermes",
    "kimi_cli": "kimi",
    "copilot_cli": "copilot",
    "opencode_cli": "opencode",
    "cursor_cli": "agent",
    "antigravity_cli": "agy",
}


def provider_binary(name: str) -> str | None:
    """Return the CLI binary name for a provider, or None if unknown."""
    return PROVIDER_BINARIES.get(name)


def provider_binary_installed(name: str) -> bool:
    """Return True when the provider's CLI binary is present on PATH.

    Unknown providers (no entry in ``PROVIDER_BINARIES``) return False.
    """
    binary = PROVIDER_BINARIES.get(name)
    if binary is None:
        return False
    return shutil.which(binary) is not None


def installed_providers() -> List[str]:
    """Return the list of known providers whose CLI binary is on PATH."""
    return [name for name in PROVIDER_BINARIES if provider_binary_installed(name)]
