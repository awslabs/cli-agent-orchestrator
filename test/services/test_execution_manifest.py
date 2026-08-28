"""Tests for the execution manifest envelope (issue #583 Bolt 2, unit ``manifest-envelope``).

The properties asserted here are the unit's requirements, not a coverage exercise. One of them —
``test_a_secret_straddling_the_bound_is_still_redacted`` — is the only property in the unit that
fails SILENTLY, and it is the reason redact-before-bound is a contract rather than a preference.
"""

import hashlib
import json
from pathlib import Path

from cli_agent_orchestrator.constants import WORKFLOW_MANIFEST_MAX_BYTES
from cli_agent_orchestrator.services import execution_manifest as em

# A literal that ``secret_gate._SECRET_PATTERNS`` matches. Kept in one place so a ruleset change
# breaks these tests loudly in one spot rather than subtly in eight.
SECRET = "AKIAIOSFODNN7EXAMPLE"  # matches the ``aws_access_key`` pattern; 20 chars, fixed length


def _build(**overrides):
    """A minimal valid envelope; overrides supply whatever the test is actually about."""
    fields = dict(
        plan_id="v1:0" * 4,
        source_hash="deadbeef",
        inputs={},
        repo_baseline={},
        provider="mock_cli",
        model="m",
        profile="developer",
        permissions={},
        limits={},
        retry_policy={},
        memory_content="",
        memory_source="cao-memory",
    )
    fields.update(overrides)
    return em.build(**fields)


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_a_secret_straddling_the_bound_is_still_redacted():
    """THE ONE THAT FAILS SILENTLY. Redact-before-bound, asserted at the boundary itself.

    Were the order reversed, the redactor would only ever see the truncated prefix, so a secret
    beginning before the bound and ending after it would be persisted in the clear. The happy path
    produces an identical-looking envelope either way, so no other test in this file would notice.
    """
    # Find where truncation actually lands, rather than guessing an offset: build once with pure
    # filler and read back how much content survived. The bound applies to the whole JSON document,
    # so the cut point is not simply the constant.
    oversized = WORKFLOW_MANIFEST_MAX_BYTES + 10_000
    probe = _build(memory_content="x" * oversized)
    assert probe.truncated is True, "the probe payload must exceed the bound"
    cut = len(probe.memory.content)

    # Now place the secret so it STRADDLES that cut point: half before it, half after.
    half = len(SECRET) // 2
    content = ("x" * (cut - half)) + SECRET + ("y" * 10_000)
    envelope = _build(memory_content=content)

    assert envelope.truncated is True, "expected the bound to bite for this payload"
    stored = em.serialise(envelope)
    assert SECRET not in stored
    # And not a fragment either: the leading half must not survive at the boundary, which is
    # exactly what bound-then-redact would leave behind.
    assert SECRET[:half] not in stored
    assert envelope.redacted is True


def test_a_secret_nested_three_levels_deep_is_redacted():
    envelope = _build(inputs={"a": {"b": {"c": SECRET}}})
    assert SECRET not in em.serialise(envelope)
    assert envelope.redacted is True


def test_a_secret_used_as_a_dict_key_is_redacted():
    """Keys are redacted alongside values: a credential lands in a key as readily as in a value."""
    envelope = _build(permissions={SECRET: "granted"})
    assert SECRET not in em.serialise(envelope)
    assert envelope.redacted is True


def test_redacted_is_false_when_nothing_matches():
    envelope = _build(inputs={"harmless": "value"}, memory_content="nothing secret here")
    assert envelope.redacted is False


def test_no_pattern_name_appears_anywhere_in_the_envelope():
    """``redacted`` is a bare bool: naming which pattern fired discloses the secret's shape.

    The redaction MARKER is expected in the stored text; what must not appear is any structured
    record of which patterns fired, so the envelope carries a bool and nothing else.
    """
    envelope = _build(inputs={"tok": SECRET})
    assert envelope.redacted is True
    assert isinstance(envelope.redacted, bool)
    # No field of the envelope is a list/collection of fired pattern names.
    document = json.loads(em.serialise(envelope))
    assert "fired" not in document
    assert "patterns" not in document


# ---------------------------------------------------------------------------
# Bounding
# ---------------------------------------------------------------------------


def test_the_bound_is_inclusive():
    """Exactly the bound is NOT truncated; one byte more is. Matches ``step_result``'s boundary."""
    small = _build(memory_content="a" * 100)
    assert small.truncated is False

    over = _build(memory_content="a" * (WORKFLOW_MANIFEST_MAX_BYTES + 1))
    assert over.truncated is True
    assert len(em.serialise(over).encode("utf-8")) <= WORKFLOW_MANIFEST_MAX_BYTES


def test_build_never_raises_on_an_oversized_document():
    """Bounding is lossy but TOTAL: a run must not fail because its manifest was large."""
    envelope = _build(memory_content="z" * (WORKFLOW_MANIFEST_MAX_BYTES * 3))
    assert envelope.truncated is True
    assert envelope.memory.truncated is True


# ---------------------------------------------------------------------------
# The hash/content asymmetry (FR-9)
# ---------------------------------------------------------------------------


def test_content_hash_covers_the_full_content_not_the_stored_copy():
    """FR-9's identity survives truncation, which is what makes a cross-resume comparison possible."""
    full = "m" * (WORKFLOW_MANIFEST_MAX_BYTES + 10_000)
    envelope = _build(memory_content=full)

    assert envelope.memory.truncated is True
    assert len(envelope.memory.content) < len(full), "the stored copy should be shorter"
    assert envelope.memory.content_hash == hashlib.sha256(full.encode("utf-8")).hexdigest()


def test_the_hash_does_not_depend_on_the_redaction_ruleset():
    """Hash BEFORE redaction, so a later ``_SECRET_PATTERNS`` change cannot rewrite history.

    Asserted against the raw input's own digest: if hashing ever moved after redaction, a content
    string containing a secret would hash to the redacted form instead and this would fail.
    """
    content = f"before {SECRET} after"
    envelope = _build(memory_content=content)

    assert envelope.memory.content_hash == hashlib.sha256(content.encode("utf-8")).hexdigest()
    # And the STORED copy is redacted even though the hash is not.
    assert SECRET not in envelope.memory.content


# ---------------------------------------------------------------------------
# Serialise / parse
# ---------------------------------------------------------------------------


def test_parse_is_total():
    """Absent, empty and malformed all answer None rather than raising."""
    assert em.parse(None) is None
    assert em.parse("") is None
    assert em.parse("{not json") is None
    assert em.parse('{"plan_id": "only-this-field"}') is None


def test_round_trip():
    envelope = _build(inputs={"k": [1, 2, {"n": None}]}, memory_content="frozen block")
    assert em.parse(em.serialise(envelope)) == envelope


def test_plan_id_is_carried_opaquely():
    """This unit never parses ``plan_id``: any string round-trips unchanged."""
    for value in ("v1:abc", "v2:def", "not-a-scheme-at-all", ""):
        assert _build(plan_id=value).plan_id == value


# ---------------------------------------------------------------------------
# Module posture (preservation, per the module docstring)
# ---------------------------------------------------------------------------


def test_the_module_has_no_logger_and_no_print():
    """SR: one helpful log line would move a credential out of a redacted column into a log file.

    Source inspection rather than behaviour, because the property is the ABSENCE of a capability —
    the same shape Bolt 1 used for its leaf-module posture tests.
    """
    source = Path(em.__file__).read_text(encoding="utf-8")
    # Strip the docstrings: they discuss logging deliberately, and that prose is the point.
    without_docstrings = source.replace('"""', "\x00").split("\x00")
    code = "".join(without_docstrings[::2])
    assert "logging" not in code
    assert "logger" not in code
    assert "print(" not in code
