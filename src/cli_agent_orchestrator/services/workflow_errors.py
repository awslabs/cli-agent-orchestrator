"""Exception types for replay and recovery (issue #583, unit ``workflow-errors``).

LEAF MODULE: it imports ``enum`` and NOTHING ELSE — in particular nothing from
``cli_agent_orchestrator`` (BR-1). That import poverty is the module's entire reason for
existing rather than an incidental property of it: it is what lets ``workflow_service`` and
``workflow_journal`` both bind these names at MODULE level, which removes the cycle that
forced the function-local import inside ``workflow_journal.lookup_replay``.

WHY THAT LAZY IMPORT WAS WORTH REMOVING EVEN THOUGH IT WORKED. It worked by accident of
timing. Anything that changed import order — a new module-level import, a test that imported
``workflow_journal`` first, a tool that walked the package eagerly — turned it back into an
``ImportError`` whose message named whichever module happened to be imported first, sending a
reader to the wrong file. It was a latent failure with a misleading error. BR-1's AST-walk
test is the forcing function that keeps this module a leaf; a text grep could not be, because
this docstring discusses ``workflow_service`` and ``workflow_journal`` at length and a grep
would match its own prose.

NO SHARED BASE CLASS (TD-1). The two exceptions are independent ``Exception`` subclasses.
They demand different remedies, which is the whole reason the two verdicts are separate: a
divergence is reconciled by a human looking at what changed in the script (FR-3); a halt is
resolved by a human authorising a rerun (FR-4 guard 2 / FR-6 / FR-7). A common parent would
make ``except WorkflowReplayError`` the path of least resistance, and that one clause would
collapse two remedies into one handler — most visibly at the HTTP boundary, which must map
the two SEPARATELY. Adding a base later breaks no ``except`` clause; removing one after
callers rely on it does. The asymmetry is why this starts without one.

MESSAGES AND ``str()`` CARRY IDENTIFIERS ONLY (SR-1/SR-5). A message may name a ``run_id``,
a ``step_id``, a generation counter or a ``HaltRule`` code. It may NOT carry a prompt, a
working directory, a model id, a tool list, an env value, or a fingerprint — not even a
fingerprint, which is a hash rather than plaintext, because echoing it is noise and
``step-fingerprint``'s SR-2 forbids it anyway. ``str()`` is constrained and not just the
stored attributes: Python prints the rendered form on an unhandled exception, to stderr, to a
log handler, and into whatever captures process output, and none of those sinks is redacted.
Both renders below interpolate ONLY what the caller passed in and invent no detail.

``diverged_fields`` IS DELIBERATELY ABSENT (BR-4), correcting ``unit-of-work.md``:57-58.
Nothing can populate it: ``step-fingerprint`` hashes the ten execution-affecting components
and never stores them (its SR-1), and per-field hashes — which would answer "which field
changed" — were deferred at that unit's ``nfr-requirements`` Q1. An always-empty list reads
to a consumer as "no fields diverged" when the truth is "we cannot tell", which is the
opposite conclusion. A later per-field-hash unit can add the field WITH A REAL VALUE;
deferred, not lost. BR-4's test asserts the attribute's ABSENCE, so a well-meaning
re-addition without a populating source fails loudly.

WHAT THIS MODULE DOES NOT DO. It never raises: nothing in Bolt 1A raises either type, because
``lookup_replay`` — the only raise site — has zero production callers (BR-8, verified against
``src/``). It decides nothing (``replay-gate``, unit 7, owns when a halt fires), maps nothing
to an HTTP status (``run-step-replay-branch``, unit 9), and persists nothing
(``recovery-decision-intake``, unit 11, owns a durable halt reason). ``StaleGenerationError``
deliberately stays in ``workflow_service`` (BR-7): it is raised inside that module, so no
cycle involves it, and breaking a cycle is this module's only warrant.
"""

from enum import Enum


class HaltRule(str, Enum):
    """Which decision-order condition forced a halt (TD-2, FR-12 diagnosability).

    NAMED BY CONDITION, NEVER BY RULE NUMBER. Rule numbers are a *presentation* of the
    decision order and an edit to it renumbers them; the conditions cannot be renumbered.
    This workflow has already been bitten twice by two numbering schemes for one set, and a
    persisted ``rule_2`` that later means something else is the same defect with a longer
    fuse. The rule numbers in the comments below are a reading aid, not the contract.

    WHY A CLOSED ``(str, Enum)`` IS A SECURITY PROPERTY AND NOT ONLY A DESIGN NICETY (SR-3).
    With ``diverged_fields`` gone (BR-4), ``reason`` is the only free text on either
    exception, so the pressure to put a field VALUE somewhere is real. A closed enum
    cannot hold arbitrary text; a plain ``str`` code could, and would reintroduce that
    smuggling path through something that merely *looks* structured and reviewed. The closed
    vocabulary is what makes "put the detail somewhere safe" an actual option rather than an
    instruction to be disciplined.

    Adding a member later is easy. RENAMING one is not, once a code has been logged or
    persisted — which is the argument for semantic names.

    THE LAST TWO MEMBERS WERE ADDED BY PR #628's REVIEW, and each closes a path on which the
    gate answered ``REPLAY`` for a stored result that is not a faithful substitute for the
    original call. Neither reuses an existing member, because a persisted code that later
    means something adjacent is exactly the defect the paragraph above forbids:

    * ``OUTCOME_FAILED`` — the row settled as a FAILURE. It is NOT ``ENVELOPE_ABSENT``: such a
      row usually HAS an envelope (``result-envelope`` BR-1 writes one unconditionally), and
      the remedy differs — a human decides whether to re-run the failure or accept it.
    * ``ENVELOPE_LOSSY`` — the envelope reports its own ``truncated``/``redacted``. Also not
      ``ENVELOPE_ABSENT``: the envelope is present and readable, it just no longer carries the
      text the original call returned, and ``RunStepResponse`` has no field in which to say so.

    Anything iterating this set is a place that must be widened in the SAME change: four
    test modules assert the member count or the full name->value map, and
    ``step_replay.decide`` is asserted to PRODUCE every member.

    Form echoes ``RecoveryPolicy`` (unit 2) so a consumer can compare, serialise and persist
    a member without a custom encoder.
    """

    INTERRUPTED_NO_POLICY = "interrupted_no_policy"  # rule 2 — running row, no usable policy
    ENVELOPE_ABSENT = "envelope_absent"  # rule 3 — settled with no envelope (FR-4 guard 2)
    PROVENANCE_UNVERIFIABLE = "provenance_unverifiable"  # rule 5 — legacy/absent scheme (FR-6)
    POLICY_MANUAL = "policy_manual"  # rule 7 — fingerprints match, policy is manual (FR-7)
    OUTCOME_FAILED = "outcome_failed"  # rule 8 — the settled row records a FAILED outcome
    ENVELOPE_LOSSY = "envelope_lossy"  # rule 9 — the stored envelope truncated/redacted itself


class ReplayDivergenceError(Exception):
    """A settled row's fingerprint differs from the current call's (FR-3, A2, DR-4).

    Both values are computed under the CURRENT scheme; "the script changed between runs at
    the same key". Moved here from ``workflow_service`` by ADR-583-9 and still re-exported
    from there, so every existing import path keeps resolving (BR-2/INV-5).

    RAISED BY NOTHING IN BOLT 1A. Its only raise site is ``workflow_journal.lookup_replay``,
    which has zero production callers (BR-8), so no HTTP mapping exists for it yet either
    (unit 9 owns that).

    STRUCTURED FIELDS, NOT JUST A MESSAGE (BR-6). ``step_id`` and ``reason`` are readable
    attributes because FR-3 requires divergence to be *actionable*: a caller that must regex
    a message to learn which step failed is coupled to the message's wording, and the wording
    is exactly the part a later edit changes freely. ``str()`` carries both as well, so an
    uncaught traceback is still diagnostic — structure for programs, text for humans.

    ``reason`` NAMES THE CONDITION, NEVER THE DATA THAT DIFFERED (SR-2). It may carry
    identifiers and the fixed description of what happened; it may not carry a field value, a
    prompt, a path, or either fingerprint. This class cannot enforce that — its writer is a
    later unit — so the constraint lives here, on the field's definition, and
    ``replay-verification-guard`` (unit 13) is the unit positioned to assert it across the
    boundary.

    THERE IS DELIBERATELY NO ``rule`` ATTRIBUTE HERE (TD-2). A divergence is always the same
    rule, so the attribute would be a constant carrying no information — precisely the
    inert-attribute trap ``diverged_fields`` was dropped for. Adding it "for symmetry" would
    repeat the error this design just corrected.

    Args:
        step_id: which step diverged. The one datum a caller always has.
        reason: human-readable cause. Identifiers and fixed prose only (SR-1/SR-2).
    """

    def __init__(self, *, step_id: str, reason: str) -> None:
        # KEYWORD-ONLY (TD-3): ``(step_id, reason)`` and ``(reason, step_id)`` are both
        # plausible readings of a two-string signature, and silently transposing them yields
        # an exception that reads correctly and says the wrong thing. A transposition must be
        # impossible, not merely discouraged.
        self.step_id = step_id
        self.reason = reason
        # The rendered form is what an uncaught traceback prints (SR-5), so it must be both
        # diagnostic and safe. It interpolates only the two arguments — nothing is invented.
        super().__init__(f"step '{step_id}': {reason}")


class RecoveryDecisionRequired(Exception):
    """The replay gate reached a state it must not resolve alone (FR-4 guard 2, FR-6, FR-7).

    SHIPS WITH NO RAISER IN BOLT 1A, and that is admissible where ``diverged_fields`` is not
    (BR-5). An exception class with no raiser DECLARES A CONTRACT — "this situation has a
    name and a shape" — and a consumer concludes nothing from it until it is raised. A field
    with no writer ASSERTS A FACT, and a consumer reads a wrong value immediately. Only the
    second lies. First raisers are ``replay-gate`` (unit 7) and ``recovery-decision-intake``
    (unit 11).

    Carries ``rule`` in addition to ``step_id``/``reason`` because the gate always knows
    which condition it hit — that is exactly what makes the field populatable, and it gives
    the "record the detail" impulse a safe, closed outlet (TD-2/SR-3). A caller asking "why
    did this halt?" reads an attribute instead of parsing prose.

    ``reason`` NAMES THE RULE THAT FIRED, NEVER THE DATA THAT DIFFERED (SR-2) — the same
    constraint as on ``ReplayDivergenceError``, and the reason ``rule`` exists.

    Args:
        step_id: which step halted.
        rule: which decision-order condition fired. A closed vocabulary (``HaltRule``).
        reason: which rule fired, in words a human can act on. Identifiers and fixed prose
            only (SR-1/SR-2).
    """

    def __init__(self, *, step_id: str, rule: HaltRule, reason: str) -> None:
        # KEYWORD-ONLY (TD-3) — see ``ReplayDivergenceError.__init__``. With three arguments,
        # two of them strings, the transposition hazard is strictly worse.
        self.step_id = step_id
        self.rule = rule
        self.reason = reason
        # ``rule.value`` rather than ``rule``: for a ``(str, Enum)`` mixin the interpolated
        # form of the member itself varies by Python version, and the stable, greppable
        # rendering is the value. Only the three arguments appear (SR-1/SR-5).
        super().__init__(f"step '{step_id}' [{rule.value}]: {reason}")
