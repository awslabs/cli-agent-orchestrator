"""The whole replay decision, in one total function (issue #583, unit ``replay-gate``).

ONE journal read, no writes, no other I/O (BR-13/SR-5, NFR-2 — the gate sits on the resume hot
path). This module is the ONLY place the ten-rule decision order exists, and ``decide``'s
verdict is the only thing callers branch on.

RULES 8 AND 9 WERE ADDED BY PR #628's REVIEW AND THE ORIGINAL EIGHT WERE NOT RENUMBERED. Both
close a path on which the gate answered ``REPLAY`` for a stored result that is not a faithful
substitute for what the original call returned — a failed outcome (rule 8) and an envelope that
reports its own lossiness (rule 9). They are APPENDED, immediately before the catch-all, for
one reason: every earlier rule keeps its number, its condition and its position, so the three
load-bearing orderings below are untouched and no existing test name is left describing a
different rule. The cost is stated rather than hidden — a ``manual`` row whose outcome FAILED
still halts as ``POLICY_MANUAL`` (rule 7 is reached first), which is true but less specific
than ``OUTCOME_FAILED``. Both halt, both are resolved by the same two decisions, and the
operator sees the failed state on ``cao workflow status``; buying a marginally better halt
code by renumbering seven rules is not a trade this module makes.

THE ORDER OF THE TEN RULES IS THE CONTRACT, NOT A PRESENTATION OF IT (BR-1). First match
wins, and reordering them changes behaviour with NO SIGNAL — the same class of silent break as
reordering ``step_fingerprint.compute``'s ten components. Three orderings are load-bearing, and
each will look like a tidying opportunity to a later reader:

* **Rule 1 admits ``rerun_authorized``, and rule 2 guards on ``running`` EXACTLY — never on
  "not settled" (BR-2/INV-3).** Both halves are one fix for one infinite-halt defect: a
  ``manual`` step halts, the human authorises a rerun, the row becomes non-settled, and a
  rule 2 written as "not settled" halts it again — forever, with no escape from the mechanism
  built to provide one. The state is written by ``recovery-decision-intake`` (unit 12);
  rule 1 admits it before anything produces it, deliberately.
* **Rules 4 and 5 precede rule 6 — provenance BEFORE equality (BR-4/INV-1/INV-2).** "The
  stored hash differs from the computed one" is meaningless when the stored hash was computed
  under different rules, so an unverifiable fingerprint is never reported as divergence.
* **Rule 4 precedes rule 5 (BR-3).** Rule 4 first means an author who declared re-execution
  safe is not punished across the scheme-upgrade window; rule 5 second means unverifiable
  provenance never replays as a match (FR-6). Swapping them breaks FR-5, and deleting either
  one breaks a different requirement.
* **``replay_authorized`` is EXCLUDED FROM RULES 7 AND 9 ONLY — it is not a rule of its
  own (``recovery-decision-intake`` BR-4/SR-8).** A ``skip`` decision means "use the stored
  result", so the human authorised USING a stored result, not bypassing the checks that decide
  whether one is usable. Hoisting the exclusion into a rule placed before rule 6 would
  silently disable FR-3's loud failure on a changed script; placed before rule 3 it would
  replay a row with no envelope. As an exclusion on rules 7 and 9, rules 3-6 all still fire
  and the row falls to the catch-all — which is exactly what ``skip`` means. This is the
  placement most likely to look like a tidying opportunity, and the three tests in
  ``test_recovery_decision_intake.py::TestSkipAuthorisationKeepsTheSafetyRules`` — one per
  surviving safety rule — are what fail if it moves. Rule 8 needs NO exclusion and that is
  not an inconsistency: it tests ``state``, which ``apply_decisions`` has already overwritten
  with the authorised value, whereas rules 7 and 9 test the policy and the envelope and so
  survive that overwrite. The dividing line is "does the condition read ``state``", not "is
  the rule new".

``reconcile`` IS DEFERRED, WHICH IS WHY IT HAS NO BRANCH HERE (BR-11). ``component-methods.md``
rules 2 and 4 say ``reconcile`` -> ``EXECUTE`` "via the reconciliation operation". No such
operation exists anywhere in ``src/`` and #583's frozen scope defers generic external-system
reconciliation, so ``RECONCILE`` and ``IDEMPOTENT`` are INDISTINGUISHABLE at this gate today:
both take the single :data:`_REEXECUTION_PERMITTED` membership test and produce a plain
``EXECUTE`` carrying the same ``reason``. Stating the deferral is the point — a fifth verdict
for a behaviour nothing can serve would repeat the ``diverged_fields`` mistake (BR-7). When the
operation ships, this module is where the change starts, and the equivalence test in
``test_step_replay.py`` is what fails first.

THREE CATEGORIES OF OUTCOME, THREE BEHAVIOURS (BR-15/TD-6). "Total" and "raises ``ValueError``"
read as a contradiction at a glance, so the split is stated rather than inferred:

* a **decision outcome** is RETURNED — ``DIVERGED`` and ``DECISION_REQUIRED`` are verdicts,
  never exceptions (BR-9). Unit 9 owns the raise and the HTTP mapping in one place, which is
  why this module imports :class:`HaltRule` and NOTHING ELSE from ``workflow_errors``: an
  unused import of ``ReplayDivergenceError`` would be an invitation (TD-1);
* a **precondition violation** RAISES ``ValueError`` — a non-``v2`` incoming ``fingerprint``
  (SR-3/TD-2). Checked BEFORE the read, so a caller bug costs no journal round-trip;
* an **infrastructure failure** PROPAGATES unchanged — the one ``get_step`` call is not
  wrapped (BR-10/INV-4). An unreadable journal degrading to ``EXECUTE`` would re-run completed
  work under exactly the conditions FR-1 exists to prevent.

NOTHING IS LOGGED (SR-4) AND NOTHING IS PERSISTED (SR-5). The caller logs, holding ``verdict``,
``rule`` and ``reason``; a durable halt reason is unit 11's and a state transition unit 12's.
``reason`` carries IDENTIFIERS AND FIXED PHRASES ONLY — never a fingerprint, a prompt, a path,
a model id, a tool list, or any of the ten execution-affecting field values (SR-1, inherited
from ``step-fingerprint``'s SR-2). The gate never holds those values: it holds two digests and
one row, and it echoes NEITHER digest, not even in the ``ValueError``.

WHAT THIS MODULE DOES NOT DO. It raises neither ``ReplayDivergenceError`` nor
``RecoveryDecisionRequired`` and names neither (unit 9); it writes nothing to the journal
(unit 8 rewires the settle); it defines neither authorised-state TRANSITION — it only READS
the two states ``workflow_journal.apply_decisions`` writes (unit 12); it
persists no halt reason (unit 11); and it re-sanitises nothing on the ``REPLAY`` path — the
envelope was redacted and then bounded by ``build_envelope`` before it reached SQLite (unit 2's
SR-1/BR-2), so this module imports neither ``secret_gate`` nor ``build_envelope`` (SR-2) and
returns exactly what was persisted.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from cli_agent_orchestrator.models.workflow import RecoveryPolicy, StepResultEnvelope
from cli_agent_orchestrator.services.step_fingerprint import scheme_of
from cli_agent_orchestrator.services.step_result import parse_envelope
from cli_agent_orchestrator.services.workflow_errors import HaltRule
from cli_agent_orchestrator.services.workflow_journal import StepRow, get_step

# The one scheme a fingerprint may be compared under. Named because BOTH the SR-3 precondition
# and rules 4-5 ask the same question of it; two literals is the drift that would let the
# incoming check and the stored check disagree about what "current" means.
_CURRENT_SCHEME = "v2"

# The policies whose author declared re-execution safe (rules 2 and 4). ONE tuple, tested in
# two places, and deliberately NOT two branches: ``RECONCILE`` has no reconciliation operation
# to differ by (BR-11), so a separate arm for it would be a branch nothing can serve. This is a
# module-private literal beside the logic that reads it, not an addition to ``constants.py``
# (TD-1's "no new constant") — the same placement argument ``step_fingerprint`` makes for
# ``CREATION_ONLY``.
_REEXECUTION_PERMITTED = (RecoveryPolicy.IDEMPOTENT, RecoveryPolicy.RECONCILE)

# Row states this module tests by name. Bare string literals, matching how ``workflow_journal``
# already spells state values (``lookup_replay``, ``begin_step``, ``settle_run_state_if_running``):
# importing ``StepState`` for three literals would add a SIXTH package import and a sixth
# dependency edge (TD-1). ``test_step_replay.py`` pins each one against its ``StepState``
# member from the test file's own import, so a rename on either side fails loudly — and all
# three members now exist, added with their writer by ``recovery-decision-intake`` (unit 12).
_RUNNING = "running"
_RERUN_AUTHORIZED = "rerun_authorized"
_REPLAY_AUTHORIZED = "replay_authorized"
# Rule 8's state (PR #628 review). A fourth literal on the same terms as the three above, and
# pinned against ``StepState.FAILED`` by the same test that pins them.
_FAILED = "failed"


class ReplayVerdict(str, Enum):
    """What the caller must do about this step — a CLOSED four-member vocabulary.

    Four members, not five: ``EXECUTE_VIA_RECONCILE`` was considered and rejected because no
    reconciliation operation exists to serve it (BR-11). ``(str, Enum)`` echoes
    ``RecoveryPolicy`` and ``HaltRule`` so a consumer can compare, serialise and log a member
    without a custom encoder.
    """

    EXECUTE = "execute"  # run the step normally
    REPLAY = "replay"  # do not run it; use the envelope (FR-1)
    DIVERGED = "diverged"  # the script changed at this key; fail loudly (FR-3)
    DECISION_REQUIRED = "decision_required"  # a human must choose (FR-4 guard 2, FR-6, FR-7)


@dataclass(frozen=True)
class ReplayDecision:
    """One gate decision. In-memory only — nothing here is ever persisted (SR-5).

    ``frozen=True`` (TD-4): a decision is a statement about a moment, and a caller able to
    mutate one could launder a ``DIVERGED`` into a ``REPLAY`` — the single most consequential
    edit available in this subsystem. Freezing costs nothing, because :func:`decide`
    constructs each instance once and never revises it.

    THE TWO OPTIONAL FIELDS ARE EACH CONDITIONAL ON EXACTLY ONE VERDICT (BR-6), and
    :func:`decide` is the only construction site, which is what enforces it:

    * ``envelope`` is set **iff** ``verdict is REPLAY``. A populated envelope on any other
      verdict is a defect — it would offer a caller a result it was told not to use.
    * ``rule`` is set **iff** ``verdict is DECISION_REQUIRED`` (INV-5). ``HaltRule``'s six
      members map one-to-one onto the six halting rules, and
      ``RecoveryDecisionRequired(*, step_id, rule, reason)`` requires one — without this field
      the only path from gate to raiser would be parsing the prose ``reason``, i.e. reading
      English to recover a value that already exists as a closed enum (BR-8/TD-3).

    THERE IS DELIBERATELY NO ``diverged_fields`` (BR-7/TD-3, ruled at this unit's Q1).
    ``step_fingerprint.compute`` returns ONE digest over ten components and only the digest is
    persisted, so nothing can populate a per-field list; an always-empty one reads as "no
    fields diverged" when the truth is "we cannot tell" — the opposite conclusion. This is the
    same field, by name, that ``workflow-errors`` deleted from both exception types on
    identical reasoning (its BR-4). A test asserts its ABSENCE, so a well-meaning re-addition
    without a populating source fails loudly instead of misleading unit 9.

    Args:
        verdict: what the caller must do. Always set.
        envelope: the stored result to serve, iff ``verdict is REPLAY``. Returned exactly as
            ``parse_envelope`` reconstructed it — this module adds and removes nothing (SR-2).
        reason: human-readable cause. Always set. IDENTIFIERS AND FIXED PHRASES ONLY, never a
            fingerprint and never a field value (SR-1).
        rule: which decision-order condition halted, iff ``verdict is DECISION_REQUIRED``.
    """

    verdict: ReplayVerdict
    envelope: Optional[StepResultEnvelope]
    reason: str
    rule: Optional[HaltRule]


def decide(
    run_id: str,
    step_id: str,
    fingerprint: str,
    declared_policy: Optional[RecoveryPolicy],
) -> ReplayDecision:
    """Apply the eight ordered rules to one step and return the verdict. FIRST MATCH WINS.

    THE ORDER IS THE CONTRACT (BR-1) — see the module docstring for the three orderings that
    are load-bearing and why none of them may be rearranged for readability. Each rule below
    is annotated with the requirement it serves, so a reader can see what an edit costs.

    TOTAL OVER VALID INPUTS (BR-12/TD-6): every reachable path returns a ``ReplayDecision``
    and rule 10 is the catch-all. The two ways out that are not verdicts are categorically
    different and both deliberate — a precondition violation raises, an infrastructure failure
    propagates (BR-15). See ``Raises``.

    Args:
        run_id: the run being resumed. Read, and named in ``reason``.
        step_id: the step key being decided. Read, named in ``reason``, and named in the
            ``ValueError``.
        fingerprint: THIS call's fingerprint, computed under the current scheme by
            ``step_fingerprint.compute``. Validated first (SR-3).
        declared_policy: what the step's author declared, or ``None`` for undeclared.
            ``None`` IS NOT A DEFAULT THAT FALLS THROUGH (BR-5): rule 2 halts on it because
            the alternative there is re-execution, and the catch-all REPLAYS on it because replay
            executes nothing. ``RecoveryPolicy``'s docstring (``models/workflow.py``:125-128)
            argues that asymmetry; ``MANUAL`` differs from undeclared only at rule 7, where a
            verified replay is available and a human asked to see the step anyway.

    Returns:
        A ``ReplayDecision`` whose ``envelope`` is set iff the verdict is ``REPLAY`` and whose
        ``rule`` is set iff the verdict is ``DECISION_REQUIRED`` (BR-6).

    Raises:
        ValueError: if ``fingerprint`` was not computed under the current scheme (SR-3).
            CHECKED BEFORE THE READ — a caller bug should not cost a journal round-trip, and
            it must not reach rule 6 either: there it would mismatch a stored ``v2`` value and
            halt the run with ``DIVERGED``, i.e. report tampering-shaped evidence about the
            user's script when the defect is in CAO. That false attribution is the one real
            threat this unit introduces, and it is a truthfulness threat rather than a
            confidentiality one. The message names ``step_id`` and echoes NEITHER the supplied
            value nor the stored one (SR-1): the supplied value is arbitrary caller text under
            a digest prohibition.
        sqlite3.Error: propagated unchanged from the one ``get_step`` call, which is NOT
            wrapped (BR-10/INV-4). Degrading an unreadable journal to ``EXECUTE`` would
            re-run completed work under exactly the conditions FR-1 exists to prevent, and it
            would do so under the guise of a safe default.
    """
    # SR-3 — the precondition, before any read. ``scheme_of`` is total over ``str | None``, so
    # even a ``None`` smuggled past the type checker classifies as ``absent`` and lands here
    # rather than corrupting a comparison downstream.
    if scheme_of(fingerprint) != _CURRENT_SCHEME:
        raise ValueError(
            f"step '{step_id}': the supplied call fingerprint was not computed under the "
            f"current scheme, so no replay decision can be made for this step"
        )

    # THE one journal read (BR-13). Unwrapped on purpose (BR-10).
    row: Optional[StepRow] = get_step(run_id, step_id)

    # Every ``reason`` opens with the two identifiers and nothing else (SR-1).
    where = f"run '{run_id}' step '{step_id}'"

    # ---- rule 1: never dispatched, or a human consented ----
    if row is None:
        return ReplayDecision(
            verdict=ReplayVerdict.EXECUTE,
            envelope=None,
            reason=f"{where}: no journal row exists, so this step has never been dispatched",
            rule=None,
        )
    if row.state == _RERUN_AUTHORIZED:
        # BR-2/INV-3. Admitting this state here is HALF of the infinite-halt fix; the other
        # half is rule 2's exact-``running`` guard immediately below. Neither works alone.
        return ReplayDecision(
            verdict=ReplayVerdict.EXECUTE,
            envelope=None,
            reason=f"{where}: a human authorised a rerun of this step",
            rule=None,
        )

    # ---- rule 2: dispatched, outcome unknown (FR-7's trigger) ----
    # EXACTLY ``running``, never "not settled" (BR-2). Written as the broader condition this
    # re-halts a human-authorised rerun forever, and the ``rerun_authorized`` arm above cannot
    # save it because a later state added by unit 12 would fall in here instead.
    if row.state == _RUNNING:
        if declared_policy in _REEXECUTION_PERMITTED:
            return ReplayDecision(
                verdict=ReplayVerdict.EXECUTE,
                envelope=None,
                reason=(
                    f"{where}: the step was dispatched and its outcome is unknown, and its "
                    f"declared recovery policy permits re-execution"
                ),
                rule=None,
            )
        return ReplayDecision(
            verdict=ReplayVerdict.DECISION_REQUIRED,
            envelope=None,
            reason=(
                f"{where}: the step was dispatched and its outcome is unknown, and no "
                f"declared recovery policy permits re-execution"
            ),
            rule=HaltRule.INTERRUPTED_NO_POLICY,
        )

    # ---- the row is treated as settled from here on ----
    # "Settled" here means "not absent, not rerun-authorised, not running" — an OPEN negative,
    # which is what BR-2 requires, and it has a consequence worth stating exactly rather than
    # reassuringly: a state a LATER unit adds without extending rule 1 arrives at rule 3 and is
    # judged on its envelope, so with a readable envelope and a matching current-scheme
    # fingerprint IT REPLAYS. That is correct for a state meaning "this result stands" and
    # WRONG for one meaning "do not use the stored result", and only rule 1 can express the
    # difference. So any unit adding a non-settled state must extend rule 1 in the same change
    # — the way ``rerun_authorized`` already is, before unit 12 writes it. Broadening rule 2
    # instead is the one repair that is never available (BR-2). ``replay_authorized`` is the
    # OTHER case of that same rule and the reason it is stated as a pair: it means "this result
    # stands, serve it", so it belongs on this settled path and takes no rule-1 arm — it is
    # judged by rules 3-6 exactly like any other settled row (unit 12's BR-5), and by rule 9
    # only via that rule's explicit exclusion.

    # ---- rule 3: settled with no readable result (FR-4 guard 2) ----
    # A CORRUPT envelope IS an absent envelope (BR-14): ``parse_envelope`` collapses NULL,
    # malformed JSON and valid-JSON-of-the-wrong-shape to ``None`` because every one of them
    # means "this row cannot be replayed". Rule 3 is that rule, so no separate branch exists.
    envelope = parse_envelope(row.result_json)
    if envelope is None:
        return ReplayDecision(
            verdict=ReplayVerdict.DECISION_REQUIRED,
            envelope=None,
            reason=(
                f"{where}: the row is settled but carries no readable result envelope, so "
                f"there is nothing to replay"
            ),
            rule=HaltRule.ENVELOPE_ABSENT,
        )

    # ---- rules 4 and 5: provenance, BEFORE equality (BR-3/BR-4) ----
    # ``!= _CURRENT_SCHEME`` rather than ``in ("legacy", "absent")`` (TD-5): the negative asks
    # the question the rule means — is this verifiable under the current scheme — and needs no
    # edit if a third unverifiable scheme ever appears. An allowlist would silently route a
    # future scheme into ``v2``'s branch, treating an unverifiable fingerprint as verifiable,
    # which is the exact failure FR-6 exists to prevent. ``legacy`` and ``absent`` stay
    # DISTINCT FACTS in ``reason`` even though they route identically.
    scheme = scheme_of(row.call_fingerprint)
    if scheme != _CURRENT_SCHEME:
        if declared_policy in _REEXECUTION_PERMITTED:
            # Rule 4 first: an author who declared re-execution safe is not punished across
            # the scheme-upgrade window (FR-5's composition with FR-6, BR-3).
            return ReplayDecision(
                verdict=ReplayVerdict.EXECUTE,
                envelope=None,
                reason=(
                    f"{where}: the stored call fingerprint's scheme is '{scheme}' rather than "
                    f"the current scheme, and its declared recovery policy permits "
                    f"re-execution"
                ),
                rule=None,
            )
        # Rule 5 second: unverifiable provenance NEVER replays as a match (FR-6), and is
        # never labelled DIVERGED either (BR-4/INV-2) — comparing a hash computed under
        # narrower rules would answer a question nobody asked.
        return ReplayDecision(
            verdict=ReplayVerdict.DECISION_REQUIRED,
            envelope=None,
            reason=(
                f"{where}: the stored call fingerprint's scheme is '{scheme}' rather than the "
                f"current scheme, so its provenance cannot be verified"
            ),
            rule=HaltRule.PROVENANCE_UNVERIFIABLE,
        )

    # ---- rule 6: the script changed at this key (FR-3) ----
    # Both values are current-scheme here — the incoming by the SR-3 precondition, the stored
    # by rules 4-5 — which is what makes this comparison meaningful (INV-1).
    if row.call_fingerprint != fingerprint:
        return ReplayDecision(
            verdict=ReplayVerdict.DIVERGED,
            envelope=None,
            reason=(
                f"{where}: the stored call fingerprint differs from this call's under the "
                f"current scheme, so the step changed between runs at the same key"
            ),
            rule=None,
        )

    # ---- rule 7: a verified match, but a human asked to see it (FR-7) ----
    # ...UNLESS the human has since answered (``recovery-decision-intake`` BR-4/BR-5, SR-8).
    # ``replay_authorized`` is the durable form of a ``skip`` decision, and rules 7 and 9 are
    # the only rules it is excluded from — an exclusion, deliberately not a rule of its own
    # placed earlier, because rules 3-6 must still decide whether the stored result is USABLE
    # before this row is allowed to serve it. The row therefore falls to the catch-all and
    # replays with the envelope, which is what ``skip`` means. It needs no rule-1 arm either:
    # rule 1 is for
    # states meaning EXECUTE, and a ``replay_authorized`` row still holds the result it was
    # authorised to serve, so it belongs on the settled path where rules 3-5 run first.
    if declared_policy is RecoveryPolicy.MANUAL and row.state != _REPLAY_AUTHORIZED:
        return ReplayDecision(
            verdict=ReplayVerdict.DECISION_REQUIRED,
            envelope=None,
            reason=(
                f"{where}: the call fingerprints match under the current scheme, and the "
                f"declared recovery policy is 'manual'"
            ),
            rule=HaltRule.POLICY_MANUAL,
        )

    # ---- rule 8: the recorded outcome was a FAILURE (PR #628 review, Copilot F1) ----
    # A ``failed`` row reached the old catch-all and REPLAYED: the route answered HTTP 200
    # with a ``StepHandle``, so the author's script continued past a call that FAILED on the
    # original drive — where it raised, because ``run_agent_step``'s ``StepExecutionError``
    # became a 502/504 and the shim turned that into ``ShimHTTPError``. Replaying it does not
    # reproduce the original run; it silently deletes a failure from it.
    #
    # HALT rather than re-execute (the ruling on that finding). Re-executing would grant
    # permission the step's author never gave — the same reasoning rule 2 already applies to a
    # crash-window row — so this is fail-closed, consistent with every other condition the
    # gate cannot resolve alone.
    #
    # BOTH DECISIONS ESCAPE THIS RULE, AND NEITHER NEEDS AN EXPLICIT EXCLUSION — which is
    # worth stating because rule 9 below DOES need one and the asymmetry looks like an
    # oversight. ``apply_decisions`` writes the authorised value INTO ``state``, overwriting
    # ``failed``: a ``rerun`` row reads ``rerun_authorized`` and rule 1 has already returned
    # ``EXECUTE``; a ``skip`` row reads ``replay_authorized``, so this test simply does not
    # match and the row continues to the catch-all. Without that, a human who answered this
    # halt would be asked again on every subsequent resume — the infinite-halt defect BR-2
    # exists to prevent, in a new place. Rule 9's condition is the ENVELOPE's, not the state's,
    # so it survives the overwrite and has to exclude the state by hand.
    # Gate 5 in ``script_runner.resume_script_run`` also rejects an unconsumed authorisation
    # on a later resume unless that step receives a fresh decision.
    if row.state == _FAILED:
        return ReplayDecision(
            verdict=ReplayVerdict.DECISION_REQUIRED,
            envelope=None,
            reason=(
                f"{where}: the stored result records a FAILED outcome, so replaying it would "
                f"report a success the original run never produced"
            ),
            rule=HaltRule.OUTCOME_FAILED,
        )

    # ---- rule 9: the stored envelope reports its own lossiness (PR #628 review, F5) ----
    # ``build_envelope`` redacts and then bounds ``last_message`` before it reaches SQLite, so
    # a stored envelope's text is not necessarily the text the original call returned — and the
    # original call returned the RAW text, because the success arm of the route answers with
    # ``run_agent_step``'s own ``last_message`` and never with the envelope. ``RunStepResponse``
    # has no ``truncated``/``redacted`` field either, so a replayed response cannot even say
    # that what it served is abridged. An unchanged script that feeds a >32 KiB or redacted
    # result into its next prompt therefore computes a DIFFERENT next-step fingerprint and
    # diverges — reporting "the script changed" when nothing about the script changed.
    #
    # Fail closed for the same reason as rule 8, with the same ``replay_authorized`` exclusion:
    # a human who answers ``skip`` has accepted the abridged text, and must not be re-asked
    # forever. Note the flags are the ENVELOPE's own self-report (unit 2's INV-5), so this rule
    # needs no second copy of the redaction or bounding logic — which is what keeps SR-2's "no
    # ``secret_gate``, no ``build_envelope`` import" posture intact.
    if (envelope.truncated or envelope.redacted) and row.state != _REPLAY_AUTHORIZED:
        return ReplayDecision(
            verdict=ReplayVerdict.DECISION_REQUIRED,
            envelope=None,
            reason=(
                f"{where}: the stored result envelope reports itself truncated or redacted, "
                f"so its text is not what the original call returned"
            ),
            rule=HaltRule.ENVELOPE_LOSSY,
        )

    # ---- rule 10: the catch-all, INCLUDING an undeclared policy (FR-1/BR-5) ----
    # Undeclared replays where ``MANUAL`` halts, because replay executes nothing. The envelope
    # is handed back exactly as ``parse_envelope`` produced it (SR-2).
    return ReplayDecision(
        verdict=ReplayVerdict.REPLAY,
        envelope=envelope,
        reason=(
            f"{where}: the call fingerprints match under the current scheme and a stored "
            f"result envelope is available, so the recorded result stands"
        ),
        rule=None,
    )
