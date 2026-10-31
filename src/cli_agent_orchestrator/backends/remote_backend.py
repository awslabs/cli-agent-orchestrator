"""RemoteBackend — run terminals on execution-only runners (#745, opt-in).

Selected with ``terminal.backend = "remote"`` (``CAO_TERMINAL_BACKEND=remote``
or ``cao-server --terminal remote``). The central ``cao-server`` keeps every
responsibility it has today — API, database, orchestration, status detection,
inbox delivery — and only the terminal itself (tmux plus the provider CLI)
lives in another container, driven by a ``cao-runner`` that dialled in to
:class:`~cli_agent_orchestrator.runner.hub.RunnerHub`.

What deliberately does NOT change: the output/status pipeline. ``pipe_pane``
asks the runner to stream the pane's bytes, and the hub writes them into the
same server-local FIFO that ``FifoManager`` created for a tmux pane. The FIFO
reader, log writer, status monitor and inbox service run exactly as they do
locally, so there is one status implementation rather than a remote copy of it.

Provider files: providers write per-terminal files (system prompt, MCP config)
into the server's ``CAO_HOME_DIR/tmp`` and pass their paths on the command line
they type. Before any input reaches a remote pane, files named for that
terminal are staged into the runner's own tmp dir, and the server's tmp-dir
prefix in the typed text is rewritten to the runner's. No provider has to know
it is remote.

Explicitly unsupported (raises rather than misroutes): interactive attach from
the server host (``attach_session``, ``prepare_web_attach``), git worktrees,
relative or server-validated working directories, and providers outside
``supported_providers`` — their launch reads or probes the server's filesystem
and would report the server's state as the runner's.
"""

import base64
import logging
import os
import threading
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from cli_agent_orchestrator.backends.base import (
    TerminalBackend,
    TerminalBackendError,
    TerminalCleanupOutcome,
    TerminalCleanupResult,
)
from cli_agent_orchestrator.constants import CAO_HOME_DIR, TMUX_HISTORY_LIMIT
from cli_agent_orchestrator.runner.protocol import (
    MAX_STAGED_FILE_BYTES,
    RunnerError,
    RunnerOutcomeUnknown,
    RunnerUnavailable,
)

if TYPE_CHECKING:
    from cli_agent_orchestrator.runner.hub import RunnerHub

logger = logging.getLogger(__name__)

# Re-arming the output pipes a restarted runner lost: attempts, and the base
# delay between them (linear backoff).
_REARM_ATTEMPTS = 3
_REARM_RETRY_DELAY = 2.0

# Providers whose launch path has been traced end to end for remote execution:
# everything they need on the runner is either typed into the pane or staged
# from CAO_HOME_DIR/tmp. Others read or probe the server host (kiro's
# capability probe, opencode/copilot agent dirs, provider homes outside tmp)
# and are refused until they are taught otherwise.
DEFAULT_REMOTE_PROVIDERS = frozenset({"claude_code", "mock_cli"})

_UNSUPPORTED = (
    "is not supported by the remote terminal backend: the terminal runs on a "
    "cao-runner, not on the server host"
)


class RemoteBackend(TerminalBackend):
    """TerminalBackend that executes every operation on a connected runner."""

    def __init__(
        self, hub: Optional["RunnerHub"] = None, supported_providers: Optional[frozenset] = None
    ) -> None:
        self._hub = hub
        self.supported_providers = (
            supported_providers if supported_providers is not None else DEFAULT_REMOTE_PROVIDERS
        )
        self._tmp_dir = str(CAO_HOME_DIR / "tmp")
        # (runner_id, file name) -> (size, mtime_ns) last staged, so an
        # unchanged prompt is not re-sent with every keystroke batch.
        self._staged: Dict[tuple, tuple] = {}
        self._staged_lock = threading.Lock()
        # Per window: installing a pipe's sink and arming the runner is one
        # step. Two re-arms (a reconnect's and the FIFO watchdog's) must not
        # interleave, or the runner streams a pipe whose sink is already gone.
        self._pipe_locks: Dict[Tuple[str, str], threading.Lock] = {}
        self._pipe_locks_guard = threading.Lock()

    # --- Wiring ---

    def attach_hub(self, hub: "RunnerHub", *, restore: bool = True) -> None:
        """Use ``hub`` for every call; bind it to the registry and status detector.

        With ``restore`` (the server's case), the placements persisted in the
        terminal rows are seeded back as routes before any runner reconnects.
        """
        self._hub = hub
        hub.bind(
            ownership=_terminal_owned,
            relocate=_record_runner,
            reachability=_mark_reachability,
            rearm=self._rearm_pipes,
        )
        if restore:
            from cli_agent_orchestrator.clients.database import list_runner_placements

            hub.seed_placements(list_runner_placements())

    @property
    def hub(self) -> "RunnerHub":
        if self._hub is None:
            raise TerminalBackendError(
                "remote terminal backend has no runner hub in this process "
                "(only cao-server hosts one)"
            )
        return self._hub

    # --- Capability hooks consulted by terminal_service ---

    @property
    def resolves_paths_locally(self) -> bool:
        return False

    def unsupported_provider_reason(self, provider: str) -> Optional[str]:
        if provider in self.supported_providers:
            return None
        return (
            f"Provider '{provider}' {_UNSUPPORTED}. Supported: "
            f"{', '.join(sorted(self.supported_providers))}"
        )

    def agent_server_env(self) -> Optional[Dict[str, str]]:
        """The address an agent ON A RUNNER uses to reach this server.

        Forwarded into the agent's MCP config in place of this server's own
        bind address (see ``utils.mcp_resolution.cao_server_env``). Configured
        with ``CAO_RUNNER_AGENT_API_HOST`` / ``CAO_RUNNER_AGENT_API_PORT``,
        typically the server's in-cluster Service name.
        """
        env: Dict[str, str] = {}
        host = os.environ.get("CAO_RUNNER_AGENT_API_HOST")
        port = os.environ.get("CAO_RUNNER_AGENT_API_PORT")
        if host:
            env["CAO_API_HOST"] = host
        if port:
            env["CAO_API_PORT"] = port
        return env

    def _rearm_pipes(self, lost: List[Tuple[str, str, str]]) -> None:
        """Re-open each lost pipe, retrying briefly.

        A window still failing after the retries is left to the FIFO liveness
        watchdog, which re-arms a pane whose content moves while its FIFO stays
        silent.
        """
        pending = list(lost)
        for attempt in range(_REARM_ATTEMPTS):
            failed: List[Tuple[str, str, str]] = []
            for session_name, window_name, fifo_path in pending:
                try:
                    self.pipe_pane(session_name, window_name, fifo_path)
                except Exception as exc:  # noqa: BLE001 - one window must not stop the rest
                    logger.warning(
                        f"re-arming output of {session_name}:{window_name} failed "
                        f"(attempt {attempt + 1}/{_REARM_ATTEMPTS}): {exc}"
                    )
                    failed.append((session_name, window_name, fifo_path))
            if not failed:
                return
            pending = failed
            time.sleep(_REARM_RETRY_DELAY * (attempt + 1))

    def placement_of(self, terminal_id: str) -> Optional[str]:
        located = self._hub.route_for_terminal(terminal_id) if self._hub is not None else None
        return located[0] if located else None

    # --- Helpers ---

    def _routed(self, session_name: str, window_name: str) -> Tuple[str, Optional[str]]:
        route = self.hub.route(session_name, window_name)
        if route is None:
            raise ValueError(f"Window '{window_name}' not found in session '{session_name}'")
        return route

    @staticmethod
    def _window_args(
        session_name: str, window_name: str, terminal_id: Optional[str], **extra: Any
    ) -> Dict[str, Any]:
        """Arguments of a window-scoped op, guarded by the terminal it targets.

        Window names are reusable: a window can be killed and a new terminal
        created under the same name while an earlier call is still in flight.
        The runner refuses an op whose ``expected_terminal_id`` no longer owns
        that window, so a late call can never land on its successor.
        """
        args: Dict[str, Any] = {"session_name": session_name, "window_name": window_name}
        if terminal_id:
            args["expected_terminal_id"] = terminal_id
        args.update(extra)
        return args

    def _forget_terminal_files(self, runner_id: str, terminal_id: Optional[str]) -> None:
        if not terminal_id:
            return
        with self._staged_lock:
            for key in [
                k for k in self._staged if k[0] == runner_id and k[1].startswith(terminal_id)
            ]:
                del self._staged[key]

    def _stage_files(self, runner_id: str, terminal_id: Optional[str]) -> None:
        if not terminal_id or not os.path.isdir(self._tmp_dir):
            return
        files: List[Dict[str, str]] = []
        signatures: Dict[tuple, tuple] = {}
        for name in sorted(os.listdir(self._tmp_dir)):
            if not name.startswith(terminal_id):
                continue
            path = os.path.join(self._tmp_dir, name)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            st = os.stat(path)
            signature = (st.st_size, st.st_mtime_ns)
            with self._staged_lock:
                unchanged = self._staged.get((runner_id, name)) == signature
            if unchanged:
                continue
            if st.st_size > MAX_STAGED_FILE_BYTES:
                raise TerminalBackendError(
                    f"provider file {name} is {st.st_size} bytes; refusing to stage it"
                )
            with open(path, "rb") as fh:
                files.append({"name": name, "data": base64.b64encode(fh.read()).decode()})
            signatures[(runner_id, name)] = signature
        if files:
            self.hub.call(runner_id, "stage_files", {"files": files})
            with self._staged_lock:
                self._staged.update(signatures)

    def _rewrite_paths(self, runner_id: str, text: str) -> str:
        info = self.hub.runner_info(runner_id)
        if info is None or info.tmp_dir == self._tmp_dir:
            return text
        return text.replace(self._tmp_dir + "/", info.tmp_dir.rstrip("/") + "/")

    def _create(
        self,
        op: str,
        session_name: str,
        window_name: str,
        terminal_id: str,
        working_directory: Optional[str],
        extra_env: Optional[Dict[str, str]],
        *,
        new_session: bool,
        window_shell: Optional[str] = None,
    ) -> str:
        if working_directory is not None and not os.path.isabs(working_directory):
            raise ValueError(
                f"Working directory must be absolute for a remote terminal: {working_directory}"
            )
        hub = self.hub
        runner_id = hub.reserve(session_name, new_session=new_session)
        hub.note_placing(terminal_id)
        try:
            args: Dict[str, Any] = {
                "session_name": session_name,
                "window_name": window_name,
                "terminal_id": terminal_id,
                "working_directory": working_directory,
                "extra_env": dict(extra_env or {}),
            }
            if op == "create_window":
                args["window_shell"] = window_shell
                # This runner may host other windows of the session, or none:
                # a session is a logical group that can span runners.
                args["create_session_if_missing"] = True
            try:
                actual = hub.call(runner_id, op, args)
            except RunnerOutcomeUnknown:
                # The pane may exist on the runner with no route and, once the
                # caller rolls back, no registry row. Reap it when it is seen.
                hub.mark_for_reaping(runner_id, terminal_id)
                raise
            if not isinstance(actual, str) or not actual:
                raise TerminalBackendError(f"runner {runner_id} returned no window name")
            if not hub.add_route(session_name, actual, runner_id, terminal_id):
                # The runner reported another terminal in that window since it
                # replied: ours is unconfirmed, so treat it as a lost create.
                hub.mark_for_reaping(runner_id, terminal_id)
                raise RunnerOutcomeUnknown(
                    f"runner {runner_id}: {session_name}:{actual} was re-routed to another "
                    f"terminal before {terminal_id} could be routed; outcome unknown"
                )
            logger.info(
                f"terminal {terminal_id} placed on runner {runner_id} ({session_name}:{actual})"
            )
            return actual
        finally:
            hub.release_reservation(runner_id)

    # --- Session lifecycle ---

    def create_session(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        working_directory: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> str:
        return self._create(
            "create_session",
            session_name,
            window_name,
            terminal_id,
            working_directory,
            extra_env,
            new_session=True,
        )

    def session_exists(self, session_name: str) -> bool:
        try:
            return self.session_exists_strict(session_name)
        except TerminalBackendError as exc:
            # The base contract collapses an unanswerable lookup to False, but
            # here the usual cause is a routed session whose runner is out of
            # reach — and "False" would send a create for that name to another
            # runner while the original is still alive. A routed session is
            # therefore reported as existing; only an unrouted one is "absent".
            routed = bool(self.hub.session_runners(session_name))
            logger.warning(
                f"session_exists({session_name}) could not be answered ({exc}); "
                f"assuming {'present' if routed else 'absent'}"
            )
            return routed

    def session_exists_strict(self, session_name: str) -> bool:
        hub = self.hub
        runners = hub.session_runners(session_name)
        if not runners:
            if hub.can_prove_unrouted_absence():
                return False
            raise RunnerUnavailable(
                f"cannot prove session '{session_name}' is absent: a known runner has not "
                "reported yet"
            )
        for runner_id in runners:
            if hub.call(runner_id, "session_exists_strict", {"session_name": session_name}):
                return True
        return False

    def list_sessions(self) -> List[Dict[str, str]]:
        hub = self.hub
        merged: Dict[str, Dict[str, str]] = {}
        for runner_id in hub.connected_runners():
            try:
                sessions = hub.call(runner_id, "list_sessions", {})
            except TerminalBackendError as exc:
                logger.warning(f"list_sessions on runner {runner_id} failed: {exc}")
                continue
            for entry in sessions or []:
                if isinstance(entry, dict) and entry.get("name"):
                    merged.setdefault(entry["name"], entry)
        # A session routed to a runner that is out of reach still exists (see
        # session_exists); leaving it out would turn a transient disconnect into
        # a 404 for a session whose registry rows and route are intact.
        for session_name in hub.routed_sessions():
            merged.setdefault(
                session_name, {"id": session_name, "name": session_name, "status": "detached"}
            )
        return list(merged.values())

    def kill_session(self, session_name: str) -> bool:
        hub = self.hub
        runners = hub.session_runners(session_name)
        if not runners:
            return False
        all_gone = True
        for runner_id in runners:
            try:
                killed = bool(hub.call(runner_id, "kill_session", {"session_name": session_name}))
            except (RunnerUnavailable, RunnerOutcomeUnknown) as exc:
                # Unconfirmed: never report it gone, and make sure the panes
                # die when the runner is next heard from instead of running on
                # unowned once the caller drops the session's rows.
                logger.warning(f"kill_session({session_name}) on runner {runner_id}: {exc}")
                for route_runner, _window, terminal_id in hub.session_routes(session_name):
                    if route_runner == runner_id and terminal_id:
                        hub.mark_for_reaping(runner_id, terminal_id)
                killed = False
            if killed:
                hub.drop_session_routes(session_name, runner_id)
            else:
                all_gone = False
        return all_gone

    # --- Window lifecycle ---

    def create_window(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        working_directory: Optional[str] = None,
        window_shell: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> str:
        return self._create(
            "create_window",
            session_name,
            window_name,
            terminal_id,
            working_directory,
            extra_env,
            new_session=False,
            window_shell=window_shell,
        )

    def kill_window(self, session_name: str, window_name: str) -> bool:
        route = self.hub.route(session_name, window_name)
        if route is None:
            return False
        runner_id, terminal_id = route
        try:
            killed = bool(
                self.hub.call(
                    runner_id,
                    "kill_window",
                    self._window_args(session_name, window_name, terminal_id),
                )
            )
        except ValueError:
            # The runner's guard: this window no longer belongs to the routed
            # terminal (or is gone), so there is nothing of it left to kill.
            self.hub.drop_route(session_name, window_name, runner_id, terminal_id)
            self._forget_terminal_files(runner_id, terminal_id)
            return False
        except (RunnerUnavailable, RunnerOutcomeUnknown) as exc:
            # Teardown logs a failed kill and still drops the terminal's row,
            # so an unconfirmed kill must not leave the agent running unowned:
            # it is reaped the next time its runner reports it.
            logger.warning(
                f"kill_window({session_name}:{window_name}) on runner {runner_id}: {exc}"
            )
            if terminal_id:
                self.hub.mark_for_reaping(runner_id, terminal_id)
            return False
        if killed:
            self.hub.drop_route(session_name, window_name, runner_id, terminal_id)
            self._forget_terminal_files(runner_id, terminal_id)
        return killed

    def cleanup_terminal_exact(
        self,
        terminal_id: str,
        session_name: Optional[str] = None,
        window_name: Optional[str] = None,
        *,
        close: bool = True,
    ) -> TerminalCleanupResult:
        hub = self.hub
        located = hub.route_for_terminal(terminal_id)
        if not located and hub.is_reaping(terminal_id):
            return TerminalCleanupResult(
                TerminalCleanupOutcome.UNKNOWN, "queued for reaping on a runner not yet heard from"
            )
        candidates = [located[0]] if located else hub.connected_runners()
        if not located and not hub.can_prove_unrouted_absence():
            # A runner that has not reported to this process yet might be
            # hosting it; never call that absence.
            return TerminalCleanupResult(
                TerminalCleanupOutcome.UNKNOWN, "a known runner has not reported yet"
            )
        outcomes: List[TerminalCleanupResult] = []
        for runner_id in candidates:
            try:
                value = hub.call(
                    runner_id,
                    "cleanup_terminal_exact",
                    {
                        "terminal_id": terminal_id,
                        "session_name": session_name,
                        "window_name": window_name,
                        "close": close,
                    },
                )
                outcome = TerminalCleanupOutcome(value["outcome"])
                result = TerminalCleanupResult(
                    outcome, f"runner {runner_id}: {value.get('detail', '')}"
                )
            except (TerminalBackendError, KeyError, TypeError, ValueError) as exc:
                result = TerminalCleanupResult(
                    TerminalCleanupOutcome.UNKNOWN, f"runner {runner_id}: {exc}"
                )
            if result.reclaimed and located:
                hub.drop_route(located[1], located[2], runner_id, terminal_id)
                self._forget_terminal_files(runner_id, terminal_id)
            outcomes.append(result)
        if not outcomes:
            return TerminalCleanupResult(TerminalCleanupOutcome.UNKNOWN, "no runner connected")
        # Most informative verdict wins; ABSENT only if every runner proved it.
        for wanted in (
            TerminalCleanupOutcome.DELETED,
            TerminalCleanupOutcome.STILL_PRESENT,
            TerminalCleanupOutcome.UNKNOWN,
        ):
            for result in outcomes:
                if result.outcome is wanted:
                    return result
        return outcomes[0]

    # --- Input ---

    def send_keys(
        self,
        session_name: str,
        window_name: str,
        keys: str,
        enter_count: int = 1,
        force_bracketed_paste: bool = False,
        submit_delay: float = 0.3,
    ) -> None:
        runner_id, terminal_id = self._routed(session_name, window_name)
        self._stage_files(runner_id, terminal_id)
        self.hub.call(
            runner_id,
            "send_keys",
            self._window_args(
                session_name,
                window_name,
                terminal_id,
                keys=self._rewrite_paths(runner_id, keys),
                enter_count=enter_count,
                force_bracketed_paste=force_bracketed_paste,
                submit_delay=submit_delay,
            ),
        )

    def send_special_key(self, session_name: str, window_name: str, key: str) -> None:
        runner_id, terminal_id = self._routed(session_name, window_name)
        self.hub.call(
            runner_id,
            "send_special_key",
            self._window_args(session_name, window_name, terminal_id, key=key),
        )

    # --- Output ---

    @property
    def max_history_lines(self) -> int:
        # Runners drive tmux with CAO's own TmuxClient, which raises every
        # pane's history-limit to this floor.
        return TMUX_HISTORY_LIMIT

    def get_history(
        self,
        session_name: str,
        window_name: str,
        tail_lines: Optional[int] = None,
        strip_escapes: bool = False,
        full_history: bool = False,
        visible_only: bool = False,
        join_wrapped: bool = False,
    ) -> str:
        runner_id, terminal_id = self._routed(session_name, window_name)
        value = self.hub.call(
            runner_id,
            "get_history",
            self._window_args(
                session_name,
                window_name,
                terminal_id,
                tail_lines=tail_lines,
                strip_escapes=strip_escapes,
                full_history=full_history,
                visible_only=visible_only,
                join_wrapped=join_wrapped,
            ),
        )
        return value if isinstance(value, str) else ""

    def get_pane_working_directory(self, session_name: str, window_name: str) -> Optional[str]:
        route = self.hub.route(session_name, window_name)
        if route is None:
            return None
        try:
            value = self.hub.call(
                route[0],
                "get_pane_working_directory",
                self._window_args(session_name, window_name, route[1]),
            )
            return value if isinstance(value, str) else None
        except RunnerError:
            # Unreachable or unknown is not "no directory": the caller (e.g.
            # GET /terminals/{id}/working-directory) must see 503/504/502.
            raise
        except TerminalBackendError as exc:
            logger.warning(
                f"get_pane_working_directory({session_name}:{window_name}) failed: {exc}"
            )
            return None

    def get_pane_current_command(self, session_name: str, window_name: str) -> Optional[str]:
        route = self.hub.route(session_name, window_name)
        if route is None:
            return None
        try:
            value = self.hub.call(
                route[0],
                "get_pane_current_command",
                self._window_args(session_name, window_name, route[1]),
            )
            return value if isinstance(value, str) else None
        except TerminalBackendError as exc:
            logger.warning(f"get_pane_current_command({session_name}:{window_name}) failed: {exc}")
            return None

    # --- Attach ---

    def attach_session(self, session_name: str) -> None:
        raise TerminalBackendError(f"Attaching to '{session_name}' {_UNSUPPORTED}")

    def prepare_web_attach(self, session_name: str, window_name: str) -> List[str]:
        raise TerminalBackendError(
            f"Browser attach to '{session_name}:{window_name}' {_UNSUPPORTED}"
        )

    # --- Pipe-pane ---

    def _pipe_lock(self, session_name: str, window_name: str) -> threading.Lock:
        with self._pipe_locks_guard:
            return self._pipe_locks.setdefault((session_name, window_name), threading.Lock())

    def pipe_pane(self, session_name: str, window_name: str, file_path: str) -> None:
        with self._pipe_lock(session_name, window_name):
            self._pipe_pane_locked(session_name, window_name, file_path)

    def _pipe_pane_locked(self, session_name: str, window_name: str, file_path: str) -> None:
        runner_id, terminal_id = self._routed(session_name, window_name)
        # Sink first: output can start arriving the moment the runner arms.
        pipe_id = self.hub.open_pipe(runner_id, session_name, window_name, file_path, terminal_id)
        try:
            self.hub.call(
                runner_id,
                "pipe_pane",
                self._window_args(session_name, window_name, terminal_id, pipe_id=pipe_id),
            )
        except BaseException:
            self.hub.close_pipe(runner_id, session_name, window_name)
            raise

    def stop_pipe_pane(self, session_name: str, window_name: str) -> None:
        with self._pipe_lock(session_name, window_name):
            self._stop_pipe_pane_locked(session_name, window_name)

    def _stop_pipe_pane_locked(self, session_name: str, window_name: str) -> None:
        runner_id, terminal_id = self._routed(session_name, window_name)
        try:
            self.hub.call(
                runner_id,
                "stop_pipe_pane",
                self._window_args(session_name, window_name, terminal_id),
            )
        finally:
            self.hub.close_pipe(runner_id, session_name, window_name)


# --- Registry and status callbacks bound to the hub (see attach_hub) ---


def _terminal_owned(terminal_id: str, runner_id: str) -> Optional[bool]:
    """Whether the terminal a runner reports has a registry row.

    A row that names no runner (written before the column existed) or another
    runner is brought in line with the report, so the next server restart
    seeds the right route.
    """
    from cli_agent_orchestrator.clients.database import (
        get_terminal_runner,
        terminal_exists,
        update_terminal_runner,
    )

    try:
        if not terminal_exists(terminal_id):
            return False
        if get_terminal_runner(terminal_id) != runner_id:
            update_terminal_runner(terminal_id, runner_id)
            logger.info(f"terminal {terminal_id} recorded on runner {runner_id}")
        return True
    except Exception:  # noqa: BLE001 - "cannot tell" must never read as "unowned"
        logger.warning(f"could not check the registry row of {terminal_id}", exc_info=True)
        return None


def _record_runner(terminal_id: str, runner_id: str) -> None:
    from cli_agent_orchestrator.clients.database import update_terminal_runner

    if update_terminal_runner(terminal_id, runner_id):
        logger.info(f"terminal {terminal_id} recorded on runner {runner_id}")


def _mark_reachability(terminal_ids: List[str], reachable: bool) -> None:
    from cli_agent_orchestrator.services.status_monitor import status_monitor

    for terminal_id in terminal_ids:
        status_monitor.set_unreachable(terminal_id, not reachable)
