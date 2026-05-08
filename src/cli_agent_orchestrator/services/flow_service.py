"""Flow service for scheduled agent sessions."""

import json
import logging
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple, cast

import frontmatter  # type: ignore
from apscheduler.triggers.cron import CronTrigger  # type: ignore

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
from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.constants import DEFAULT_PROVIDER, PROVIDERS
from cli_agent_orchestrator.models.flow import Flow
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.manager import provider_manager
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


def add_flow(file_path: str) -> Flow:
    """Add flow from file."""
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

        # Validate cron expression and calculate next run
        try:
            next_run = _get_next_run_time(schedule)
        except Exception as e:
            raise ValueError(f"Invalid cron expression '{schedule}': {e}")

        # Create flow in database
        flow = db_create_flow(
            name=name,
            file_path=str(path),
            schedule=schedule,
            agent_profile=agent_profile,
            provider=provider,
            script=script,
            next_run=next_run,
        )

        logger.info(f"Added flow: {name}")
        return flow

    except Exception as e:
        logger.error(f"Failed to add flow from {file_path}: {e}")
        raise


def _enrich_flow_with_prompt(flow: Flow) -> Flow:
    """Read the prompt template from the flow file and attach it."""
    try:
        _, prompt = _parse_flow_file(Path(flow.file_path))
        flow.prompt_template = prompt.strip()
    except Exception:
        flow.prompt_template = None
    return flow


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

    # Recalculate next_run from now
    next_run = _get_next_run_time(flow.schedule)

    if not db_update_flow_enabled(name, enabled=True, next_run=next_run):
        raise ValueError(f"Failed to enable flow '{name}'")

    logger.info(f"Enabled flow: {name}")
    return True


def _is_terminal_busy(terminal_id: str) -> bool:
    try:
        return provider_manager.get_provider(terminal_id).get_status() == TerminalStatus.PROCESSING
    except Exception:
        return False


def execute_flow(name: str) -> bool:
    """Execute flow: run script, render prompt, launch session."""
    try:
        logger.info(f"Executing flow: {name}")
        flow = get_flow(name)

        # Read flow file
        file_path = Path(flow.file_path)
        _, prompt_template = _parse_flow_file(file_path)

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

            result = subprocess.run([str(script_path)], capture_output=True, text=True, timeout=30)

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
        if tmux_client.session_exists(session_name):
            terminals = list_terminals_by_session(session_name)
            # Only check the conductor terminal (matching the flow's agent profile)
            # for busy status. Worker terminals spawned by the conductor may have
            # stale status after /exit and should not block flow recycling.
            conductor_terminals = [
                t for t in terminals if t.get("agent_profile") == flow.agent_profile
            ]
            if any(_is_terminal_busy(t["id"]) for t in conductor_terminals):
                logger.info(f"Flow {name}: session {session_name} is busy, skipping")
                return False
            tmux_client.kill_session(session_name)
            delete_terminals_by_session(session_name)
        terminal = create_terminal(
            session_name=session_name,
            provider=flow.provider,
            agent_profile=flow.agent_profile,
            new_session=True,
        )

        # Send rendered prompt to terminal
        send_input(terminal.id, rendered_prompt)

        logger.info(f"Flow {name}: launched session {session_name}")
        return True

    except Exception as e:
        logger.error(f"Flow {name} failed: {e}", exc_info=True)
        raise


def get_flows_to_run() -> List[Flow]:
    """Get flows that should run now."""
    return db_get_flows_to_run()
