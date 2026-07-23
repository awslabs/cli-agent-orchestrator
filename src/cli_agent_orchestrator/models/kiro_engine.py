"""Kiro CLI engine selection and deterministic resolution."""

from __future__ import annotations

from enum import Enum
from typing import Optional, Union


class KiroEngine(str, Enum):
    """The Kiro engines CAO can identify during the Phase 0 migration."""

    V2 = "v2"
    KAS = "kas"


EngineValue = Optional[Union[KiroEngine, str]]


def parse_kiro_engine(value: EngineValue) -> Optional[KiroEngine]:
    """Validate an optional engine value without accepting aliases."""
    if value is None:
        return None
    try:
        return KiroEngine(value)
    except ValueError as exc:
        raise ValueError(
            f"Invalid Kiro engine {value!r}; expected one of: "
            f"{KiroEngine.V2.value}, {KiroEngine.KAS.value}"
        ) from exc


def resolve_kiro_engine(
    explicit: EngineValue = None,
    profile: EngineValue = None,
    persisted: EngineValue = None,
) -> KiroEngine:
    """Resolve one engine at a creation or restoration boundary.

    A persisted value is authoritative for restoration. At creation boundaries,
    an explicit value and the selected profile must agree when both are present.
    Parent-terminal state is deliberately not an input: child creation never
    inherits an engine implicitly.
    """
    persisted_engine = parse_kiro_engine(persisted)
    if persisted_engine is not None:
        return persisted_engine

    explicit_engine = parse_kiro_engine(explicit)
    profile_engine = parse_kiro_engine(profile)
    if explicit_engine is not None and profile_engine is not None:
        if explicit_engine != profile_engine:
            raise ValueError(
                "Kiro engine conflict: explicit selection "
                f"{explicit_engine.value!r} differs from selected profile "
                f"{profile_engine.value!r}"
            )
    return explicit_engine or profile_engine or KiroEngine.V2
