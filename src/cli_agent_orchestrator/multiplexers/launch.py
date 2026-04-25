"""Launch template helpers for backend-owned direct process spawns."""

from __future__ import annotations

import os
import shutil
import sys
from typing import Literal, Sequence

from cli_agent_orchestrator.multiplexers.base import LaunchSpec

# Avoid importing ProviderType from providers.manager to prevent circular imports
# (multiplexers.launch → providers.manager → multiplexers.launch).  Use a module-level
# constant instead so this string only appears once and a typo is caught at definition.
_PROVIDER_KEY_CODEX = "codex"


def default_platform() -> Literal["windows", "unix"]:
    return "windows" if sys.platform == "win32" else "unix"


def _windows_codex_candidates() -> tuple[str, ...]:
    username = os.environ.get("USERNAME") or os.environ.get("USER")
    candidates = [
        os.path.expandvars(
            r"%LOCALAPPDATA%\..\scoop\apps\nodejs-lts\current\bin\codex.cmd"
        )
    ]
    if username:
        candidates.append(
            rf"C:\Users\{username}\scoop\apps\nodejs-lts\current\bin\codex.cmd"
        )
    return tuple(candidates)


def build_launch_spec(
    provider: str,
    command_argv: Sequence[str],
    *,
    platform: Literal["windows", "unix"] | None = None,
) -> LaunchSpec:
    """Resolve a LaunchSpec for a provider on the current (or stated) platform.

    `command_argv[0]` is treated as the bare command name to resolve.
    The remaining elements are passed through verbatim.

    Resolver order (Windows):
      1. explicit ``CAO_<PROVIDER>_BIN`` env override
      2. ``where.exe <name>.cmd`` lookup (Scoop/Node shim discovery)
      3. fall back to bare ``command_argv[0]``

    On non-Windows: trust shell PATH (use ``command_argv[0]`` verbatim).
    """
    resolved_platform = platform or default_platform()
    argv = tuple(command_argv)
    if not argv:
        raise ValueError("command_argv must not be empty")

    if provider != _PROVIDER_KEY_CODEX or resolved_platform != "windows":
        return LaunchSpec(argv=argv, provider=provider)

    override = os.environ.get("CAO_CODEX_BIN")
    if override:
        return LaunchSpec(argv=(override, *argv[1:]), provider=provider)

    resolved = shutil.which("codex.cmd")
    if resolved:
        return LaunchSpec(argv=(resolved, *argv[1:]), provider=provider)

    for candidate in _windows_codex_candidates():
        if os.path.exists(candidate):
            return LaunchSpec(argv=(candidate, *argv[1:]), provider=provider)

    return LaunchSpec(argv=argv, provider=provider)
