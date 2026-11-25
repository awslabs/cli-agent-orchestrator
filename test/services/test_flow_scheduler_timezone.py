"""The flow scheduler must work, and must not break start-up, under a POSIX ``TZ`` string.

``tzlocal`` (which APScheduler asks for the local zone) refuses a ``TZ`` such as ``AEST-10`` or
``UTC0`` that is not a zoneinfo name. These tests run in a subprocess so the import itself,
which reads ``TZ`` once, is what is under test.
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import flow_service

POSIX_ZONES = ["AEST-10", "UTC0"]


def run_python(code: str, tz: str, home: Path) -> subprocess.CompletedProcess:
    """Run ``code`` in a fresh interpreter with ``TZ`` set and a private CAO home."""
    env = {**os.environ, "TZ": tz, "CAO_HOME_DIR": str(home)}
    return subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=180
    )


def _local_offset() -> timedelta:
    offset = datetime.now().astimezone().utcoffset()
    assert offset is not None
    return offset


@pytest.mark.parametrize("tz", POSIX_ZONES)
def test_the_server_module_imports_under_a_posix_tz(tz, tmp_path):
    result = run_python("import cli_agent_orchestrator.api.main", tz, tmp_path)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("tz", POSIX_ZONES)
def test_schedule_list_runs_under_a_posix_tz(tz, tmp_path):
    code = (
        "from click.testing import CliRunner\n"
        "from cli_agent_orchestrator.cli.main import cli\n"
        "result = CliRunner().invoke(cli, ['schedule', 'list'])\n"
        "print(result.exit_code, result.output.strip())\n"
    )

    result = run_python(code, tz, tmp_path)

    assert result.returncode == 0, result.stderr
    # The fallback warning shares the CliRunner's output; the exit code and the listing matter.
    assert result.stdout.startswith("0 ")
    assert result.stdout.strip().endswith("No flows found")


@pytest.mark.parametrize(("tz", "offset"), [("AEST-10", "+10:00"), ("UTC0", "+00:00")])
def test_a_weekday_schedule_is_valid_and_fires_under_a_posix_tz(tz, offset, tmp_path):
    code = (
        "import json\n"
        "from datetime import datetime\n"
        "from cli_agent_orchestrator.services import flow_service\n"
        "definition = {'name': 'daily', 'schedule': '0 9 * * mon-fri',\n"
        "              'agent_profile': 'developer', 'provider': 'claude_code'}\n"
        "errors = flow_service.validate_flow_definition(definition, must_be_new=False)\n"
        "times = flow_service.next_fire_times(\n"
        "    '0 9 * * mon-fri', 2, datetime(2026, 11, 20, 8, 0))\n"
        "print(json.dumps({'errors': errors,\n"
        "                  'times': [t.isoformat() for t in times]}))\n"
    )

    result = run_python(code, tz, tmp_path)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "errors": [],
        # Friday 2026-11-20, then Monday 2026-11-23, at the offset the POSIX string names.
        "times": [f"2026-11-20T09:00:00{offset}", f"2026-11-23T09:00:00{offset}"],
    }


def test_an_unusable_local_zone_falls_back_to_the_local_offset_and_warns(monkeypatch, caplog):
    # The host's own zone may be UTC (CI), where "the local offset" and "UTC" cannot be told apart.
    monkeypatch.setenv("TZ", "AEST-10")
    time.tzset()
    try:
        _fall_back_and_check(monkeypatch, caplog)
    finally:
        monkeypatch.undo()
        time.tzset()


def _fall_back_and_check(monkeypatch, caplog):
    def refuse(*_args, **_kwargs):
        raise LookupError("tzlocal() does not support non-zoneinfo timezones like AEST-10")

    monkeypatch.setattr(flow_service.CronTrigger, "from_crontab", refuse)

    with caplog.at_level(logging.WARNING, logger=flow_service.logger.name):
        zone = flow_service._resolve_scheduler_timezone()

    assert isinstance(zone, timezone)  # a fixed offset, not a zone that needs a database
    assert zone.utcoffset(None) == timedelta(hours=10), "the fallback is the local offset, not UTC"
    assert any("time zone" in record.getMessage() for record in caplog.records)


def test_a_usable_local_zone_is_used_as_it_is(caplog):
    with caplog.at_level(logging.WARNING, logger=flow_service.logger.name):
        zone = flow_service._resolve_scheduler_timezone()

    assert isinstance(zone, tzinfo)
    assert not caplog.records


@pytest.mark.usefixtures("isolated_memory_db")
@pytest.mark.parametrize(
    "drift", [timedelta(hours=-1), timedelta(hours=1)], ids=["behind", "ahead"]
)
def test_a_fallback_offset_gone_stale_does_not_refire_or_skip_a_flow(drift, tmp_path, monkeypatch):
    """The fallback has no daylight-saving rules, so it is read at each call and not kept.

    Under a ``TZ`` that tzlocal cannot load but that has such rules, a server that runs across a
    clock change would hold the offset it had at import. Set the held fallback one hour off the
    local offset and run a flow that was due 5 minutes ago: it must not be due again at the next
    poll (offset behind), and its next run must not be an hour late (offset ahead).
    """
    monkeypatch.setattr(flow_service, "_SCHEDULER_TZ", timezone(_local_offset() + drift))
    script = tmp_path / "check.sh"
    script.write_text('#!/bin/sh\necho \'{"execute": false, "output": {}}\'\n')
    script.chmod(0o755)
    flow_file = tmp_path / "poll.flow.md"
    flow_file.write_text(
        '---\nname: poll\nschedule: "*/5 * * * *"\nagent_profile: developer\n'
        f"script: {script}\n---\nPrompt.\n"
    )
    database.create_flow(
        name="poll",
        file_path=str(flow_file),
        schedule="*/5 * * * *",
        agent_profile="developer",
        provider="kiro_cli",
        script=str(script),
        next_run=datetime.now() - timedelta(minutes=5),
    )
    assert [f.name for f in database.get_flows_to_run()] == ["poll"]

    before = datetime.now()
    assert asyncio.run(flow_service.execute_flow("poll")) is False

    stored = database.get_flow("poll").next_run
    assert database.get_flows_to_run() == [], f"due again at the next poll (next_run {stored})"
    assert before < stored <= datetime.now() + timedelta(minutes=5), f"next_run {stored}"
