"""Canonical JSON byte encoding for control-plane recovery receipts.

Invariant: every receipt, RPC, and digest-bearing record in the recovery
control plane has exactly one canonical byte representation, so two
independent encoders always derive identical SHA-256 digests.

Failure mode prevented: two semantically equal records encoded with
different whitespace, escape, key-order, or number spellings hash
differently, which silently breaks digest binding between the conductor
and the fork and makes replay/tamper evidence unverifiable.

Why this guard exists: the stock ``json.dumps`` cannot express the
required rules (fixed caller-specified field order rather than
lexicographic, a restricted escape set, literal UTF-8 for non-ASCII,
non-negative base-10 integers only, exactly one trailing 0x0A), so the
encoder is implemented directly and pinned by golden vectors.
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable, Mapping


class CanonicalEncodingError(ValueError):
    """A value cannot be represented in the canonical encoding."""


def _encode_string(value: str) -> str:
    """Encode one JSON string under the restricted escape rules.

    Literal UTF-8 for every code point except ``"`` (``\\"``), ``\\``
    (``\\\\``), and U+0000-U+001F (``\\u00xx`` with lowercase hex).  No
    other escaping is permitted; ``/`` is never escaped; no Unicode
    normalization is applied.
    """
    parts = ['"']
    for char in value:
        code = ord(char)
        if char == '"':
            parts.append('\\"')
        elif char == "\\":
            parts.append("\\\\")
        elif code < 0x20:
            parts.append("\\u%04x" % code)
        else:
            parts.append(char)
    parts.append('"')
    return "".join(parts)


def _encode_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        # bool must precede int: bool is an int subclass, and the integer
        # rule (non-negative base-10) must never spell True as "1".
        return "true" if value else "false"
    if isinstance(value, int):
        if value < 0:
            raise CanonicalEncodingError(
                "canonical integers are non-negative base-10; negative values are refused"
            )
        return str(value)
    if isinstance(value, str):
        return _encode_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_encode_value(item) for item in value) + "]"
    if isinstance(value, Mapping):
        # Field order is the mapping's insertion order.  Canonical objects
        # in this design pin a fixed field order per schema (not
        # lexicographic), so the caller controls order by construction.
        return (
            "{"
            + ",".join(
                _encode_string(str(key)) + ":" + _encode_value(item) for key, item in value.items()
            )
            + "}"
        )
    raise CanonicalEncodingError(
        f"value of type {type(value).__name__} has no canonical encoding "
        "(only null, bool, non-negative int, str, list, and object are permitted)"
    )


def encode_canonical(value: Any, *, trailing_newline: bool = True) -> bytes:
    """Encode ``value`` to canonical bytes.

    The container may be any supported value; canonical objects in this
    design are always one JSON object followed by exactly one final 0x0A
    when ``trailing_newline`` is set.  The object is the entire content;
    nothing precedes or follows it.
    """
    text = _encode_value(value)
    raw = text.encode("utf-8")
    if trailing_newline:
        return raw + b"\n"
    return raw


def canonical_sha256(value: Any) -> str:
    """SHA-256 hex digest over the canonical bytes (with trailing 0x0A)."""
    return hashlib.sha256(encode_canonical(value)).hexdigest()


def build_canonical(fields: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    """Build an insertion-ordered mapping from ``(key, value)`` pairs.

    This helper exists so call sites construct canonical objects with the
    schema's fixed field order explicitly and readably; duplicate keys are
    refused because they would make the encoded bytes ambiguous.
    """
    result: dict[str, Any] = {}
    for key, value in fields:
        if key in result:
            raise CanonicalEncodingError(f"duplicate canonical field: {key!r}")
        result[key] = value
    return result
