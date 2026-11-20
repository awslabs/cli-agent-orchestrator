"""A small closed-schema validator for request bodies.

Every request body is checked against a spec of known fields. Unknown fields are rejected, which is
how "no ``all`` field" is enforced for the stop endpoints.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

from .errors import ShelfError


def bad(message: str) -> ShelfError:
    return ShelfError("invalid_request", message)


@dataclass(frozen=True)
class Field:
    """One property of a body.

    ``kind`` is one of ``str``, ``int``, ``number``, ``bool``, ``enum``, ``array``, ``object``.
    """

    kind: str
    required: bool = False
    nullable: bool = False
    min_len: int = 0
    max_len: Optional[int] = None
    pattern: Optional[str] = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    choices: Tuple[Any, ...] = ()
    items: Optional["Field"] = None
    min_items: int = 0
    max_items: Optional[int] = None
    spec: Optional[Mapping[str, "Field"]] = None
    too_many: bool = False  # exceeding max_items is reported as ``too_many``


def _check(name: str, value: Any, f: Field) -> Any:
    if value is None:
        if f.nullable:
            return None
        raise bad(f"{name} must not be null")
    if f.kind == "str":
        if not isinstance(value, str):
            raise bad(f"{name} must be a string")
        if len(value) < max(f.min_len, 0) or (f.max_len is not None and len(value) > f.max_len):
            raise bad(f"{name} has an invalid length")
        if f.pattern is not None and re.fullmatch(f.pattern, value) is None:
            raise bad(f"{name} has an invalid format")
    elif f.kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise bad(f"{name} must be an integer")
        _range(name, value, f)
    elif f.kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise bad(f"{name} must be a number")
        _range(name, value, f)
    elif f.kind == "bool":
        if not isinstance(value, bool):
            raise bad(f"{name} must be true or false")
    elif f.kind == "enum":
        if not isinstance(value, str) or value not in f.choices:
            raise bad(f"{name} is not an allowed value")
    elif f.kind == "array":
        if not isinstance(value, list):
            raise bad(f"{name} must be an array")
        if len(value) < f.min_items:
            raise bad(f"{name} needs at least {f.min_items} item(s)")
        if f.max_items is not None and len(value) > f.max_items:
            if f.too_many:
                raise ShelfError("too_many", f"{name} has more than {f.max_items} items")
            raise bad(f"{name} has more than {f.max_items} items")
        if f.items is not None:
            return [_check(f"{name}[{i}]", v, f.items) for i, v in enumerate(value)]
    elif f.kind == "object":
        if not isinstance(value, dict):
            raise bad(f"{name} must be an object")
        return validate_object(value, f.spec or {}, name)
    return value


def _range(name: str, value: float, f: Field) -> None:
    if f.minimum is not None and value < f.minimum:
        raise bad(f"{name} is below {f.minimum}")
    if f.maximum is not None and value > f.maximum:
        raise bad(f"{name} is above {f.maximum}")


def validate_object(
    obj: Mapping[str, Any], spec: Mapping[str, Field], where: str = "body"
) -> Dict[str, Any]:
    """Return a copy of ``obj`` after checking it against ``spec`` (closed: no extra keys)."""
    if not isinstance(obj, dict):
        raise bad(f"{where} must be a JSON object")
    unknown = sorted(set(obj) - set(spec))
    if unknown:
        shown = unknown[0] if re.fullmatch(r"[a-z_]{1,30}", unknown[0]) else "(unrecognised name)"
        raise bad(f"unknown field in {where}: {shown}")
    out: Dict[str, Any] = {}
    for key, field in spec.items():
        if key not in obj:
            if field.required:
                raise bad(f"{key} is required")
            continue
        out[key] = _check(key, obj[key], field)
    return out


def strip_control(text: str, keep_newlines: bool = True) -> str:
    """Remove control characters (kept: tab, and newline when asked)."""
    allowed = {"\t", "\n"} if keep_newlines else {"\t"}
    return "".join(ch for ch in text if ch in allowed or (ord(ch) >= 32 and ord(ch) != 127))


def has_control(text: str) -> bool:
    return any(ord(c) < 32 and c not in "\t\n" or ord(c) == 127 for c in text)


NAME_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_-]{0,58}"
TERMINAL_ID_PATTERN = r"[a-f0-9]{8}"
NAME_RE = re.compile(NAME_PATTERN)
TERMINAL_ID_RE = re.compile(TERMINAL_ID_PATTERN)


def check_name(name: Any) -> str:
    if not isinstance(name, str) or NAME_RE.fullmatch(name) is None:
        raise bad("invalid session name")
    return name


def check_terminal_id(tid: Any) -> str:
    if not isinstance(tid, str) or TERMINAL_ID_RE.fullmatch(tid) is None:
        raise bad("invalid terminal id")
    return tid
