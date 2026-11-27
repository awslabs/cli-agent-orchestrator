"""Database support for creating a flow disabled and editing a flow's definition columns."""

from datetime import datetime

import pytest

from cli_agent_orchestrator.clients import database

pytestmark = pytest.mark.usefixtures("isolated_memory_db")

PAST = datetime(2020, 1, 1, 9, 0)
LATER = datetime(2030, 6, 1, 8, 30)


def create(name="daily-report", **extra):
    return database.create_flow(
        name=name,
        file_path=f"/flows/{name}.flow.md",
        schedule="0 9 * * *",
        agent_profile="developer",
        provider="claude_code",
        script="gate.py",
        next_run=PAST,
        **extra,
    )


def due_names():
    return [flow.name for flow in database.get_flows_to_run()]


def test_create_flow_is_enabled_by_default():
    created = create()

    assert created.enabled is True
    assert database.get_flow("daily-report").enabled is True
    assert due_names() == ["daily-report"]


def test_create_flow_can_store_a_disabled_flow_that_never_comes_due():
    created = create(enabled=False)

    assert created.enabled is False
    assert database.get_flow("daily-report").enabled is False
    assert [f.name for f in database.list_flows() if not f.enabled] == ["daily-report"]
    assert due_names() == []


def test_update_flow_definition_changes_the_definition_columns_and_next_run():
    create()

    updated = database.update_flow_definition(
        "daily-report",
        schedule="30 8 * * *",
        agent_profile="reviewer",
        provider="q_cli",
        next_run=LATER,
    )

    row = database.get_flow("daily-report")
    assert updated is True
    assert (row.schedule, row.agent_profile, row.provider, row.next_run) == (
        "30 8 * * *",
        "reviewer",
        "q_cli",
        LATER,
    )


def test_update_flow_definition_leaves_script_enabled_and_file_path_alone():
    create(enabled=False)

    database.update_flow_definition(
        "daily-report",
        schedule="30 8 * * *",
        agent_profile="reviewer",
        provider="q_cli",
        next_run=LATER,
    )

    row = database.get_flow("daily-report")
    assert (row.script, row.enabled, row.file_path) == (
        "gate.py",
        False,
        "/flows/daily-report.flow.md",
    )


def test_update_flow_definition_without_next_run_keeps_the_stored_one():
    create()

    database.update_flow_definition(
        "daily-report", schedule="30 8 * * *", agent_profile="developer", provider="claude_code"
    )

    row = database.get_flow("daily-report")
    assert (row.schedule, row.next_run) == ("30 8 * * *", PAST)


def test_update_flow_definition_touches_only_the_named_flow():
    create("first-flow")
    create("second-flow")

    database.update_flow_definition(
        "first-flow",
        schedule="30 8 * * *",
        agent_profile="reviewer",
        provider="q_cli",
        next_run=LATER,
    )

    other = database.get_flow("second-flow")
    assert (other.schedule, other.agent_profile, other.provider) == (
        "0 9 * * *",
        "developer",
        "claude_code",
    )


def test_update_flow_definition_for_an_unknown_flow_returns_false_and_creates_nothing():
    updated = database.update_flow_definition(
        "missing", schedule="30 8 * * *", agent_profile="developer", provider="claude_code"
    )

    assert updated is False
    assert database.list_flows() == []
