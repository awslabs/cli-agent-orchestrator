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
import struct
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any, Mapping, NoReturn, Optional, Protocol, Sequence

from cli_agent_orchestrator.services import claude_native_launch, codex_native_launch
from cli_agent_orchestrator.services import execution_mode as em
from cli_agent_orchestrator.services import kimi_native_launch, muse_native_launch
from cli_agent_orchestrator.services import native_attachment

LAUNCH_SCHEMA = "cao-native-tui-launch-v1"
OBSERVATION_SCHEMA = "cao-native-tui-pane-observation-v1"
INNER_EXEC_CONVERGENCE_TIMEOUT_SECONDS = 2.0
INNER_EXEC_CONVERGENCE_POLL_SECONDS = 0.05
#: The cold-start runway one native provider boot is allowed, defined once
#: for every launch-phase wait that observes that boot.  The v2 seam lets a
#: launched pane take this long to become input-ready
#: (``managed_launch_v2.NATIVE_PANE_READY_TIMEOUT_SECONDS`` is this same
#: constant): provider startup includes profile MCP handshakes, and a
#: healthy cold start on a loaded host does not fit in a short boot window.
#: One constant rather than per-layer literals, because the layers observe
#: the *same* boot at different phases -- two independent values already
#: drifted into contradiction once (COND-0314), and no layer may quietly
#: widen or narrow a boot another layer's evidence depends on.
NATIVE_COLD_START_RUNWAY_SECONDS = 60.0
#: How long to wait for a title-rewriting Kimi build to render the native
#: header that proves the resumed session, and how often to re-read it.  The
#: header is the first thing the boot paints, so the window must cover the
#: whole cold-start runway the launch tolerates: the installed 15 s backstop
#: froze a legitimate cold/loaded Kimi 0.31.0 boot whose exact header painted
#: after it (COND-0314 -- the same stable pane subsequently rendered the
#: exact bound session, version, and worktree, too late to be admitted; the
#: launch had already failed closed before task delivery).  One adequate
#: bounded observation window, never a retry: a pane that never renders the
#: header within the runway freezes rather than publishing an unproven
#: attachment, and the freeze detail names the exact deadline it observed
#: under.  The window is enforced against the monotonic deadline itself --
#: a final poll that resumes late under scheduler or host load freezes
#: rather than capturing past the bound; the overshoot is real time, not
#: an epsilon the contract absorbs.
KIMI_RENDER_CONVERGENCE_TIMEOUT_SECONDS = NATIVE_COLD_START_RUNWAY_SECONDS
KIMI_RENDER_CONVERGENCE_POLL_SECONDS = 0.1
ENV_EXECUTABLE = os.path.realpath("/usr/bin/env")
MAX_SHEBANG_LINE_BYTES = 512
MAX_PROCESS_ARGV_BYTES = 1024 * 1024

#: A fresh launch: this call declared the attachment, started the pane,
#: and published the proven process identity.
OUTCOME_LAUNCHED = "launched"
#: The bound generation already owned an ``attached`` session.  Returned
#: without touching the pane — relaunching would be the second TUI.
OUTCOME_ALREADY_ATTACHED = "already_attached"
#: Re-entry over a ``starting`` row converged by observation alone.
OUTCOME_RECONCILED = "reconciled"

OUTCOMES = frozenset({OUTCOME_LAUNCHED, OUTCOME_ALREADY_ATTACHED, OUTCOME_RECONCILED})

#: The bound-session proof channel a launch published under.  A closed
#: vocabulary so success evidence is typed like the freeze evidence: a reader
#: of the launch result (or a later auditor) learns *how* the resumed session
#: was proven rather than inferring it from the argv.  ``SESSION_PROOF_ARGV``
#: is the kernel-argv proof every provider and build uses when the resumed id
#: is still readable there; ``SESSION_PROOF_KIMI_RENDERED`` is the rendered
#: native-header proof (rule ``kimi-native-header-v1``) used only by a Kimi
#: build proven to rewrite its process title after parsing.  Any other value
#: reaching publication fails closed rather than silently behaving like argv.
SESSION_PROOF_ARGV = "argv"
SESSION_PROOF_KIMI_RENDERED = kimi_native_launch.RULE_KIMI_NATIVE_HEADER
SESSION_PROOFS = frozenset({SESSION_PROOF_ARGV, SESSION_PROOF_KIMI_RENDERED})

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
AMBIGUOUS_PROCESS_IMAGE_MISMATCH = "pane_process_image_does_not_match_inner_binary"
#: The pane's rendered native header does not prove the bound session.  A
#: distinct reason from :data:`AMBIGUOUS_ARGV_MISMATCH` because the evidence
#: channel is different -- the argv was erased by a post-parse title rewrite,
#: so the binding was being proven from the rendered header instead, and a
#: reconciler needs to know it is looking at the screen, not the process table.
AMBIGUOUS_PANE_RENDER_MISMATCH = "pane_render_does_not_show_bound_session"


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

    def capture_render(self, pane_id: str) -> list[str]:
        """The rendered rows of one exact pane right now; raise on any failure.

        A distinct, read-only evidence channel from :meth:`observe`, targeted
        at the immutable ``pane_id`` of the observation that fences the proof
        rather than at a session/window (which resolves to the *active* pane
        and could flip between the observation and the capture).  The process
        table proves *which process* the pane is; the rendered screen proves
        *which session that process's TUI is running* for a provider that
        rewrites its process title after parsing and so leaves the resumed
        session id unreadable from the argv.  Deliberately still only a read:
        nothing is sent to the pane, so the discipline against growing an input
        side is preserved.  A capture that could not be made must raise -- an
        unreadable render is an unresolved observation, not an empty one.
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


def _verify_argv_resumes_session(
    *,
    provider: str,
    native_session_id: str,
    observation: Mapping[str, Any],
) -> None:
    """Freeze unless the observed argv still resumes exactly the bound session.

    The argv half of the bound-session check, on its own so the rendered-header
    half (:func:`_verify_rendered_session`) can replace it for a provider that
    rewrites its title without also re-checking the cwd it did not change.
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


def _verify_pane_cwd(
    *,
    provider: str,
    native_session_id: str,
    working_directory: str,
    observation: Mapping[str, Any],
) -> None:
    """Freeze unless the observed process is still in the bound directory."""
    observed_cwd = os.path.realpath(observation["cwd"])
    if observed_cwd != working_directory:
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


def _verify_bound_session_and_cwd(
    *,
    provider: str,
    native_session_id: str,
    working_directory: str,
    observation: Mapping[str, Any],
) -> None:
    """Freeze unless an observation still describes the claimed session."""
    _verify_argv_resumes_session(
        provider=provider,
        native_session_id=native_session_id,
        observation=observation,
    )
    _verify_pane_cwd(
        provider=provider,
        native_session_id=native_session_id,
        working_directory=working_directory,
        observation=observation,
    )


def _verify_rendered_session(
    *,
    provider: str,
    native_session_id: str,
    provider_version: Optional[str],
    rendered_rows: Sequence[str],
) -> None:
    """Freeze unless the rendered native header proves exactly the bound session.

    The rendered-header analogue of :func:`_verify_argv_resumes_session`: used
    for a Kimi build proven to rewrite its process title after parsing, where
    the resumed session id was erased from the kernel argv and the TUI's own
    header is the proof that survives the rewrite.  Re-checked here, at
    publication, from the rows the launch settled on, so the attachment rests
    on evidence a later reader can re-walk.
    """
    if not kimi_native_launch.renders_session_exactly(
        rendered_rows, native_session_id, provider_version=provider_version
    ):
        _freeze(
            provider=provider,
            native_session_id=native_session_id,
            reason=AMBIGUOUS_PANE_RENDER_MISMATCH,
            detail=(
                f"the pane's rendered native header does not prove session "
                f"{native_session_id!r}; the build rewrites its process title after parsing, "
                "so the resumed session is proven from the rendered header rather than the "
                "argv, and a header that is missing, ambiguous, or names a different session "
                "is not the one claimed"
            ),
        )


def _env_shebang_interpreter(wrapper_executable: str) -> Optional[str]:
    """Return the sole interpreter token from a pinned env shebang.

    The wrapper path has already been digest-verified by the launch path.  Read
    only a bounded first line and accept exactly ``#!/usr/bin/env <token>``.
    Multi-token forms such as ``env -S`` intentionally receive no transient
    allowance.
    """
    try:
        with open(wrapper_executable, "rb") as wrapper:
            first_line = wrapper.readline(MAX_SHEBANG_LINE_BYTES + 1)
    except OSError:
        return None
    if not first_line or len(first_line) > MAX_SHEBANG_LINE_BYTES:
        return None
    try:
        shebang = first_line.rstrip(b"\r\n").decode("ascii")
    except UnicodeDecodeError:
        return None
    if not shebang.startswith("#!"):
        return None
    payload = shebang[2:]
    if any(character.isspace() and character not in " \t" for character in payload):
        return None
    tokens = payload.strip(" \t").split()
    if len(tokens) != 2 or os.path.realpath(tokens[0]) != ENV_EXECUTABLE:
        return None
    if tokens[1].startswith("-"):
        return None
    return tokens[1]


def _await_inner_exec(
    transport: NativePaneTransport,
    *,
    provider: str,
    native_session_id: str,
    working_directory: str,
    observation: dict[str, Any],
    wrapper_executable: str,
    launch_argv: Sequence[str],
    expected_inner_executable: Optional[str],
    absent_reason: str,
) -> dict[str, Any]:
    """Observe a routed wrapper until its same process execs the admitted inner.

    The route wrapper is the pane's first process and consumes the one-shot
    credential before it execs the provider binary.  tmux can return from
    window creation during that short interval.  Publication must wait for the
    exec, but the wait is observation-only: the pane is never written to, and a
    changed process identity, wrong session, wrong cwd, disappearance, or
    unreadable observation still freezes the attachment.
    """
    if expected_inner_executable is None:
        return observation

    identity = (
        observation["pane_id"],
        observation["pid"],
        observation["start_marker"],
    )
    env_shebang_interpreter = _env_shebang_interpreter(wrapper_executable)
    deadline = time.monotonic() + INNER_EXEC_CONVERGENCE_TIMEOUT_SECONDS
    while True:
        _verify_bound_session_and_cwd(
            provider=provider,
            native_session_id=native_session_id,
            working_directory=working_directory,
            observation=observation,
        )
        observed_executable = observation["argv"][0] if observation["argv"] else ""
        if os.path.realpath(observed_executable) == expected_inner_executable:
            return observation

        # A shebang wrapper is briefly visible as ``interpreter wrapper
        # <original args>``.  That exact admitted wrapper phase is the only
        # image permitted to precede the inner executable; a merely
        # session-shaped foreign command must not earn a convergence window.
        wrapper_index = next(
            (
                index
                for index in range(min(2, len(observation["argv"])))
                if os.path.realpath(observation["argv"][index]) == wrapper_executable
            ),
            None,
        )
        wrapper_phase = wrapper_index is not None and observation["argv"][
            wrapper_index + 1 :
        ] == list(launch_argv[1:])
        argv = observation["argv"]
        env_wrapper_phase = (
            env_shebang_interpreter is not None
            and len(argv) >= 3
            and os.path.realpath(argv[0]) == ENV_EXECUTABLE
            and argv[1] == env_shebang_interpreter
            and os.path.realpath(argv[2]) == wrapper_executable
            and argv[3:] == list(launch_argv[1:])
        )
        if not wrapper_phase and not env_wrapper_phase:
            _freeze(
                provider=provider,
                native_session_id=native_session_id,
                reason=AMBIGUOUS_PROCESS_IMAGE_MISMATCH,
                detail=(
                    f"the pane process image {observed_executable!r} is neither the "
                    f"declared inner executable {expected_inner_executable!r} nor the "
                    "exact admitted wrapper/interpreter argv"
                ),
            )

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _freeze(
                provider=provider,
                native_session_id=native_session_id,
                reason=AMBIGUOUS_PROCESS_IMAGE_MISMATCH,
                detail=(
                    f"the pane process image did not converge from the admitted route "
                    f"wrapper to the declared inner executable "
                    f"{expected_inner_executable!r} within "
                    f"{INNER_EXEC_CONVERGENCE_TIMEOUT_SECONDS:g} seconds; last observed "
                    f"image was {observed_executable!r}"
                ),
            )
        time.sleep(min(INNER_EXEC_CONVERGENCE_POLL_SECONDS, remaining))
        next_observation = _observe(
            transport,
            provider=provider,
            native_session_id=native_session_id,
            absent_reason=absent_reason,
        )
        next_identity = (
            next_observation["pane_id"],
            next_observation["pid"],
            next_observation["start_marker"],
        )
        if next_identity != identity:
            _freeze(
                provider=provider,
                native_session_id=native_session_id,
                reason=AMBIGUOUS_PROCESS_IMAGE_MISMATCH,
                detail=(
                    "the pane process identity changed while waiting for the admitted "
                    f"inner executable: expected {identity!r}, observed {next_identity!r}"
                ),
            )
        observation = next_observation


def _kimi_rendered_proof_active(provider: str, provider_version: Optional[str]) -> bool:
    """Whether the bound session must be proven from the rendered header.

    True only for a Kimi build whose post-parse process-title rewrite and
    native header layout were both read (see
    :func:`kimi_native_launch.rendered_session_proof_for`).  For every other
    provider and build the resumed session id is still readable from the
    kernel argv, so the argv remains the proof and the rendered header is
    never consulted.
    """
    return provider == "kimi_cli" and (
        kimi_native_launch.rendered_session_proof_for(provider_version) is not None
    )


def _await_kimi_rendered_session_proof(
    transport: NativePaneTransport,
    *,
    provider: str,
    native_session_id: str,
    provider_version: Optional[str],
    observation: dict[str, Any],
    absent_reason: str,
) -> tuple[dict[str, Any], list[str]]:
    """Converge on the rendered native header that proves the bound session.

    Kimi 0.31.0 erases the resumed session id from the kernel argv, so the
    binding is read from the pane's own header once the TUI has painted it.
    The wait is observation-only -- the pane is never written to -- and every
    unresolved outcome freezes: an unreadable capture, a header that never
    renders within the bound, and a pane whose process identity changes while
    the header is awaited (which would prove the session off a stranger) all
    leave the attachment frozen rather than published.

    The monotonic deadline is the authority on observation, not merely on
    waiting.  A requested final sleep can resume *after* the deadline under
    scheduler or host load -- real overshoot, not an epsilon -- and a capture
    taken then would admit a header that painted out of bounds (the exact
    shape of the COND-0314 follow-up counterexample).  Every capture after
    the initial launch-time one is therefore taken only while
    ``now <= deadline``; the boundary itself is still observed, so a header
    visible exactly at the deadline is admitted, and a wake that resumes
    past it freezes without reading the screen again.  This phase and the
    v2 pane-ready watch are sequential and each may consume the shared
    cold-start runway, but each is genuinely bounded by its own deadline.

    A successful match is fenced by a fresh observation too.  The header rows
    come from a capture taken *after* the observation that seeded this
    identity, so a same-pane process replacement in that window (a TUI
    self-restart, an active-pane flip caught by the exact-pane capture) would
    otherwise let the launch publish the stale ``pid``/``start_marker`` of a
    dead process while the session proof came from pixels the replacement
    re-rendered.  The match is therefore only accepted once a re-observation
    confirms ``(pane_id, pid, start_marker)`` unchanged, and the fenced
    observation -- not the stale seed -- is what publication records.

    Returns the fenced observation and the rendered rows that proved the
    session, so publication can re-check the same evidence independently.
    """
    identity = (
        observation["pane_id"],
        observation["pid"],
        observation["start_marker"],
    )

    def _never_rendered_in_bound() -> NoReturn:
        """Freeze: the bound header was not observed inside the runway."""
        _freeze(
            provider=provider,
            native_session_id=native_session_id,
            reason=AMBIGUOUS_PANE_RENDER_MISMATCH,
            detail=(
                f"the pane did not render a native header proving session "
                f"{native_session_id!r} within "
                f"{KIMI_RENDER_CONVERGENCE_TIMEOUT_SECONDS:g} seconds"
            ),
        )

    deadline = time.monotonic() + KIMI_RENDER_CONVERGENCE_TIMEOUT_SECONDS
    initial_capture = True
    while True:
        # Enforce the deadline *before* reading the screen, on every capture
        # after the initial one: a sleep that resumed late must not be
        # followed by a capture whose header is then admitted.  The initial
        # capture is exempt -- a launch always looks at the screen once
        # immediately, even under a zero bound.
        if not initial_capture and time.monotonic() > deadline:
            _never_rendered_in_bound()
        initial_capture = False
        try:
            rows = list(transport.capture_render(observation["pane_id"]))
        except Exception as exc:  # noqa: BLE001 - an unreadable render is never "no header"
            _freeze(
                provider=provider,
                native_session_id=native_session_id,
                reason=AMBIGUOUS_PANE_UNREADABLE,
                detail=f"the pane's rendered screen could not be read: {exc}",
            )
        if kimi_native_launch.renders_session_exactly(
            rows, native_session_id, provider_version=provider_version
        ):
            # Fence the match.  The rows were read from a capture taken after
            # the observation that seeded ``identity``; a process replacement
            # in that window would make this header the replacement's own
            # statement while the recorded identity was the dead process's.
            # Re-observe and require unchanged before accepting, then return
            # the fenced observation so publication records the live one.
            fenced = _observe(
                transport,
                provider=provider,
                native_session_id=native_session_id,
                absent_reason=absent_reason,
            )
            fenced_identity = (
                fenced["pane_id"],
                fenced["pid"],
                fenced["start_marker"],
            )
            if fenced_identity != identity:
                _freeze(
                    provider=provider,
                    native_session_id=native_session_id,
                    reason=AMBIGUOUS_PROCESS_IMAGE_MISMATCH,
                    detail=(
                        "the pane process identity changed between the rendered-header "
                        "match and its fencing re-observation: "
                        f"expected {identity!r}, observed {fenced_identity!r}"
                    ),
                )
            return fenced, rows
        # A capture that itself crossed the deadline settles the window: the
        # header was not observed in bounds, so the wait ends here rather
        # than sleeping into a capture the pre-capture check would refuse.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _never_rendered_in_bound()
        time.sleep(min(KIMI_RENDER_CONVERGENCE_POLL_SECONDS, remaining))
        # Re-observe for continuity only: a changed pane_id/pid/start-marker
        # means the pane is no longer the admitted process, and proving the
        # session off it would bind a stranger's session.
        next_observation = _observe(
            transport,
            provider=provider,
            native_session_id=native_session_id,
            absent_reason=absent_reason,
        )
        next_identity = (
            next_observation["pane_id"],
            next_observation["pid"],
            next_observation["start_marker"],
        )
        if next_identity != identity:
            _freeze(
                provider=provider,
                native_session_id=native_session_id,
                reason=AMBIGUOUS_PROCESS_IMAGE_MISMATCH,
                detail=(
                    "the pane process identity changed while waiting for the rendered "
                    f"native header: expected {identity!r}, observed {next_identity!r}"
                ),
            )
        observation = next_observation


def _settle_session_proof(
    transport: NativePaneTransport,
    *,
    provider: str,
    native_session_id: str,
    provider_version: Optional[str],
    observation: dict[str, Any],
    absent_reason: str,
) -> tuple[dict[str, Any], str, Optional[list[str]]]:
    """Resolve the bound-session proof the launch will publish under.

    Returns ``(observation, session_proof, rendered_rows)`` where
    ``session_proof`` is one of :data:`SESSION_PROOFS`.  For a Kimi build proven
    to rewrite its title, the resumed session id is unreadable from the argv, so
    the binding is converged from the rendered header and the rows are handed
    back for an independent re-check at publication.  For every other provider
    and build the argv is still the proof, nothing is captured, and publication
    verifies from the observation's argv.
    """
    if _kimi_rendered_proof_active(provider, provider_version):
        observation, rendered_rows = _await_kimi_rendered_session_proof(
            transport,
            provider=provider,
            native_session_id=native_session_id,
            provider_version=provider_version,
            observation=observation,
            absent_reason=absent_reason,
        )
        return observation, SESSION_PROOF_KIMI_RENDERED, rendered_rows
    return observation, SESSION_PROOF_ARGV, None


def _publish(
    *,
    provider: str,
    native_session_id: str,
    terminal_id: str,
    generation: str,
    working_directory: str,
    observation: Mapping[str, Any],
    expected_inner_executable: Optional[str] = None,
    session_proof: str = SESSION_PROOF_ARGV,
    proven_rendered_rows: Optional[Sequence[str]] = None,
    provider_version: Optional[str] = None,
) -> dict[str, Any]:
    """Verify the pane runs the bound session, then publish the attachment.

    Two independent proofs, both taken before the attachment is
    published, because publication is what makes the generation
    bindable.  The session proof says *which session* the process resumed;
    the cwd proof says *which directory* it resumed it in.  Neither implies
    the other: a correct session started in the wrong directory names a
    session the provider will refuse to open, and it fails after the launch
    has already reported success.

    The session proof is taken from the argv for every provider and build
    whose resumed session id is still readable there, and from the rendered
    native header for a Kimi build proven to rewrite its process title after
    parsing (which erases the id from the argv).  Both are fail-closed and
    both are re-checked here rather than trusted from the convergence that
    preceded publication.
    """
    if session_proof == SESSION_PROOF_KIMI_RENDERED:
        _verify_rendered_session(
            provider=provider,
            native_session_id=native_session_id,
            provider_version=provider_version,
            rendered_rows=proven_rendered_rows or (),
        )
        _verify_pane_cwd(
            provider=provider,
            native_session_id=native_session_id,
            working_directory=working_directory,
            observation=observation,
        )
    elif session_proof == SESSION_PROOF_ARGV:
        _verify_bound_session_and_cwd(
            provider=provider,
            native_session_id=native_session_id,
            working_directory=working_directory,
            observation=observation,
        )
    else:
        # A closed vocabulary: an unknown proof channel never falls back to
        # argv.  The pane is live by here, so this freezes (rather than a
        # clean refusal) the way every other unresolved publication does.
        _freeze(
            provider=provider,
            native_session_id=native_session_id,
            reason=AMBIGUOUS_PUBLISH_FAILED,
            detail=(
                f"unknown session-proof channel {session_proof!r}; publication requires one "
                f"of {sorted(SESSION_PROOFS)}, and an unrecognised value must never be read "
                "as the argv proof"
            ),
        )
    if expected_inner_executable is not None:
        observed_executable = observation["argv"][0] if observation["argv"] else ""
        if os.path.realpath(observed_executable) != expected_inner_executable:
            _freeze(
                provider=provider,
                native_session_id=native_session_id,
                reason=AMBIGUOUS_PROCESS_IMAGE_MISMATCH,
                detail=(
                    f"the pane process image {observed_executable!r} is not the declared "
                    f"inner executable {expected_inner_executable!r}"
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
    session_proof: Optional[str] = None,
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
        # How the resumed session was proven this launch -- ``SESSION_PROOF_ARGV``
        # or ``SESSION_PROOF_KIMI_RENDERED`` (the rendered-header rule) -- so the
        # proof channel is named on success the way the freeze reason names it on
        # failure.  ``None`` when this call proved nothing (the generation was
        # already attached); the rule is not durable on the attachment because
        # that would need a schema migration, and inventing a parallel store is
        # worse than recording it on the launch result the caller already reads.
        "session_proof": session_proof,
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


def _muse_argv(
    *, session_id: str, binary: str, extra_args: Optional[Sequence[str]], launch_kind: str
) -> list[str]:
    # Muse Code 0.1.0 binds --session-id only on `muse exec` (headless,
    # one-shot); the interactive TUI rejects the flag. Both launch and recovery
    # therefore re-run the exec command bound to the minted session id.
    try:
        return muse_native_launch.build_launch_argv(
            session_id=session_id, muse_binary=binary, extra_args=extra_args,
        )
    except muse_native_launch.MuseNativeLaunchError as exc:
        raise NativeLaunchInvalid(str(exc)) from exc


def _codex_argv(
    *, session_id: str, binary: str, extra_args: Optional[Sequence[str]], launch_kind: str
) -> list[str]:
    if launch_kind != LAUNCH_KIND_RESUME:
        raise NativeLaunchInvalid(
            "Codex native sessions are minted by a zero-turn app-server bootstrap; "
            f"the TUI must resume the exact id, got launch_kind {launch_kind!r}"
        )
    try:
        return codex_native_launch.build_resume_argv(
            session_id=session_id,
            codex_binary=binary,
            extra_args=extra_args,
        )
    except codex_native_launch.CodexNativeLaunchError as exc:
        raise NativeLaunchInvalid(str(exc)) from exc


#: Per-provider argv construction and the matching "does this argv bind
#: exactly that session?" check. The two halves are registered together
#: on purpose: a builder paired with the wrong checker would construct a
#: correct argv and then verify it against a different provider's rules,
#: which passes and means nothing.
_ARGV_BINDERS: dict[str, dict[str, Any]] = {
    "codex": {"build": _codex_argv, "binds_exactly": codex_native_launch.resumes_exactly},
    "kimi_cli": {"build": _kimi_argv, "binds_exactly": kimi_native_launch.resumes_exactly},
    "claude_code": {"build": _claude_argv, "binds_exactly": claude_native_launch.binds_exactly},
    "muse_cli": {"build": _muse_argv, "binds_exactly": muse_native_launch.binds_exactly},
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
    expected_inner_executable: Optional[str] = None,
    provider_version: Optional[str] = None,
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

    ``provider_version`` selects the bound-session proof.  For a Kimi build
    proven to rewrite its process title after parsing, the resumed session id
    is erased from the kernel argv, so the proof is read from the rendered
    native header instead; for every other provider and build the argv remains
    the proof.  Absent (``None``), it leaves the argv proof in place, so a
    caller that does not know the version is unchanged.
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
        observation = _await_inner_exec(
            transport,
            provider=provider,
            native_session_id=native_session_id,
            working_directory=working_directory,
            observation=observation,
            wrapper_executable=binary,
            launch_argv=argv,
            expected_inner_executable=expected_inner_executable,
            absent_reason=AMBIGUOUS_START_CROSSED_NO_PANE,
        )
        observation, session_proof, proven_rendered_rows = _settle_session_proof(
            transport,
            provider=provider,
            native_session_id=native_session_id,
            provider_version=provider_version,
            observation=observation,
            absent_reason=AMBIGUOUS_START_CROSSED_NO_PANE,
        )
        attachment = _publish(
            provider=provider,
            native_session_id=native_session_id,
            terminal_id=terminal_id,
            generation=generation,
            working_directory=working_directory,
            observation=observation,
            expected_inner_executable=expected_inner_executable,
            session_proof=session_proof,
            proven_rendered_rows=proven_rendered_rows,
            provider_version=provider_version,
        )
        return _result(
            outcome=OUTCOME_RECONCILED,
            pane_handle=observation["pane_id"],
            observation=observation,
            attachment=attachment,
            session_proof=session_proof,
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
    observation = _await_inner_exec(
        transport,
        provider=provider,
        native_session_id=native_session_id,
        working_directory=working_directory,
        observation=observation,
        wrapper_executable=binary,
        launch_argv=argv,
        expected_inner_executable=expected_inner_executable,
        absent_reason=AMBIGUOUS_PANE_ABSENT_AFTER_CREATE,
    )
    observation, session_proof, proven_rendered_rows = _settle_session_proof(
        transport,
        provider=provider,
        native_session_id=native_session_id,
        provider_version=provider_version,
        observation=observation,
        absent_reason=AMBIGUOUS_PANE_ABSENT_AFTER_CREATE,
    )
    attachment = _publish(
        provider=provider,
        native_session_id=native_session_id,
        terminal_id=terminal_id,
        generation=generation,
        working_directory=working_directory,
        observation=observation,
        expected_inner_executable=expected_inner_executable,
        session_proof=session_proof,
        proven_rendered_rows=proven_rendered_rows,
        provider_version=provider_version,
    )
    return _result(
        outcome=OUTCOME_LAUNCHED,
        pane_handle=handle,
        observation=observation,
        attachment=attachment,
        session_proof=session_proof,
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

    def capture_render(
        self, pane_id: str, *, deadline_monotonic: Optional[float] = None
    ) -> list[str]:
        """The rendered rows of one exact pane; raised on any read failure.

        Read without ``-e`` so the rows are the composited viewport the
        provider's own detectors read, and not a raw stream of escape sequences.
        Targeted at the immutable ``pane_id`` of the observation that fences the
        proof (``-t %N``), never at the session/window: a window resolves to its
        *active* pane, which can flip between the observation and the capture,
        whereas a pane id names one pane for the life of the server.  A capture
        that fails is raised, never returned empty: the rendered header is the
        session proof for a title-rewriting build, and an unreadable render is an
        unresolved observation.
        """
        from cli_agent_orchestrator.clients.tmux import tmux_binary

        target = _require_text(pane_id, field="pane_id")
        argv = [
            tmux_binary(),
            "capture-pane",
            "-p",
            "-t",
            target,
        ]
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=_native_observation_timeout(deadline_monotonic, argv),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise NativeLaunchUnavailable(
                f"the rendered screen of pane {target} could not be captured within "
                f"the bound: {exc}"
            ) from exc
        except OSError as exc:
            raise NativeLaunchUnavailable(
                f"the rendered screen of pane {target} could not be captured: {exc}"
            ) from exc
        if proc.returncode != 0:
            detail = (proc.stderr or "").strip() or f"tmux exited {proc.returncode}"
            raise NativeLaunchUnavailable(f"could not capture pane {target}: {detail}")
        return (proc.stdout or "").splitlines()

    def observe(self, *, deadline_monotonic: Optional[float] = None) -> Optional[Mapping[str, Any]]:
        if deadline_monotonic is None:
            identity = self._backend.window_identity(self._session_name, self._window_name)
        else:
            identity = self._backend.window_identity(
                self._session_name,
                self._window_name,
                deadline_monotonic=deadline_monotonic,
            )
        if identity is None:
            # No identity and no window is a real absence.  No identity
            # while the window exists is a failed read, and saying
            # "absent" for that would license a relaunch on top of a live
            # process, so it raises instead.
            if deadline_monotonic is None:
                exists = self._backend.window_exists(self._session_name, self._window_name)
            else:
                exists = self._backend.window_exists(
                    self._session_name,
                    self._window_name,
                    deadline_monotonic=deadline_monotonic,
                )
            if not exists:
                return None
            raise NativeLaunchUnavailable(
                f"tmux window {self._session_name}:{self._window_name} exists but its "
                "pane identity could not be read"
            )
        if deadline_monotonic is None:
            pid = self._pane_pid()
        else:
            pid = self._pane_pid(deadline_monotonic=deadline_monotonic)
        if pid is None:
            raise NativeLaunchUnavailable(
                f"the primary process of {self._session_name}:{self._window_name} has no "
                "readable pid"
            )
        if deadline_monotonic is None:
            start_marker = _process_field(pid, "lstart=")
            command = _process_field(pid, "args=")
            process_argv = _process_argv(pid)
        else:
            start_marker = _process_field(pid, "lstart=", deadline_monotonic=deadline_monotonic)
            command = _process_field(pid, "args=", deadline_monotonic=deadline_monotonic)
            process_argv = _process_argv(pid, deadline_monotonic=deadline_monotonic)
        if start_marker is None or command is None:
            raise NativeLaunchUnavailable(
                f"the process table did not report the identity of pid {pid}"
            )
        if process_argv is None:
            raise NativeLaunchUnavailable(
                f"the kernel did not expose the exact argv of pid {pid}; refusing to "
                "validate a whitespace-rendered process-table approximation"
            )
        if deadline_monotonic is None:
            cwd = _process_cwd(pid)
        else:
            cwd = _process_cwd(pid, deadline_monotonic=deadline_monotonic)
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
            # ps renders argv as one lossy string: an argument containing
            # spaces is indistinguishable from several arguments.  The
            # native launch proof compares the kernel's boundary-preserving
            # argv instead.  ``command`` is still read above because failure
            # to observe the process table remains an unreadable identity,
            # but it is never tokenized into authoritative evidence.
            "argv": process_argv,
            "cwd": cwd,
        }

    def _pane_pid(self, *, deadline_monotonic: Optional[float] = None) -> Optional[int]:
        from cli_agent_orchestrator.clients.tmux import tmux_binary

        argv = [
            tmux_binary(),
            "display-message",
            "-p",
            "-t",
            f"{self._session_name}:{self._window_name}",
            "-F",
            "#{pane_pid}",
        ]
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=_native_observation_timeout(deadline_monotonic, argv),
                check=False,
            )
        except subprocess.TimeoutExpired:
            if deadline_monotonic is not None:
                raise
            return None
        except OSError:
            return None
        if proc.returncode != 0:
            return None
        raw = proc.stdout.strip()
        return int(raw) if raw.isdigit() and int(raw) > 0 else None


def _native_observation_timeout(deadline_monotonic: Optional[float], argv: Sequence[str]) -> float:
    """Bound one native-identity subprocess by the shared control deadline."""
    if deadline_monotonic is None:
        return 5.0
    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired(list(argv), 0)
    return min(5.0, remaining)


def _process_field(
    pid: int,
    field: str,
    *,
    deadline_monotonic: Optional[float] = None,
) -> Optional[str]:
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
    argv = [os.path.realpath(ps), "-o", field, "-p", str(pid)]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_native_observation_timeout(deadline_monotonic, argv),
            check=False,
        )
    except subprocess.TimeoutExpired:
        if deadline_monotonic is not None:
            raise
        return None
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return value or None


def _parse_darwin_procargs2(raw: bytes) -> Optional[list[str]]:
    """Decode one ``KERN_PROCARGS2`` payload without losing argv boundaries."""
    if len(raw) < struct.calcsize("i"):
        return None
    argc = struct.unpack_from("i", raw)[0]
    if argc <= 0 or argc > MAX_PROCESS_ARGV_BYTES:
        return None

    cursor = struct.calcsize("i")
    executable_end = raw.find(b"\0", cursor)
    if executable_end < 0:
        return None
    cursor = executable_end + 1
    while cursor < len(raw) and raw[cursor] == 0:
        cursor += 1

    argv: list[str] = []
    for _ in range(argc):
        argument_end = raw.find(b"\0", cursor)
        if argument_end < 0:
            return None
        argv.append(os.fsdecode(raw[cursor:argument_end]))
        cursor = argument_end + 1
    return argv if argv and argv[0] else None


def _darwin_process_argv(pid: int) -> Optional[list[str]]:
    """Read exact argv from Darwin's kernel-owned ``KERN_PROCARGS2``."""
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        sysctl = libc.sysctl
        sysctl.argtypes = [
            ctypes.POINTER(ctypes.c_int),
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.c_void_p,
            ctypes.c_size_t,
        ]
        sysctl.restype = ctypes.c_int
        mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2, pid
        size = ctypes.c_size_t()
        if sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0:
            return None
        if size.value <= 0 or size.value > MAX_PROCESS_ARGV_BYTES:
            return None
        buffer = ctypes.create_string_buffer(size.value)
        if sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
            return None
        return _parse_darwin_procargs2(buffer.raw[: size.value])
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _process_argv(
    pid: int,
    *,
    deadline_monotonic: Optional[float] = None,
) -> Optional[list[str]]:
    """The live process's exact argv, preserving every argument boundary.

    Linux exposes NUL-delimited argv in procfs. Darwin exposes the same
    kernel data through ``KERN_PROCARGS2``. A rendered ``ps args`` string
    is intentionally not a fallback: once boundaries are lost, a launch
    cannot prove that the process is running the exact admitted argv.
    """
    deadline_probe = ["process-argv", str(pid)]
    if deadline_monotonic is not None:
        _native_observation_timeout(deadline_monotonic, deadline_probe)

    argv: Optional[list[str]] = None
    proc_cmdline = f"/proc/{pid}/cmdline"
    if os.path.isdir(f"/proc/{pid}"):
        try:
            with open(proc_cmdline, "rb") as handle:
                raw = handle.read(MAX_PROCESS_ARGV_BYTES + 1)
        except OSError:
            raw = b""
        if raw and len(raw) <= MAX_PROCESS_ARGV_BYTES:
            fields = raw.split(b"\0")
            if fields[-1:] == [b""]:
                fields.pop()
            candidate = [os.fsdecode(field) for field in fields]
            if candidate and candidate[0]:
                argv = candidate
    elif sys.platform == "darwin":
        argv = _darwin_process_argv(pid)

    if deadline_monotonic is not None:
        _native_observation_timeout(deadline_monotonic, deadline_probe)
    return argv


def _process_cwd(pid: int, *, deadline_monotonic: Optional[float] = None) -> Optional[str]:
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
    argv = [os.path.realpath(lsof), "-a", "-d", "cwd", "-p", str(pid), "-Fn"]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_native_observation_timeout(deadline_monotonic, argv),
            check=False,
        )
    except subprocess.TimeoutExpired:
        if deadline_monotonic is not None:
            raise
        return None
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    # Field-per-line output: ``p<pid>``, ``fcwd``, ``n<path>``.  Only the
    # ``n`` line carries the path, and a dead pid yields no lines at all.
    for line in proc.stdout.splitlines():
        if line.startswith("n") and len(line) > 1:
            return line[1:]
    return None
