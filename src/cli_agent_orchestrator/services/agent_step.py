"""Shared agent-step execution substrate (issue #312, unit N0).

``run_agent_step`` is the single canonical create -> input -> wait -> extract ->
teardown sequence for driving one agent through one step. It is the shared
substrate both step callers converge on, SERVER-SIDE:

- the run engine (N5, future) calls it directly IN-PROCESS;
- the handoff MCP client reaches it over the single combined HTTP endpoint
  ``POST /terminals/run-step`` (api/main.py), replacing its former six granular
  round-trips.

It depends ONLY on the terminal layer (``terminal_service`` + the provider
manager), so it is backend-agnostic (BR-10/RD-4): correctness holds on the tmux
backend alone, with no per-step tmux/herdr branching.

Failure contract (RD-2.1 / REL-3.3): ``run_agent_step`` returns an
``AgentStepResult`` ONLY on success (status COMPLETED). Every failure mode —
the readiness/completion wait timing out, the terminal reaching
``TerminalStatus.ERROR`` — RAISES a narrow exception. It NEVER returns a falsy
or ``None`` "success". The caller (engine) maps the raised exception to its 3x
retry policy (FR-5.3); the HTTP handler maps it to an ``HTTPException``.
"""

import asyncio
import logging
import time
from typing import Callable, Optional

from cli_agent_orchestrator.models.kiro_engine import KiroEngine, parse_kiro_engine
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.models.terminal import AgentStepResult, TerminalStatus
from cli_agent_orchestrator.plugins import PluginRegistry
from cli_agent_orchestrator.providers.kiro_capabilities import KiroPhase0KASError
from cli_agent_orchestrator.services import frozen_run_memory, terminal_service
from cli_agent_orchestrator.services.status_monitor import status_monitor
from cli_agent_orchestrator.services.step_fingerprint import StepCallFields, compute
from cli_agent_orchestrator.services.terminal_service import OutputMode
from cli_agent_orchestrator.utils.terminal import wait_until_status

logger = logging.getLogger(__name__)

# Ready states a freshly created terminal may settle into before it can accept
# input (mirrors the handoff readiness wait): some providers process their
# system prompt as the first turn and reach COMPLETED without a bare IDLE.
_READY_STATES = {TerminalStatus.IDLE, TerminalStatus.COMPLETED}

# Working states that prove the agent picked up the prompt (used to gate the
# post-input IDLE-as-done signal below).
_WORKING_STATES = {TerminalStatus.PROCESSING, TerminalStatus.WAITING_USER_ANSWER}

# Generous readiness timeout: provider init (shell warm-up + CLI startup + MCP
# registration + auth) can take ~15-45s. Matches the handoff caller's 120s.
DEFAULT_READY_TIMEOUT = 120.0

# Poll cadence for the post-input completion wait, and the number of consecutive
# IDLE reads required before a post-input IDLE is accepted as "done" (issue #409a).
_COMPLETION_POLL_INTERVAL = 1.0
_IDLE_STABLE_POLLS = 3

# Delivery verification on the synchronous step path (#562). Readiness cannot
# prove the TUI will accept input — an OpenCode splash frame carries the same
# idle footer as a conversation-ready frame — so the paste or its Enter can be
# dropped right after the send and the worker never sees its task. Wait this
# long for pickup evidence (any working-state read) before re-delivering, and
# cap the attempts. The step's own ``timeout`` still bounds everything.
# 8s mirrors ``_DEFERRED_SUBMIT_CONFIRM_TIMEOUT`` in terminal_service: the
# same decision helper serves that deferred-init confirm loop, so both paths
# give the PROCESSING edge the same window before calling a send dropped. It
# is a consistency number with the sibling path, not a measured provider
# startup latency — tune them together.
_PROMPT_PICKUP_GRACE = 8.0
_PROMPT_REDELIVER_MAX = 3


async def _validate_reused_terminal(
    terminal_id: str,
    requested_provider: str,
    requested_engine: Optional[KiroEngine | str],
) -> None:
    """Require reuse constraints to agree with authoritative terminal metadata."""
    metadata = await asyncio.to_thread(terminal_service.get_terminal_metadata, terminal_id)
    if metadata is None:
        raise ValueError(f"Terminal '{terminal_id}' not found")

    persisted_provider = metadata.get("provider")
    if persisted_provider != requested_provider:
        raise ValueError(
            f"Provider mismatch for reused terminal '{terminal_id}': "
            f"requested {requested_provider!r}, persisted {persisted_provider!r}"
        )

    if requested_engine is None:
        return
    if persisted_provider != ProviderType.KIRO_CLI.value:
        raise ValueError("Kiro engine selection is only valid for provider 'kiro_cli'")

    explicit_engine = parse_kiro_engine(requested_engine)
    assert explicit_engine is not None
    if explicit_engine == KiroEngine.KAS:
        # KAS remains unavailable regardless of which engine the terminal
        # persisted; use the same structured Phase 0 guard as terminal creation.
        raise KiroPhase0KASError(profile_has_v2_policy=False)

    persisted_engine = parse_kiro_engine(metadata.get("engine"))
    if persisted_engine is None:
        # Legacy Kiro rows predate the engine column and are v2 by definition.
        persisted_engine = KiroEngine.V2
    if explicit_engine != persisted_engine:
        raise ValueError(
            f"Kiro engine mismatch for reused terminal '{terminal_id}': "
            f"requested {explicit_engine.value!r}, persisted {persisted_engine.value!r}"
        )


class StepExecutionError(Exception):
    """A step failed to complete successfully.

    Raised for a readiness/completion timeout or a terminal that reached
    ``TerminalStatus.ERROR``. Narrow by design so the caller (engine) can map
    it to its retry policy and the API boundary can map it to an HTTPException.

    Carries two structured fields so callers never have to scrape the message:

    - ``kind`` distinguishes a worker that *ran long* (``"timeout"``) from one
      that *crashed* (``"error"``, i.e. the terminal reached ERROR). The two
      were previously indistinguishable — both surfaced as a 504 "timed out".
    - ``terminal_id`` is the live terminal the step ran on (when known), so a
      failed caller can report/clean it up without regex-scraping the message.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str = "timeout",
        terminal_id: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.terminal_id = terminal_id


class StepCancelledError(Exception):
    """The in-flight step wait was interrupted by a cancellation signal (#409b).

    Distinct from ``StepExecutionError``: a cancellation is NOT a run-failure and
    must NOT be retried. The engine converts it into run-level CANCELLED
    convergence instead of consuming a retry attempt. ``terminal_id`` carries the
    live terminal (already best-effort torn down by ``run_agent_step`` when it
    owned it) so the caller can reconcile if needed.
    """

    def __init__(self, terminal_id: Optional[str] = None) -> None:
        super().__init__("step wait interrupted by cancellation")
        self.terminal_id = terminal_id


async def _wait_for_completion(
    terminal_id: str,
    timeout: float,
    cancel_event: Optional["asyncio.Event"] = None,
    *,
    prompt: Optional[str] = None,
) -> None:
    """Wait for a post-input step to settle, polling ``status_monitor`` (issue #409).

    Called strictly AFTER the prompt has been sent, so IDLE here can never be the
    pre-input readiness IDLE the caller already waited past.

    Completion signals (issue #409a):

    - ``COMPLETED`` — definitive done marker; returns immediately (unchanged).
    - ``IDLE`` — accepted as done ONLY after the agent was observed working (a
      ``PROCESSING`` / ``WAITING_USER_ANSWER`` read) AND IDLE then persists for
      ``_IDLE_STABLE_POLLS`` consecutive polls. This is the codex-style case where
      a provider legitimately settles back to its idle prompt after answering and
      never emits a ``COMPLETED`` marker — requiring ``COMPLETED`` alone hung the
      step until timeout and left the whole run stuck ``running``. Gating on
      observed-working is what keeps the idle-right-after-send window (before the
      agent picks up the prompt) from returning early with empty output; it mirrors
      the CLI-side ``poll_until_done`` heuristic exactly.

    Delivery verification (issue #562): readiness cannot prove a TUI will accept
    input (an OpenCode splash frame carries the same idle footer as a
    conversation-ready one), so the paste or its Enter can be dropped at send and
    the worker would sit unprompted for the whole budget. When ``prompt`` is
    given, a worker that shows NO pickup evidence (any working-state read) within
    ``_PROMPT_PICKUP_GRACE`` gets the message re-delivered — bare Enter when the
    text is still visible in the rendered pane, full paste when it vanished and
    the provider is probe-capable — up to ``_PROMPT_REDELIVER_MAX`` times,
    reusing the deferred-init confirm loop's decision helper (#479/#496).
    Redelivery only ever fires while the terminal reads IDLE and was never
    observed working; once work is seen — or the helper's direct probe confirms
    the worker is running — the step is an ordinary completion wait: a probe
    "started" verdict proves delivery, never completion, so the cached-IDLE exit
    still requires prior work. A redelivery that itself raises is logged and
    swallowed (a failed recovery attempt is not a step failure) so this wait
    never escapes with anything but its documented exceptions.

    Interruptibility (issue #409b): if ``cancel_event`` fires mid-wait, raises
    ``StepCancelledError`` PROMPTLY (it does not wait out the poll interval) so an
    in-flight — possibly hung — step becomes cancellable instead of being observed
    only at the next step boundary.

    Raises:
        StepExecutionError(kind="error"): the terminal reached ``ERROR``.
        StepExecutionError(kind="timeout"): no completion signal within ``timeout``.
        StepCancelledError: ``cancel_event`` fired while waiting.
    """
    deadline = time.monotonic() + timeout
    observed_working = False
    consecutive_idle = 0
    redeliveries = 0
    delivery_verified = False
    # Seeded at entry — i.e. AFTER ``send_input`` returned — so the first
    # grace window runs from the start of this wait, not from the send. That
    # reads long, which is the conservative direction: a false "dropped"
    # verdict only costs a wait, a false "delivered" one burns the budget.
    last_send = time.monotonic()

    while True:
        if cancel_event is not None and cancel_event.is_set():
            raise StepCancelledError(terminal_id=terminal_id)

        current = status_monitor.get_status(terminal_id)
        if current == TerminalStatus.ERROR:
            raise StepExecutionError(
                f"terminal {terminal_id} reached ERROR status",
                kind="error",
                terminal_id=terminal_id,
            )
        if current == TerminalStatus.COMPLETED:
            return
        if current == TerminalStatus.IDLE:
            # Post-input IDLE only counts once the agent has actually started
            # working — otherwise the idle-before-processing window right after
            # the send would settle immediately with empty/partial output.
            if observed_working:
                consecutive_idle += 1
                if consecutive_idle >= _IDLE_STABLE_POLLS:
                    logger.info(
                        "step on terminal %s settled IDLE post-input "
                        "(observed working; %d consecutive idle polls) — done",
                        terminal_id,
                        consecutive_idle,
                    )
                    return
        elif current in _WORKING_STATES:
            observed_working = True
            consecutive_idle = 0
        else:
            # UNKNOWN or any other non-ready status: not evidence of work and not
            # a stable idle — reset the idle streak but do not flip observed_working.
            consecutive_idle = 0

        if time.monotonic() >= deadline:
            # Defensive: a terminal that flipped to ERROR right at the deadline is
            # a crash, not a slow run (preserve the kind="error" vs "timeout" split).
            if status_monitor.get_status(terminal_id) == TerminalStatus.ERROR:
                raise StepExecutionError(
                    f"terminal {terminal_id} reached ERROR status",
                    kind="error",
                    terminal_id=terminal_id,
                )
            raise StepExecutionError(
                f"step on terminal {terminal_id} did not complete within {timeout}s",
                kind="timeout",
                terminal_id=terminal_id,
            )

        # Delivery verification (#562): no pickup evidence within the grace
        # window on a terminal still reading IDLE → the send was most likely
        # dropped (see module constants). Re-deliver off the loop via the
        # shared decision helper — #496's direct-probe guard inside it also
        # catches a worker already running under a lagging cached status.
        # ``full_resend_requires_probe``: without a probe there is NO reliable
        # "already working" check for the full re-send branch — the box check
        # matches the whole rendered pane (see ``_message_visible_in_box``),
        # and under the pyte screen path a whole turn can process inside one
        # rising-edge/quiescence burst, leaving the cached status IDLE
        # throughout while the prompt scrolls off — so a full re-send could
        # duplicate a task the worker already ran. Probe-capable providers
        # keep the full re-send; the rest keep the bare-Enter recovery,
        # which cannot duplicate a task.
        if (
            prompt is not None
            and not delivery_verified
            and not observed_working
            and current == TerminalStatus.IDLE
            and redeliveries < _PROMPT_REDELIVER_MAX
            and time.monotonic() - last_send >= _PROMPT_PICKUP_GRACE
        ):
            redeliveries += 1
            last_send = time.monotonic()
            logger.warning(
                "step on terminal %s shows no pickup %ss after send "
                "(idle, never working) — re-delivering prompt (attempt %d)",
                terminal_id,
                _PROMPT_PICKUP_GRACE,
                redeliveries,
            )
            # A failed redelivery is a failed RECOVERY attempt, not a step
            # failure: ``redeliver_dropped_message`` performs tmux I/O and can
            # raise (blocked input, vanished pane). Swallow it and let the
            # step's own deadline classify the outcome, so this wait keeps
            # its documented Raises contract (StepExecutionError /
            # StepCancelledError, never a raw terminal exception).
            try:
                already_started = await asyncio.to_thread(
                    terminal_service.redeliver_dropped_message,
                    terminal_id,
                    prompt,
                    redeliveries,
                    full_resend_requires_probe=True,
                )
            except Exception:
                logger.warning(
                    "prompt redelivery to %s failed (attempt %d) — continuing to wait",
                    terminal_id,
                    redeliveries,
                    exc_info=True,
                )
                already_started = False
            if already_started:
                # Probe saw the worker running: delivery is confirmed, stop
                # re-sending. That verdict proves delivery only, NOT
                # completion — the cached IDLE is lagging (#496) — so the
                # ordinary signals (COMPLETED, or working then stable IDLE)
                # still gate the exit.
                delivery_verified = True
            # else: a re-send was attempted; if it also shows no pickup after
            # another grace window the loop tries again, up to the cap.
            consecutive_idle = 0
            continue

        # Sleep one poll interval, but wake IMMEDIATELY if cancel fires so the
        # cancel latency is not bounded below by the poll cadence (#409b).
        if cancel_event is not None:
            try:
                await asyncio.wait_for(cancel_event.wait(), timeout=_COMPLETION_POLL_INTERVAL)
            except asyncio.TimeoutError:
                pass  # normal poll cadence — re-loop and re-check status
            else:
                raise StepCancelledError(terminal_id=terminal_id)
        else:
            await asyncio.sleep(_COMPLETION_POLL_INTERVAL)


async def resolve_effective_working_directory(
    working_directory: Optional[str],
    caller_id: Optional[str],
) -> Optional[str]:
    """Resolve the directory a freshly created terminal will ACTUALLY run in.

    Extracted verbatim from ``run_agent_step``'s create path (issue #583, unit
    ``run-step-replay-branch`` BR-10/TD-1) because two callers now need the same
    answer and there must be exactly ONE computation of it:

    * ``run_agent_step`` itself, which forwards the result to
      ``terminal_service.create_terminal`` and hashes it as
      ``StepCallFields.effective_working_directory``;
    * the ``POST /terminals/run-step`` route, which must compute a script step's
      call fingerprint BEFORE it decides whether to execute at all — and
      ``step-fingerprint``'s BR-5 permits only the EFFECTIVE directory in that
      hash. Hashing the POSTED value would not match what ``begin_step`` stored,
      so every ``caller_id``-inherited step would read as a false ``DIVERGED``.

    The route passes its answer back in through ``run_agent_step``'s existing
    ``working_directory`` parameter, so the call below simply returns it
    unchanged and no resolution happens twice. There is deliberately NO
    ``skip_resolution`` flag: a parameter whose only purpose is to disable a
    branch is the inert-parameter shape this issue has removed three times.

    BEST-EFFORT, AND THAT IS THE CONTRACT (unchanged by the extraction).
    ``asyncio.CancelledError`` is re-raised so a cancelled step stays cancelled;
    any other failure is logged and the caller falls back to the server default,
    because CWD inheritance must never fail a step that could otherwise run.

    ``caller_id`` is not authenticated/authorized (it arrives via an HTTP body);
    this is consistent with its existing use for callback routing (#284). The
    resolved path still passes ``_resolve_and_validate_working_directory`` inside
    ``create_terminal``, so risk is confined to inheriting a real existing pane's
    CWD in a single-user trust model.

    Args:
        working_directory: the explicitly requested directory, or None.
        caller_id: the supervisor terminal whose pane CWD is inherited when
            ``working_directory`` is None.

    Returns:
        ``working_directory`` when it was supplied or there is no caller to
        inherit from; the caller terminal's CWD when resolution succeeds and
        returns a non-empty path; otherwise ``working_directory`` unchanged
        (i.e. None — the server default).
    """
    # The guard is the extracted block's own condition, inverted into an early
    # return. An explicit directory always wins, and with no caller_id there is
    # nothing to inherit from.
    if working_directory is not None or caller_id is None:
        return working_directory
    try:
        resolved = await asyncio.to_thread(terminal_service.get_working_directory, caller_id)
        if resolved:
            return resolved
    except asyncio.CancelledError:
        raise
    except (
        Exception
    ) as exc:  # noqa: BLE001 — CWD inheritance is best-effort; step must not fail on it
        logger.warning(
            "resolve_effective_working_directory: failed to resolve working directory "
            "from caller %r, falling back to server default: %r",
            caller_id,
            exc,
        )
    return working_directory


async def run_agent_step(
    provider: str,
    agent: str,
    prompt: str,
    session_name: Optional[str] = None,
    reuse_terminal_id: Optional[str] = None,
    teardown: bool = True,
    timeout: float = 600.0,
    ready_timeout: float = DEFAULT_READY_TIMEOUT,
    working_directory: Optional[str] = None,
    caller_id: Optional[str] = None,
    allowed_tools: Optional[list[str]] = None,
    registry: Optional[PluginRegistry] = None,
    env_vars: Optional[dict[str, str]] = None,
    on_step_terminal_ready: Optional[Callable[[str, str], None]] = None,
    cancel_event: Optional[asyncio.Event] = None,
    engine: Optional[KiroEngine | str] = None,
    model: Optional[str] = None,
    use_worktree: bool = False,
) -> AgentStepResult:
    """Run one agent step and return its result (success only).

    Sequence:
      1. Create a terminal (or reuse ``reuse_terminal_id``).
      2. Wait until it is ready to accept input (IDLE/COMPLETED).
      3. Send ``prompt`` (sync, bracketed-paste — the existing input path).
      4. Wait until COMPLETED (in-process status poll).
      5. Extract the last agent message (provider-specific extraction).
      6. Tear the terminal down unless ``teardown=False`` or it was reused.

    Args:
        provider: Provider type string (e.g. "kiro_cli", "claude_code").
        agent: Agent profile name.
        prompt: The message to send. Any caller-side prompt shaping (e.g. the
            codex handoff banner) is applied BEFORE calling this; the substrate
            sends ``prompt`` verbatim.
        session_name: Optional existing session to create the terminal in. When
            provided, the terminal is added as a window to that EXISTING session
            (``new_session=False``). When None, a brand-new tmux session is
            created for this step (``new_session=True``) — auto-naming the
            session inside ``create_terminal``. (Passing None with the implicit
            ``new_session=False`` would always fail: the auto-generated session
            does not yet exist.)
        reuse_terminal_id: Reuse an existing terminal instead of creating one.
            When set, the create + teardown steps are skipped (no pool; the
            caller owns the terminal's lifecycle).
        teardown: When True (default) and the terminal was created here, delete
            it after extraction. Ignored when ``reuse_terminal_id`` is set.
        timeout: Max seconds to wait for the step to reach COMPLETED.
        ready_timeout: Max seconds to wait for a freshly created terminal to be
            ready to accept input.
        working_directory: Optional working directory for a freshly created
            terminal (ignored when reusing a terminal). When None and
            ``caller_id`` is set, the worker inherits the caller's pane CWD
            via ``get_working_directory()`` (best-effort; falls back to the
            server default on failure).
        caller_id: Terminal ID of the supervisor creating this terminal, recorded
            so ``send_message`` can route callbacks structurally (issue #284).
            Also used to inherit the working directory when
            ``working_directory`` is None (best-effort). None for
            operator-launched / engine steps with no supervisor.
        allowed_tools: Resolved allowed-tools list for the freshly created
            terminal (handoff inheritance). None lets ``create_terminal`` derive
            them from the agent profile.
        registry: Plugin registry forwarded to ``delete_terminal`` on teardown so
            ``post_kill_terminal`` plugin hooks fire (parity with the DELETE
            endpoint). None (the in-process engine path today) means no hooks
            dispatch — behavior unchanged.
        env_vars: Optional per-step environment variables to inject into a freshly
            created terminal (ignored when reusing a terminal). The run engine (N5)
            uses this to set ``CAO_WORKFLOW_RUN_ID`` / ``CAO_WORKFLOW_STEP_ID`` so
            the worker's ``workflow_return`` tool routes its structured output to
            the correct ``(run_id, step_id)`` store key. With ``session_name=None``
            the substrate creates a fresh session per step, so the per-step env is
            injected cleanly (no stale step_id from a shared session). Default None
            = behavior unchanged (the handoff caller passes nothing).
        on_step_terminal_ready: Optional callback invoked with
            ``(terminal_id, call_fingerprint)`` as soon as the terminal this step
            will run on EXISTS and before the prompt is sent. It fires on BOTH
            paths (issue #583, unit ``settlement-rewire`` BR-3): on the
            create path immediately after ``terminal_service.create_terminal``
            returns and BEFORE the readiness wait; on the reuse path immediately
            after the reused terminal is validated. Firing on the reuse path too
            is what gives every script step a durable ``running`` row before it
            executes — without it, a terminal-reuse call would have none and
            FR-4's guard would cover only steps that made their own terminal.
            THIS PARAMETER WAS RENAMED BECAUSE FIRING IT ON THE REUSE PATH MADE
            ITS FORMER NAME — which spoke only of terminal creation — FALSE
            (BR-4). The former name is deliberately not spelled here: a test
            greps the whole of ``src/`` for it, so the one place it survives must
            be the changelog, not the code.
            Two consumers today, both in ``script_runner``: U4's orphan sweep
            (BR-31) records the live terminal into the shared ``ScriptRunRecord``
            ``step_states`` map, so a subprocess that crashes/times out while a
            run-step call is mid-flight still leaves the in-flight terminal
            visible to ``_reconcile_orphans``; and the journal's ``begin_step``
            writes the durable ``running`` row carrying ``call_fingerprint``. A
            callback exception is logged and swallowed — step bookkeeping must
            never fail a live step. Default None = behavior unchanged.
        cancel_event: Optional ``asyncio.Event`` the engine sets to interrupt an
            in-flight completion wait (issue #409b). When set mid-wait, the step
            wait is abandoned promptly (not at the next natural boundary) and a
            ``StepCancelledError`` is raised — after tearing down a terminal this
            call created. This is what makes a hung run cancellable: the run whose
            provider never emits a completion signal is exactly the run that could
            not otherwise be killed. Default None = no cancellation seam (the
            handoff caller passes nothing) — behavior unchanged.
        engine: Explicit Kiro engine for this child step. This is never inferred
            from a parent terminal.
        model: Explicit per-call model override for a freshly created
            terminal (ignored when reusing a terminal), forwarded to
            ``terminal_service.create_terminal``. Lets a handoff caller pin
            a specific model for this one worker without a dedicated agent
            profile. Default None = behavior unchanged (profile.model, if
            any, still applies).
        use_worktree: Issue #100 Phase 1. When True and a terminal is created
            here (``reuse_terminal_id`` is None), the freshly created terminal
            gets an isolated ``git worktree`` instead of sharing
            ``working_directory`` as given — see
            ``terminal_service.create_terminal``'s own docstring for the
            resolution/teardown mechanics. Ignored when reusing a terminal.
            Default False = behavior unchanged.

    Returns:
        ``AgentStepResult`` with status COMPLETED — ONLY on success.

    Raises:
        StepExecutionError: readiness/completion wait timed out (``kind="timeout"``)
            or the terminal reached ``TerminalStatus.ERROR`` (``kind="error"``).
            ``terminal_id`` carries the live terminal so the caller can clean up.
        StepCancelledError: ``cancel_event`` fired during the completion wait
            (issue #409b) — a cancellation, NOT a run-failure (do not retry).
        ValueError / TimeoutError: propagated from ``terminal_service`` (e.g.
            terminal-create failure, unknown terminal) — surfaced, never swallowed.
    """
    created_here = reuse_terminal_id is None
    terminal_id = reuse_terminal_id

    if created_here:
        # Inherit working directory from supervisor when not explicitly set.
        # Without this, a handoff worker starts in the cao-server process CWD
        # instead of the supervisor's project directory. Best-effort: if
        # resolution fails, fall back to the server default.
        #
        # THE COMPUTATION LIVES IN ``resolve_effective_working_directory`` (issue
        # #583, unit ``run-step-replay-branch`` BR-10/TD-1) because the run-step
        # route must know this answer BEFORE it calls this function — it needs the
        # effective directory to compute the call fingerprint the replay gate
        # compares. When the route has already resolved, it passes the result in
        # as ``working_directory`` and the helper returns it unchanged, so the
        # resolution never runs twice and no flag is needed. Duplicating the
        # computation instead would be the "two implementations of one
        # security-relevant value" defect FR-2 exists to prevent.
        working_directory = await resolve_effective_working_directory(working_directory, caller_id)

    # The step's ``v2`` call identity (issue #583, unit ``settlement-rewire`` BR-1), computed
    # in the ONE window ``step-fingerprint``'s BR-5 permits: AFTER the working-directory
    # resolution above and BEFORE terminal creation below.
    #
    # THE WINDOW IS THE WHOLE REASON THIS LIVES HERE rather than in either callback. Both
    # callback factories are built in the route (``api/main.py``) before ``run_agent_step`` is
    # called at all — hence before resolution — and the settle callback runs later still, once
    # the step has already executed. ``effective_working_directory`` must be the directory the
    # step ACTUALLY ran in: when ``working_directory is None and caller_id is not None`` the
    # block above replaces it with the caller terminal's CWD, so hashing the POSTED value
    # would give two runs that executed in genuinely different directories one identity, and
    # one would replay the other's result.
    #
    # ONE STATEMENT, UNCONDITIONAL — computed exactly once per step (INV-1). The
    # ``if created_here:`` test is repeated below rather than folding this into either branch,
    # because a per-branch computation would duplicate the field assembly and the two copies
    # could drift.
    #
    # On the reuse path the four creation-only components are sentinel-ised by ``compute``
    # itself (BR-1a/BR-5), which is CORRECT and must not be "fixed": the resolution block
    # above is inside ``if created_here:``, so on a reuse call those fields describe a
    # terminal this call did not make and the implementation discards them. The tuple is never
    # shortened — ten components on both paths.
    #
    # The digest is NEVER logged, echoed or put in an exception (SR-7).
    call_fingerprint = compute(
        StepCallFields(
            provider=provider,
            agent=agent,
            prompt=prompt,
            model=model,
            # ``StepCallFields.engine`` is the enum's ``value`` by contract — the CALLER
            # normalises, so ``step_fingerprint`` can stay a stdlib-only leaf module.
            engine=engine.value if isinstance(engine, KiroEngine) else engine,
            allowed_tools=None if allowed_tools is None else tuple(allowed_tools),
            effective_working_directory=working_directory,
            use_worktree=use_worktree,
            reused_terminal=not created_here,
            timeout=timeout,
        )
    )

    def _notify_terminal_ready(ready_terminal_id: str) -> None:
        """Fire ``on_step_terminal_ready`` best-effort — bookkeeping never fails a step.

        Called from BOTH paths (BR-3). Kept as one nested helper with one ``try`` so the
        two call sites cannot diverge in their error posture, while each keeps its own
        position guarantee: on the create path this must run BEFORE the readiness wait
        (BR-31's window), which is why the invocation is not simply hoisted below the
        create/reuse branch.
        """
        if on_step_terminal_ready is None:
            return
        try:
            on_step_terminal_ready(ready_terminal_id, call_fingerprint)
        except (
            Exception
        ) as exc:  # noqa: BLE001 — step bookkeeping is best-effort; step must not fail on it
            logger.warning(
                "run_agent_step: on_step_terminal_ready callback failed for terminal %s: %s",
                ready_terminal_id,
                exc,
            )

    if created_here:
        # When no session_name is supplied we must CREATE a fresh tmux session
        # (new_session=True): create_terminal auto-names it. Leaving the default
        # new_session=False here would auto-generate a name and then immediately
        # fail with "Session '<name>' not found", since that session does not
        # exist yet. When a session_name IS supplied, add a window to it
        # (new_session=False) — this is the handoff "same session as supervisor"
        # path.
        new_session = session_name is None

        # create_terminal already runs provider.initialize() (which waits for
        # IDLE); a failure raises (ValueError/TimeoutError) and propagates.
        terminal = await terminal_service.create_terminal(
            provider,
            agent,
            session_name=session_name,
            new_session=new_session,
            working_directory=working_directory,
            allowed_tools=allowed_tools,
            caller_id=caller_id,
            env_vars=env_vars,
            engine=engine,
            model=model,
            use_worktree=use_worktree,
        )
        terminal_id = terminal.id

        # BR-31: make the terminal this call just made visible to U4's orphan
        # sweep, and (issue #583, BR-3) write its durable ``running`` row, BEFORE
        # the readiness wait / input send — the dangerous edge is a subprocess
        # that dies while this call is mid-flight, between the terminal
        # appearing and the journal write. Doing both now closes that window,
        # and the position matters: the readiness wait below can run for
        # ``ready_timeout`` seconds, so notifying after it would reopen exactly
        # the gap BR-31 was added to close.
        _notify_terminal_ready(terminal_id)

        # Secondary in-process readiness wait: provider.initialize() can return a
        # false-positive on the shell prompt before the CLI is truly ready, so we
        # confirm a ready status before sending input (same guard handoff uses).
        ready = await wait_until_status(terminal_id, _READY_STATES, timeout=ready_timeout)
        if not ready:
            # Surface the live terminal so it can be inspected/cleaned up, then
            # fail fast. We do NOT auto-delete here: leaving the terminal lets
            # the caller decide (handoff surfaces terminal_id on failure).
            raise StepExecutionError(
                f"terminal {terminal_id} did not reach a ready status within " f"{ready_timeout}s",
                kind="timeout",
                terminal_id=terminal_id,
            )
    else:
        assert terminal_id is not None
        await _validate_reused_terminal(terminal_id, provider, engine)
        # BR-3: the reuse path notifies too. Until this unit the hook fired only
        # inside the create branch, so a terminal-reuse call wrote NO durable
        # ``running`` row and FR-4's guard covered only steps that made their own
        # terminal, leaving reuse to depend on the journal's no-begin rescue
        # instead. Notifying after validation rather than before it keeps the
        # order honest: a call rejected by ``_validate_reused_terminal`` never
        # ran, so it must not leave a ``running`` row behind.
        _notify_terminal_ready(terminal_id)

    assert terminal_id is not None  # for type-checkers: set in both branches

    # Send the prompt. send_input is synchronous tmux I/O (bracketed paste +
    # key sends); run it off the event loop so a slow tmux call cannot freeze
    # the whole server for other requests (same hazard as issue #382, which was
    # only fixed for DELETE /sessions). Any failure raises and propagates.
    # issue #583 Bolt 2, ``memory-resolve-once``: hand this run's FROZEN memory block to the terminal
    # so a replayed run sees the memory the ORIGINAL run recorded rather than the store's state today
    # (FR-9). The run id is read from ``env_vars`` rather than taken as a new parameter, because the
    # engine already sets ``CAO_WORKFLOW_RUN_ID`` there for the worker's ``workflow_return`` routing.
    #
    # Resolution happens HERE and nowhere earlier, which is the requirement rather than an
    # optimisation: a run that never creates a terminal must resolve nothing, because an unresolved
    # block is sensitive text that would be stored for no reason (NFR-1).
    #
    # ``None`` means there is no workflow manifest to honour, so use the historical live-memory path.
    # ``""`` is different: it means an existing manifest's memory fill could not persist and MUST be
    # passed through explicitly, suppressing ``send_input``'s live-memory fallback.
    frozen_memory = await asyncio.to_thread(
        frozen_run_memory.frozen_memory_for,
        (env_vars or {}).get("CAO_WORKFLOW_RUN_ID"),
        terminal_id,
        prompt,
    )
    if frozen_memory is None:
        # The call is left BYTE-IDENTICAL on the no-frozen-block path, rather than passing an extra
        # `None`. Existing tests assert this exact two-argument shape, and keeping them passing
        # unchanged is the strongest available evidence for C-1: a non-workflow step reaches
        # ``send_input`` exactly as it did before this unit.
        await asyncio.to_thread(terminal_service.send_input, terminal_id, prompt)
    else:
        await asyncio.to_thread(
            terminal_service.send_input,
            terminal_id,
            prompt,
            frozen_memory=frozen_memory,
        )

    # Wait for completion — IN-PROCESS poll of status_monitor (NOT the
    # HTTP-polling wait_until_terminal_status, which would reintroduce the
    # self-loopback the single-seam rule forbids). Accepts a post-input IDLE as a
    # completion signal alongside COMPLETED (issue #409a) and is interruptible via
    # ``cancel_event`` (issue #409b). ``prompt`` arms the delivery check (#562):
    # a worker still idle and never working _PROMPT_PICKUP_GRACE after the send
    # gets the task re-delivered before the budget burns down. Raises
    # StepExecutionError on timeout/ERROR, or StepCancelledError if cancellation
    # fires mid-wait.
    try:
        await _wait_for_completion(terminal_id, timeout, cancel_event, prompt=prompt)
    except StepCancelledError:
        # A cancellation is NOT a run-failure. Tear down a terminal this call
        # created (best-effort — never let cleanup mask the cancellation), then
        # re-raise so the engine converges the run to CANCELLED without retrying.
        if created_here:
            await _best_effort_teardown(terminal_id, registry)
        raise

    # Extract the last agent message via the provider-specific path (mirrors
    # how the handoff caller obtained output: get_output in LAST mode runs the
    # provider's extract_last_message_from_script under the hood). This does a
    # blocking tmux capture-pane plus regex extraction over the scrollback —
    # potentially seconds for a large transcript — so run it off the loop.
    # Clean up a terminal owned by this call if output extraction fails. Reused
    # terminals remain owned by the caller.
    try:
        last_message = await asyncio.to_thread(
            terminal_service.get_output, terminal_id, OutputMode.LAST
        )
    except BaseException:
        if teardown and created_here:
            await _best_effort_teardown(terminal_id, registry)
        raise

    result = AgentStepResult(
        terminal_id=terminal_id,
        last_message=last_message,
        status=TerminalStatus.COMPLETED,
    )

    if teardown and created_here:
        await _best_effort_teardown(terminal_id, registry)

    return result


async def _best_effort_teardown(terminal_id: str, registry: Optional[PluginRegistry]) -> None:
    """Exit-then-delete a terminal this call created — best-effort (never raises).

    Mirrors the old handoff lifecycle: send the provider's graceful exit command
    first, THEN delete. A failure in either step is logged and swallowed — it must
    never turn a settled step (success OR cancellation) into a failure. Shared by
    the success teardown and the cancellation path (issue #409b) so a cancelled
    step reclaims its terminal exactly the way a successful one does.
    """
    try:
        # Graceful CLI shutdown before kill_window (e.g. "/exit" for Claude Code,
        # C-d for others). Off the loop: exit_terminal_cli is blocking tmux I/O.
        await asyncio.to_thread(terminal_service.exit_terminal_cli, terminal_id)
    except (
        Exception
    ) as exc:  # noqa: BLE001 — graceful exit is best-effort; the step already settled
        logger.warning(
            "run_agent_step: failed to send graceful exit to terminal %s " "before teardown: %s",
            terminal_id,
            exc,
        )
    try:
        # Thread the registry so post_kill_terminal plugin hooks dispatch
        # (parity with the DELETE endpoint); None = no hooks (engine path).
        # Off the loop: delete_terminal does blocking tmux kills, a full-history
        # scrollback snapshot, and DB writes — the exact teardown that wedged the
        # server in issue #382.
        await asyncio.to_thread(terminal_service.delete_terminal, terminal_id, registry=registry)
    except Exception as exc:  # noqa: BLE001 — teardown is best-effort; the step already settled
        logger.warning(
            "run_agent_step: failed to tear down terminal %s after settle: %s",
            terminal_id,
            exc,
        )
