"""Kiro CLI engine selection and deterministic resolution."""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional, Union

from pydantic import GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema

# Input spellings that name an engine under another value. ``kas`` was CAO's
# first name for Kiro's v3 engine (#470); Kiro's own flags, docs and help say
# v3. It stays accepted on input and is never stored or returned (#1167).
_ENGINE_ALIASES = {"kas": "v3"}


class KiroEngine(str, Enum):
    """The Kiro agent engines CAO can select.

    ``KAS`` is a Python alias of ``V3`` (one member, one stored spelling), so
    ``KiroEngine.KAS is KiroEngine.V3`` and existing imports keep working.
    """

    V2 = "v2"
    V3 = "v3"
    KAS = "v3"

    @classmethod
    def _missing_(cls, value: object) -> Optional["KiroEngine"]:
        # pydantic-core and FastAPI consult this hook when a value is not a
        # member value, so every field typed ``KiroEngine`` (API bodies and
        # query parameters, profiles, flows, workflow steps, terminal models)
        # accepts the ``kas`` alias and validates it to ``V3``. Provisional
        # mechanism for the before-validator of #1167 Phase 1 (A16).
        if isinstance(value, str):
            canonical = canonical_kiro_engine(value)
            if canonical != value:
                return cls(canonical)
        return None

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        # pydantic lists every ``__members__`` value, aliases included, so the
        # schema would say ``["v2", "v3", "v3"]``. List each value once.
        json_schema = handler.resolve_ref_schema(handler(core_schema))
        if isinstance(json_schema.get("enum"), list):
            json_schema["enum"] = list(dict.fromkeys(json_schema["enum"]))
        return json_schema


EngineValue = Optional[Union[KiroEngine, str]]


def canonical_kiro_engine(value: Any) -> Optional[str]:
    """Return the stored spelling of an engine value WITHOUT validating it.

    A member maps to its value and an alias to its target (``"kas"`` to
    ``"v3"``); any other string is returned unchanged, so an invalid value
    stays itself for the caller that owns the validation decision. Pure: the
    idempotency fingerprint relies on that.
    """
    if value is None:
        return None
    if isinstance(value, KiroEngine):
        return value.value
    text = str(value)
    return _ENGINE_ALIASES.get(text, text)


def parse_kiro_engine(value: EngineValue) -> Optional[KiroEngine]:
    """Validate an optional engine value, mapping the ``kas`` alias to v3."""
    canonical = canonical_kiro_engine(value)
    if canonical is None:
        return None
    try:
        return KiroEngine(canonical)
    except ValueError as exc:
        raise ValueError(
            f"Invalid Kiro engine {value!r}; expected one of: "
            f"{KiroEngine.V2.value}, {KiroEngine.V3.value} "
            "('kas' is an alias of 'v3')"
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
