"""Pure primitives for gated agent-profile value resolution."""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from enum import Enum
from string import Template
from types import MappingProxyType
from typing import Any, Callable, TypeAlias

ProfilePath: TypeAlias = tuple[str | int, ...]

_SAFE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_ASCII_NAME_START = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_")
_ASCII_NAME_CONTINUE = _ASCII_NAME_START | frozenset("0123456789")
_PROFILE_VALUE_MAX_DEPTH = 64


class DestinationClass(str, Enum):
    """Closed destination classes used by profile value policy."""

    MCP_ENV = "mcp_env"
    AUTHORITY = "authority"
    MAPPING_KEY = "mapping_key"
    BODY = "body"
    UNCLASSIFIED = "unclassified"


class NodeRole(str, Enum):
    """Structural role of a profile node."""

    VALUE = "value"
    KEY = "key"
    BODY = "body"


class ReferenceForm(str, Enum):
    """Supported profile-reference spellings."""

    BARE = "bare"
    BRACED = "braced"


BODY_PATH: tuple[str, ...] = ("system_prompt",)
ValueResolver: TypeAlias = Callable[[ProfilePath, DestinationClass, Any], Any]


class ProfileValueRefusal(Exception):
    """Safe local refusal; service-layer translation is intentionally external."""

    def __init__(self, code: str, destination: DestinationClass) -> None:
        self.code = code
        self.destination = destination
        super().__init__(code, destination.value)

    def __str__(self) -> str:
        return f"{self.code}:{self.destination.value}"

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(code={self.code!r}, " f"destination={self.destination.value!r})"
        )


def env_slot_key(path: ProfilePath) -> str | None:
    """Return the MCP environment key for an exact structural env-slot path."""

    if (
        len(path) == 4
        and path[0] == "mcpServers"
        and type(path[1]) is str
        and path[2] == "env"
        and type(path[3]) is str
    ):
        return path[3]
    return None


def structural_destination(
    path: ProfilePath,
    *,
    role: NodeRole,
) -> DestinationClass:
    """Classify a node solely from its syntax-tree position."""

    if role is NodeRole.BODY:
        return DestinationClass.BODY
    if role is NodeRole.KEY:
        return DestinationClass.MAPPING_KEY
    if env_slot_key(path) is not None:
        return DestinationClass.MCP_ENV
    return DestinationClass.UNCLASSIFIED


class AuthorityResolver:
    """Resolve admitted profile references from immutable authority snapshots."""

    def __init__(
        self,
        *,
        authority_env: Mapping[str, str],
        credential_names: Collection[str],
    ) -> None:
        if not isinstance(authority_env, Mapping):
            raise ValueError("authority_env must be a mapping")
        if isinstance(
            credential_names,
            (str, bytes, bytearray, memoryview, Mapping),
        ) or not isinstance(credential_names, Collection):
            raise ValueError("credential_names must be a non-string collection")

        items = list(authority_env.items())
        names = list(credential_names)
        if any(
            type(key) is not str or type(value) is not str or _SAFE_NAME.fullmatch(key) is None
            for key, value in items
        ):
            raise ValueError("authority_env contains an invalid entry")
        if any(type(name) is not str or _SAFE_NAME.fullmatch(name) is None for name in names):
            raise ValueError("credential_names contains an invalid entry")

        authority = dict(items)
        credentials = frozenset(names)
        if authority.keys() & credentials:
            raise ValueError("authority and credential names must be disjoint")

        self._authority = MappingProxyType(authority)
        self._credentials = credentials
        self._accepted: dict[ProfilePath, str] = {}

    @property
    def accepted(self) -> Mapping[ProfilePath, str]:
        """Return a fresh read-only snapshot of accepted authority names."""

        return MappingProxyType(dict(self._accepted))

    def reset(self) -> None:
        """Clear per-parse acceptance records without changing policy."""

        self._accepted.clear()

    def resolve(
        self,
        path: ProfilePath,
        destination: DestinationClass,
        raw_scalar: Any,
    ) -> Any:
        if destination is DestinationClass.AUTHORITY:
            raise ValueError("authority is a refined destination")
        if destination is DestinationClass.BODY and path != BODY_PATH:
            raise ValueError("body destination requires the body path")
        if destination is DestinationClass.MCP_ENV and env_slot_key(path) is None:
            raise ValueError("mcp env destination requires an env-slot path")

        if destination is DestinationClass.MCP_ENV and type(raw_scalar) is not str:
            self._refuse("mcp_env_value_not_plain_string", destination)
        if type(raw_scalar) is not str:
            return raw_scalar

        slot_key = env_slot_key(path)
        effective: DestinationClass = destination
        if destination is DestinationClass.MCP_ENV and slot_key in self._authority:
            effective = DestinationClass.AUTHORITY

        if destination is DestinationClass.BODY:
            self._inspect_body(raw_scalar)
            return raw_scalar

        references = self._metadata_references(raw_scalar, destination)
        if not references:
            return raw_scalar

        names = [name for _, name in references]
        if any(name in self._credentials for name in names):
            if effective in (
                DestinationClass.MCP_ENV,
                DestinationClass.AUTHORITY,
            ):
                self._refuse(
                    "mcp_credential_destination_unsupported",
                    destination,
                )
            if effective is DestinationClass.MAPPING_KEY:
                self._refuse("credential_reference_in_key", destination)
            self._refuse("credential_destination_unsupported", destination)

        if any(name not in self._authority for name in names):
            self._refuse("credential_reference_unsupported", destination)

        if effective is DestinationClass.MAPPING_KEY:
            self._refuse("credential_reference_in_key", destination)
        if effective is not DestinationClass.AUTHORITY:
            self._refuse("authority_destination_unclassified", destination)

        assert slot_key is not None
        if len(references) != 1 or not self._is_whole_reference(
            raw_scalar,
            references[0],
        ):
            self._refuse("credential_reference_unsupported", destination)
        name = references[0][1]
        if name != slot_key:
            self._refuse("authority_reference_alias", destination)

        self._accepted[path] = name
        return self._authority[name]

    @staticmethod
    def _refuse(code: str, destination: DestinationClass) -> None:
        raise ProfileValueRefusal(code, destination) from None

    @classmethod
    def _metadata_references(
        cls,
        raw: str,
        destination: DestinationClass,
    ) -> list[tuple[ReferenceForm, str]]:
        matches = list(Template.pattern.finditer(raw))
        if any(match.group("invalid") is not None for match in matches):
            cls._refuse("credential_reference_unsupported", destination)

        references: list[tuple[ReferenceForm, str]] = []
        for match in matches:
            named = match.group("named")
            braced = match.group("braced")
            if named is not None:
                references.append((ReferenceForm.BARE, named))
            elif braced is not None:
                references.append((ReferenceForm.BRACED, braced))
        return references

    @staticmethod
    def _is_whole_reference(
        raw: str,
        reference: tuple[ReferenceForm, str],
    ) -> bool:
        form, name = reference
        if form is ReferenceForm.BARE:
            return raw == f"${name}"
        return raw == f"${{{name}}}"

    def _inspect_body(self, raw: str) -> None:
        index = 0
        while index < len(raw):
            if raw[index] != "$":
                index += 1
                continue

            next_index = index + 1
            if next_index >= len(raw):
                index += 1
                continue

            if raw[next_index] == "{":
                closing = raw.find("}", next_index + 1)
                if closing == -1:
                    index += 1
                    continue
                name = raw[next_index + 1 : closing]
                if _SAFE_NAME.fullmatch(name) is not None:
                    if name in self._credentials:
                        self._refuse(
                            "credential_reference_in_prompt_body",
                            DestinationClass.BODY,
                        )
                    index = closing + 1
                    continue
                index += 1
                continue

            if raw[next_index] not in _ASCII_NAME_START:
                index += 1
                continue
            end = next_index + 1
            while end < len(raw) and raw[end] in _ASCII_NAME_CONTINUE:
                end += 1
            name = raw[next_index:end]
            if name in self._credentials:
                self._refuse(
                    "credential_reference_in_prompt_body",
                    DestinationClass.BODY,
                )
            index = end


def _is_mcp_env_container_path(path: ProfilePath) -> bool:
    return len(path) == 3 and path[0] == "mcpServers" and type(path[1]) is str and path[2] == "env"


def _resolve_metadata_value(
    raw: Any,
    value_resolver: ValueResolver,
    path: ProfilePath = (),
    *,
    _depth: int = 0,
    _active_container_ids: frozenset[int] = frozenset(),
) -> Any:
    if _is_mcp_env_container_path(path):
        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            AuthorityResolver._refuse(
                "mcp_env_value_not_plain_string",
                DestinationClass.MCP_ENV,
            )
        resolved_env: dict[str, str] = {}
        for key, value in raw.items():
            if type(key) is not str or type(value) is not str:
                AuthorityResolver._refuse(
                    "mcp_env_value_not_plain_string",
                    DestinationClass.MCP_ENV,
                )
            child_path = (*path, key)
            value_resolver(
                child_path,
                structural_destination(child_path, role=NodeRole.KEY),
                key,
            )
            resolved_env[key] = value_resolver(
                child_path,
                structural_destination(child_path, role=NodeRole.VALUE),
                value,
            )
        return resolved_env

    active_container_ids = _active_container_ids
    if isinstance(raw, (Mapping, list, tuple)):
        destination = structural_destination(path, role=NodeRole.VALUE)
        if _depth > _PROFILE_VALUE_MAX_DEPTH:
            AuthorityResolver._refuse(
                "profile_document_depth_exceeded",
                destination,
            )
        container_id = id(raw)
        if container_id in active_container_ids:
            AuthorityResolver._refuse(
                "profile_document_cycle",
                destination,
            )
        active_container_ids = active_container_ids | frozenset((container_id,))

    if isinstance(raw, Mapping):
        resolved: dict[Any, Any] = {}
        for key, value in raw.items():
            child_path = (*path, key)
            value_resolver(
                child_path,
                structural_destination(child_path, role=NodeRole.KEY),
                key,
            )
            resolved[key] = _resolve_metadata_value(
                value,
                value_resolver,
                child_path,
                _depth=_depth + 1,
                _active_container_ids=active_container_ids,
            )
        return resolved
    if isinstance(raw, list):
        return [
            _resolve_metadata_value(
                item,
                value_resolver,
                (*path, index),
                _depth=_depth + 1,
                _active_container_ids=active_container_ids,
            )
            for index, item in enumerate(raw)
        ]
    if isinstance(raw, tuple):
        return tuple(
            _resolve_metadata_value(
                item,
                value_resolver,
                (*path, index),
                _depth=_depth + 1,
                _active_container_ids=active_container_ids,
            )
            for index, item in enumerate(raw)
        )
    return value_resolver(
        path,
        structural_destination(path, role=NodeRole.VALUE),
        raw,
    )


def _resolve_body_value(raw: str, value_resolver: ValueResolver) -> Any:
    return value_resolver(
        BODY_PATH,
        structural_destination(BODY_PATH, role=NodeRole.BODY),
        raw,
    )


__all__ = [
    "AuthorityResolver",
    "BODY_PATH",
    "DestinationClass",
    "NodeRole",
    "ProfilePath",
    "ProfileValueRefusal",
    "ReferenceForm",
    "ValueResolver",
    "env_slot_key",
    "structural_destination",
]
