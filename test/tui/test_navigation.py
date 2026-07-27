"""Unit tests for :mod:`cli_agent_orchestrator.tui.navigation` (U6, guided surfaces).

Covers the W-1/W-2 navigation flow: listing command groups from U2's catalog,
drilling into a group's commands, routing a selected command into U3's
``CommandBuilder.select`` (asserted with the exact path), the client-side ``[/]``
filter narrowing the group/command list, and reaching the ``workflow`` /
``memory`` guided composers through the *same* flow (FR-6.1). The optional
read-only workflow listing (U4 ``GET /workflows``) is exercised including the
server-down degrade.

Every collaborator (U2 catalog, U3 builder, U4 client) is a mock — no real
``cao`` binary and no live server are touched. U6 adds no CLI/HTTP logic; these
tests assert it only *orchestrates* the existing units.
"""

from __future__ import annotations

from typing import List
from unittest import mock

from cli_agent_orchestrator.tui.command_catalog import Command, CommandGroup
from cli_agent_orchestrator.tui.navigation import (
    MEMORY_GROUP,
    WORKFLOW_GROUP,
    NavigationModel,
    WorkflowListView,
    command_path_string,
)
from cli_agent_orchestrator.tui.server_client import ServerUnavailable, WorkflowSummary

# --------------------------------------------------------------------------- #
# Catalog fixtures (faithful to what U2 produces).                               #
# --------------------------------------------------------------------------- #

GROUPS = [
    CommandGroup("launch", "Launch a session"),
    CommandGroup("session", "Manage sessions"),
    CommandGroup("workflow", "Run and manage workflows"),
    CommandGroup("memory", "Store and recall memory"),
]

WORKFLOW_COMMANDS = [
    Command("run", "Run a workflow", ["workflow", "run"]),
    Command("list", "List workflows", ["workflow", "list"]),
    Command("cancel", "Cancel a workflow", ["workflow", "cancel"]),
]

MEMORY_COMMANDS = [
    Command("store", "Store a memory", ["memory", "store"]),
    Command("recall", "Recall a memory", ["memory", "recall"]),
]

SESSION_COMMANDS = [
    Command("status", "Session status", ["session", "status"]),
    Command("list", "List sessions", ["session", "list"]),
]


def _catalog() -> mock.MagicMock:
    """A mock U2 CommandCatalog wired with the fixture groups/commands."""

    catalog = mock.MagicMock()
    catalog.groups.return_value = GROUPS

    def _commands(group: str) -> List[Command]:
        return {
            "workflow": WORKFLOW_COMMANDS,
            "memory": MEMORY_COMMANDS,
            "session": SESSION_COMMANDS,
            "launch": [],  # a leaf group (no subcommands)
        }[group]

    catalog.commands.side_effect = _commands
    return catalog


# --------------------------------------------------------------------------- #
# W-1: listing groups + drilling into commands.                                  #
# --------------------------------------------------------------------------- #


def test_lists_command_groups_from_catalog() -> None:
    """The navigator lists top-level groups sourced from U2 ``groups()`` (W-1)."""

    nav = NavigationModel(_catalog())

    assert [g.name for g in nav.groups()] == ["launch", "session", "workflow", "memory"]
    assert nav.level == "groups"
    assert nav.visible_names() == ["launch", "session", "workflow", "memory"]


def test_groups_are_cached_for_the_run() -> None:
    """Repeated group reads hit the catalog once (stable surface, cached)."""

    catalog = _catalog()
    nav = NavigationModel(catalog)

    nav.groups()
    nav.visible_groups()
    nav.groups()

    catalog.groups.assert_called_once_with()


def test_enter_group_lists_its_commands() -> None:
    """Drilling into a group lists its subcommands from U2 ``commands()`` (W-2)."""

    nav = NavigationModel(_catalog())

    commands = nav.enter_group("session")

    assert [c.name for c in commands] == ["status", "list"]
    assert nav.level == "commands"
    assert nav.active_group == "session"
    assert nav.visible_names() == ["status", "list"]


def test_back_returns_to_group_level() -> None:
    """``back()`` pops from a group's commands to the top-level group list."""

    nav = NavigationModel(_catalog())
    nav.enter_group("session")

    nav.back()

    assert nav.level == "groups"
    assert nav.active_group is None
    assert nav.visible_names() == ["launch", "session", "workflow", "memory"]


# --------------------------------------------------------------------------- #
# W-1/W-2: selecting a command routes into U3 CommandBuilder.select(path).        #
# --------------------------------------------------------------------------- #


def test_open_command_routes_into_builder_select_with_path() -> None:
    """Selecting a command hands off to U3 ``CommandBuilder.select`` (W-2).

    The core U6 seam: assert the builder's ``select`` was called with the
    command's exact argv path — U6 does the routing, U3 owns the build.
    """

    builder = mock.MagicMock()
    nav = NavigationModel(_catalog(), builder=builder)

    command = Command("status", "Session status", ["session", "status"])
    returned = nav.open_command(command)

    builder.select.assert_called_once_with(["session", "status"])
    assert returned is builder
    assert nav.active_command is command


def test_open_command_uses_full_path_for_nested_command() -> None:
    """The routed path is the command's full argv prefix, not just its name."""

    builder = mock.MagicMock()
    nav = NavigationModel(_catalog(), builder=builder)

    nav.enter_group("workflow")
    nav.open_command(WORKFLOW_COMMANDS[0])  # workflow run

    builder.select.assert_called_once_with(["workflow", "run"])


# --------------------------------------------------------------------------- #
# W-1 step 3: the [/] filter narrows the group/command list (client-side).       #
# --------------------------------------------------------------------------- #


def test_filter_narrows_the_group_list() -> None:
    """``[/]`` filters visible groups by a case-insensitive substring (no network)."""

    catalog = _catalog()
    nav = NavigationModel(catalog)

    nav.set_filter("work")
    assert nav.visible_names() == ["workflow"]

    # Case-insensitive.
    nav.set_filter("SESS")
    assert nav.visible_names() == ["session"]

    # No extra catalog round-trip — the filter is purely client-side.
    catalog.groups.assert_called_once_with()


def test_filter_narrows_the_command_list_within_a_group() -> None:
    """``[/]`` filters visible *commands* once drilled into a group."""

    nav = NavigationModel(_catalog())
    nav.enter_group("workflow")

    nav.set_filter("can")
    assert nav.visible_names() == ["cancel"]


def test_filter_that_matches_nothing_yields_empty_list() -> None:
    """A filter with no matches narrows to an empty list (edge case, no crash)."""

    nav = NavigationModel(_catalog())

    nav.set_filter("zzz-nope")
    assert nav.visible_names() == []
    # Movement on an empty list clamps to 0 rather than raising.
    assert nav.move(1) == 0


def test_clear_filter_restores_full_list() -> None:
    """Clearing the filter shows the full list again."""

    nav = NavigationModel(_catalog())
    nav.set_filter("work")
    assert nav.visible_names() == ["workflow"]

    nav.clear_filter()
    assert nav.visible_names() == ["launch", "session", "workflow", "memory"]


def test_entering_a_group_resets_the_filter() -> None:
    """Drilling into a group clears any active group-level filter."""

    nav = NavigationModel(_catalog())
    nav.set_filter("work")

    nav.enter_group("workflow")
    assert nav.filter_text == ""
    assert nav.visible_names() == ["run", "list", "cancel"]


# --------------------------------------------------------------------------- #
# Selection movement.                                                            #
# --------------------------------------------------------------------------- #


def test_move_clamps_within_visible_list() -> None:
    """Highlight movement clamps to the visible list bounds."""

    nav = NavigationModel(_catalog())

    assert nav.selected_index == 0
    assert nav.move(-1) == 0  # clamp at top
    assert nav.move(2) == 2
    assert nav.move(99) == 3  # clamp at bottom (4 groups)


# --------------------------------------------------------------------------- #
# FR-6.1: workflow / memory guided composers reached through the SAME flow.      #
# --------------------------------------------------------------------------- #


def test_workflow_composer_is_the_same_nav_build_flow() -> None:
    """The workflow guided composer IS entering the ``workflow`` group (FR-6.1).

    Not a special screen: ``enter_workflow`` drills the ``workflow`` catalog
    group, and selecting one of its commands routes into U3 exactly like any
    other command.
    """

    builder = mock.MagicMock()
    nav = NavigationModel(_catalog(), builder=builder)

    commands = nav.enter_workflow()
    assert nav.active_group == WORKFLOW_GROUP
    assert [c.name for c in commands] == ["run", "list", "cancel"]
    assert nav.is_guided_group(WORKFLOW_GROUP)

    nav.open_command(commands[0])
    builder.select.assert_called_once_with(["workflow", "run"])


def test_memory_composer_is_the_same_nav_build_flow() -> None:
    """The memory guided composer IS entering the ``memory`` group (FR-6.1)."""

    builder = mock.MagicMock()
    nav = NavigationModel(_catalog(), builder=builder)

    commands = nav.enter_memory()
    assert nav.active_group == MEMORY_GROUP
    assert [c.name for c in commands] == ["store", "recall"]
    assert nav.is_guided_group(MEMORY_GROUP)

    nav.open_command(commands[1])
    builder.select.assert_called_once_with(["memory", "recall"])


def test_non_guided_group_is_not_flagged_as_guided() -> None:
    """A plain group (e.g. ``session``) is not a guided composer."""

    nav = NavigationModel(_catalog())
    assert nav.is_guided_group("session") is False


# --------------------------------------------------------------------------- #
# Optional read-only workflow listing (U4 GET /workflows).                       #
# --------------------------------------------------------------------------- #


def test_workflow_list_view_reads_from_u4_workflows() -> None:
    """The optional workflow list sources names from U4 ``workflows()`` (read-only)."""

    client = mock.MagicMock()
    client.workflows.return_value = [
        WorkflowSummary(name="deploy"),
        WorkflowSummary(name="review"),
    ]

    view = WorkflowListView(client=client)
    view.load()

    client.workflows.assert_called_once_with()
    assert view.names() == ["deploy", "review"]
    assert view.unavailable is False


def test_workflow_list_view_server_down_degrades_not_crash() -> None:
    """Edge case: a server-down workflow read degrades to an unavailable flag.

    Command building still works; the read simply reports unavailable rather
    than raising into the shell (FR-9.1).
    """

    client = mock.MagicMock()
    client.workflows.side_effect = ServerUnavailable("cao-server down")

    view = WorkflowListView(client=client)
    result = view.load()

    assert result == []
    assert view.names() == []
    assert view.unavailable is True


def test_workflow_list_view_empty_is_normal_state() -> None:
    """Edge case: an empty workflow list is a normal empty-state, not an error."""

    client = mock.MagicMock()
    client.workflows.return_value = []

    view = WorkflowListView(client=client)
    assert view.load() == []
    assert view.unavailable is False


def test_workflow_list_compose_routes_into_workflow_group() -> None:
    """``compose`` routes the shared navigator into the ``workflow`` group (FR-6.1)."""

    nav = NavigationModel(_catalog(), builder=mock.MagicMock())
    view = WorkflowListView(client=mock.MagicMock())

    commands = view.compose(nav)

    assert nav.active_group == WORKFLOW_GROUP
    assert [c.name for c in commands] == ["run", "list", "cancel"]


# --------------------------------------------------------------------------- #
# Display helper.                                                                #
# --------------------------------------------------------------------------- #


def test_command_path_string_renders_cao_prefix() -> None:
    """The display helper renders a path as the ``cao ...`` prefix (no execution)."""

    assert command_path_string(["workflow", "run"]) == "cao workflow run"
    assert command_path_string(["launch"]) == "cao launch"
