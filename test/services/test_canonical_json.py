"""Tests for the canonical JSON encoder (T-CB-3 fork-side surface)."""

from __future__ import annotations

import hashlib

import pytest

from cli_agent_orchestrator.services.canonical_json import (
    CanonicalEncodingError,
    build_canonical,
    canonical_sha256,
    encode_canonical,
)

# Golden vector GV-1 (nominal, request_revision 1): an independent encoder
# must reproduce these exact bytes and digest.
GV1_FIELDS = {
    "encoding": "cao-callback-req-v2",
    "project": "cao-conductor-self-heal",
    "task_id": "self-heal-demo-task",
    "run_id": "run-0001",
    "obligation_generation": "obgen-7c2e4a1b",
    "logical_callback_id": "0f8fad5a-1c87-4d3e-9b96-1b6b2c8e5f10",
    "request_revision": 1,
    "supersedes_request_sha256": None,
    "attempt_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
    "terminal_id": "a1b2c3d4",
    "terminal_generation": "gen-000042",
    "status": "done",
    "summary": "ok",
    "report_sha256": "a" * 64,
    "envelope_core_sha256": "b" * 64,
    "sealed_at": "2026-07-23T12:00:00Z",
}
GV1_BYTES = 630
GV1_SHA256 = "9fe77e88ae62dad23a73aacaf34784624f532cfa8f3d66762bd1bfdc3254c5f6"

# Golden vector GV-2 (null task_id/core, revision 2, non-ASCII, C0 escape).
GV2_OVERRIDES = {
    "task_id": None,
    "run_id": "run-0002",
    "obligation_generation": "obgen-11d4e5f6",
    "logical_callback_id": "3d813cbb-47fb-42ba-91df-831e1593ac29",
    "request_revision": 2,
    "supersedes_request_sha256": "e" * 64,
    "attempt_id": "9b2e6679-7425-40de-944b-e07fc1f90ae7",
    "terminal_id": "deadbeef",
    "terminal_generation": "gen-000007",
    "status": "blocked",
    "summary": "café\theld",
    "report_sha256": "c" * 64,
    "envelope_core_sha256": None,
    "sealed_at": "2026-07-23T13:30:00Z",
}
GV2_BYTES = 629
GV2_SHA256 = "b3966baea0367fa49f57f70b7390477d82237318a4fcb184a2822546ad625f4c"


def _gv2_fields() -> dict:
    fields = dict(GV1_FIELDS)
    fields.update(GV2_OVERRIDES)
    return fields


def test_gv1_reproduces_byte_for_byte() -> None:
    raw = encode_canonical(GV1_FIELDS)
    assert len(raw) == GV1_BYTES
    assert hashlib.sha256(raw).hexdigest() == GV1_SHA256
    assert canonical_sha256(GV1_FIELDS) == GV1_SHA256


def test_gv2_reproduces_byte_for_byte() -> None:
    raw = encode_canonical(_gv2_fields())
    assert len(raw) == GV2_BYTES
    assert hashlib.sha256(raw).hexdigest() == GV2_SHA256


def test_exact_trailing_newline_and_no_other_literal_newline() -> None:
    raw = encode_canonical({"a": "x\ny"})
    assert raw.endswith(b"\n")
    assert not raw[:-1].count(b"\n")  # interior newline must use the  ú000a form
    assert b"\\u000a" in raw


def test_tab_escapes_to_lowercase_u00xx() -> None:
    assert encode_canonical({"s": "a\tb"}) == b'{"s":"a\\u0009b"}\n'


def test_non_ascii_is_literal_utf8() -> None:
    raw = encode_canonical({"s": "café"})
    assert "café".encode("utf-8") in raw
    assert b"\\u00e9" not in raw


def test_slash_is_never_escaped() -> None:
    assert encode_canonical({"s": "/abs/path"}) == b'{"/s":"/abs/path"}\n'.replace(b'"/s"', b'"s"')


def test_quote_and_backslash_escape() -> None:
    assert encode_canonical({"s": 'a"b\\c'}) == b'{"s":"a\\"b\\\\c"}\n'


def test_integer_spelling_rules() -> None:
    assert encode_canonical({"n": 0}) == b'{"n":0}\n'
    assert encode_canonical({"n": 42}) == b'{"n":42}\n'
    with pytest.raises(CanonicalEncodingError):
        encode_canonical({"n": -1})


def test_bool_spelling_and_int_subclass_guard() -> None:
    assert encode_canonical({"b": True}) == b'{"b":true}\n'
    assert encode_canonical({"b": False}) == b'{"b":false}\n'


def test_float_refused() -> None:
    with pytest.raises(CanonicalEncodingError):
        encode_canonical({"n": 1.5})


def test_field_order_is_insertion_order_not_lexicographic() -> None:
    ordered = {"z": 1, "a": 2}
    assert encode_canonical(ordered) == b'{"z":1,"a":2}\n'


def test_no_insignificant_whitespace() -> None:
    raw = encode_canonical({"a": [1, {"b": None}]})
    assert b" " not in raw
    assert b": " not in raw


def test_no_trailing_newline_option() -> None:
    assert encode_canonical({"a": 1}, trailing_newline=False) == b'{"a":1}'


def test_build_canonical_preserves_order_and_refuses_duplicates() -> None:
    obj = build_canonical([("b", 1), ("a", 2)])
    assert list(obj) == ["b", "a"]
    with pytest.raises(CanonicalEncodingError):
        build_canonical([("b", 1), ("b", 2)])
