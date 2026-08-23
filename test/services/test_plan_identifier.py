"""Tests for the run-level plan identifier (issue #583 Bolt 2, unit ``plan-identifier``).

The properties here are the unit's contract. Two of them are load-bearing beyond ordinary coverage:

* ``test_the_digest_of_a_known_field_set_is_pinned`` is what makes a reordered component list fail
  LOUDLY. Reordering silently changes every stored value while the ``plan-v1:`` prefix stays
  ``plan-v1:``, so neither classification nor equality can detect it — every existing approval would
  quietly stop matching. Without a pinned digest nothing in the suite would notice.
* ``test_changing_each_field_in_turn_changes_the_value`` asserts FR-8's Pass criterion field by field
  rather than assuming it. A field silently omitted from the hash is invisible to every other test.
"""

import dataclasses
from pathlib import Path

from cli_agent_orchestrator.services import plan_identifier as pi
from cli_agent_orchestrator.services.plan_identifier import PlanFields, compute, scheme_of


def _fields(**overrides) -> PlanFields:
    base = PlanFields(
        source_hash="h",
        inputs={"a": 1, "b": 2},
        repo_baseline={},
        provider="mock_cli",
        model="m",
        profile="dev",
        permissions={},
        limits={},
        retry_policy={},
    )
    return dataclasses.replace(base, **overrides) if overrides else base


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


def test_the_digest_of_a_known_field_set_is_pinned():
    """THE GUARD AGAINST A SILENT REORDERING. See the module docstring.

    If this fails after a change to the component order, the framing, the prefix, or any normaliser,
    that is the test doing its job — every stored ``plan_id`` and therefore every recorded approval has
    just been invalidated. Update the literal ONLY together with a scheme-version bump.
    """
    assert compute(_fields()) == (
        "plan-v1:0743b0b78d9c2a75c770a6aad22caa4681d5e4705d155e0ab98482bb2cf728d0"
    )


def test_changing_each_field_in_turn_changes_the_value():
    """FR-8's Pass criterion, asserted field by field rather than assumed.

    A field left out of the hash would be invisible to every other test in this file: the plan would
    change, the identifier would not, and a changed plan would execute under a stale approval.
    """
    baseline = compute(_fields())
    altered = {
        "source_hash": "different",
        "inputs": {"a": 1, "b": 3},
        "repo_baseline": {"sha": "x"},
        "provider": "claude_code",
        "model": "other",
        "profile": "reviewer",
        "permissions": {"write": True},
        "limits": {"steps": 5},
        "retry_policy": {"retries": 2},
    }
    assert set(altered) == {f.name for f in dataclasses.fields(PlanFields)}, (
        "every one of the nine fields must be exercised; this guard catches a field added to "
        "PlanFields without being added here"
    )
    for name, value in altered.items():
        assert compute(_fields(**{name: value})) != baseline, f"{name} does not affect plan_id"


def test_the_output_shape_is_fixed_regardless_of_input_size():
    """72 characters, no user bytes: neither an injection nor a growth vector."""
    for inputs in ({}, {"k": "v"}, {"big": "x" * 100_000}):
        value = compute(_fields(inputs=inputs))
        assert value.startswith("plan-v1:")
        assert len(value) == 72
        hex_part = value[len("plan-v1:") :]
        assert len(hex_part) == 64
        assert hex_part == hex_part.lower()
        assert all(c in "0123456789abcdef" for c in hex_part)


# ---------------------------------------------------------------------------
# Canonical form
# ---------------------------------------------------------------------------


def test_dict_key_order_does_not_change_the_value():
    """A reordered map executes identically, so it must not force a re-approval."""
    assert compute(_fields(inputs={"a": 1, "b": 2})) == compute(_fields(inputs={"b": 2, "a": 1}))


def test_list_order_does_change_the_value():
    """A reordered permissions list or retry sequence may genuinely execute differently."""
    assert compute(_fields(inputs=[1, 2])) != compute(_fields(inputs=[2, 1]))


def test_none_and_empty_string_are_distinct():
    """ "No profile" and "the empty profile" are different plans."""
    assert compute(_fields(profile=None)) != compute(_fields(profile=""))


def test_absence_in_different_positions_is_distinct():
    """The tuple is never shortened, so two plans cannot collide by which field is missing."""
    assert compute(_fields(provider=None)) != compute(_fields(model=None))


def test_integer_and_float_spellings_agree():
    """``600`` and ``600.0`` are one identity: an int in a float-typed field is ordinary in Python."""
    assert compute(_fields(limits={"timeout": 600})) == compute(_fields(limits={"timeout": 600.0}))


def test_distinct_large_integers_have_distinct_plan_ids():
    assert compute(_fields(inputs={"n": 2**53})) != compute(_fields(inputs={"n": 2**53 + 1}))


def test_an_oversized_integer_can_be_hashed():
    assert compute(_fields(inputs={"n": 10**400})).startswith("plan-v1:")


def test_booleans_are_not_normalised_into_numbers():
    """``bool`` subclasses ``int``; normalising ``True`` to a number would erase flag-versus-count."""
    assert compute(_fields(permissions={"write": True})) != compute(
        _fields(permissions={"write": 1})
    )


def test_framing_is_injective_where_a_naive_join_would_collide():
    """Length-prefixed framing closes the limit ``step_fingerprint`` deferred to a ``v3:`` decision.

    Under a naive single-separator join these two field sets render identically: the boundary between
    two adjacent components becomes indistinguishable from the separator inside one of them. A collision
    here means one plan executing under another plan's approval.
    """
    sep = pi._FRAME_SEP
    a = _fields(provider=f"x{sep}y", model="z")
    b = _fields(provider="x", model=f"y{sep}z")
    assert compute(a) != compute(b)


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def test_scheme_of_reports_three_states():
    assert scheme_of(compute(_fields())) == "plan-v1"
    assert scheme_of("v2:" + "a" * 64) == "unknown"
    assert scheme_of(None) == "absent"
    assert scheme_of("") == "absent"


def test_scheme_of_never_raises():
    for value in (None, "", "plan-v1:", "garbage", "\x00", "plan-v2:abc", "a" * 10_000):
        assert scheme_of(value) in {"plan-v1", "unknown", "absent"}


def test_absent_is_distinguishable_from_unknown():
    """The distinction is the point: re-approval versus a human ruling are different remedies."""
    assert scheme_of(None) != scheme_of("v2:abc")


# ---------------------------------------------------------------------------
# Module posture (preservation)
# ---------------------------------------------------------------------------


def test_the_module_has_no_logger_and_no_print():
    """One helpful log line would move a credential out of a hash and into a world-readable file."""
    source = Path(pi.__file__).read_text(encoding="utf-8")
    # Strip docstrings: they discuss logging deliberately, and that prose is the point.
    code = "".join(source.replace('"""', "\x00").split("\x00")[::2])
    assert "logging" not in code
    assert "logger" not in code
    assert "print(" not in code


def test_the_module_imports_only_the_four_permitted_names():
    """Leaf purity: ``hashlib``, ``json``, ``dataclasses``, ``typing`` and nothing else."""
    source = Path(pi.__file__).read_text(encoding="utf-8")
    # Strip docstrings BEFORE scanning: prose wraps onto lines beginning "from ..." and would be
    # mistaken for an import. (Found by this test failing on its own first run.)
    code = "".join(source.replace('"""', "\x00").split("\x00")[::2])
    imports = [line.strip() for line in code.splitlines() if line.startswith(("import ", "from "))]
    assert imports == [
        "import hashlib",
        "import json",
        "from dataclasses import dataclass",
        "from typing import Any, Literal, Optional",
    ]
    # No import from elsewhere in this codebase: the module must stay a leaf.
    assert "cli_agent_orchestrator" not in "".join(imports)
