"""Profiles browser + launch profile-picker for the ``cao tui`` thin shell (U6).

:class:`ProfilesBrowser` is the guided surface over the agent-profile concept.
Per FR-7.1 / ADR-003 it is labelled **"Profiles"** in *every* user-facing string
(never "Agents"); the underlying REST paths — ``GET /agents/profiles`` and
``GET /agents/profiles/{name}`` — are consumed *unchanged* through U4's
:class:`~cli_agent_orchestrator.tui.server_client.ServerClient`. The label is a
pure view-layer constant; nothing is renamed.

What it does (W-3):

* **List** the profiles via :meth:`ServerClient.profiles`.
* **Detail** a selected profile via :meth:`ServerClient.profile` — showing its
  provider, tools/capabilities, and description (the issue §2.7 preview).
* **Launch picker**: from a selected profile, produce a pre-filled
  ``cao launch --agents <profile>`` build by handing off to the U3
  :class:`~cli_agent_orchestrator.tui.command_builder.CommandBuilder`.
* **Provider readiness**: the chosen profile's provider install/PATH status is
  shown as TEXT (NFR-6) from U4's
  :class:`~cli_agent_orchestrator.tui.provider_preflight.ProviderPreflight`.

Error handling (FR-9.1 / BR-5): a
:class:`~cli_agent_orchestrator.tui.server_client.ServerUnavailable` during any
read flips the browser into an *unavailable* view state — command building and
copy still work; the shell never crashes. A
:class:`~cli_agent_orchestrator.tui.server_client.ProfileNotFound` on a detail
read is a renderable "profile not found" note, not a crash.

RD-e=A: there is **no** status view / status pane here and no ``[s]`` handler —
this module wires no key bindings at all (the App owns the key map).

U6 adds **no** CLI or HTTP logic of its own: it composes U4 (reads), U4's
pre-flight, and U3 (the launch build). Import rule (thin shell, enforced by
``test/tui/test_thin_shell_boundary.py``): only the standard library and the
``tui`` package's own modules may be imported here — never
``cli``/``services``/``clients``/``backends``/``providers``/``models``.

Design references: business-logic-model W-3, frontend-components (ProfilesBrowser),
code-generation-plan Step 2.
"""

from __future__ import annotations

from typing import List, Optional

from cli_agent_orchestrator.tui.command_builder import CommandBuilder
from cli_agent_orchestrator.tui.command_catalog import Param
from cli_agent_orchestrator.tui.provider_preflight import PreflightRow, ProviderPreflight
from cli_agent_orchestrator.tui.server_client import (
    ProfileDetail,
    ProfileNotFound,
    ProfileSummary,
    ServerClient,
    ServerUnavailable,
)

# The user-facing label for the agent-profile concept — used EVERYWHERE in this
# surface (FR-7.1 / ADR-003 label-only). The API paths stay ``/agents/...``; only
# the display noun is "Profiles". Deliberately never "Agents".
PROFILES_LABEL = "Profiles"

# Title/heading strings, all derived from PROFILES_LABEL so the label is applied
# in one place and can never drift to "Agents".
BROWSER_TITLE = PROFILES_LABEL
DETAIL_TITLE = f"{PROFILES_LABEL} detail"

# Guiding empty-state copy (an empty list is not an error — W-3 / edge cases).
EMPTY_STATE_TEXT = f"No {PROFILES_LABEL.lower()} found."

# Copy shown when cao-server is unreachable mid-browse (FR-9.1). Command building
# and copy still work; only live reads are unavailable.
UNAVAILABLE_TEXT = (
    f"{PROFILES_LABEL} are unavailable: cao-server is not reachable. "
    "You can still build and copy commands; live reads resume when the server is up."
)

# The launch subcommand and the profile flag it pre-fills. ``cao launch --agents
# <profile>`` is the exact CLI contract (the flag is named ``--agents``; only the
# UI concept is relabelled "Profiles"). No rename of the flag (ADR-003).
LAUNCH_PATH = ["launch"]
AGENTS_FLAG = "--agents"

# View states the browser can occupy.
STATE_LIST = "list"
STATE_DETAIL = "detail"
STATE_UNAVAILABLE = "unavailable"
STATE_NOT_FOUND = "not_found"


class ProfilesBrowser:
    """Browse "Profiles", preview detail, and launch — composing U4/U3 (W-3).

    Holds view/navigation state only (the current state, the loaded summaries,
    the selected/detailed profile, the selection index). All data comes from
    U4's read-only client; the launch build is delegated to U3. Server-down is a
    first-class *state*, not an exception that escapes the shell.
    """

    def __init__(
        self,
        client: Optional[ServerClient] = None,
        *,
        builder: Optional[CommandBuilder] = None,
        preflight: Optional[ProviderPreflight] = None,
    ) -> None:
        """Bind the U4 client, the U3 builder, and the U4 pre-flight helper.

        Args:
            client: Read-only :class:`ServerClient` (U4) — the sole read source.
                Defaults to a fresh client on the constants-derived base URL.
            builder: The U3 :class:`CommandBuilder` the launch picker pre-fills.
                Defaults to a fresh builder (no catalog bound; the launch build
                supplies its own param, so no catalog lookup is required).
            preflight: The U4 :class:`ProviderPreflight`. Defaults to one bound
                to the same ``client`` so provider readiness shares the seam.
        """

        self._client = client if client is not None else ServerClient()
        self._builder = builder if builder is not None else CommandBuilder()
        self._preflight = preflight if preflight is not None else ProviderPreflight(self._client)

        self._state = STATE_LIST
        self._profiles: List[ProfileSummary] = []
        self._selected_index = 0
        self._detail: Optional[ProfileDetail] = None
        self._not_found_name: Optional[str] = None

    # -- accessors ---------------------------------------------------------- #

    @property
    def label(self) -> str:
        """The user-facing label for this surface — always "Profiles"."""

        return PROFILES_LABEL

    @property
    def title(self) -> str:
        """The browser heading (labelled "Profiles", never "Agents")."""

        return BROWSER_TITLE

    @property
    def state(self) -> str:
        """The current view state (list / detail / unavailable / not_found)."""

        return self._state

    @property
    def unavailable(self) -> bool:
        """True when a read failed because cao-server was unreachable (FR-9.1)."""

        return self._state == STATE_UNAVAILABLE

    @property
    def builder(self) -> CommandBuilder:
        """The U3 builder the launch picker pre-fills."""

        return self._builder

    @property
    def profiles(self) -> List[ProfileSummary]:
        """The most recently loaded profile summaries (read-only copy)."""

        return list(self._profiles)

    @property
    def detail(self) -> Optional[ProfileDetail]:
        """The detail of the profile currently opened, if any."""

        return self._detail

    @property
    def selected_index(self) -> int:
        """Index of the highlighted profile within the loaded list."""

        return self._selected_index

    @property
    def not_found_name(self) -> Optional[str]:
        """The profile name that a detail read reported as not found, if any."""

        return self._not_found_name

    # -- W-3 step 1: list --------------------------------------------------- #

    def load(self) -> List[ProfileSummary]:
        """List "Profiles" via ``GET /agents/profiles`` (U4), handling server-down.

        On success the browser enters the ``list`` state with the fetched
        summaries (an empty list is a normal empty-state, not an error). A
        :class:`ServerUnavailable` flips the browser to the ``unavailable`` state
        and yields an empty list — the shell never crashes (FR-9.1 / BR-5).

        Returns:
            The loaded profile summaries (empty on empty list or server-down).
        """

        try:
            self._profiles = list(self._client.profiles())
        except ServerUnavailable:
            self._profiles = []
            self._detail = None
            self._state = STATE_UNAVAILABLE
            return []

        self._state = STATE_LIST
        self._selected_index = 0
        self._detail = None
        self._not_found_name = None
        return self.profiles

    def names(self) -> List[str]:
        """The names of the loaded "Profiles" (for the selectable list pane)."""

        return [profile.name for profile in self._profiles]

    def is_empty(self) -> bool:
        """True when no "Profiles" are loaded (drives the empty-state copy)."""

        return not self._profiles

    def list_text(self) -> str:
        """The list-pane body: one profile name per line, or the empty-state copy.

        Rendered under the "Profiles" heading. When cao-server is unreachable the
        unavailable copy is shown instead (never a crash / never colour-only).
        """

        if self._state == STATE_UNAVAILABLE:
            return UNAVAILABLE_TEXT
        if self.is_empty():
            return EMPTY_STATE_TEXT
        return "\n".join(self.names())

    # -- selection movement ------------------------------------------------- #

    def move(self, delta: int) -> int:
        """Move the highlight by ``delta`` rows, clamped to the loaded list."""

        count = len(self._profiles)
        if count == 0:
            self._selected_index = 0
            return 0
        self._selected_index = max(0, min(self._selected_index + delta, count - 1))
        return self._selected_index

    def selected_profile(self) -> Optional[ProfileSummary]:
        """The summary currently highlighted, or ``None`` when the list is empty."""

        if not self._profiles:
            return None
        return self._profiles[self._selected_index]

    # -- W-3 step 1: detail ------------------------------------------------- #

    def open_detail(self, name: str) -> Optional[ProfileDetail]:
        """Show one profile's detail via ``GET /agents/profiles/{name}`` (U4).

        Enters the ``detail`` state with the profile's provider / tools
        (capabilities) / description — the §2.7 preview. A
        :class:`ProfileNotFound` enters the ``not_found`` state (renderable note,
        not a crash); a :class:`ServerUnavailable` mid-browse flips to the
        ``unavailable`` state (FR-9.1).

        Args:
            name: The profile name to detail (from the list selection).

        Returns:
            The :class:`ProfileDetail`, or ``None`` on not-found / server-down.
        """

        try:
            self._detail = self._client.profile(name)
        except ProfileNotFound:
            self._detail = None
            self._not_found_name = name
            self._state = STATE_NOT_FOUND
            return None
        except ServerUnavailable:
            self._detail = None
            self._state = STATE_UNAVAILABLE
            return None

        self._not_found_name = None
        self._state = STATE_DETAIL
        return self._detail

    def detail_text(self, detail: Optional[ProfileDetail] = None) -> str:
        """Render the §2.7 preview: provider / tools / description (as TEXT).

        Uses the passed detail or the last-opened one. When the browser is in the
        ``unavailable`` / ``not_found`` state the matching copy is returned
        instead. All labels are neutral field names; the surface title says
        "Profiles" — never "Agents".
        """

        if self._state == STATE_UNAVAILABLE:
            return UNAVAILABLE_TEXT
        if self._state == STATE_NOT_FOUND:
            return f"{PROFILES_LABEL[:-1]} '{self._not_found_name}' not found."

        target = detail if detail is not None else self._detail
        if target is None:
            return "Select a profile to preview its detail."

        tools = ", ".join(target.capabilities) if target.capabilities else "(none listed)"
        lines = [
            f"{DETAIL_TITLE}: {target.name}",
            "",
            f"provider:    {target.provider or '(profile default)'}",
            f"tools:       {tools}",
            f"description: {target.description or '(none)'}",
        ]
        if target.role:
            lines.append(f"role:        {target.role}")
        return "\n".join(lines)

    # -- W-3 step 3: provider readiness ------------------------------------- #

    def provider_readiness(self, provider: Optional[str]) -> str:
        """Provider install/PATH readiness for ``provider`` as TEXT (NFR-6).

        Sourced from U4's :class:`ProviderPreflight` (the sole ``GET
        /agents/providers`` seam). A server-down read degrades to a text note,
        never a crash. ``None`` (a profile with no explicit provider) reports
        that the CLI default applies.

        Args:
            provider: The profile's provider name, or ``None``.

        Returns:
            A one-line readiness string (e.g. ``"provider 'claude_code':
            installed yes"``) — text only, never colour.
        """

        if not provider:
            return "provider: (profile default — resolved by the cao CLI on launch)"
        try:
            rows: List[PreflightRow] = self._preflight.rows()
        except ServerUnavailable:
            return f"provider '{provider}': readiness unavailable (cao-server not reachable)"
        for row in rows:
            if row.name == provider:
                return f"provider '{provider}': installed {row.installed_text}"
        return f"provider '{provider}': not reported by cao-server"

    def selected_provider_readiness(self) -> str:
        """Readiness text for the currently detailed profile's provider (NFR-6)."""

        provider = self._detail.provider if self._detail is not None else None
        return self.provider_readiness(provider)

    # -- W-3 step 2: launch picker (pre-fill U3 build) ---------------------- #

    def launch_build(self, profile_name: str) -> CommandBuilder:
        """Pre-fill a ``cao launch --agents <profile>`` build via U3 (W-3 step 2).

        Selects ``cao launch`` on the U3 builder and records ``--agents`` =
        ``profile_name``. The flag stays ``--agents`` (the CLI contract is
        unchanged, ADR-003); only the browsing concept is labelled "Profiles".
        The build/preview/run and copy flow is then U3's — U6 adds no CLI logic.

        Args:
            profile_name: The chosen profile to pre-fill as ``--agents``.

        Returns:
            The bound :class:`CommandBuilder`, selected on ``launch`` with
            ``--agents`` set — its :meth:`CommandBuilder.preview_argv` yields
            ``["cao", "launch", "--agents", "<profile>"]``.
        """

        # ``--agents`` takes a value; declare it explicitly so the builder emits
        # the flag/value pair without needing a catalog round-trip.
        agents_param = Param(
            name=AGENTS_FLAG,
            kind="option",
            required=True,
            takes_value=True,
            choices=None,
            help="Profile to launch",
        )
        self._builder.select(LAUNCH_PATH, params=[agents_param])
        self._builder.set_arg(AGENTS_FLAG, profile_name)
        return self._builder

    def launch_selected(self) -> Optional[CommandBuilder]:
        """Pre-fill a launch build for the highlighted profile (convenience).

        Returns:
            The pre-filled builder, or ``None`` when no profile is selected
            (empty list / server-down).
        """

        selected = self.selected_profile()
        if selected is None:
            return None
        return self.launch_build(selected.name)
