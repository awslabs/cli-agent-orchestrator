"""Start the real provider TUI as a worker pane's own primary process.

This is the native half of the closed ``native_tui | acp`` launch
contract.  Its sibling, the ACP branch, starts a bridge process that
speaks the provider's Agent Client Protocol over private stdio pipes and
leaves the pane showing raw JSON-RPC.  This branch starts the provider's
own terminal UI instead, so the pane a human opens is the session, not a
transport log.

A per-provider argv binder builds the argv; this module runs the launch.
The split matters because argv construction is pure and the launch is
not: everything here either changes durable ownership or starts a
process, and the ordering between those two is the whole point.

The binder is chosen by canonical provider rather than hardcoded, because
the two supported providers acquire their identity in opposite
directions. Kimi's session is minted by a separate ACP process and the
TUI *resumes* it, so a Kimi launch is always a resume. Claude's identity
is a uuid chosen before any provider I/O and handed to the TUI as
``--session-id``, so a first Claude launch *starts* the session and only
a recovery resumes it. That is why :func:`start` takes ``launch_kind``:
the difference is not a detail of argv formatting, it is which of the two
things is happening, and a caller that gets it wrong must be refused
rather than quietly given the other form.

Three orderings are load-bearing, and each exists because of a specific
way a native launch corrupts a live provider session:

**Ownership is claimed before the process starts.**  A launch that
started the TUI first and recorded ownership afterwards would have a
window in which a running process holds a provider session that no
durable record names.  A second launcher reading the store in that window
sees the session as free and attaches to it, and neither side can
subsequently tell that the transcript it is reading contains another
controller's turns.  So :func:`native_attachment.declare` and
:func:`native_attachment.mark_starting` are both crossed before the
first byte of process creation.

**The pane is proven to be running the session we claimed.**  The
installed Kimi resume option takes an *optional* argument: given no id it
opens an interactive picker rather than failing.  A launch that trusted
its own argv would therefore hold a record naming one session while the
pane runs whichever session a picker landed on.  After the pane exists
this module reads back the primary process's real command line and
requires it to resume exactly the bound session, and it publishes the
attachment only then.

**Every unresolved outcome freezes rather than retries.**  A pane
creation that raises, a pane that cannot be read, and a pane whose
command line does not match are all states in which a provider process
may or may not be holding the session.  Each one marks the attachment
``ambiguous``, which preserves the owner permanently and blocks every
later claim.  Nothing here ever relaunches, because the failure this
guards against — two TUIs on one session — is exactly what a retry
against an uncertain outcome produces.

That last rule also governs re-entry.  A caller that crashed between
``mark_starting`` and publication comes back to a ``starting`` row, and
``starting`` cannot distinguish "the process was never started" from
"the process started and has since exited".  Re-entry therefore observes
and never launches: it publishes the attachment if the pane is there
running the right session, and freezes otherwise.  Recovering a frozen
session takes an explicit no-survivor proof through
:func:`native_attachment.release`, which is a deliberate, evidence-bearing
act rather than a retry.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from datetime import datetime, timezone
from typing import Any, Mapping, NoReturn, Optional, Protocol, Sequence

from cli_agent_orchestrator.services import claude_native_launch
from cli_agent_orchestrator.services import execution_mode as em
from cli_agent_orchestrator.services import kimi_native_launch, native_attachment

LAUNCH_SCHEMA = "cao-native-tui-launch-v1"
OBSERVATION_SCHEMA = "cao-native-tui-pane-observation-v1"

#: A fresh launch: this call declared the attachment, started the pane,
#: and published the proven process identity.
OUTCOME_LAUNCHED = "launched"
#: The bound generation already owned an ``attached`` session.  Returned
#: without touching the pane — relaunching would be the second TUI.
OUTCOME_ALREADY_ATTACHED = "already_attached"
#: Re-entry over a ``starting`` row converged by observation alone.
OUTCOME_RECONCILED = "reconciled"

OUTCOMES = frozenset({OUTCOME_LAUNCHED, OUTCOME_ALREADY_ATTACHED, OUTCOME_RECONCILED})

#: Freeze reasons.  Each names the exact boundary that was crossed with
#: an unknown result, because "ambiguous" alone tells a later reconciler
#: nothing about where to look.
AMBIGUOUS_PANE_CREATE = "pane_create_outcome_unknown"
AMBIGUOUS_PANE_UNREADABLE = "pane_observation_unreadable"
AMBIGUOUS_PANE_ABSENT_AFTER_CREATE = "pane_absent_after_create"
AMBIGUOUS_START_CROSSED_NO_PANE = "start_crossed_with_no_observable_pane"
AMBIGUOUS_ARGV_MISMATCH = "pane_argv_does_not_resume_bound_session"
AMBIGUOUS_PANE_WORKDIR_MISMATCH = "pane_cwd_is_not_the_bound_working_directory"
AMBIGUOUS_PUBLISH_FAILED = "attachment_publication_failed"


class NativeLaunchError(RuntimeError):
    """Base class for every native-TUI launch failure."""

    code = "native-tui-launch-error"


class NativeLaunchInvalid(NativeLaunchError):
    """A caller supplied something unusable.  Nothing was claimed or started."""

    code = "native-tui-launch-invalid"


class NativeLaunchConflict(NativeLaunchError):
    """The session or the generation is not in a state that permits a launch."""

    code = "native-tui-launch-conflict"


class NativeLaunchAmbiguous(NativeLaunchError):
    """A side effect's outcome is unknown; the attachment is frozen.

    Carries the freeze ``reason`` so a caller can report which boundary
    was crossed without re-reading the attachment row.
    """

    code = "native-tui-launch-ambiguous"

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


class NativeLaunchUnavailable(NativeLaunchError):
    """A dependency this module needs could not be reached."""

    code = "native-tui-launch-unavailable"


class NativePaneTransport(Protocol):
    """The pane this module starts the TUI in.

    Deliberately two methods and no more.  A transport that could also
    *send* to the pane would let a launch path grow an input side, and
    input to a native session belongs to the control adapter, whose
    queue/steer distinction and receipt discipline this module has none
    of.
    """

    def create_pane(self, *, argv: Sequence[str]) -> str:
        """Start ``argv`` as the pane's own primary process; return its handle.

        Must raise on any failure.  A transport that swallowed a failure
        and returned a handle would send this module on to publish an
        attachment for a process that does not exist.
        """
        ...

    def observe(self) -> Optional[Mapping[str, Any]]:
        """Observe the live pane, or ``None`` when it provably does not exist.

        ``None`` is a *present, empty observation*; raise instead when
        the observation could not be made at all.  The distinction is the
        difference between "nothing is there" and "we did not look", and
        collapsing the two is how a launcher talks itself into a retry.

        A returned mapping must carry ``pane_id``, an integer ``pid``, a
        ``start_marker``, the primary process's observed ``argv``, and
        its observed ``cwd``.  A transport that cannot report the cwd
        must raise: an observation missing it is unreadable, not exempt.
        """
        ...


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise NativeLaunchInvalid(f"{field} must be a non-empty string; got {value!r}")
    return value


def _validate_binary(binary: str, binary_sha256: str) -> str:
    """Accept only the exact, canonical, digest-matched provider binary.

    Ambient ``PATH`` resolution is refused for the same reason the ACP
    branch refuses it: which provider actually ran would then depend on
    the environment the pane inherited rather than on anything recorded,
    and the acceptance evidence downstream binds a specific binary.
    """
    _require_text(binary, field="binary")
    digest = _require_text(binary_sha256, field="binary_sha256").lower()
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise NativeLaunchInvalid("binary_sha256 must be a 64-character hex sha256 digest")
    if not os.path.isabs(binary) or os.path.realpath(binary) != binary:
        raise NativeLaunchInvalid(
            f"provider binary must be a canonical absolute path; got {binary!r} — "
            "an ambient PATH lookup would leave which provider ran undetermined"
        )
    if not os.path.isfile(binary) or not os.access(binary, os.X_OK):
        raise NativeLaunchInvalid(f"provider binary is not an executable file: {binary}")
    try:
        with open(binary, "rb") as handle:
            observed = hashlib.sha256(handle.read()).hexdigest()
    except OSError as exc:
        raise NativeLaunchUnavailable(f"provider binary is unreadable: {exc}") from exc
    if observed != digest:
        raise NativeLaunchInvalid(
            "provider binary digest does not match the pinned digest; refusing to launch "
            "a provider whose bytes are not the ones that were admitted"
        )
    return binary


def _validate_working_directory(working_directory: str) -> str:
    """Accept only the canonical directory the bound session was minted in.

    Checked here, immediately before the launch, even though the caller
    checked it when the reservation was taken and the bootstrap checked
    it again when the session was minted.  The three checks bracket the
    two windows in which the recorded path and the resumed path could
    drift: between reserving and minting, and between minting and
    starting the pane.  A drift caught in either window costs a typed
    refusal; the same drift caught by the provider costs a pane that
    exits about a second after a launch that reported success.

    Refused, never rewritten.  This value is the one the reservation
    echoes and the one the session was filed under; substituting a
    different string here would make the pane disagree with both.
    """
    path = _require_text(working_directory, field="working_directory")
    if not os.path.isabs(path) or os.path.realpath(path) != path:
        raise NativeLaunchInvalid(
            f"working_directory must be a canonical absolute path; got {path!r} "
            f"(realpath {os.path.realpath(path)!r}) — the bound session is filed under the "
            "path string it was minted with, and the TUI resuming it reports only the "
            "realpath, so a non-canonical launch cannot find its own session"
        )
    if not os.path.isdir(path):
        raise NativeLaunchInvalid(f"working_directory is not an existing directory: {path}")
    return path


def _freeze(
    *,
    provider: str,
    native_session_id: str,
    reason: str,
    detail: str,
) -> NoReturn:
    """Freeze the attachment and raise, in that order.

    The freeze is committed first so a caller that dies handling the
    exception still leaves the session blocked rather than free.
    """
    try:
        native_attachment.mark_ambiguous(
            provider=provider, native_session_id=native_session_id, reason=reason
        )
    except native_attachment.NativeAttachmentError as exc:
        raise NativeLaunchUnavailable(
            f"could not freeze {provider} session {native_session_id} after {reason}: {exc}; "
            f"the original condition was: {detail}"
        ) from exc
    raise NativeLaunchAmbiguous(reason, detail)


def _validated_observation(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Accept a pane observation only when every field it must carry is real."""
    if not isinstance(raw, Mapping):
        raise NativeLaunchInvalid("pane observation must be a mapping")
    pane_id = raw.get("pane_id")
    pid = raw.get("pid")
    start_marker = raw.get("start_marker")
    argv = raw.get("argv")
    cwd = raw.get("cwd")
    if not isinstance(pane_id, str) or not pane_id:
        raise NativeLaunchInvalid("pane observation requires a non-empty pane_id")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise NativeLaunchInvalid("pane observation requires a positive integer pid")
    if not isinstance(start_marker, str) or not start_marker:
        raise NativeLaunchInvalid(
            "pane observation requires a start_marker; a bare pid is not identity because "
            "pids are recycled"
        )
    if not isinstance(argv, (list, tuple)) or not all(isinstance(item, str) for item in argv):
        raise NativeLaunchInvalid(
            "pane observation requires the observed argv as a list of strings"
        )
    if not isinstance(cwd, str) or not cwd:
        raise NativeLaunchInvalid(
            "pane observation requires the primary process's observed cwd; without it the "
            "session's recorded directory cannot be checked against the one the process is "
            "actually in, which is the check that catches a resume filed under another path"
        )
    return {
        "schema": OBSERVATION_SCHEMA,
        "pane_id": pane_id,
        "pid": pid,
        "start_marker": start_marker,
        "argv": list(argv),
        "cwd": cwd,
    }


def _observe(
    transport: NativePaneTransport,
    *,
    provider: str,
    native_session_id: str,
    absent_reason: str,
) -> dict[str, Any]:
    """Observe the pane, freezing on either kind of failure to observe.

    Absence and unreadability freeze with *different* reasons even though
    both freeze, because the two send a later reconciler to different
    evidence: one to the process table, the other to the transport.
    """
    try:
        raw = transport.observe()
    except Exception as exc:  # noqa: BLE001 - an unreadable pane is never "no pane"
        _freeze(
            provider=provider,
            native_session_id=native_session_id,
            reason=AMBIGUOUS_PANE_UNREADABLE,
            detail=f"the pane could not be observed at all: {exc}",
        )
    if raw is None:
        _freeze(
            provider=provider,
            native_session_id=native_session_id,
            reason=absent_reason,
            detail="the pane is provably absent, which cannot distinguish a process that "
            "never started from one that started and exited",
        )
    try:
        return _validated_observation(raw)
    except NativeLaunchInvalid as exc:
        _freeze(
            provider=provider,
            native_session_id=native_session_id,
            reason=AMBIGUOUS_PANE_UNREADABLE,
            detail=f"the pane observation was incomplete: {exc}",
        )


def _publish(
    *,
    provider: str,
    native_session_id: str,
    terminal_id: str,
    generation: str,
    working_directory: str,
    observation: Mapping[str, Any],
) -> dict[str, Any]:
    """Verify the pane runs the bound session, then publish the attachment.

    Two independent proofs, both taken before the attachment is
    published, because publication is what makes the generation
    bindable.  The argv proves *which session* the process resumed; the
    cwd proves *which directory* it resumed it in.  Neither implies the
    other: a correct argv started in the wrong directory names a session
    the provider will refuse to open, and it fails after the launch has
    already reported success.
    """
    if not _binder(provider)["binds_exactly"](observation["argv"], native_session_id):
        _freeze(
            provider=provider,
            native_session_id=native_session_id,
            reason=AMBIGUOUS_ARGV_MISMATCH,
            detail=(
                f"the pane's primary process does not bind exactly {native_session_id!r}; "
                "on both supported providers a resume that lost its id opens an "
                "interactive picker rather than failing, so the running session may be "
                "a different one"
            ),
        )
    observed_cwd = os.path.realpath(observation["cwd"])
    if observed_cwd != working_directory:
        # Frozen rather than refused, because a process is running: it
        # holds the bound session in a directory the session was not
        # filed under, so it will fail to open it and exit shortly.
        # Freezing before publication means the generation never becomes
        # bindable, no task byte is ever typed at it, and the session
        # stays permanently blocked instead of appearing free to the next
        # claimant while a doomed process is still winding down.
        _freeze(
            provider=provider,
            native_session_id=native_session_id,
            reason=AMBIGUOUS_PANE_WORKDIR_MISMATCH,
            detail=(
                f"the pane's primary process is in {observed_cwd!r}, but session "
                f"{native_session_id!r} is bound to {working_directory!r}; the provider "
                "resolves a resume against the directory the session was minted in, so this "
                "pane cannot open the session it was started for"
            ),
        )
    try:
        return native_attachment.mark_attached(
            provider=provider,
            native_session_id=native_session_id,
            terminal_id=terminal_id,
            generation=generation,
            execution_mode=em.NATIVE_TUI,
            process_identity=native_attachment.process_identity(
                pid=observation["pid"], start_marker=observation["start_marker"]
            ),
            pane_id=observation["pane_id"],
        )
    except native_attachment.NativeAttachmentError as exc:
        # The process is running and holding the session, but its identity
        # is not on record.  Without a published identity no later
        # no-survivor proof can name it, so this is frozen rather than
        # left as a live attachment nobody can ever release.
        _freeze(
            provider=provider,
            native_session_id=native_session_id,
            reason=AMBIGUOUS_PUBLISH_FAILED,
            detail=f"the pane is live but its identity could not be published: {exc}",
        )


def _result(
    *,
    outcome: str,
    provider: str,
    native_session_id: str,
    terminal_id: str,
    generation: str,
    binary: str,
    binary_sha256: str,
    argv: Sequence[str],
    pane_handle: Optional[str],
    observation: Optional[Mapping[str, Any]],
    attachment: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema": LAUNCH_SCHEMA,
        "outcome": outcome,
        "provider": provider,
        "native_session_id": native_session_id,
        "terminal_id": terminal_id,
        "generation": generation,
        "execution_mode": em.NATIVE_TUI,
        "binary": binary,
        "binary_sha256": binary_sha256,
        "argv": list(argv),
        # The digest of the exact argv this module launched.  A readiness
        # receipt quotes it so what was admitted can be compared against
        # what ran, without the receipt having to carry the argv itself.
        "launch_argv_sha256": hashlib.sha256(
            "\x00".join(argv).encode("utf-8", "surrogatepass")
        ).hexdigest(),
        "pane_handle": pane_handle,
        "pane_observation": dict(observation) if observation is not None else None,
        "attachment": dict(attachment),
        "completed_at": _now(),
    }


#: A launch that starts a session whose id was chosen before it, versus
#: one that reattaches to a session that already exists. Named rather
#: than inferred: "is this the first launch?" is a fact the caller holds
#: and this module cannot recover, and guessing it would mean sometimes
#: resuming a session that was never started.
LAUNCH_KIND_NEW = "new"
LAUNCH_KIND_RESUME = "resume"
LAUNCH_KINDS = (LAUNCH_KIND_NEW, LAUNCH_KIND_RESUME)


def _kimi_argv(
    *, session_id: str, binary: str, extra_args: Optional[Sequence[str]], launch_kind: str
) -> list[str]:
    if launch_kind != LAUNCH_KIND_RESUME:
        raise NativeLaunchInvalid(
            "kimi native sessions are minted by the ACP bootstrap before the TUI starts, "
            f"so the only lawful launch form is a resume; got launch_kind {launch_kind!r}"
        )
    try:
        return kimi_native_launch.build_resume_argv(
            session_id=session_id, kimi_binary=binary, extra_args=extra_args
        )
    except kimi_native_launch.KimiNativeLaunchError as exc:
        raise NativeLaunchInvalid(str(exc)) from exc


def _claude_argv(
    *, session_id: str, binary: str, extra_args: Optional[Sequence[str]], launch_kind: str
) -> list[str]:
    builder = (
        claude_native_launch.build_launch_argv
        if launch_kind == LAUNCH_KIND_NEW
        else claude_native_launch.build_resume_argv
    )
    try:
        return builder(session_id=session_id, claude_binary=binary, extra_args=extra_args)
    except claude_native_launch.ClaudeNativeLaunchError as exc:
        raise NativeLaunchInvalid(str(exc)) from exc


#: Per-provider argv construction and the matching "does this argv bind
#: exactly that session?" check. The two halves are registered together
#: on purpose: a builder paired with the wrong checker would construct a
#: correct argv and then verify it against a different provider's rules,
#: which passes and means nothing.
_ARGV_BINDERS: dict[str, dict[str, Any]] = {
    "kimi_cli": {"build": _kimi_argv, "binds_exactly": kimi_native_launch.resumes_exactly},
    "claude_code": {"build": _claude_argv, "binds_exactly": claude_native_launch.binds_exactly},
}

SUPPORTED_NATIVE_PROVIDERS = frozenset(_ARGV_BINDERS)


def _binder(provider: str) -> dict[str, Any]:
    binder = _ARGV_BINDERS.get(provider)
    if binder is None:
        raise NativeLaunchInvalid(
            f"no native-TUI argv binding is implemented for provider {provider!r}; "
            f"implemented: {sorted(SUPPORTED_NATIVE_PROVIDERS)}"
        )
    return binder


def start(
    *,
    provider: str,
    native_session_id: str,
    terminal_id: str,
    generation: str,
    execution_mode: str,
    intent: Mapping[str, Any],
    binary: str,
    binary_sha256: str,
    working_directory: str,
    transport: NativePaneTransport,
    extra_args: Optional[Sequence[str]] = None,
    launch_kind: str = LAUNCH_KIND_RESUME,
) -> dict[str, Any]:
    """Claim, launch, prove, and publish one native TUI attachment.

    ``execution_mode`` is taken rather than assumed so that an ACP caller
    arriving here is refused instead of silently running the native
    branch.  The two modes are separate launch branches; a caller that
    reaches the wrong one has a bug that must surface as a rejection, not
    as a working launch in the mode it did not ask for.

    ``working_directory`` is required rather than inferred from the
    transport for the same reason: it is the directory the bound session
    was minted in, and it is checked twice here — once before anything is
    claimed, and once against the running process before the attachment
    is published.
    """
    provider = _require_text(provider, field="provider")
    native_session_id = _require_text(native_session_id, field="native_session_id")
    terminal_id = _require_text(terminal_id, field="terminal_id")
    generation = _require_text(generation, field="generation")

    try:
        mode = em.validate_mode(execution_mode)
    except em.ExecutionModeError as exc:
        raise NativeLaunchInvalid(str(exc)) from exc
    if mode != em.NATIVE_TUI:
        raise NativeLaunchInvalid(
            f"the native TUI launch branch refuses execution_mode {mode!r}; the two modes "
            "are separate launch branches and never fall back to one another"
        )

    binary = _validate_binary(binary, binary_sha256)
    # Before ``declare``, so a non-canonical directory costs a refusal
    # with nothing claimed and no pane started, rather than a frozen
    # attachment.
    working_directory = _validate_working_directory(working_directory)

    if launch_kind not in LAUNCH_KINDS:
        raise NativeLaunchInvalid(
            f"launch_kind must be one of {list(LAUNCH_KINDS)}; got {launch_kind!r}"
        )
    binder = _binder(provider)
    argv = binder["build"](
        session_id=native_session_id,
        binary=binary,
        extra_args=extra_args,
        launch_kind=launch_kind,
    )
    if not binder["binds_exactly"](argv, native_session_id):
        # Unreachable through the builders, and checked anyway: this is
        # the last point before a claim at which a wrong argv costs
        # nothing, and the first point after it at which it costs a
        # frozen session.
        raise NativeLaunchInvalid("the constructed argv does not bind exactly the bound session")

    try:
        record, _acquired = native_attachment.declare(
            provider=provider,
            native_session_id=native_session_id,
            terminal_id=terminal_id,
            generation=generation,
            execution_mode=em.NATIVE_TUI,
            intent=intent,
        )
    except native_attachment.NativeAttachmentInvalid as exc:
        raise NativeLaunchInvalid(str(exc)) from exc
    except native_attachment.NativeAttachmentConflict as exc:
        raise NativeLaunchConflict(str(exc)) from exc
    except native_attachment.NativeAttachmentError as exc:
        raise NativeLaunchUnavailable(str(exc)) from exc

    common: dict[str, Any] = {
        "provider": provider,
        "native_session_id": native_session_id,
        "terminal_id": terminal_id,
        "generation": generation,
        "binary": binary,
        "binary_sha256": binary_sha256,
        "argv": argv,
    }

    if record["state"] == native_attachment.ATTACHED:
        return _result(
            outcome=OUTCOME_ALREADY_ATTACHED,
            pane_handle=record["owner"]["pane_id"],
            observation=None,
            attachment=record,
            **common,
        )

    if record["state"] == native_attachment.DRAINING:
        raise NativeLaunchConflict(
            f"{provider} session {native_session_id} is draining for this generation; "
            "a draining owner is winding the session down and must not be relaunched into"
        )

    if record["state"] == native_attachment.STARTING:
        # Re-entry after a crash somewhere around process start.  Observe
        # only: whether a process exists is exactly what is unknown, and
        # launching a second one is the failure this branch prevents.
        observation = _observe(
            transport,
            provider=provider,
            native_session_id=native_session_id,
            absent_reason=AMBIGUOUS_START_CROSSED_NO_PANE,
        )
        attachment = _publish(
            provider=provider,
            native_session_id=native_session_id,
            terminal_id=terminal_id,
            generation=generation,
            working_directory=working_directory,
            observation=observation,
        )
        return _result(
            outcome=OUTCOME_RECONCILED,
            pane_handle=observation["pane_id"],
            observation=observation,
            attachment=attachment,
            **common,
        )

    try:
        native_attachment.mark_starting(
            provider=provider,
            native_session_id=native_session_id,
            terminal_id=terminal_id,
            generation=generation,
            execution_mode=em.NATIVE_TUI,
        )
    except native_attachment.NativeAttachmentError as exc:
        # Nothing has been started, so this is a clean refusal rather than
        # an ambiguity: the row stays ``declared`` and a later attempt by
        # the same owner resumes from here.
        raise NativeLaunchConflict(
            f"could not record the start of {provider} session {native_session_id}: {exc}"
        ) from exc

    try:
        handle = transport.create_pane(argv=argv)
    except Exception as exc:  # noqa: BLE001 - a failed create may still have created
        _freeze(
            provider=provider,
            native_session_id=native_session_id,
            reason=AMBIGUOUS_PANE_CREATE,
            detail=f"pane creation raised, so whether a provider process exists is unknown: {exc}",
        )
    if not isinstance(handle, str) or not handle:
        _freeze(
            provider=provider,
            native_session_id=native_session_id,
            reason=AMBIGUOUS_PANE_CREATE,
            detail=f"pane creation returned no usable handle ({handle!r}); a process may be running",
        )

    observation = _observe(
        transport,
        provider=provider,
        native_session_id=native_session_id,
        absent_reason=AMBIGUOUS_PANE_ABSENT_AFTER_CREATE,
    )
    attachment = _publish(
        provider=provider,
        native_session_id=native_session_id,
        terminal_id=terminal_id,
        generation=generation,
        working_directory=working_directory,
        observation=observation,
    )
    return _result(
        outcome=OUTCOME_LAUNCHED,
        pane_handle=handle,
        observation=observation,
        attachment=attachment,
        **common,
    )


class TmuxNativePane:
    """A real tmux window whose primary process is the provider TUI.

    Bound to one window at construction so :meth:`observe` needs no
    handle: re-entry after a crash observes the same window a previous
    attempt would have created, which is what makes the ``starting``
    reconcile possible at all.

    The window is created through ``create_window_with_argv``, which
    execs the argv directly — no shell is started and nothing is typed
    into one.  That is what makes the TUI the pane's own process rather
    than a command line some shell happens to be running, and it is why
    the observed primary-process argv is a meaningful check.
    """

    def __init__(
        self,
        backend: Any,
        *,
        session_name: str,
        window_name: str,
        terminal_id: str,
        working_directory: Optional[str] = None,
        extra_env: Optional[Mapping[str, str]] = None,
    ) -> None:
        self._backend = backend
        self._session_name = _require_text(session_name, field="session_name")
        self._window_name = _require_text(window_name, field="window_name")
        self._terminal_id = _require_text(terminal_id, field="terminal_id")
        self._working_directory = working_directory
        self._extra_env = dict(extra_env) if extra_env else {}

    def create_pane(self, *, argv: Sequence[str]) -> str:
        handle = self._backend.create_window_with_argv(
            self._session_name,
            self._window_name,
            self._terminal_id,
            list(argv),
            self._working_directory,
            dict(self._extra_env),
        )
        return str(handle)

    def observe(self) -> Optional[Mapping[str, Any]]:
        identity = self._backend.window_identity(self._session_name, self._window_name)
        if identity is None:
            # No identity and no window is a real absence.  No identity
            # while the window exists is a failed read, and saying
            # "absent" for that would license a relaunch on top of a live
            # process, so it raises instead.
            if not self._backend.window_exists(self._session_name, self._window_name):
                return None
            raise NativeLaunchUnavailable(
                f"tmux window {self._session_name}:{self._window_name} exists but its "
                "pane identity could not be read"
            )
        pid = self._pane_pid()
        if pid is None:
            raise NativeLaunchUnavailable(
                f"the primary process of {self._session_name}:{self._window_name} has no "
                "readable pid"
            )
        start_marker = _process_field(pid, "lstart=")
        command = _process_field(pid, "args=")
        if start_marker is None or command is None:
            raise NativeLaunchUnavailable(
                f"the process table did not report the identity of pid {pid}"
            )
        cwd = _process_cwd(pid)
        if cwd is None:
            # Raised, not omitted.  A caller that could not read the cwd
            # has not proven the pane is in the right directory, and the
            # only safe reading of an unproven check is that the
            # observation failed.
            raise NativeLaunchUnavailable(
                f"the working directory of pid {pid} could not be read, so the pane cannot "
                "be shown to be running in the directory its session was minted in"
            )
        return {
            "pane_id": str(identity["pane_id"]),
            "pid": pid,
            "start_marker": start_marker,
            # A whitespace split of the observed command line.  It is
            # exact for the argv this module launches — the session id is
            # validated to carry no whitespace, so the resume option and
            # its argument stay adjacent tokens however the binary path
            # splits — and any split that does not yield exactly one
            # resume option fails the check rather than passing it.
            "argv": command.split(),
            "cwd": cwd,
        }

    def _pane_pid(self) -> Optional[int]:
        from cli_agent_orchestrator.clients.tmux import tmux_binary

        try:
            proc = subprocess.run(
                [
                    tmux_binary(),
                    "display-message",
                    "-p",
                    "-t",
                    f"{self._session_name}:{self._window_name}",
                    "-F",
                    "#{pane_pid}",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0:
            return None
        raw = proc.stdout.strip()
        return int(raw) if raw.isdigit() and int(raw) > 0 else None


def _process_field(pid: int, field: str) -> Optional[str]:
    """One ``ps`` field for one pid, or ``None`` when it cannot be read.

    Queried one field per call.  ``lstart`` contains spaces, so asking
    for it alongside ``args`` produces output no parser can split back
    apart without guessing where the date ends — and a guess here would
    corrupt either the start marker or the command line, both of which
    are load-bearing evidence.
    """
    ps = shutil.which("ps")
    if not ps:
        return None
    try:
        proc = subprocess.run(
            [os.path.realpath(ps), "-o", field, "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return value or None


def _process_cwd(pid: int) -> Optional[str]:
    """The live working directory of one pid, or ``None`` if unreadable.

    Read from the kernel rather than from anything the launcher recorded,
    because the point of the check it feeds is to catch a pane that is
    *not* where the record says it is.  Asking the same record twice
    would prove nothing.

    ``ps`` cannot report a working directory on either platform, so this
    goes to ``/proc`` where that exists and to ``lsof`` otherwise.  Both
    report the resolved path, which is what makes the comparison
    meaningful: a process started in a symlinked directory reports the
    real one, exactly as the provider's own runtime does.
    """
    proc_link = f"/proc/{pid}/cwd"
    if os.path.isdir(f"/proc/{pid}"):
        try:
            return os.readlink(proc_link) or None
        except OSError:
            return None
    lsof = shutil.which("lsof")
    if not lsof:
        return None
    try:
        proc = subprocess.run(
            [os.path.realpath(lsof), "-a", "-d", "cwd", "-p", str(pid), "-Fn"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    # Field-per-line output: ``p<pid>``, ``fcwd``, ``n<path>``.  Only the
    # ``n`` line carries the path, and a dead pid yields no lines at all.
    for line in proc.stdout.splitlines():
        if line.startswith("n") and len(line) > 1:
            return line[1:]
    return None
