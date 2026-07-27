"""Command-group navigation for the ``cao tui`` thin shell (U6, guided surfaces).

:class:`NavigationModel` is the navigator: it lists the top-level ``cao`` command
groups (from :meth:`CommandCatalog.groups`), drills into a group's commands
(:meth:`CommandCatalog.commands`), and — on selecting a command — hands off to
:class:`~cli_agent_orchestrator.tui.command_builder.CommandBuilder` via
:meth:`CommandBuilder.select`, which drives the U3 build -> preview -> run flow.
The ``[/]`` search filters the *currently visible* group/command names purely
client-side (a substring match over already-fetched catalog names — no network).

The workflow / memory guided composers (FR-6.1) are **not** special screens:
they are this same nav -> build -> preview -> run flow scoped to the ``workflow``
and ``memory`` command groups. :class:`NavigationModel` simply routes the
operator into those groups; running/authoring is a U3 shell-out
(``cao workflow ...`` / ``cao memory ...``), never a workflow-service import.
:class:`WorkflowListView` offers the optional *read-only* workflow listing over
U4's ``GET /workflows`` (which degrades to an unavailable flag when the server is
down) — it never mutates and never imports a workflow service.

U6 adds **no** CLI or HTTP logic of its own: it is pure orchestration of U2
(catalog), U3 (builder), and U4 (read-only workflow list). RD-e=A: there is no
status view here and no ``[s]`` binding — this module wires no key handlers at
all (the App owns the key map).

Import rule (thin shell, enforced by ``test/tui/test_thin_shell_boundary.py``):
only the standard library and the ``tui`` package's own modules may be imported
here. No ``cli``/``services``/``clients``/``backends``/``providers``/``models``.

Design references: business-logic-model W-1/W-2, frontend-components (NavList /
guided composers), code-generation-plan Steps 1 and 3.
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from cli_agent_orchestrator.tui.command_builder import CommandBuilder
from cli_agent_orchestrator.tui.command_catalog import Command, CommandCatalog, CommandGroup
from cli_agent_orchestrator.tui.server_client import (
    ServerClient,
    ServerUnavailable,
    WorkflowSummary,
)

# The two guided-composer groups (FR-6.1). They are ordinary catalog groups, not
# bespoke screens — navigating into them yields the same build/preview/run flow.
WORKFLOW_GROUP = "workflow"
MEMORY_GROUP = "memory"

# Navigation levels: browsing top-level groups, or a selected group's commands.
LEVEL_GROUPS = "groups"
LEVEL_COMMANDS = "commands"


def _matches(name: str, needle: str) -> bool:
    """True when ``needle`` is a case-insensitive substring of ``name``.

    The client-side ``[/]`` filter primitive (W-1 step 3): a plain substring
    match over catalog names, never a network round-trip.
    """

    return needle.strip().lower() in name.lower()


class NavigationModel:
    """Navigate the ``cao`` command surface and hand off to the U3 builder (W-1).

    The model owns *view/navigation* state only (which level, which group, the
    filter string, the selection index) — never domain state (that is U3's
    :class:`~cli_agent_orchestrator.tui.command_builder.BuilderState`). Every
    command surface, including the ``workflow``/``memory`` guided composers, is
    reached through the same two-level drill: groups -> commands -> build.
    """

    def __init__(
        self,
        catalog: CommandCatalog,
        builder: Optional[CommandBuilder] = None,
    ) -> None:
        """Bind the U2 catalog and the U3 builder the nav routes selections into.

        Args:
            catalog: The live command catalog (U2) — the source of groups and
                commands. Queried lazily; group results are cached for the run.
            builder: The command builder (U3) a command selection hands off to.
                Defaults to a fresh :class:`CommandBuilder` bound to ``catalog``
                so :meth:`open_command` resolves params from the same catalog.
        """

        self._catalog = catalog
        self._builder = builder if builder is not None else CommandBuilder(catalog)
        self._filter = ""
        self._active_group: Optional[str] = None
        self._selected_index = 0
        self._groups_cache: Optional[List[CommandGroup]] = None
        self._active_command: Optional[Command] = None

    # -- accessors ---------------------------------------------------------- #

    @property
    def builder(self) -> CommandBuilder:
        """The U3 builder a command selection hands off to."""

        return self._builder

    @property
    def level(self) -> str:
        """``"groups"`` when browsing groups, ``"commands"`` inside a group."""

        return LEVEL_GROUPS if self._active_group is None else LEVEL_COMMANDS

    @property
    def active_group(self) -> Optional[str]:
        """The group currently drilled into, or ``None`` at the groups level."""

        return self._active_group

    @property
    def active_command(self) -> Optional[Command]:
        """The command most recently opened into the builder, if any."""

        return self._active_command

    @property
    def filter_text(self) -> str:
        """The current ``[/]`` search string (empty when not filtering)."""

        return self._filter

    @property
    def selected_index(self) -> int:
        """Index of the highlighted row within the currently visible list."""

        return self._selected_index

    # -- catalog reads (cached groups; per-call commands) ------------------- #

    def groups(self) -> List[CommandGroup]:
        """Return every top-level command group (cached per run; W-1)."""

        if self._groups_cache is None:
            self._groups_cache = list(self._catalog.groups())
        return list(self._groups_cache)

    def commands(self, group: str) -> List[Command]:
        """Return a group's subcommands from the catalog (W-2)."""

        return list(self._catalog.commands(group))

    # -- filtering (client-side, W-1 step 3) -------------------------------- #

    def visible_groups(self) -> List[CommandGroup]:
        """Groups narrowed by the ``[/]`` filter (substring over names)."""

        if not self._filter:
            return self.groups()
        return [group for group in self.groups() if _matches(group.name, self._filter)]

    def visible_commands(self) -> List[Command]:
        """The active group's commands narrowed by the ``[/]`` filter.

        Empty when no group is active. A leaf group (no subcommands) yields an
        empty list — the App shows guiding empty-state copy, not an error.
        """

        if self._active_group is None:
            return []
        commands = self.commands(self._active_group)
        if not self._filter:
            return commands
        return [command for command in commands if _matches(command.name, self._filter)]

    def visible_names(self) -> List[str]:
        """The names of the rows visible at the current level (post-filter)."""

        if self.level == LEVEL_GROUPS:
            return [group.name for group in self.visible_groups()]
        return [command.name for command in self.visible_commands()]

    # -- filter mutation ---------------------------------------------------- #

    def set_filter(self, text: str) -> None:
        """Set the ``[/]`` search string and reset the highlight to the top."""

        self._filter = text or ""
        self._selected_index = 0

    def clear_filter(self) -> None:
        """Clear the ``[/]`` search string (show the full list again)."""

        self.set_filter("")

    # -- selection movement ------------------------------------------------- #

    def move(self, delta: int) -> int:
        """Move the highlight by ``delta`` rows, clamped to the visible list.

        Args:
            delta: Rows to move (negative = up, positive = down).

        Returns:
            The new selection index (``0`` when the list is empty).
        """

        count = len(self.visible_names())
        if count == 0:
            self._selected_index = 0
            return 0
        self._selected_index = max(0, min(self._selected_index + delta, count - 1))
        return self._selected_index

    # -- drill / route (W-1/W-2) -------------------------------------------- #

    def enter_group(self, group: str) -> List[Command]:
        """Drill into ``group``, clearing the filter, and return its commands.

        This is the single entry point for every group — including the
        ``workflow`` and ``memory`` guided composers (FR-6.1): navigating into
        them is exactly navigating into any other catalog group.
        """

        self._active_group = group
        self._filter = ""
        self._selected_index = 0
        return self.commands(group)

    def back(self) -> None:
        """Return from a group's commands to the top-level group list."""

        self._active_group = None
        self._filter = ""
        self._selected_index = 0

    def open_command(self, command: Command) -> CommandBuilder:
        """Hand a selected command off to the U3 builder (the nav -> build seam).

        Calls :meth:`CommandBuilder.select` with the command's full argv path;
        the builder then owns the build/preview/run flow. U6 adds no CLI logic —
        it only routes the selection.

        Args:
            command: The catalog command chosen by the operator.

        Returns:
            The bound :class:`CommandBuilder`, now selected on ``command.path``.
        """

        self._builder.select(command.path)
        self._active_command = command
        return self._builder

    # -- guided composers (FR-6.1) ------------------------------------------ #

    def enter_workflow(self) -> List[Command]:
        """Enter the ``workflow`` guided composer (the standard group drill)."""

        return self.enter_group(WORKFLOW_GROUP)

    def enter_memory(self) -> List[Command]:
        """Enter the ``memory`` guided composer (the standard group drill)."""

        return self.enter_group(MEMORY_GROUP)

    def is_guided_group(self, group: str) -> bool:
        """True for the ``workflow``/``memory`` guided-composer groups (FR-6.1)."""

        return group in (WORKFLOW_GROUP, MEMORY_GROUP)


class WorkflowListView:
    """Optional read-only listing of indexed workflows (U4 ``GET /workflows``).

    A convenience for the workflow guided composer: it shows *which* workflows
    exist so the operator can pick one to compose a ``cao workflow ...`` command
    around. It is strictly read-only — running or authoring a workflow is a U3
    shell-out, never an in-process call, and this view imports no workflow
    service (FR-6.1). A server-down read degrades to the :attr:`unavailable`
    flag (the App renders the unreachable state) rather than crashing (FR-9.1).
    """

    def __init__(self, client: Optional[ServerClient] = None) -> None:
        """Bind the read-only server client (U4).

        Args:
            client: The read-only :class:`ServerClient`. Defaults to a fresh
                client bound to the constants-derived base URL.
        """

        self._client = client if client is not None else ServerClient()
        self._workflows: List[WorkflowSummary] = []
        self.unavailable = False

    def load(self) -> List[WorkflowSummary]:
        """Fetch the workflow list; a server-down read sets :attr:`unavailable`.

        Returns:
            The (possibly empty) workflow list. An empty list is a normal
            empty-state, not an error; an unreachable server yields ``[]`` and
            flips :attr:`unavailable` to ``True``.
        """

        try:
            self._workflows = list(self._client.workflows())
            self.unavailable = False
        except ServerUnavailable:
            self._workflows = []
            self.unavailable = True
        return self.workflows

    @property
    def workflows(self) -> List[WorkflowSummary]:
        """The most recently loaded workflow summaries (read-only copy)."""

        return list(self._workflows)

    def names(self) -> List[str]:
        """The names of the loaded workflows (for a selectable list)."""

        return [workflow.name for workflow in self._workflows]

    def compose(self, navigation: NavigationModel) -> List[Command]:
        """Route into the ``workflow`` group's build flow (FR-6.1 convenience).

        Args:
            navigation: The shared :class:`NavigationModel` to drill.

        Returns:
            The ``workflow`` group's commands (the same nav -> build flow).
        """

        return navigation.enter_workflow()


def command_path_string(path: Sequence[str]) -> str:
    """Render a command path as the ``cao ...`` prefix it will build (display).

    A tiny display helper (e.g. ``["workflow", "run"]`` -> ``"cao workflow
    run"``). It builds no command and runs nothing — the exact argv and its
    execution are U3's responsibility.
    """

    return " ".join(["cao", *path])
