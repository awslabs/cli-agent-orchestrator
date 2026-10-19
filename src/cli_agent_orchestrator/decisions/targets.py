"""Target source seam; only the reserved ephemeral namespace implies a source."""

from enum import Enum

from cli_agent_orchestrator.utils import agent_profiles


class TargetKind(str, Enum):
    INSTALLED = "installed"
    EPHEMERAL = "ephemeral"


def profile_source(name: str | None) -> TargetKind:
    # Classify by name alone, as the launch path does: a reserved name is ephemeral even when
    # its live file is missing or malformed, and no profile file is read here.
    if name is not None and agent_profiles.routes_to_ephemeral_store(name):
        return TargetKind.EPHEMERAL
    return TargetKind.INSTALLED
