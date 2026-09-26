"""Flow service for scheduled agent sessions."""

import asyncio
import json
import logging
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, cast

import frontmatter  # type: ignore
from apscheduler.triggers.cron import CronTrigger  # type: ignore

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.clients.database import create_flow as db_create_flow
from cli_agent_orchestrator.clients.database import delete_flow as db_delete_flow
from cli_agent_orchestrator.clients.database import (
    delete_terminals_by_session,
)
from cli_agent_orchestrator.clients.database import get_flow as db_get_flow
from cli_agent_orchestrator.clients.database import get_flows_to_run as db_get_flows_to_run
from cli_agent_orchestrator.clients.database import list_flows as db_list_flows
from cli_agent_orchestrator.clients.database import (
    list_terminals_by_session,
)
from cli_agent_orchestrator.clients.database import update_flow_enabled as db_update_flow_enabled
from cli_agent_orchestrator.clients.database import (
    update_flow_run_times as db_update_flow_run_times,
)
from cli_agent_orchestrator.constants import DEFAULT_PROVIDER, PROVIDERS
from cli_agent_orchestrator.models.flow import Flow
from cli_agent_orchestrator.models.kiro_engine import parse_kiro_engine
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.manager import provider_manager
from cli_agent_orchestrator.security.principal import Principal, may_start_work
from cli_agent_orchestrator.services.fifo_reader import fifo_manager
from cli_agent_orchestrator.services.script_runner import (
    remote_script_runtime,
    script_callback_env,
)
from cli_agent_orchestrator.services.status_monitor import status_monitor
from cli_agent_orchestrator.services.terminal_service import create_terminal, send_input
from cli_agent_orchestrator.utils.template import render_template

logger = logging.getLogger(__name__)


def _get_next_run_time(cron_expression: str) -> datetime:
    """Calculate next run time from cron expression."""
    trigger = CronTrigger.from_crontab(cron_expression)
    next_time = trigger.get_next_fire_time(None, datetime.now())
    if next_time is None:
        raise ValueError(
            f"Could not calculate next run time for cron expression: {cron_expression}"
        )
    return cast(datetime, next_time)


def _parse_flow_file(file_path: Path) -> Tuple[Dict, str]:
    """Parse flow file and return metadata and prompt template.

    Returns:
        Tuple of (metadata dict, prompt template string)
    """
    if not file_path.exists():
        raise ValueError(f"Flow file not found: {file_path}")

    with open(file_path, "r") as f:
        post = frontmatter.load(f)

    return post.metadata, post.content


def add_flow(file_path: str, owner: Optional[str] = None) -> Flow:
    """Add flow from file.

    ``owner`` is the canonical principal id of the caller registering the
    schedule (#745). It is recorded now because this is the last moment it is
    known: the flow fires from a background daemon whose only other candidate
    for "who is this for" is the server's own identity.
    """
    try:
        path = Path(file_path).resolve()
        metadata, _ = _parse_flow_file(path)

        # Validate required fields
        required_fields = ["name", "schedule", "agent_profile"]
        for field in required_fields:
            if field not in metadata:
                raise ValueError(f"Missing required field: {field}")

        name = metadata["name"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(name)):
            raise ValueError(f"Invalid flow name '{name}': must match ^[A-Za-z0-9_-]{{1,64}}$")
        schedule = metadata["schedule"]
        agent_profile = metadata["agent_profile"]
        provider = metadata.get(
            "provider", DEFAULT_PROVIDER
        )  # Optional, defaults to DEFAULT_PROVIDER
        script = metadata.get("script", "")  # Optional

        # Validate cron expression and calculate next run
        try:
            next_run = _get_next_run_time(schedule)
        except Exception as e:
            raise ValueError(f"Invalid cron expression '{schedule}': {e}")

        # Construct the model before persisting the registration so front-matter
        # engine values receive Pydantic's canonical v2/kas validation.
        validated_flow = Flow(
            name=name,
            file_path=str(path),
            schedule=schedule,
            agent_profile=agent_profile,
            provider=provider,
            engine=metadata.get("engine"),
            script=script,
            last_run=None,
            next_run=next_run,
            enabled=True,
            prompt_template=None,
            owner=owner,
        )

        # Create flow in database
        flow = db_create_flow(
            name=name,
            file_path=str(path),
            schedule=schedule,
            agent_profile=agent_profile,
            provider=provider,
            script=script,
            next_run=next_run,
            owner=owner,
        )
        flow = Flow.model_validate({**flow.model_dump(), "engine": validated_flow.engine})

        logger.info(f"Added flow: {name}")
        return flow

    except Exception as e:
        logger.error(f"Failed to add flow from {file_path}: {e}")
        raise


def _enrich_flow_with_prompt(flow: Flow) -> Flow:
    """Read the prompt template from the flow file and attach it."""
    try:
        metadata, prompt = _parse_flow_file(Path(flow.file_path))
    except Exception:
        return Flow.model_validate({**flow.model_dump(), "prompt_template": None})

    enriched_flow = {
        **flow.model_dump(),
        "engine": metadata.get("engine"),
        "prompt_template": prompt.strip(),
    }
    try:
        return Flow.model_validate(enriched_flow)
    except ValueError:
        # Flow files can be edited after registration. Do not let an invalid
        # engine value in one file prevent callers from reading other flows.
        logger.warning(
            "Ignoring invalid engine metadata for flow %s in %s",
            flow.name,
            flow.file_path,
        )
        return Flow.model_validate({**enriched_flow, "engine": None})


def list_flows() -> List[Flow]:
    """List all flows."""
    return [_enrich_flow_with_prompt(f) for f in db_list_flows()]


def get_flow(name: str) -> Flow:
    """Get flow by name."""
    flow = db_get_flow(name)
    if not flow:
        raise ValueError(f"Flow '{name}' not found")
    return _enrich_flow_with_prompt(flow)


def remove_flow(name: str) -> bool:
    """Remove flow."""
    if not db_delete_flow(name):
        raise ValueError(f"Flow '{name}' not found")
    logger.info(f"Removed flow: {name}")
    return True


def disable_flow(name: str) -> bool:
    """Disable flow."""
    if not db_update_flow_enabled(name, enabled=False):
        raise ValueError(f"Flow '{name}' not found")
    logger.info(f"Disabled flow: {name}")
    return True


def enable_flow(name: str) -> bool:
    """Enable flow and recalculate next_run."""
    flow = get_flow(name)
    if flow is None:
        raise ValueError(f"Flow '{name}' not found")

    # Recalculate next_run from now
    next_run = _get_next_run_time(flow.schedule)

    if not db_update_flow_enabled(name, enabled=True, next_run=next_run):
        raise ValueError(f"Failed to enable flow '{name}'")

    logger.info(f"Enabled flow: {name}")
    return True


# The pre-script's wall-clock bound, unchanged from the literal it replaces, plus
# the SIGTERM→SIGKILL grace a remote runtime needs to answer within it.
PRE_SCRIPT_TIMEOUT = 30
PRE_SCRIPT_TERM_GRACE = 5.0


def _pre_script_env(flow_name: str) -> Dict[str, str]:
    """The env a pre-script gets when it runs in a runtime instead of here.

    The local path inherits the server process environment. Forwarding that
    across the boundary would hand an execution pod every secret the server
    holds — `CAO_RUNTIME_TOKEN`, provider credentials, whatever the operator set
    — to run a health check, so the remote env is CONSTRUCTED like the workflow
    path's (`script_runner.build_env`): the OS floor, the flow's own name, and a
    callback base rewritten to the address peers can reach.

    This is a deliberate difference between the two paths, not an oversight: a
    pre-script that reads some other inherited variable works locally and sees it
    unset remotely. It is recorded in `docs/flows.md` rather than left to be
    discovered.
    """
    from cli_agent_orchestrator.constants import API_BASE_URL

    return script_callback_env(
        {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "CAO_API_BASE_URL": API_BASE_URL,
            "CAO_FLOW_NAME": flow_name,
        }
    )


async def _run_pre_script(flow_name: str, script_path: Path) -> Tuple[Optional[int], str, str]:
    """Run a flow's pre-script and return its raw (returncode, stdout, stderr).

    #745: when `CAO_SCRIPT_RUNTIME` names a runtime the script runs THERE — this
    is user code, and the issue's boundary puts user code in an execution
    workload rather than beside the central database. The server keeps what it
    already owned: the schedule, the JSON contract, the execute/skip decision
    and the launch. Unset is the unchanged local path; a runtime that is named
    but not connected raises, because the fallback for a placement the operator
    asked for cannot be "run it here after all".

    A runtime that dies mid-script, or does not answer, raises — the outcome is
    genuinely unknown, and the caller's contract for an unusable pre-script is
    already an exception (a missing script, a non-zero exit and unparseable JSON
    all raise today). Silently treating unknown as `execute: false` would look
    like a healthy skip.
    """
    runtime_id = remote_script_runtime()
    if runtime_id is None:
        result = subprocess.run(
            [str(script_path)], capture_output=True, text=True, timeout=PRE_SCRIPT_TIMEOUT
        )
        return result.returncode, result.stdout, result.stderr

    from cli_agent_orchestrator.runtime_channel.protocol import CommandType
    from cli_agent_orchestrator.runtime_channel.registry import (
        RuntimeUnavailableError,
        runtime_registry,
    )

    conn = runtime_registry.get_runtime(runtime_id)
    if conn is None:
        raise ValueError(f"script runtime '{runtime_id}' is not connected")

    script_body = await asyncio.to_thread(script_path.read_text)
    try:
        result_frame = await conn.send_command(
            CommandType.RUN_SCRIPT,
            {
                "script": script_body,
                "env": _pre_script_env(flow_name),
                "timeout": PRE_SCRIPT_TIMEOUT,
                "term_grace": PRE_SCRIPT_TERM_GRACE,
                # A pre-script's shebang picks its interpreter; docs/flows.md's
                # example is bash. Never sys.executable.
                "mode": "executable",
            },
            # Outlive the script's own bound so the runtime answers first; a
            # missing answer is a disconnect, not a skip.
            timeout=PRE_SCRIPT_TIMEOUT + PRE_SCRIPT_TERM_GRACE + 30.0,
        )
    except RuntimeUnavailableError as exc:
        raise ValueError(
            f"script runtime '{runtime_id}' disconnected while running the "
            f"pre-script for flow {flow_name} (outcome unknown)"
        ) from exc
    except (asyncio.TimeoutError, TimeoutError) as exc:
        raise ValueError(
            f"script runtime '{runtime_id}' did not answer the pre-script for "
            f"flow {flow_name} (outcome unknown)"
        ) from exc

    payload = result_frame.payload or {}
    if payload.get("timed_out"):
        raise ValueError(
            f"Pre-script for flow {flow_name} exceeded the {PRE_SCRIPT_TIMEOUT}s bound "
            f"in runtime {runtime_id}"
        )
    return payload.get("returncode"), payload.get("stdout", ""), payload.get("stderr", "")


def _flow_launch_runtime() -> Optional[str]:
    """The runtime a scheduled flow's agent is launched on, or None for here.

    Relocating the pre-script (above) is only half of the issue's flow story: the
    session the flow then launches is user code too, and in the cluster topology
    the server container has no tmux to launch it in. ``CAO_FLOW_RUNTIME`` names
    the execution runtime that does.

    Deliberately a SEPARATE decision from ``CAO_SCRIPT_RUNTIME``: a health check
    and a long-lived agent are different workloads, and an operator may well want
    the check beside the server's supervisor while agents go to dedicated worker
    pods. Unset — the default, and every single-host installation — is the
    unchanged local launch.

    Unlike the pre-script path, this does not silently degrade to local when the
    named runtime is absent; the launch attempt fails loudly (below). Falling back
    would create the agent session in the container this env var exists to keep
    user code out of, where an operator would then have to go find it.
    """
    return os.environ.get("CAO_FLOW_RUNTIME", "").strip() or None


async def _launch_flow_terminal(
    flow: Flow, session_name: str, owner_id: Optional[str]
) -> Any:  # -> Terminal
    """Create the flow's agent terminal, here or in its execution runtime (#745)."""
    runtime_id = _flow_launch_runtime()
    if runtime_id is None:
        return await create_terminal(
            session_name=session_name,
            provider=flow.provider,
            agent_profile=flow.agent_profile,
            new_session=True,
            engine=flow.engine,
            # The agent this schedule launches works for whoever REGISTERED the
            # schedule, not for whoever the daemon runs as (#745). Carrying it
            # onto the terminal row is what lets the same owner be read later,
            # when this terminal sends a message and nothing about the original
            # registration request is still in scope.
            owner=owner_id,
        )

    from fastapi import HTTPException

    from cli_agent_orchestrator.runtime_channel.api import (
        CreateRemoteTerminalBody,
        launch_remote_terminal,
    )

    # LAUNCH now carries the engine (CreateRemoteTerminalBody.engine, forwarded
    # in the payload and honored by the bridge, persisted centrally by
    # launch_remote_terminal), so a non-default-engine flow can run remotely —
    # the earlier blanket refusal here was stale (guojing1217 on #802).
    try:
        return await launch_remote_terminal(
            runtime_id,
            CreateRemoteTerminalBody(
                provider=flow.provider,
                agent_profile=flow.agent_profile,
                session_name=session_name,
                engine=flow.engine,
            ),
            # Server-written, exactly as the HTTP path writes the caller's
            # principal: the owner is never handed to the runtime in the LAUNCH
            # payload, because an identity given to an executor is an identity it
            # could re-present.
            owner_id=owner_id,
        )
    except HTTPException as exc:
        # The shared launch path reports transport and execution failures as HTTP
        # status; a scheduler has no response to put them in. Keep the detail
        # (which distinguishes "not connected" from "outcome unknown") and raise
        # the ValueError this function's other failures already raise.
        raise ValueError(
            f"flow {flow.name}: remote launch on runtime '{runtime_id}' failed: {exc.detail}"
        ) from exc


def _remote_flow_terminals(terminals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The terminals in this list a runtime executes, not this host."""
    from cli_agent_orchestrator.runtime_channel.registry import runtime_registry

    return [t for t in terminals if runtime_registry.is_remote(t["id"])]


async def _recycle_remote_flow_terminals(flow_name: str, terminals: List[Dict[str, Any]]) -> bool:
    """Tear down a previous run's REMOTE flow terminals, in their runtime (#745).

    The local arm below asks this host's tmux whether the flow session survives.
    For a remotely launched flow that question is unanswerable here — the session
    is in another pod — so the central terminal rows are the handle, and teardown
    goes over the channel, where the runtime runs the same local cleanup (tmux
    kill, FIFO reader, provider state) the local arm does inline.

    Returns False when the session must not be recycled yet: the conductor is
    still working, or a teardown did not confirm. Retaining the rows keeps the
    retry handle, matching the local deferred-cleanup arm.
    """
    from cli_agent_orchestrator.runtime_channel.api import remote_delete_terminal
    from cli_agent_orchestrator.runtime_channel.registry import runtime_registry

    remote = _remote_flow_terminals(terminals)
    if not remote:
        return True

    # Index 0 is the conductor, per ``list_terminals_by_session``'s ordering
    # contract — the same read, and the same caveat, as the local arm.
    conductor = terminals[0]
    if (
        runtime_registry.is_remote(conductor["id"])
        and runtime_registry.get_status(conductor["id"]) == TerminalStatus.PROCESSING
    ):
        logger.info("Flow %s: remote session is busy, skipping", flow_name)
        return False

    for t in remote:
        try:
            await remote_delete_terminal(t["id"])
        except Exception as e:
            # Any failure here — a refusing runtime, a disconnect, a timeout —
            # means the previous run's session may still be alive. Do not launch
            # a second one into it.
            logger.warning(
                "Flow %s: remote cleanup deferred for terminal %s: %s", flow_name, t["id"], e
            )
            return False
    return True


def _is_terminal_busy(terminal_id: str) -> bool:
    try:
        return status_monitor.get_status(terminal_id) == TerminalStatus.PROCESSING
    except Exception:
        return False


async def execute_flow(name: str) -> bool:
    """Execute flow: run script, render prompt, launch session."""
    try:
        logger.info(f"Executing flow: {name}")
        flow = get_flow(name)

        # Dispatch gate (#745): a schedule outlives the request that created it,
        # so whether its owner may still start work is a question only answerable
        # HERE, at the moment work would begin. Placed above the pre-script
        # deliberately — that script is the owner's code too, and running it is
        # starting work on their behalf.
        #
        # The schedule still advances (the same thing the execute=false arm does
        # below), so a withdrawn owner's flow does not re-queue every minute
        # forever; and nothing about this path touches disable/remove, which stay
        # available to stop the flow for good.
        owner = Principal.parse(flow.owner)
        if not may_start_work(owner):
            db_update_flow_run_times(
                name, last_run=datetime.now(), next_run=_get_next_run_time(flow.schedule)
            )
            logger.warning(
                "Flow %s: not dispatched — owner %s is revoked", name, owner.id if owner else "?"
            )
            return False

        # Read flow file
        file_path = Path(flow.file_path)
        metadata, prompt_template = _parse_flow_file(file_path)

        # get_flow degrades an invalid engine to None so one bad file cannot
        # break listing. Executing on that None would silently launch v2, so
        # re-validate the raw value here: at the execution boundary a rejected
        # engine must fail rather than fall back to the default.
        parse_kiro_engine(metadata.get("engine"))

        # If no script, always execute with empty output
        if not flow.script:
            output = {"execute": True, "output": {}}
        else:
            # Execute script
            script_path = Path(flow.script)
            if not script_path.is_absolute():
                script_path = file_path.parent / script_path

            if not script_path.exists():
                raise ValueError(f"Script not found: {script_path}")

            returncode, stdout, stderr = await _run_pre_script(name, script_path)

            if returncode != 0:
                logger.error(f"Script failed: {stderr}")
                raise ValueError(f"Script failed with exit code {returncode}: {stderr}")

            # Parse JSON output
            try:
                output = json.loads(stdout)
            except json.JSONDecodeError as e:
                raise ValueError(f"Script output is not valid JSON: {e}")

            if "execute" not in output:
                raise ValueError("Script output missing 'execute' field")

            if "output" not in output:
                raise ValueError("Script output missing 'output' field")

        # Update last_run and calculate next_run
        now = datetime.now()
        next_run = _get_next_run_time(flow.schedule)
        db_update_flow_run_times(name, last_run=now, next_run=next_run)

        # Check if we should execute
        if not output["execute"]:
            logger.info(f"Flow {name}: skipped (execute=false)")
            return False

        # Render prompt template
        if not isinstance(output["output"], dict):
            raise ValueError("Script output 'output' field must be a dictionary")
        output_dict: Dict[str, Any] = output["output"]  # type: ignore[assignment]
        rendered_prompt = render_template(prompt_template, output_dict)

        # Launch session
        session_name = f"cao-flow-{flow.name}"
        terminals = list_terminals_by_session(session_name)
        # Remote rows first: their session is in another pod, so nothing below
        # can see or clean it. Re-read afterwards so a session whose placement
        # changed between runs (the operator set or cleared CAO_FLOW_RUNTIME)
        # still has its local leftovers handled by the local arm.
        if _remote_flow_terminals(terminals):
            if not await _recycle_remote_flow_terminals(name, terminals):
                return False
            terminals = list_terminals_by_session(session_name)
        if get_backend().session_exists(session_name):
            # Only check the first (conductor) terminal for busy status.
            # Worker terminals spawned by the conductor may have stale status
            # after /exit and should not block flow recycling.
            #
            # Index 0 is the oldest surviving terminal -- normally the conductor
            # -- per the ordering contract documented on
            # ``list_terminals_by_session``; do not restate the rule here, and do
            # not reorder that read. This is the consumer with real blast radius:
            # if index 0 is a quiet WORKER rather than the conductor, the busy
            # check passes and the kill_session below tears down a session whose
            # conductor is mid-run. Two documented ways index 0 can be a worker
            # (both pre-existing, both listed on that function): the conductor's
            # own row was deleted, or a row was inserted during flow recycling,
            # which does not hold ``session_lifecycle_lock``.
            conductor = terminals[0] if terminals else None
            # Off the loop: get_status() can fork a tmux capture-pane for a
            # PROCESSING terminal (status_monitor.py's stale-PROCESSING
            # fallback), and execute_flow runs on the shared event loop.
            if conductor and await asyncio.to_thread(_is_terminal_busy, conductor["id"]):
                logger.info(f"Flow {name}: session {session_name} is busy, skipping")
                return False
            for t in terminals:
                # Tear down the event-driven pipeline for each recycled terminal:
                # stop the FIFO reader thread (and unlink its *.fifo file) and clear
                # the StatusMonitor buffers. Without this, repeated flow runs leak
                # background reader threads and stale FIFO files / status entries.
                try:
                    fifo_manager.stop_reader(t["id"])
                except Exception as e:
                    logger.warning(f"Failed to stop FIFO reader for {t['id']}: {e}")
                try:
                    status_monitor.clear_terminal(t["id"])
                except Exception as e:
                    logger.warning(f"Failed to clear status buffers for {t['id']}: {e}")
            get_backend().kill_session(session_name)
            # A provider's private state must outlive the process that owns
            # it.  Grok cleanup confirms any escaped updater has stopped
            # before recursively deleting its private GROK_HOME.
            cleanup_complete = True
            for t in terminals:
                # Do not bulk-delete DB rows if a Grok private home is still
                # owned by a process we cannot safely inspect. Retained rows
                # are the retry handle for a later terminal cleanup.
                if provider_manager.cleanup_provider(t["id"]) is False:
                    cleanup_complete = False
            if not cleanup_complete:
                logger.warning(
                    "Flow %s recycling cleanup deferred; retaining terminal metadata for retry",
                    name,
                )
                return False
            delete_terminals_by_session(session_name)
        elif terminals:
            # A previous recycle can have killed the backend session but safely
            # retained its terminal rows because a Grok-owned private home was
            # still in use.  Do not create a same-named flow session until those
            # rows have been retried: doing so would abandon their only cleanup
            # handle and could collide with the deterministic GROK_HOME path.
            cleanup_complete = True
            for terminal_metadata in terminals:
                if provider_manager.cleanup_provider(terminal_metadata["id"]) is False:
                    cleanup_complete = False
            if not cleanup_complete:
                logger.warning("Flow %s has retained terminal cleanup; deferring next run", name)
                return False
            delete_terminals_by_session(session_name)
        terminal = await _launch_flow_terminal(flow, session_name, owner.id if owner else None)

        # Send rendered prompt to terminal. send_input is blocking tmux I/O
        # (now additionally a pane-foreground-command probe on top of the
        # existing bracketed-paste delivery, see clients/tmux.py's
        # _pane_is_bracketed_paste_incompatible) -- run it off the event loop
        # so a slow tmux call can't freeze every other request (same hazard
        # class as issue #382, already fixed for POST /terminals/{id}/input
        # in api/main.py's send_terminal_input; this call site was missed).
        await asyncio.to_thread(send_input, terminal.id, rendered_prompt)

        logger.info(f"Flow {name}: launched session {session_name}")
        return True

    except Exception as e:
        logger.error(f"Flow {name} failed: {e}", exc_info=True)
        raise


def get_flows_to_run() -> List[Flow]:
    """Get flows that should run now."""
    return db_get_flows_to_run()
