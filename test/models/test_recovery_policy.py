"""Tests for the ``RecoveryPolicy`` vocabulary and ``parse_policy`` (issue #583).

Unit ``recovery-policy``. Rule ids are local to that unit's functional design
(``construction/recovery-policy/functional-design/business-rules.md``,
``.../nfr-requirements/security-requirements.md``,
``.../nfr-requirements/tech-stack-decisions.md``):

- BR-1 / INV-3  the set is closed at exactly three members; "undeclared" is not sayable
- BR-2 / INV-2  ``None`` and ``""`` mean *not declared* and are NEVER coerced to ``MANUAL``
- BR-3 / INV-1  an out-of-set non-empty value raises ``ValueError`` (never degrades to ``None``)
- BR-4          no normalisation: no case-folding, no stripping, no aliasing
- BR-5          the enum subclasses ``str``, so a member serialises to its own text
- BR-6 / SR-2 / INV-4  nothing here infers a policy from a prompt, agent or tool list

SR-1 / TD-1 are NOT covered here. The ``recovery`` request field belongs to
``run-step-replay-branch`` (Bolt 1B), which is where ``component-dependency.md`` assigns
it; Bolt 1A is deliberately invisible from the outside, so it ships no public field. The
boundary tests -- that an out-of-set value is rejected with a 422 naming ``recovery``, that
each declared value is accepted, and that ``parse_policy`` is NOT on the route's path --
move with the field to that unit. Until then BR-3 below tests the FUNCTION only, and makes
no claim about the boundary.
"""

import inspect
import json
import typing
from typing import Optional

import pytest

from cli_agent_orchestrator.models import workflow as workflow_models
from cli_agent_orchestrator.models.workflow import RecoveryPolicy, parse_policy

# Inputs a "detect side effects" helper would reach for. FR-7 makes inferring a step's
# external effects an explicit Fail, and this module is where such a helper would land.
_INFERENCE_INPUTS = frozenset(
    {"prompt", "agent", "tools", "allowed_tools", "working_directory", "cwd"}
)


class TestVocabulary:
    def test_set_is_closed_at_three(self):
        """BR-1, INV-3: an added member fails this test."""
        assert set(RecoveryPolicy) == {
            RecoveryPolicy.IDEMPOTENT,
            RecoveryPolicy.RECONCILE,
            RecoveryPolicy.MANUAL,
        }
        assert len(RecoveryPolicy) == 3

    def test_no_member_means_undeclared(self):
        """BR-1: absence is ``None``, not a value — so "undeclared" is unsayable.

        A ``NONE`` member would let a caller send ``recovery="none"`` over the wire to
        mean something the replay gate has no rule for.
        """
        values = {member.value for member in RecoveryPolicy}
        assert values == {"idempotent", "reconcile", "manual"}
        assert "none" not in values
        assert "" not in values
        assert not hasattr(RecoveryPolicy, "NONE")


class TestParsePolicy:
    def test_absent_returns_none(self):
        """BR-2, INV-2: absent or empty is *not declared* — a state, not an error.

        ``""`` is treated as absent rather than invalid because an empty body field is
        indistinguishable from an omitted one over HTTP.
        """
        assert parse_policy(None) is None
        assert parse_policy("") is None

    def test_none_is_not_coerced_to_manual(self):
        """BR-2, the load-bearing rule of the unit.

        Looks trivial, is not. ``None`` and ``MANUAL`` behave identically in gate rules
        2, 3 and 5 and differently in rules 7/8: ``manual`` halts even when a verified
        replay is available, undeclared replays. A future "helpful" coercion here would
        be invisible in this unit and would surface far away as "legacy scripts can no
        longer resume", because rule 7 would then fire for every old-surface step.
        """
        assert parse_policy(None) is not RecoveryPolicy.MANUAL
        assert parse_policy(None) != RecoveryPolicy.MANUAL
        assert parse_policy("") is not RecoveryPolicy.MANUAL
        assert parse_policy("") != RecoveryPolicy.MANUAL

    def test_each_member_parses(self):
        """BR-1: every member round-trips through its own wire text."""
        for member in RecoveryPolicy:
            assert parse_policy(member.value) is member
        assert parse_policy("idempotent") is RecoveryPolicy.IDEMPOTENT
        assert parse_policy("reconcile") is RecoveryPolicy.RECONCILE
        assert parse_policy("manual") is RecoveryPolicy.MANUAL

    @pytest.mark.parametrize("value", ["idempotant", "none", "x"])
    def test_out_of_set_raises(self, value):
        """BR-3, INV-1: raises rather than degrading to ``None`` or a best match.

        Returning ``None`` for a misspelled ``idempotent`` would silently convert it into
        an undeclared step that halts on resume, sending the author to debug the gate
        instead of their spelling.
        """
        with pytest.raises(ValueError):
            parse_policy(value)

    @pytest.mark.parametrize("value", ["Manual", " manual", "idem"])
    def test_no_normalisation(self, value):
        """BR-4: no case-folding, no whitespace stripping, no abbreviation or aliasing.

        A three-member closed set does not need fuzzy matching, and accepting near-misses
        would make the lint rule (unit 12) unable to tell a declared policy from a
        nearly-declared one.
        """
        with pytest.raises(ValueError):
            parse_policy(value)

    def test_str_mixin(self):
        """BR-5: a member compares equal to its own text and needs no custom encoder."""
        assert RecoveryPolicy.MANUAL == "manual"
        assert RecoveryPolicy.IDEMPOTENT == "idempotent"
        assert RecoveryPolicy.RECONCILE == "reconcile"
        assert json.dumps({"r": RecoveryPolicy.MANUAL}) == '{"r": "manual"}'


class TestNoInference:
    def test_module_infers_nothing(self):
        """BR-6, SR-2, INV-4: no function here guesses a step's external effects.

        Scoping note, stated rather than glossed: BR-6 and SR-2 say "the module's public
        surface is the enum plus ``parse_policy`` and nothing else". That is true of the
        *unit*, not of the *file* — TD-2 puts the enum in ``models/workflow.py``, which
        already hosts the whole workflow-spec grammar (``WorkflowSpec``,
        ``validate_only``, ``is_reserved`` and more). So the assertion made here is the
        one that is both true and load-bearing: ``parse_policy`` takes exactly one
        ``Optional[str]``, and no module-level function in the file accepts any input a
        side-effect inference would need.
        """
        hints = typing.get_type_hints(parse_policy)
        assert list(inspect.signature(parse_policy).parameters) == ["value"]
        assert hints["value"] == Optional[str]
        assert hints["return"] == Optional[RecoveryPolicy]

        for name, fn in inspect.getmembers(workflow_models, inspect.isfunction):
            if fn.__module__ != workflow_models.__name__:
                continue  # re-exported import, not defined here
            offending = set(inspect.signature(fn).parameters) & _INFERENCE_INPUTS
            assert not offending, (
                f"{name}() accepts {sorted(offending)} — FR-7 makes inferring a step's "
                "external effects an explicit Fail (BR-6, SR-2)"
            )
