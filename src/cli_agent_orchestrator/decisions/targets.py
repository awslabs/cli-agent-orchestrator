"""Target source seam; names never imply source."""

from enum import Enum


class TargetKind(str, Enum):
    INSTALLED = "installed"
    EPHEMERAL = "ephemeral"


def profile_source(name: str | None) -> TargetKind:
    return TargetKind.INSTALLED
