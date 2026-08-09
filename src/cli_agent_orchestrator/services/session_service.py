"""Session service for session-level operations.

This module provides session management functionality for CAO, where a "session"
corresponds to a tmux session that may contain multiple terminal windows (agents).

Session Hierarchy:
- Session: A tmux session (e.g., "cao-my-project")
  - Terminal: A tmux window within the session (e.g., "developer-abc123")
    - Provider: The CLI agent running in the terminal (e.g., KiroCliProvider)

Key Operations:
- list_sessions(): Get all CAO-managed sessions (filtered by SESSION_PREFIX)
- get_session(): Get session details including all terminal metadata
- delete_session(): Clean up session, providers, database records, and tmux session

Session Lifecycle:
1. create_terminal() with new_session=True creates a new tmux session
2. Additional terminals are added via create_terminal() with new_session=False
3. delete_session() removes the entire session and all contained terminals
"""

import logging
from typing import Dict, List, Optional

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.clients.database import list_terminals_by_session
from cli_agent_orchestrator.constants import SESSION_PREFIX
from cli_agent_orchestrator.models.terminal import Terminal
from cli_agent_orchestrator.plugins import (
    PluginRegistry,
    PostCreateSessionEvent,
    PostKillSessionEvent,
)
from cli_agent_orchestrator.services.plugin_dispatch import dispatch_plugin_event
from cli_agent_orchestrator.services.session_env import clear_session_env
from cli_agent_orchestrator.services.stable_agent_roster import ROLE_SUPERVISOR
from cli_agent_orchestrator.services.terminal_service import create_terminal
from cli_agent_orchestrator.utils.agent_profiles import resolve_provider

logger = logging.getLogger(__name__)


class SessionStopRefused(RuntimeError):
    """A zero-effect precondition refused the stop before any mutation.

    No pane was collected and no lifecycle row changed (e.g. an open callback
    recovery). Distinct from a partial collection so the API boundary can map
    it to a retryable conflict rather than an opaque failure.
    """


class SessionStopPartial(RuntimeError):
    """Some panes refused collection after the lifecycle row was already stopped.

    The row is preserved and a retry converges, so this is a visible, recoverable
    state — not a silent half-teardown. Carries the IDs that were collected and
    the structured per-terminal errors for server-side logging; only bounded,
    redacted guidance crosses the HTTP boundary.
    """

    def __init__(
        self,
        message: str,
        *,
        collected_terminal_ids: List[str],
        errors: List[Dict],
    ) -> None:
        super().__init__(message)
        self.collected_terminal_ids = list(collected_terminal_ids)
        self.errors = list(errors)


async def create_session(
    provider: str | None,
    agent_profile: str,
    session_name: str | None = None,
    working_directory: str | None = None,
    allowed_tools: list[str] | None = None,
    registry: PluginRegistry | None = None,
    env_vars: dict[str, str] | None = None,
) -> Terminal:
    """Create a new session by creating its initial terminal.

    ``env_vars`` are operator-forwarded env vars from ``cao launch --env``.
    They are persisted on the session record so every worker spawned later
    in the same session inherits them. See issue #248.

    A **stopped** name is refused. Stopping records what the session would
    restore to and, in time, what to relaunch; a new campaign taking that
    name silently inherits all of it, and a later resume would relaunch the
    wrong workers against the wrong provider sessions. The refusal is the
    cheap half of that defence, and it matters most for reserved reusable
    names — a repair session on a fixed name is guaranteed to hit this.
    """
    if session_name:
        from cli_agent_orchestrator.services import session_lifecycle

        declared = session_lifecycle.describe(session_name)
        if declared["lifecycle"] == session_lifecycle.STOPPED:
            raise ValueError(
                f"session {session_name!r} is stopped and still holds what a resume would "
                f"restore ({declared['restore_to']!r}); delete the session to release the "
                "name, or pick another"
            )

    if provider is None:
        resolved_provider = resolve_provider(agent_profile, fallback_provider="kiro_cli")
    else:
        resolved_provider = provider

    terminal = await create_terminal(
        provider=resolved_provider,
        agent_profile=agent_profile,
        session_name=session_name,
        new_session=True,
        working_directory=working_directory,
        allowed_tools=allowed_tools,
        registry=registry,
        env_vars=env_vars,
        # M3-A: session creation owns the initial supervisor role and
        # passes it explicitly — role is launch truth, never a
        # profile-name heuristic.
        stable_agent_role=ROLE_SUPERVISOR,
    )
    dispatch_plugin_event(
        registry,
        "post_create_session",
        PostCreateSessionEvent(
            session_id=terminal.session_name,
            session_name=terminal.session_name,
        ),
    )
    return terminal


def list_sessions() -> List[Dict]:
    """List all sessions from tmux."""
    try:
        tmux_sessions = get_backend().list_sessions()
        return [s for s in tmux_sessions if s["id"].startswith(SESSION_PREFIX)]
    except Exception as e:
        logger.error(f"Failed to list sessions: {e}")
        return []


def get_session(session_name: str) -> Dict:
    """Get session with terminals."""
    try:
        if not get_backend().session_exists(session_name):
            raise ValueError(f"Session '{session_name}' not found")

        tmux_sessions = get_backend().list_sessions()
        session_data = next((s for s in tmux_sessions if s["id"] == session_name), None)

        if not session_data:
            raise ValueError(f"Session '{session_name}' not found")

        # Read through the projection, which is the one authority on what a
        # terminal is: it observes liveness rather than trusting the stored
        # row, reports a lifecycle instead of a provider status for a pane
        # that no longer resolves, and covers both protocol vintages.
        #
        # This route is what the dashboard and ``conduct status`` read. While
        # it returned raw rows, a terminal whose window had been deleted
        # rendered as provider ``Unknown`` forever — indistinguishable from a
        # healthy worker awaiting detection — and a managed v2 worker
        # appeared in neither view, because its row lives in a separate
        # table. Meanwhile ``cao session status`` *was* projected, so the two
        # human views disagreed by construction.
        #
        # The projection derives the provider status itself, for a live pane
        # only, so nothing is enriched here.
        #
        # Deliberately not applied to ``delete_session``, the watchdog or
        # cleanup: those are the machine paths the v2 store's write/consume
        # isolation is about, and they must keep seeing v1 rows only. The
        # boundary this crosses is *human visibility*, which was never the
        # thing being isolated.
        from cli_agent_orchestrator.services import terminal_projection

        terminals = terminal_projection.project_session(session_name)
        return {"session": session_data, "terminals": terminals}

    except Exception as e:
        logger.error(f"Failed to get session {session_name}: {e}")
        raise


def delete_session(session_name: str, registry: PluginRegistry | None = None) -> Dict:
    """Delete session and cleanup.

    Returns:
        Dict with 'deleted' (list of deleted session names) and 'errors' (list of error dicts).
    """
    result: Dict = {"deleted": [], "errors": []}
    session_claim = None
    try:
        from cli_agent_orchestrator.services import callback_recovery
        from cli_agent_orchestrator.services import session_lifecycle as sl
        from cli_agent_orchestrator.services import terminal_service

        # Canonicalize before every physical or durable effect. Otherwise a
        # bare name can tear down an absent session while forget() alone
        # releases the lifecycle row belonging to the real prefixed session.
        session_name = sl.normalise_session_name(session_name)

        backend = get_backend()
        session_claim = callback_recovery.session_lifecycle_claim(
            type(backend).__name__, session_name
        )
        session_claim.__enter__()
        # This final existence observation, snapshot, physical teardown, and
        # durable environment cleanup are one session/workspace lifecycle.
        session_alive = backend.session_exists(session_name)
        terminals = list_terminals_by_session(session_name)

        # Clean up each terminal (snapshot, kill window, FIFO reader,
        # status buffer, provider, DB) via the event-driven teardown path.
        terminal_errors = []
        claim_keys = callback_recovery.terminal_lifecycle_claim_set(*terminals)
        with callback_recovery.generation_lifecycle_claims(claim_keys):
            for terminal in terminals:
                if callback_recovery.terminal_has_open_recovery(
                    terminal["id"], terminal.get("generation")
                ):
                    terminal_errors.append(
                        {
                            "terminal_id": terminal["id"],
                            "detail": "open callback recovery",
                        }
                    )

            if not terminal_errors:
                for terminal in terminals:
                    try:
                        generation = terminal.get("generation")
                        kwargs = {}
                        if generation:
                            kwargs = {
                                "expected_generation": generation,
                                "expected_session": terminal.get("tmux_session") or session_name,
                            }
                        terminal_service.delete_terminal(
                            terminal["id"], registry=registry, **kwargs
                        )
                    except Exception as e:
                        logger.warning(f"Failed to cleanup terminal {terminal['id']}: {e}")
                        terminal_errors.append({"terminal_id": terminal["id"], "detail": str(e)})

            if terminal_errors:
                raise RuntimeError(
                    "session deletion held because terminal cleanup failed: " f"{terminal_errors}"
                )

        # A deleted session must not leave its declaration behind. A later
        # session taking the name would inherit a stranger's `complete` or
        # `paused` — and both of those are marshal suppressors, so a
        # brand-new live campaign would start out invisible to the thing
        # whose job is to notice it wedging.
        try:
            sl.forget(session_name)
        except Exception as exc:  # noqa: BLE001 - deletion must still complete
            logger.warning("Could not forget the declared state of %s: %s", session_name, exc)

        # Re-check under the session claim: a concurrent create cannot add a
        # replacement window between this observation and kill_session.
        session_alive = backend.session_exists(session_name)
        # Kill backend session only if it still exists
        if session_alive:
            backend.kill_session(session_name)

        # Drop the per-session forwarded-env mapping (issue #248). Safe
        # even when no vars were forwarded — the helper is a no-op then.
        # Strict (cond-0050): a delete that cannot complete durably raises
        # rather than leaving a stale row behind to be silently inherited.
        clear_session_env(session_name)

        result["deleted"].append(session_name)
        logger.info(f"Deleted session: {session_name}")
        dispatch_plugin_event(
            registry,
            "post_kill_session",
            PostKillSessionEvent(session_id=session_name, session_name=session_name),
        )
        return result

    except Exception as e:
        logger.error(f"Failed to delete session {session_name}: {e}")
        raise
    finally:
        if session_claim is not None:
            session_claim.__exit__(None, None, None)


def stop_session(
    session_name: str,
    *,
    declared_by: str,
    restore_to: Optional[str] = None,
    note: Optional[str] = None,
    expected_epoch: Optional[int] = None,
    registry: PluginRegistry | None = None,
    archived: bool = False,
) -> Dict:
    """Collect a session's panes while preserving its declared stopped state.

    The row-preserving counterpart to ``delete_session``. A lifecycle stop
    must both tear the fleet down and keep the truth a resume needs: the
    lifecycle row stays in ``stopped`` with its ``restore_to`` target, the
    forwarded environment survives so a resume relaunches against it, and the
    per-terminal snapshots/recovery artifacts survive because collection goes
    through the same event-driven ``delete_terminal`` path deletion does.

    The order is load-bearing:

    * the session-lifecycle claim is held across the whole operation, so a
      concurrent create cannot add a window between the stopped check and the
      teardown;
    * admission (an open callback recovery) refuses *before* anything is
      written or collected — collecting a terminal mid-recovery would lose
      the one-shot refusal the recovery is adjudicating, and a stop recorded
      while one is open would be a false state;
    * the stopped declaration is written *before* any pane is collected, so a
      write or admission failure deletes nothing, and a fully collected fleet
      can never be left declared working;
    * on a mid-collection failure the row is already stopped, so the
      divergence is visible (not silent) and a retry re-collects what remains
      — re-stopping is idempotent and keeps ``restore_to``.

    Deliberately not symmetric with deletion: this never calls ``forget`` and
    never clears the forwarded env. Releasing the name is the destructive
    ``DELETE`` path's job, kept separate so an ordinary stop can never become
    an accidental cleanup.
    """
    from cli_agent_orchestrator.services import callback_recovery
    from cli_agent_orchestrator.services import session_lifecycle as sl
    from cli_agent_orchestrator.services import terminal_service

    # cond-0221: canonicalize once, before any physical or admission effect, so
    # a bare name (`repair`) reaches the physical claim, the terminal listing,
    # the exact-generation deletion's expected_session, backend existence/kill,
    # and the durable row as `cao-repair`. It also makes the lifecycle write
    # claim taken here and the one taken inside ``sl.stop()``/``set_archived()``
    # (which normalize internally) the same key — re-entrant — rather than a
    # raw-vs-canonical pair that inverts the canonical lock order.
    session_name = sl.normalise_session_name(session_name)

    backend = get_backend()
    collected: List[str] = []
    record: Dict = {}
    with callback_recovery.session_lifecycle_claim(type(backend).__name__, session_name):
        # cond-0221: hold the lifecycle write claim across admission, the stop
        # write, and collection. Every lifecycle mutation takes it, so a
        # competing ``declare``/``set_kind``/… cannot commit during the stop —
        # it waits, then sees ``stopped`` and is refused by the transition.
        # Acquired before the generation claims: the write claim sorts after the
        # physical session-workspace claim and before terminal-generation claims
        # (physical < write < generation), and ``sl.stop()``'s own ``_write``
        # takes it re-entrantly via the thread-local, so it does not self-deadlock.
        with callback_recovery.session_lifecycle_write_claim(session_name):
            terminals = list_terminals_by_session(session_name)
            claim_keys = callback_recovery.terminal_lifecycle_claim_set(*terminals)
            with callback_recovery.generation_lifecycle_claims(claim_keys):
                # Admission: an open callback recovery blocks both collection and
                # the declaration. Checked before the lifecycle write so a refusal
                # leaves no false stopped state, and before any deletion so it
                # collects nothing.
                for terminal in terminals:
                    if callback_recovery.terminal_has_open_recovery(
                        terminal["id"], terminal.get("generation")
                    ):
                        raise SessionStopRefused(
                            f"session stop held because terminal {terminal['id']} has an "
                            "open callback-recovery operation; collection is refused until "
                            "callback completion or a terminal refusal/manual disposition"
                        )

                # Record the stop (preserving the row and restore_to) before any
                # collection. A write failure here raises and collects nothing.
                if archived:
                    record = sl.set_archived(
                        session_name,
                        True,
                        declared_by=declared_by,
                        note=note,
                        expected_epoch=expected_epoch,
                    )
                else:
                    record = sl.stop(
                        session_name,
                        declared_by=declared_by,
                        restore_to=restore_to,
                        note=note,
                        expected_epoch=expected_epoch,
                    )

                # Collect the panes via the same event-driven teardown deletion
                # uses: delete_terminal snapshots before killing, so recovery
                # artifacts survive. The forwarded env is intentionally left.
                errors = []
                for terminal in terminals:
                    try:
                        generation = terminal.get("generation")
                        kwargs = {}
                        if generation:
                            kwargs = {
                                "expected_generation": generation,
                                "expected_session": terminal.get("tmux_session") or session_name,
                            }
                        terminal_service.delete_terminal(
                            terminal["id"], registry=registry, **kwargs
                        )
                        collected.append(terminal["id"])
                    except Exception as e:  # noqa: BLE001 - one pane must not erase the rest
                        logger.warning(
                            "Failed to collect terminal %s during stop of %s: %s",
                            terminal["id"],
                            session_name,
                            e,
                        )
                        errors.append({"terminal_id": terminal["id"], "detail": str(e)})
                if errors:
                    # The row is already stopped and the env is preserved, so the
                    # divergence is visible and a retry converges. Surface the
                    # structured detail for server-side logging; the HTTP layer
                    # exposes only bounded, redacted guidance.
                    raise SessionStopPartial(
                        "session stop partially collected; lifecycle preserved; " "retry converges",
                        collected_terminal_ids=collected,
                        errors=errors,
                    )

            # Kill the enclosing backend session if anything remains. Per-window
            # teardown already removed each pane; this clears empty shells.
            if backend.session_exists(session_name):
                backend.kill_session(session_name)

    return {**record, "collected_terminal_ids": collected, "errors": []}
