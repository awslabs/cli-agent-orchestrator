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

import logging
from typing import Optional

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.models.workflow import AgentStepResult
from cli_agent_orchestrator.services import terminal_service
from cli_agent_orchestrator.services.status_monitor import status_monitor
from cli_agent_orchestrator.services.terminal_service import OutputMode
from cli_agent_orchestrator.utils.terminal import wait_until_status

logger = logging.getLogger(__name__)

# Ready states a freshly created terminal may settle into before it can accept
# input (mirrors the handoff readiness wait): some providers process their
# system prompt as the first turn and reach COMPLETED without a bare IDLE.
_READY_STATES = {TerminalStatus.IDLE, TerminalStatus.COMPLETED}

# Generous readiness timeout: provider init (shell warm-up + CLI startup + MCP
# registration + auth) can take ~15-45s. Matches the handoff caller's 120s.
DEFAULT_READY_TIMEOUT = 120.0


class StepExecutionError(Exception):
    """A step failed to complete successfully.

    Raised for a readiness/completion timeout or a terminal that reached
    ``TerminalStatus.ERROR``. Narrow by design so the caller (engine) can map
    it to its retry policy and the API boundary can map it to an HTTPException.
    """


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
            None, ``terminal_service.create_terminal`` auto-generates one.
        reuse_terminal_id: Reuse an existing terminal instead of creating one.
            When set, the create + teardown steps are skipped (no pool; the
            caller owns the terminal's lifecycle).
        teardown: When True (default) and the terminal was created here, delete
            it after extraction. Ignored when ``reuse_terminal_id`` is set.
        timeout: Max seconds to wait for the step to reach COMPLETED.
        ready_timeout: Max seconds to wait for a freshly created terminal to be
            ready to accept input.
        working_directory: Optional working directory for a freshly created
            terminal (ignored when reusing a terminal).

    Returns:
        ``AgentStepResult`` with status COMPLETED — ONLY on success.

    Raises:
        StepExecutionError: readiness wait timed out, completion wait timed out,
            or the terminal reached ``TerminalStatus.ERROR``.
        ValueError / TimeoutError: propagated from ``terminal_service`` (e.g.
            terminal-create failure, unknown terminal) — surfaced, never swallowed.
    """
    created_here = reuse_terminal_id is None
    terminal_id = reuse_terminal_id

    if created_here:
        # create_terminal already runs provider.initialize() (which waits for
        # IDLE); a failure raises (ValueError/TimeoutError) and propagates.
        terminal = await terminal_service.create_terminal(
            provider, agent, session_name=session_name, working_directory=working_directory
        )
        terminal_id = terminal.id

        # Secondary in-process readiness wait: provider.initialize() can return a
        # false-positive on the shell prompt before the CLI is truly ready, so we
        # confirm a ready status before sending input (same guard handoff uses).
        ready = await wait_until_status(terminal_id, _READY_STATES, timeout=ready_timeout)
        if not ready:
            # Surface the live terminal so it can be inspected/cleaned up, then
            # fail fast. We do NOT auto-delete here: leaving the terminal lets
            # the caller decide (handoff surfaces terminal_id on failure).
            raise StepExecutionError(
                f"terminal {terminal_id} did not reach a ready status within " f"{ready_timeout}s"
            )

    assert terminal_id is not None  # for type-checkers: set in both branches

    # Send the prompt (sync). Any failure raises and propagates.
    terminal_service.send_input(terminal_id, prompt)

    # Wait for completion — IN-PROCESS poll of status_monitor (NOT the
    # HTTP-polling wait_until_terminal_status, which would reintroduce the
    # self-loopback the single-seam rule forbids). False => timeout => raise.
    completed = await wait_until_status(terminal_id, TerminalStatus.COMPLETED, timeout=timeout)
    if not completed:
        # Distinguish a hard ERROR end-state from a plain timeout for the message.
        current = status_monitor.get_status(terminal_id)
        if current == TerminalStatus.ERROR:
            raise StepExecutionError(f"terminal {terminal_id} reached ERROR status")
        raise StepExecutionError(
            f"step on terminal {terminal_id} did not complete within {timeout}s"
        )

    # A terminal can reach a transient ERROR state that wait_until_status would
    # not see as COMPLETED, but defensively re-check before claiming success.
    final_status = status_monitor.get_status(terminal_id)
    if final_status == TerminalStatus.ERROR:
        raise StepExecutionError(f"terminal {terminal_id} reached ERROR status")

    # Extract the last agent message via the provider-specific path (mirrors
    # how the handoff caller obtained output: get_output in LAST mode runs the
    # provider's extract_last_message_from_script under the hood).
    last_message = terminal_service.get_output(terminal_id, OutputMode.LAST)

    result = AgentStepResult(
        terminal_id=terminal_id,
        last_message=last_message,
        status=TerminalStatus.COMPLETED,
    )

    if teardown and created_here:
        # Best-effort teardown: a delete failure must not turn a successful step
        # into a failure (the work is done and captured). Log it; never swallow
        # silently.
        try:
            terminal_service.delete_terminal(terminal_id)
        except Exception as exc:  # noqa: BLE001 — teardown is best-effort; step already succeeded
            logger.warning(
                "run_agent_step: failed to tear down terminal %s after success: %s",
                terminal_id,
                exc,
            )

    return result
