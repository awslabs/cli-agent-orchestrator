"""Flow service for scheduled agent sessions."""

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple, TypedDict, cast

import frontmatter  # type: ignore
from apscheduler.triggers.cron import CronTrigger  # type: ignore

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.clients.database import create_flow as db_create_flow
from cli_agent_orchestrator.clients.database import delete_flow as db_delete_flow
from cli_agent_orchestrator.clients.database import delete_terminals_by_ids
from cli_agent_orchestrator.clients.database import get_flow as db_get_flow
from cli_agent_orchestrator.clients.database import get_flows_to_run as db_get_flows_to_run
from cli_agent_orchestrator.clients.database import list_flows as db_list_flows
from cli_agent_orchestrator.clients.database import (
    update_flow_definition as db_update_flow_definition,
)
from cli_agent_orchestrator.clients.database import update_flow_enabled as db_update_flow_enabled
from cli_agent_orchestrator.clients.database import (
    update_flow_run_times as db_update_flow_run_times,
)
from cli_agent_orchestrator.constants import DEFAULT_PROVIDER
from cli_agent_orchestrator.models.flow import Flow
from cli_agent_orchestrator.models.kiro_engine import parse_kiro_engine
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.manager import provider_manager
from cli_agent_orchestrator.services.fifo_reader import fifo_manager
from cli_agent_orchestrator.services.session_service import list_current_session_terminals
from cli_agent_orchestrator.services.status_monitor import status_monitor
from cli_agent_orchestrator.services.terminal_service import (
    create_terminal,
    send_input,
    should_retain_deferred_failure_tombstone,
)
from cli_agent_orchestrator.utils.template import render_template

logger = logging.getLogger(__name__)


class FieldError(TypedDict):
    """One problem with one field of a flow definition."""

    field: str
    message: str


_FLOW_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
NAME_MESSAGE = "must match ^[A-Za-z0-9_-]{1,64}$ (letters, digits, '_' and '-', at most 64)"
NAME_TAKEN_MESSAGE = "a flow with this name already exists"
# Far longer than any hand-written cron; it bounds the per-item cost of the fire-time scan.
SCHEDULE_MAX_LENGTH = 256
SCHEDULE_TOO_LONG_MESSAGE = f"is too long (at most {SCHEDULE_MAX_LENGTH} characters)"
# The scheduler matches day and weekday together, so 29 February on one weekday recurs 28 or
# 40 years apart (2072 to 2112 skips the non-leap 2100); no schedule that fires waits longer.
_LONGEST_FIRE_GAP = timedelta(days=366 * 41)

# The first trigger resolves and caches the local time zone, which reads zone files. Doing it at
# import keeps validate_flow_definition free of file access, whichever call comes first.
CronTrigger.from_crontab("* * * * *")


def next_fire_times(
    cron_expression: str, n: int, after: Optional[datetime] = None
) -> List[datetime]:
    """Return up to ``n`` ascending fire times of a 5-field cron, the first after ``after``.

    The day-of-week field is read as APScheduler reads it (numbers count from Monday as 0), so
    ``1-5`` is Tuesday to Saturday; day names mean what they say.

    Args:
        cron_expression: Five-field cron expression.
        n: How many fire times to return at most.
        after: Naive local time to start after; defaults to now.

    Returns:
        Fewer than ``n`` times only when the schedule stops firing (for example 30 February).

    Raises:
        ValueError: The scheduler refuses the expression.
    """
    trigger = CronTrigger.from_crontab(cron_expression)
    times: List[datetime] = []
    current = _next_fire_within_gap(trigger, None, after if after is not None else datetime.now())
    while current is not None and len(times) < n:
        times.append(current)
        # The trigger takes min(now, previous + 1us) as its start, so passing the previous fire
        # time for both arguments moves it strictly past that time.
        current = _next_fire_within_gap(trigger, current, current)
    return times


def _next_fire_within_gap(
    trigger: CronTrigger, previous: Optional[datetime], start: datetime
) -> Optional[datetime]:
    """Next fire time of ``trigger`` after ``start``, or None if none falls within the gap.

    Unbounded, APScheduler walks year by year to 9999 for a schedule that never fires and
    re-checks every list item at each step, which a request can make take seconds. Bounding
    each step by the longest gap a firing schedule can have keeps every real fire time.
    """
    trigger.end_date = start.astimezone(trigger.timezone) + _LONGEST_FIRE_GAP
    return cast(Optional[datetime], trigger.get_next_fire_time(previous, start))


def _get_next_run_time(cron_expression: str) -> datetime:
    """Calculate next run time from cron expression."""
    times = next_fire_times(cron_expression, 1)
    if not times:
        raise ValueError(
            f"Could not calculate next run time for cron expression: {cron_expression}"
        )
    return times[0]


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


def _has_control_character(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def validate_flow_definition(fields: Mapping[str, Any], *, must_be_new: bool) -> List[FieldError]:
    """Check every rule for a flow definition; never touches the file system.

    Args:
        fields: ``name``, ``schedule``, ``agent_profile`` and ``provider``; a missing or
            non-string value is a field error.
        must_be_new: Also refuse a name that belongs to a registered flow. Create and validate
            pass True; an edit passes False so a flow does not collide with itself.

    Returns:
        One error per bad field, in the order name, schedule, agent_profile, provider; empty
        when the definition is valid.
    """
    errors: List[FieldError] = []

    def add(field: str, message: str) -> None:
        errors.append({"field": field, "message": message})

    name = fields.get("name")
    if not isinstance(name, str) or not _FLOW_NAME.fullmatch(name):
        add("name", NAME_MESSAGE)
    elif must_be_new and db_get_flow(name) is not None:
        add("name", NAME_TAKEN_MESSAGE)

    for key in ("schedule", "agent_profile", "provider"):
        value = fields.get(key)
        if value is None:
            add(key, "is required")
        elif not isinstance(value, str):
            add(key, "must be a string")
        elif key == "schedule" and len(value) > SCHEDULE_MAX_LENGTH:
            add(key, SCHEDULE_TOO_LONG_MESSAGE)
        elif _has_control_character(value):
            add(key, "must not contain control characters")
        elif key == "schedule":
            try:
                if not next_fire_times(value, 1):
                    add(key, "this schedule never fires")
            except Exception as exc:  # APScheduler raises ValueError subclasses; stay tolerant
                add(key, str(exc))
        elif not value.strip():
            add(key, "must not be empty")
    return errors


def find_create_conflict(name: str, target: str) -> Optional[str]:
    """Say why a new flow called ``name`` cannot be written to ``target``, or return None.

    A conflict is a registered flow with that name, or a registered flow whose file is the
    same file as ``target`` (symlinks and hard links included), which a create would overwrite.
    A stray file at ``target`` that no flow is registered from is not a conflict, and a
    registered flow whose file is gone cannot collide with anything.

    Args:
        name: Name of the flow to create.
        target: Path the new flow's file would be written to.

    Returns:
        A message naming the registered flow, or None when the create may go ahead.
    """
    if db_get_flow(name) is not None:
        return f"Flow '{name}' already exists"
    for flow in db_list_flows():
        try:
            if os.path.samefile(target, flow.file_path):
                return (
                    f"Flow '{name}' would overwrite {flow.file_path}, "
                    f"the file flow '{flow.name}' is registered from"
                )
        except OSError:
            continue  # the target is not there yet, or the registered file is gone
    return None


def _legacy_message(error: FieldError, fields: Mapping[str, Any]) -> str:
    """Word a field error the way ``add_flow`` always has; callers and tests match on it."""
    if error["field"] == "name":
        return f"Invalid flow name '{fields.get('name')}': {error['message']}"
    if error["field"] == "schedule":
        return f"Invalid cron expression '{fields.get('schedule')}': {error['message']}"
    return f"Invalid {error['field']}: {error['message']}"


def add_flow(file_path: str, enabled: bool = True) -> Flow:
    """Add flow from file; ``enabled=False`` registers it without arming it."""
    try:
        path = Path(file_path).resolve()
        metadata, _ = _parse_flow_file(path)

        # Validate required fields
        required_fields = ["name", "schedule", "agent_profile"]
        for field in required_fields:
            if field not in metadata:
                raise ValueError(f"Missing required field: {field}")

        name = metadata["name"]
        schedule = metadata["schedule"]
        agent_profile = metadata["agent_profile"]
        provider = metadata.get(
            "provider", DEFAULT_PROVIDER
        )  # Optional, defaults to DEFAULT_PROVIDER
        script = metadata.get("script", "")  # Optional

        definition = {
            "name": name,
            "schedule": schedule,
            "agent_profile": agent_profile,
            "provider": provider,
        }
        errors = validate_flow_definition(definition, must_be_new=False)
        if errors:
            raise ValueError(_legacy_message(errors[0], definition))
        next_run = _get_next_run_time(schedule)

        # Construct the model before persisting the registration so front-matter
        # engine values receive Pydantic's canonical v2/v3 validation (kas is read as v3).
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
            enabled=enabled,
            prompt_template=None,
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
            enabled=enabled,
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


class FlowNotFoundError(ValueError):
    """No flow with that name."""


class FlowFileMissingError(ValueError):
    """The flow is registered but its file no longer exists."""


class FlowDefinitionError(ValueError):
    """The merged definition failed validation; ``errors`` lists every field error."""

    def __init__(self, errors: List[FieldError]) -> None:
        super().__init__("; ".join(f"{e['field']}: {e['message']}" for e in errors))
        self.errors = errors


class FlowFileOutsideHomeError(ValueError):
    """The flow's file is not under the flows directory this server may rewrite."""


_EDITABLE = ("schedule", "agent_profile", "provider")


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` through a temp file in the same directory and a rename.

    The temp file is removed when anything fails before the rename, so the old file stays intact.
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def update_flow(name: str, changes: Mapping[str, Optional[str]], flows_dir: Path) -> Flow:
    """Edit a flow in place: merge ``changes`` over the stored flow, validate, rewrite its file.

    Only the keys named in ``changes`` are rewritten in the front matter (``script``, ``engine``
    and unknown keys survive, comments in the front matter do not), and ``prompt_template``
    replaces the body. A disabled flow stays disabled and keeps its stored ``next_run``; an
    enabled one gets a fresh ``next_run``. The file is replaced before the row is updated, and
    two edits of one flow at once are not serialized (the last write wins).

    Args:
        name: The flow to edit.
        changes: ``schedule``, ``agent_profile``, ``provider`` and ``prompt_template``; a
            missing key or a None value leaves that field as stored.
        flows_dir: The directory whose files this server may rewrite; the flow's file must
            resolve (symlinks and ``..`` followed) to a place under it.

    Returns:
        The edited flow.

    Raises:
        FlowNotFoundError: No such flow, or it was removed while the edit was written.
        FlowFileMissingError: The flow is registered but its file is gone; nothing was written.
        FlowDefinitionError: The merged definition is invalid; nothing was written.
        FlowFileOutsideHomeError: The file is registered from outside ``flows_dir``.
    """
    flow = db_get_flow(name)
    if flow is None:
        raise FlowNotFoundError(f"Flow '{name}' not found")
    path = Path(flow.file_path).resolve()
    if not path.is_relative_to(flows_dir.resolve()):
        raise FlowFileOutsideHomeError(
            f"Flow '{name}' is registered from {flow.file_path}, outside the flows directory; "
            "edit that file"
        )
    if not path.is_file():
        raise FlowFileMissingError(f"Flow '{name}': flow file missing: {flow.file_path}")
    merged: Dict[str, Any] = {"name": name, **{key: getattr(flow, key) for key in _EDITABLE}}
    edited = {key: changes[key] for key in _EDITABLE if changes.get(key) is not None}
    merged.update(edited)
    errors = validate_flow_definition(merged, must_be_new=False)
    if errors:
        raise FlowDefinitionError(errors)
    next_run = _get_next_run_time(merged["schedule"]) if flow.enabled else None

    post = frontmatter.load(str(path))
    post.metadata.update(edited)
    if changes.get("prompt_template") is not None:
        post.content = cast(str, changes["prompt_template"])
    _atomic_write_text(path, frontmatter.dumps(post, sort_keys=False) + "\n")
    if not db_update_flow_definition(
        name, merged["schedule"], merged["agent_profile"], merged["provider"], next_run
    ):
        raise FlowNotFoundError(f"Flow '{name}' was removed while its file was being rewritten")
    return get_flow(name)


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


def _is_terminal_busy(terminal_id: str) -> bool:
    try:
        return status_monitor.get_status(terminal_id) == TerminalStatus.PROCESSING
    except Exception:
        return False


def _delete_recycled_terminal_rows(terminal_ids: List[str]) -> None:
    """Release names only after the existing bulk deletion succeeds."""
    if delete_terminals_by_ids(terminal_ids, end_reason="session_teardown"):
        from cli_agent_orchestrator.services import ephemeral_service

        for terminal_id in terminal_ids:
            ephemeral_service.release(terminal_id, "terminal_gone")


async def execute_flow(name: str) -> bool:
    """Execute flow: run script, render prompt, launch session."""
    try:
        logger.info(f"Executing flow: {name}")
        flow = get_flow(name)

        # Advance the schedule before anything below can raise. A failed run
        # then waits for its next cron slot, as an execute=false run does,
        # instead of staying due and re-running on every flow_daemon poll.
        now = datetime.now()
        next_run = _get_next_run_time(flow.schedule)
        db_update_flow_run_times(name, last_run=now, next_run=next_run)

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

            # Off the loop: the script can run for up to 30s, and execute_flow
            # runs on the shared event loop, so every request would wait on it.
            result = await asyncio.to_thread(
                subprocess.run, [str(script_path)], capture_output=True, text=True, timeout=30
            )

            if result.returncode != 0:
                logger.error(f"Script failed: {result.stderr}")
                raise ValueError(
                    f"Script failed with exit code {result.returncode}: {result.stderr}"
                )

            # Parse JSON output
            try:
                output = json.loads(result.stdout)
            except json.JSONDecodeError as e:
                raise ValueError(f"Script output is not valid JSON: {e}")

            if "execute" not in output:
                raise ValueError("Script output missing 'execute' field")

            if "output" not in output:
                raise ValueError("Script output missing 'output' field")

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
        backend = get_backend()
        backend_exists = backend.session_exists(session_name)
        terminals = list_current_session_terminals(session_name, backend_exists=backend_exists)
        if backend_exists:
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
            backend.kill_session(session_name)
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
            _delete_recycled_terminal_rows([str(t["id"]) for t in terminals])
        elif terminals:
            # A previous recycle can have killed the backend session but safely
            # retained its terminal rows because a Grok-owned private home was
            # still in use.  Do not create a same-named flow session until those
            # rows have been retried: doing so would abandon their only cleanup
            # handle and could collide with the deterministic GROK_HOME path.
            # The absent-backend view is raw history, including tombstones
            # whose external owners have not acknowledged their failures.
            cleanup_rows = [
                terminal_metadata
                for terminal_metadata in terminals
                if not should_retain_deferred_failure_tombstone(
                    str(terminal_metadata["id"]), terminal_metadata
                )
            ]
            cleanup_complete = True
            for terminal_metadata in cleanup_rows:
                if provider_manager.cleanup_provider(terminal_metadata["id"]) is False:
                    cleanup_complete = False
            if not cleanup_complete:
                logger.warning("Flow %s has retained terminal cleanup; deferring next run", name)
                return False
            _delete_recycled_terminal_rows([str(t["id"]) for t in cleanup_rows])
        terminal = await create_terminal(
            session_name=session_name,
            provider=flow.provider,
            agent_profile=flow.agent_profile,
            new_session=True,
            engine=flow.engine,
        )

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
