"""Return a provider session to circulation once its owner is gone.

:mod:`native_attachment` can refuse a second attacher, and it can record
a release — but nothing ever called that release.  Every claim this
system has ever taken is therefore still live, held by a process that in
almost every case died days ago, and because ``declare`` refuses a live
owner, every one of those provider sessions is permanently unresumable.
A store that only ever acquires is not a lock; it is a leak with a
conflict check on it.

This module is the missing half: the observer that decides whether an
owner survived, and the two callers that act on the answer — terminal
teardown, which closes the claim as it ends, and a sweep, which closes
the claims that were already lost.

The observation is deliberately lopsided
=======================================

Only one answer releases anything: the pid is **gone**, reported by
``os.kill(pid, 0)`` raising ``ProcessLookupError``.  Every other reading
— alive, alive-under-a-different-start-marker, unreadable, or never
published at all — leaves the claim exactly where it is.

That asymmetry is not caution for its own sake.  The stored
``start_marker`` is ``ps -o lstart=`` output: naive local wall-clock,
rendered in the writing process's timezone and locale.  A design that
released on "pid is alive but its marker differs from the stored one"
would read a DST rollover, a ``TZ`` change, or a locale change as
evidence that a *live* owner is a recycled pid, and hand its session to
a second attacher.  ``ProcessLookupError`` carries no such dependency:
an absent pid is absent in every timezone.

The marker is still read, and still recorded, because it is exactly the
evidence a *human* is supposed to weigh.  It just never decides anything
here.  On this install that costs one row — a genuinely recycled pid
whose real owner is dead — which stays claimed until an operator looks at
the live process and attests, on their name, that it is not the recorded
owner.  Unresolved is a survivable outcome; a forged no-survivor is not.

A row with no published process identity is likewise never released
here.  ``declare`` writes the claim *before* the provider process
exists, so an identity-less row is either a launch in flight or a crash
during one, and those are indistinguishable from the outside.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from cli_agent_orchestrator.services import native_attachment

logger = logging.getLogger(__name__)

#: Recorded in every proof this module produces, so a stored release can
#: be attributed to this observer rather than to some future one with
#: different standards.
OBSERVER = "cao.native_attachment_recovery"

#: What the observation concluded about the owning process.
OWNER_GONE = "gone"
OWNER_ALIVE = "alive"
OWNER_UNOBSERVABLE = "unobservable"
OWNER_UNPUBLISHED = "unpublished"

#: How long teardown waits for a just-killed provider to actually leave
#: the process table.  ``kill-window`` sends SIGHUP and returns; the
#: provider gets to run its own shutdown first.  Two seconds is long
#: enough for every CLI observed here and short enough that a hung
#: provider does not hold a teardown open — it just does not get
#: released, which is the safe outcome.
TEARDOWN_GRACE_SECONDS = 2.0
_TEARDOWN_POLL_SECONDS = 0.05


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _pid_state(pid: int) -> str:
    """Three-way liveness for one pid: gone, alive, or unobservable.

    The existing helper in this codebase — ``native_tui_launch._process_field``
    — collapses "no such process" and "``ps`` could not be run" into a
    single ``None``.  That is fine where it is used, and fatal here: the
    proof this feeds requires an observation that was actually made, and
    "we did not look" must never be recorded as "nothing is there".

    ``PermissionError`` means the pid exists and belongs to somebody
    else, which is alive.  Any other ``OSError`` is a failed look.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return OWNER_UNOBSERVABLE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return OWNER_GONE
    except PermissionError:
        return OWNER_ALIVE
    except OSError:
        return OWNER_UNOBSERVABLE
    return OWNER_ALIVE


def _live_start_marker(pid: int) -> Optional[str]:
    """The pid's current start marker, or ``None`` if it could not be read.

    Read through the *same* producer that wrote the stored markers
    (``native_tui_launch._process_field``, i.e. ``ps -o lstart=`` with the
    caller's inherited locale and timezone).  Using a different one — a
    normalised format, a pinned ``LC_ALL``, ``/proc`` — would render a
    string that cannot equal anything already on record, and every
    comparison would report a mismatch that is really a formatting
    difference.

    Evidence only.  Nothing in this module branches on the result.
    """
    from cli_agent_orchestrator.services import native_tui_launch

    try:
        return native_tui_launch._process_field(pid, "lstart=")
    except Exception:  # noqa: BLE001 - evidence is best-effort by definition
        return None


def observe_owner(record: Mapping[str, Any], *, read_marker: bool = True) -> dict[str, Any]:
    """Look for the process that holds this attachment.

    Returns the observation, never raises for an ordinary negative
    result.  ``survivors`` is present in every return value — empty only
    when the owner was looked for and found absent — because
    :func:`native_attachment.no_survivor_proof` refuses an omitted
    observation and an omitted one is what "we did not look" looks like.

    ``read_marker=False`` skips the ``ps`` call that renders the live
    start marker.  The marker is evidence for a human and decides
    nothing, so a caller polling for a process to exit — where the answer
    it wants is ``gone``, and ``gone`` never reads a marker at all — has
    no reason to fork ``ps`` forty times to compute a field it discards.
    """
    owner = record.get("owner") or {}
    identity = owner.get("process_identity")
    observed_at = _now()

    if not isinstance(identity, Mapping) or not isinstance(identity.get("pid"), int):
        return {
            "disposition": OWNER_UNPUBLISHED,
            "survivors": [],
            "observed_at": observed_at,
            "observer": OBSERVER,
            "detail": (
                "the owner never published a process identity, so there is nothing to look "
                "for; the claim was written before the provider process existed"
            ),
        }

    pid = int(identity["pid"])
    stored_marker = identity.get("start_marker")
    state = _pid_state(pid)

    if state == OWNER_GONE:
        return {
            "disposition": OWNER_GONE,
            "survivors": [],
            "observed_at": observed_at,
            "observer": OBSERVER,
            "detail": f"pid {pid} is not in the process table",
        }

    if state == OWNER_UNOBSERVABLE:
        return {
            "disposition": OWNER_UNOBSERVABLE,
            "survivors": [dict(identity)],
            "observed_at": observed_at,
            "observer": OBSERVER,
            "detail": f"pid {pid} could not be checked; an unchecked owner is treated as alive",
        }

    live_marker = _live_start_marker(pid) if read_marker else None
    if not read_marker:
        marker_verdict = "not-read"
    elif live_marker is None:
        marker_verdict = "unreadable"
    elif live_marker == stored_marker:
        marker_verdict = "matches"
    else:
        marker_verdict = "differs"
    return {
        "disposition": OWNER_ALIVE,
        "survivors": [{"pid": pid, "start_marker": live_marker}],
        "observed_at": observed_at,
        "observer": OBSERVER,
        "stored_start_marker": stored_marker,
        "start_marker_verdict": marker_verdict,
        "detail": (
            f"pid {pid} is alive and its start marker {marker_verdict} the recorded one; "
            "a live pid is never released by automation, because a marker mismatch is as "
            "easily a timezone change as a recycled pid"
        ),
    }


def survivors_blocking_adjudication(observation: Mapping[str, Any]) -> list[Any]:
    """The survivors an operator is not permitted to decide past.

    ``observe_owner`` puts an entry in ``survivors`` for an owner it could
    not check, which is right for automation — an unchecked owner is
    treated as alive, and nothing automatic may release it.  It is wrong
    for adjudication, and in a way that broke the valve for the exact case
    it was built for: an operator would freeze a claim *because* the
    process table would not answer, and then be refused with "the frozen
    owner is still observably alive" about a process nobody observed.

    So the veto is on a process actually *seen* running.  "We could not
    look" is not a survivor sighting; it is the condition that makes a
    human necessary, and having asked for one it would be perverse to
    then refuse their answer.  Both steps still carry their name.
    """
    if observation.get("disposition") != OWNER_ALIVE:
        return []
    return list(observation.get("survivors") or [])


def release_if_owner_gone(
    record: Mapping[str, Any],
    *,
    observation: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Release one attachment, but only on a proven-absent owner.

    Returns an outcome dict in every case — released, skipped, refused, or
    errored — so a caller sweeping hundreds of rows gets a per-row account
    rather than an exception it has to interpret.  Nothing escapes.

    That includes the unexpected.  One malformed row must not abort a
    sweep over every other row: an ``execution_mode`` this build no longer
    recognises raises ``ExecutionModeInvalid``, which is not a
    ``NativeAttachmentError`` and would otherwise take the whole pass down
    — leaving the operator with a partial run and no report saying which
    rows were even examined.
    """
    owner = record.get("owner") or {}
    outcome: dict[str, Any] = {
        "provider": record.get("provider"),
        "native_session_id": record.get("native_session_id"),
        "state": record.get("state"),
        "owner_terminal_id": owner.get("terminal_id"),
        "owner_generation": owner.get("generation"),
    }

    if record.get("state") not in native_attachment.LIVE_STATES:
        outcome.update(
            action="skipped",
            reason="not-live",
            detail=f"state {record.get('state')!r} holds nothing to release",
        )
        return outcome

    observed = dict(observation) if observation is not None else observe_owner(record)
    outcome["disposition"] = observed["disposition"]
    outcome["observation"] = observed

    if observed["disposition"] != OWNER_GONE:
        outcome.update(action="skipped", reason=observed["disposition"], detail=observed["detail"])
        return outcome

    try:
        proof = native_attachment.no_survivor_proof(
            provider=record["provider"],
            native_session_id=record["native_session_id"],
            terminal_id=owner["terminal_id"],
            generation=owner["generation"],
            execution_mode=owner["execution_mode"],
            pane_id=owner.get("pane_id"),
            process_identity=owner.get("process_identity"),
            survivors=observed["survivors"],
            observed_at=observed["observed_at"],
            observer=observed["observer"],
        )
        released = native_attachment.release(
            provider=record["provider"],
            native_session_id=record["native_session_id"],
            terminal_id=owner["terminal_id"],
            generation=owner["generation"],
            execution_mode=owner["execution_mode"],
            proof=proof,
        )
    except native_attachment.NativeAttachmentError as exc:
        # A conflict here is the store working: the row moved between the
        # listing and the write, so the compare-and-swap refused rather
        # than overwriting whoever won.  Report it and move on; forcing
        # would be the double-attach this whole module guards against.
        outcome.update(action="refused", reason=exc.code, detail=str(exc))
        return outcome
    except Exception as exc:  # noqa: BLE001 - one bad row must not end the pass
        logger.warning(
            "Could not resolve the %s claim on session %s: %s",
            record.get("provider"),
            record.get("native_session_id"),
            exc,
        )
        outcome.update(action="errored", reason=type(exc).__name__, detail=str(exc))
        return outcome
    outcome.update(action="released", reason=OWNER_GONE, state=released["state"])
    return outcome


def release_owned_by_terminal(
    terminal_id: str,
    *,
    generation: Optional[str] = None,
    grace_seconds: Optional[float] = None,
) -> list[dict[str, Any]]:
    """Close the claims held by a terminal that is being torn down.

    Called from teardown *after* the window kill, because that is the
    first moment the answer can be yes.  ``kill-window`` sends SIGHUP and
    returns immediately; the provider still gets to shut itself down, so
    an observation taken the instant the window disappears finds the
    process very much alive.  This waits — briefly, and only while the
    answer is still "alive" — for it to leave the process table.

    An owner that is still alive when the grace expires is **left
    attached**, not frozen.  Freezing was the first design here and it was
    wrong: ``mark_ambiguous`` is terminal for automation, so it would
    convert a state the sweep can resolve on its own — the provider dies a
    second later, or a minute later, and the next sweep releases it — into
    one that permanently requires a human.  It also mislabels the
    evidence.  "Frozen" means ownership could not be determined; here it
    was determined exactly, and the answer was "still running".

    Nothing is lost by leaving it: the claim is listed, the sweep reports
    it every pass, and the release happens the moment the process is
    actually gone.  An operator who needs it back sooner can freeze it
    deliberately and adjudicate — two named steps, rather than teardown
    guessing on their behalf.
    """
    records = [
        record
        for record in native_attachment.list_attachments(owner_terminal_id=terminal_id)
        if record["state"] in native_attachment.LIVE_STATES
        and (generation is None or record["owner"]["generation"] == generation)
    ]
    if not records:
        return []

    # Resolved at call time, not bound as a default argument.  A default
    # captures the module constant once at import, so a caller or a test
    # that sets ``TEARDOWN_GRACE_SECONDS`` would change nothing and
    # believe it had — which is exactly what the suite's own fixture was
    # doing before this was noticed.
    if grace_seconds is None:
        grace_seconds = TEARDOWN_GRACE_SECONDS

    outcomes: list[dict[str, Any]] = []
    for record in records:
        observed = _observe_until_gone(record, grace_seconds=grace_seconds)
        outcomes.append(release_if_owner_gone(record, observation=observed))
    return outcomes


def _observe_until_gone(record: Mapping[str, Any], *, grace_seconds: float) -> dict[str, Any]:
    """Re-observe while the owner is still alive, up to the grace bound.

    Waits with a blocking sleep, so it declines to wait at all when it
    finds itself on a running event loop.  Teardown is normally reached
    through ``asyncio.to_thread`` and this does not come up — but not
    every caller does that, and stalling an event loop for two seconds per
    claim to save a sweep the trouble is a bad trade.  Not waiting costs
    nothing permanent: the claim stays listed and the next sweep releases
    it the moment the process is gone.
    """
    observed = observe_owner(record)
    if observed["disposition"] != OWNER_ALIVE or grace_seconds <= 0:
        return observed
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        return observed
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        time.sleep(_TEARDOWN_POLL_SECONDS)
        polled = observe_owner(record, read_marker=False)
        if polled["disposition"] != OWNER_ALIVE:
            return polled
    # Still alive. Take one full observation so the recorded evidence
    # carries the marker comparison a reader will want, which the cheap
    # polls above deliberately skipped.
    return observe_owner(record)


#: Why an operator froze a claim by hand.  ``mark_ambiguous`` does not
#: validate its reason against the launch-time vocabulary, so this is a
#: convention like the others — but it must be distinguishable from them:
#: every existing reason describes something the *launcher* could not
#: verify, and this one describes a deliberate human act.
OPERATOR_FREEZE_REASON = "operator_declared_owner_unresolvable"


def freeze_for_adjudication(
    *,
    provider: str,
    native_session_id: str,
    operator: str,
    detail: str,
    attest_live_pid_is_not_the_owner: bool = False,
) -> dict[str, Any]:
    """Move a stuck live claim into the state a human can adjudicate.

    ``adjudicate`` deliberately only accepts a frozen row, and teardown
    deliberately never freezes one.  That leaves a real gap: a claim whose
    owner can never be settled by observation stays in a live state, and
    the sweep reports it every pass without ever resolving it.  There
    would be no operator path at all — which is the defect this whole
    change exists to remove.

    This is that path, and it is two steps on purpose.  Freezing says "I
    cannot determine this"; adjudicating says "I have decided anyway".
    Collapsing them into one command would let the second sentence be
    spoken without the first ever having been true.

    Exactly two dispositions are freezable.

    **Unobservable** — the process table would not answer.  Nothing
    further will ever answer either, so a human is the only remaining
    resort.

    **Alive with a start marker that differs from the recorded one** —
    the recycled-pid case, and only on an explicit
    ``attest_live_pid_is_not_the_owner``.  Automation may never act on a
    marker mismatch, because a timezone change produces exactly the same
    evidence; a human can look at the process and tell.  The attestation
    is recorded in the freeze reason together with both markers, so the
    next reader sees the judgement rather than inferring it.  Without this
    path the recycled-pid row has no valve at all, and every future pid
    collision strands another session permanently.

    Everything else is refused, and two of the refusals matter.

    An owner **alive with a matching marker** is the recorded owner,
    running.  No attestation makes that releasable.

    An owner with **no published identity** is refused even though it
    looks like the most stuck row on the list.  ``declare`` writes the
    claim before the provider process exists, so a launch that is
    currently cold-starting is indistinguishable from a launch that
    crashed — both are ``starting`` with no pid, and on the operator's
    screen they are the same line.  Freezing one blocks its own
    ``mark_attached``, so the identity is never published, the process
    keeps running, and the subsequent adjudication sees an empty survivor
    list and hands the session away underneath it.  That is the
    double-attach this module exists to prevent, reached through the
    command meant to repair it.
    """
    record = native_attachment.get(provider, native_session_id)
    if record is None:
        raise native_attachment.NativeAttachmentNotFound(
            f"no attachment declared for {provider} session {native_session_id}"
        )
    observation = observe_owner(record)
    disposition = observation["disposition"]

    if disposition == OWNER_GONE:
        raise native_attachment.NativeAttachmentConflict(
            f"the owner of {provider} session {native_session_id} is provably gone; "
            "release it with `cao attachment sweep --apply` rather than freezing it "
            "for a judgement call nobody needs to make"
        )
    if disposition == OWNER_UNPUBLISHED:
        raise native_attachment.NativeAttachmentConflict(
            f"{provider} session {native_session_id} has no published process identity, so a "
            "launch still starting up and a launch that crashed look identical; freezing it "
            "would block the running launch from publishing and then release its session"
        )

    reason = f"{OPERATOR_FREEZE_REASON}: {operator}: {detail}"

    if disposition == OWNER_ALIVE:
        verdict = observation.get("start_marker_verdict")
        if verdict != "differs":
            raise native_attachment.NativeAttachmentConflict(
                f"the owner of {provider} session {native_session_id} is running "
                f"({observation['detail']}); a live owner is not an unresolvable "
                "ownership question and is refused"
            )
        if not attest_live_pid_is_not_the_owner:
            raise native_attachment.NativeAttachmentConflict(
                f"pid {observation['survivors'][0].get('pid')} is alive but its start marker "
                f"differs from the recorded one. That is either a recycled pid or a timezone "
                "change, and only a human can tell which. Re-run attesting that the live "
                "process is not the recorded owner if you have checked it."
            )
        reason = (
            f"{native_attachment.RECYCLED_PID_ATTESTATION}: {operator}: {detail} "
            f"(recorded start_marker {observation.get('stored_start_marker')!r}; "
            f"live start_marker {observation['survivors'][0].get('start_marker')!r})"
        )

    return native_attachment.mark_ambiguous(
        provider=provider,
        native_session_id=native_session_id,
        reason=reason,
        expected_epoch=record["epoch"],
    )


def sweep(*, apply: bool = False) -> dict[str, Any]:
    """Release every live claim whose owner is provably gone.

    The counterpart to the teardown wiring, and the reason this is a
    standing service rather than a one-time script.  Teardown can only
    close the claims it is present for; the largest single producer of
    lost claims is the server exiting, which runs no teardown at all —
    there is no shutdown hook that enumerates terminals, and no boot-time
    adjudication on the tmux backend.  Every one of those claims outlives
    both its process and the row that pointed at it, and the only way
    back is to go and look.

    Dry-run by default.  ``--apply`` semantics match the repair
    precedent in this codebase, and the report is the same shape either
    way, with ``applied`` echoed back so a reader can never mistake a
    plan for a result.
    """
    started = _now()
    records = native_attachment.list_attachments(states=native_attachment.LIVE_STATES)
    outcomes: list[dict[str, Any]] = []
    for record in records:
        try:
            observed = observe_owner(record)
        except Exception as exc:  # noqa: BLE001 - one bad row must not end the pass
            outcomes.append(
                {
                    "provider": record.get("provider"),
                    "native_session_id": record.get("native_session_id"),
                    "state": record.get("state"),
                    "action": "errored",
                    "reason": type(exc).__name__,
                    "detail": str(exc),
                }
            )
            continue
        if not apply:
            releasable = observed["disposition"] == OWNER_GONE
            outcomes.append(
                {
                    "provider": record["provider"],
                    "native_session_id": record["native_session_id"],
                    "state": record["state"],
                    "owner_terminal_id": record["owner"]["terminal_id"],
                    "owner_generation": record["owner"]["generation"],
                    "disposition": observed["disposition"],
                    "action": "would-release" if releasable else "skipped",
                    "reason": observed["disposition"],
                    "detail": observed["detail"],
                    "observation": observed,
                }
            )
            continue
        outcomes.append(release_if_owner_gone(record, observation=observed))

    counts: dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome["action"]] = counts.get(outcome["action"], 0) + 1
        key = f"owner-{outcome.get('disposition', 'n/a')}"
        counts[key] = counts.get(key, 0) + 1
    return {
        "schema": "cao-native-attachment-sweep-v1",
        "applied": apply,
        "observer": OBSERVER,
        "started_at": started,
        "finished_at": _now(),
        "examined": len(records),
        "counts": counts,
        # Recorded because the stored start markers are rendered in the
        # writing process's timezone and locale, so a report produced under
        # different ones will disagree with every marker it read.  Nothing
        # here decides on a marker, but a reader comparing two reports needs
        # to know whether the environment moved underneath them.
        "environment": {
            # The effective zone, not just ``TZ`` — which is unset on a
            # default install, so recording only the variable wrote three
            # nulls into every report and told a later reader nothing.
            "tz": time.strftime("%Z%z"),
            "tz_env": os.environ.get("TZ"),
            "lc_all": os.environ.get("LC_ALL"),
            "lc_time": os.environ.get("LC_TIME"),
        },
        "outcomes": outcomes,
    }


#: Set to ``apply`` to let the boot sweep release what it finds.  The
#: default reports and changes nothing, for a reason that is about blast
#: radius rather than confidence: entering this application's lifespan is
#: something a *test* does, and two tests in this repository do it without
#: stubbing the recovery steps.  A boot sweep that mutated by default
#: would therefore release live rows out of an operator's real database as
#: a side effect of running pytest — which this repository has been bitten
#: by before, and says so where it happened.
SWEEP_ON_BOOT_ENV = "CAO_ATTACHMENT_SWEEP_ON_BOOT"


def sweep_at_startup() -> dict[str, Any]:
    """Look, at boot, for claims lost to the previous process's death.

    Boot is the one moment the answer is unambiguous for those claims: the
    pids are gone and will not come back, while the panes that genuinely
    survived the restart still hold living processes and are refused on
    exactly the same test.  Nothing else in this system notices them —
    there is no shutdown hook that enumerates terminals, and a server that
    dies runs no teardown at all — so if this pass does not report them,
    nobody does.

    Reports by default and never raises: a boot must not fail because a
    sweep could not run.
    """
    apply = os.environ.get(SWEEP_ON_BOOT_ENV, "").strip().lower() == "apply"
    try:
        report = sweep(apply=apply)
    except Exception as exc:  # noqa: BLE001 - a boot must not fail on a sweep
        logger.warning("Native attachment sweep failed at startup: %s", exc)
        return {"schema": "cao-native-attachment-sweep-v1", "applied": apply, "error": str(exc)}

    if apply:
        released = report["counts"].get("released", 0)
        if released:
            logger.info(
                "Released %d native session claim(s) whose owning process is gone "
                "(examined %d).",
                released,
                report["examined"],
            )
        return report

    releasable = report["counts"].get("would-release", 0)
    if releasable:
        logger.warning(
            "%d provider-native session(s) are claimed by processes that no longer exist "
            "and cannot be resumed until the claims are released. Run "
            "`cao attachment sweep --apply`, or set %s=apply to do it at boot.",
            releasable,
            SWEEP_ON_BOOT_ENV,
        )
    return report
