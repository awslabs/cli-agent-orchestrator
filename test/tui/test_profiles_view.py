"""Unit tests for :mod:`cli_agent_orchestrator.tui.profiles_view` (U6, guided surfaces).

Covers W-3: listing "Profiles" from U4 ``profiles()`` (mocked), detail from U4
``profile(name)`` (provider / tools / description — the §2.7 preview), the
FR-7.1 / ADR-003 label invariant (every user-facing string says **"Profiles"**,
never "Agents"), provider readiness from U4 ``ProviderPreflight`` as TEXT, and
the launch picker pre-filling ``cao launch --agents <profile>`` into a U3 build.

Server-down (``ServerUnavailable``) becomes an *unavailable view state* (not a
crash — FR-9.1), including mid-browse. Edge cases: an empty profile list and a
``ServerUnavailable`` raised while opening a detail. Also asserts RD-e=A: U6
ships no status view / status pane and binds no ``[s]`` key.

Every collaborator (U4 client, U4 pre-flight, U3 builder) is a mock — no live
server, no real ``cao``. U6 adds no CLI/HTTP logic; it composes U4 + U3.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List
from unittest import mock

import cli_agent_orchestrator
from cli_agent_orchestrator.tui import navigation, profiles_view
from cli_agent_orchestrator.tui.profiles_view import (
    PROFILES_LABEL,
    ProfilesBrowser,
)
from cli_agent_orchestrator.tui.server_client import (
    ProfileDetail,
    ProfileNotFound,
    ProfileSummary,
    ServerAuthRequired,
    ServerClientError,
    ServerUnavailable,
)

# --------------------------------------------------------------------------- #
# Fixtures (faithful to the f570de1 /agents/profiles response shape).            #
# --------------------------------------------------------------------------- #

PROFILE_SUMMARIES = [
    ProfileSummary(name="backend-dev", description="Backend engineer", role="dev"),
    ProfileSummary(name="reviewer", description="Code reviewer", role="review"),
    ProfileSummary(name="architect", description="System architect", role="arch"),
]

BACKEND_DETAIL = ProfileDetail(
    name="backend-dev",
    description="Backend engineer",
    provider="claude_code",
    role="dev",
    capabilities=["read", "write", "bash"],
)


def _browser(
    *,
    profiles: List[ProfileSummary] | None = None,
    detail: ProfileDetail | None = None,
    builder: mock.MagicMock | None = None,
    preflight: mock.MagicMock | None = None,
) -> ProfilesBrowser:
    """Build a ProfilesBrowser over mocked U4/U3 collaborators."""

    client = mock.MagicMock()
    client.profiles.return_value = profiles if profiles is not None else PROFILE_SUMMARIES
    client.profile.return_value = detail if detail is not None else BACKEND_DETAIL
    return ProfilesBrowser(
        client=client,
        builder=builder if builder is not None else mock.MagicMock(),
        preflight=preflight if preflight is not None else mock.MagicMock(),
    )


# --------------------------------------------------------------------------- #
# W-3 step 1: list from U4.profiles(); detail from U4.profile(name).             #
# --------------------------------------------------------------------------- #


def test_list_sources_from_u4_profiles() -> None:
    """The browser lists "Profiles" from U4 ``profiles()`` (W-3, mocked client)."""

    browser = _browser()
    loaded = browser.load()

    assert [p.name for p in loaded] == ["backend-dev", "reviewer", "architect"]
    assert browser.names() == ["backend-dev", "reviewer", "architect"]
    assert browser.state == "list"
    assert browser.is_empty() is False


def test_detail_sources_from_u4_profile_name() -> None:
    """Opening a detail calls U4 ``profile(name)`` and shows provider/tools/desc."""

    client = mock.MagicMock()
    client.profiles.return_value = PROFILE_SUMMARIES
    client.profile.return_value = BACKEND_DETAIL
    browser = ProfilesBrowser(client=client, builder=mock.MagicMock(), preflight=mock.MagicMock())
    browser.load()

    detail = browser.open_detail("backend-dev")

    client.profile.assert_called_once_with("backend-dev")
    assert detail == BACKEND_DETAIL
    assert browser.state == "detail"

    body = browser.detail_text()
    assert "backend-dev" in body
    assert "claude_code" in body  # provider
    assert "read, write, bash" in body  # tools (capabilities)
    assert "Backend engineer" in body  # description


def test_selected_profile_tracks_movement() -> None:
    """Highlight movement selects within the loaded list (clamped)."""

    browser = _browser()
    browser.load()

    assert browser.selected_profile().name == "backend-dev"
    browser.move(1)
    assert browser.selected_profile().name == "reviewer"
    browser.move(99)  # clamp at bottom
    assert browser.selected_profile().name == "architect"


# --------------------------------------------------------------------------- #
# FR-7.1 / ADR-003: "Profiles" everywhere; NEVER "Agents".                        #
# --------------------------------------------------------------------------- #


def test_label_is_profiles_never_agents() -> None:
    """Every user-facing label says "Profiles"; the concept is never "Agents"."""

    browser = _browser()
    browser.load()
    browser.open_detail("backend-dev")

    assert PROFILES_LABEL == "Profiles"
    assert browser.label == "Profiles"
    assert browser.title == "Profiles"

    # The concept label must never surface as "Agents"/"Agent" in the UI CHROME
    # (labels/titles/static copy) — the actual FR-7.1/ADR-003 invariant. We
    # deliberately scan only the static, U6-authored strings, NOT list_text() /
    # detail_text() / provider_readiness(), which render dynamic server data
    # (a profile literally named "Agents-worker" or a description mentioning
    # "agents" is the server's content, not a U6 label defect). The dynamic
    # surfaces are exercised for behaviour in their own tests.
    chrome = " ".join(
        [
            browser.label,
            browser.title,
            profiles_view.EMPTY_STATE_TEXT,
            profiles_view.UNAVAILABLE_TEXT,
            profiles_view.BROWSER_TITLE,
            profiles_view.DETAIL_TITLE,
        ]
    )
    assert "Profiles" in chrome
    assert "Agents" not in chrome
    assert "Agent " not in chrome


def test_user_facing_constants_do_not_say_agents() -> None:
    """The module's user-facing label constants never read "Agents"."""

    for text in (
        profiles_view.PROFILES_LABEL,
        profiles_view.BROWSER_TITLE,
        profiles_view.DETAIL_TITLE,
        profiles_view.EMPTY_STATE_TEXT,
        profiles_view.UNAVAILABLE_TEXT,
    ):
        assert "Agents" not in text


def test_api_path_flag_stays_agents_unchanged() -> None:
    """ADR-003 label-only: the CLI flag stays ``--agents`` (no rename)."""

    # The launch flag the picker pre-fills is the unchanged CLI contract.
    assert profiles_view.AGENTS_FLAG == "--agents"


# --------------------------------------------------------------------------- #
# W-3 step 2: launch picker pre-fills `cao launch --agents <profile>` (U3).       #
# --------------------------------------------------------------------------- #


def test_launch_build_prefills_agents_flag_into_u3() -> None:
    """The launch picker pre-fills ``--agents <profile>`` into a real U3 build.

    Uses a real :class:`CommandBuilder` (not a mock) to prove the resulting argv
    is exactly ``cao launch --agents <profile>`` — the byte-identical U3 preview.
    """

    from cli_agent_orchestrator.tui.command_builder import CommandBuilder

    browser = ProfilesBrowser(
        client=mock.MagicMock(),
        builder=CommandBuilder(),
        preflight=mock.MagicMock(),
    )

    builder = browser.launch_build("backend-dev")

    assert builder.preview_argv() == ["cao", "launch", "--agents", "backend-dev"]
    assert builder.preview_string() == "cao launch --agents backend-dev"


def test_launch_selected_prefills_highlighted_profile() -> None:
    """``launch_selected`` pre-fills a build for the highlighted profile."""

    from cli_agent_orchestrator.tui.command_builder import CommandBuilder

    client = mock.MagicMock()
    client.profiles.return_value = PROFILE_SUMMARIES
    browser = ProfilesBrowser(client=client, builder=CommandBuilder(), preflight=mock.MagicMock())
    browser.load()
    browser.move(1)  # highlight "reviewer"

    builder = browser.launch_selected()

    assert builder is not None
    assert builder.preview_argv() == ["cao", "launch", "--agents", "reviewer"]


def test_launch_selected_none_when_no_profile() -> None:
    """With no profiles loaded, the launch picker yields ``None`` (no crash)."""

    browser = _browser(profiles=[])
    browser.load()
    assert browser.launch_selected() is None


# --------------------------------------------------------------------------- #
# W-3 step 3: provider readiness from U4 ProviderPreflight (TEXT).               #
# --------------------------------------------------------------------------- #


def test_provider_readiness_from_preflight_text() -> None:
    """Provider readiness is TEXT sourced from U4 ``ProviderPreflight`` (NFR-6)."""

    from cli_agent_orchestrator.tui.provider_preflight import PreflightRow

    preflight = mock.MagicMock()
    preflight.rows.return_value = [
        PreflightRow("claude_code", "claude", "yes"),
        PreflightRow("codex", "codex", "no"),
    ]
    browser = _browser(preflight=preflight)

    installed = browser.provider_readiness("claude_code")
    missing = browser.provider_readiness("codex")

    assert "claude_code" in installed and "yes" in installed
    assert "codex" in missing and "no" in missing
    # No colour codes — plain text only.
    assert "\x1b[" not in installed


def test_provider_readiness_server_down_is_text_not_crash() -> None:
    """A server-down provider read degrades to a text note, never a crash."""

    preflight = mock.MagicMock()
    preflight.rows.side_effect = ServerUnavailable("cao-server down")
    browser = _browser(preflight=preflight)

    text = browser.provider_readiness("claude_code")
    assert "unavailable" in text.lower()


def test_provider_readiness_none_reports_default() -> None:
    """A profile with no explicit provider reports the CLI default applies."""

    browser = _browser()
    text = browser.provider_readiness(None)
    assert "default" in text.lower()


# --------------------------------------------------------------------------- #
# FR-9.1 / BR-5: server-down → unavailable state (edge cases).                    #
# --------------------------------------------------------------------------- #


def test_server_down_on_load_enters_unavailable_state() -> None:
    """Edge case: ``ServerUnavailable`` on list → unavailable state, not a crash."""

    client = mock.MagicMock()
    client.profiles.side_effect = ServerUnavailable("cao-server down")
    browser = ProfilesBrowser(client=client, builder=mock.MagicMock(), preflight=mock.MagicMock())

    result = browser.load()

    assert result == []
    assert browser.unavailable is True
    assert browser.state == "unavailable"
    # The list pane renders the unavailable copy (still "Profiles"-labelled).
    assert "unavailable" in browser.list_text().lower()
    assert "Profiles" in browser.list_text()


def test_server_down_mid_browse_on_detail_enters_unavailable() -> None:
    """Edge case: ``ServerUnavailable`` while opening a detail mid-browse.

    The list loaded fine, then the server drops before the detail read — the
    browser flips to the unavailable state instead of crashing (FR-9.1).
    """

    client = mock.MagicMock()
    client.profiles.return_value = PROFILE_SUMMARIES
    client.profile.side_effect = ServerUnavailable("cao-server dropped")
    browser = ProfilesBrowser(client=client, builder=mock.MagicMock(), preflight=mock.MagicMock())

    browser.load()
    assert browser.state == "list"  # list succeeded

    detail = browser.open_detail("backend-dev")

    assert detail is None
    assert browser.unavailable is True
    assert browser.state == "unavailable"


def test_empty_profile_list_is_guiding_empty_state() -> None:
    """Edge case: an empty profile list is a guiding empty-state, not an error."""

    browser = _browser(profiles=[])
    loaded = browser.load()

    assert loaded == []
    assert browser.is_empty() is True
    assert browser.state == "list"  # empty is normal, not unavailable
    assert browser.unavailable is False
    body = browser.list_text()
    assert "No profiles" in body.lower().replace("  ", " ") or "no profiles" in body.lower()
    assert "Profiles" in profiles_view.EMPTY_STATE_TEXT or "profiles" in body.lower()


def test_profile_not_found_is_renderable_note_not_crash() -> None:
    """A 404 on detail → not-found state with a renderable note (not a crash)."""

    client = mock.MagicMock()
    client.profiles.return_value = PROFILE_SUMMARIES
    client.profile.side_effect = ProfileNotFound("agent profile 'ghost' not found")
    browser = ProfilesBrowser(client=client, builder=mock.MagicMock(), preflight=mock.MagicMock())
    browser.load()

    detail = browser.open_detail("ghost")

    assert detail is None
    assert browser.state == "not_found"
    assert browser.not_found_name == "ghost"
    assert "ghost" in browser.detail_text()


# --------------------------------------------------------------------------- #
# FR-9.1 / BR-5: a MALFORMED payload (ServerClientError) degrades identically.   #
#                                                                                #
# ServerClientError and ServerUnavailable both inherit Exception DIRECTLY — they #
# are siblings, neither a subclass of the other (server_client.py L55 / L64). A  #
# catch of ServerUnavailable alone therefore does NOT catch ServerClientError,   #
# so a contract-drift payload would escape into the shell. These pin the three   #
# `except (ServerUnavailable, ServerClientError)` sites (load / open_detail /     #
# provider_readiness) to the malformed-payload branch specifically: they FAIL if  #
# any site is reverted to `except ServerUnavailable:` (the mutation the reviewer  #
# fix corrects), because the ServerClientError then propagates and the assertion  #
# on the degraded state is never reached.                                        #
# --------------------------------------------------------------------------- #


def test_malformed_payload_on_load_degrades_to_unavailable_state() -> None:
    """A malformed ``/agents/profiles`` payload (ServerClientError, NOT a server-down)
    degrades ``load`` to the visible *unavailable* view state — never a crash.

    Guards the L204 catch site. A ``ServerClientError`` is raised (a drifted list
    payload the server *did* answer), distinct from ``ServerUnavailable``
    (unreachability). We assert on the degraded state the code sets — the state
    flag AND the copy the user sees — not merely that nothing escaped.
    """

    client = mock.MagicMock()
    client.profiles.side_effect = ServerClientError(
        "/agents/profiles: expected a JSON array, got dict"
    )
    browser = ProfilesBrowser(client=client, builder=mock.MagicMock(), preflight=mock.MagicMock())

    result = browser.load()

    assert result == []
    assert browser.unavailable is True
    assert browser.state == "unavailable"
    # The user sees the unavailable copy in the list pane (visible degradation).
    assert browser.list_text() == profiles_view.UNAVAILABLE_TEXT
    assert "unavailable" in browser.list_text().lower()


def test_malformed_payload_on_detail_degrades_to_unavailable_state() -> None:
    """A malformed ``/agents/profiles/{name}`` payload (ServerClientError) degrades
    ``open_detail`` to the visible *unavailable* state mid-browse — never a crash.

    Guards the L287 catch site. The list loaded fine; the detail read then returns
    a drifted payload (``ServerClientError``, not ``ServerUnavailable``). The
    browser must flip to the unavailable state and render the unavailable copy.
    """

    client = mock.MagicMock()
    client.profiles.return_value = PROFILE_SUMMARIES
    client.profile.side_effect = ServerClientError(
        "/agents/profiles/backend-dev: missing required key 'name'"
    )
    browser = ProfilesBrowser(client=client, builder=mock.MagicMock(), preflight=mock.MagicMock())

    browser.load()
    assert browser.state == "list"  # list succeeded; the drift is on the detail read

    detail = browser.open_detail("backend-dev")

    assert detail is None
    assert browser.detail is None
    assert browser.unavailable is True
    assert browser.state == "unavailable"
    # The user sees the unavailable copy in the detail pane (visible degradation).
    assert browser.detail_text() == profiles_view.UNAVAILABLE_TEXT


def test_malformed_provider_payload_degrades_to_text_note() -> None:
    """A malformed ``/agents/providers`` payload (ServerClientError) degrades
    ``provider_readiness`` to the visible text note (NFR-6) — never a crash.

    Guards the L351 catch site. ``ProviderPreflight.rows()`` raises a
    ``ServerClientError`` (a drifted providers payload, not unreachability); the
    surface must return the readiness-unavailable text, plain (no colour codes).
    """

    preflight = mock.MagicMock()
    preflight.rows.side_effect = ServerClientError(
        "/agents/providers: expected a JSON array, got dict"
    )
    browser = _browser(preflight=preflight)

    text = browser.provider_readiness("claude_code")

    assert "claude_code" in text
    assert "unavailable" in text.lower()
    assert "\x1b[" not in text  # text only, never colour (NFR-6)


# --------------------------------------------------------------------------- #
# RD-e=A: NO status view / status pane in U6; NO [s] key handler.                 #
# --------------------------------------------------------------------------- #

_U6_MODULES = (
    Path(profiles_view.__file__),
    Path(navigation.__file__),
)


def test_u6_defines_no_status_view_class() -> None:
    """RD-e=A: no StatusView / status-pane class is defined anywhere in U6."""

    for module_path in _U6_MODULES:
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        class_names = [node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
        for name in class_names:
            lowered = name.lower()
            assert "status" not in lowered, f"{module_path.name} defines status class {name}"
            assert "statusview" not in lowered
            assert "statuspane" not in lowered


def test_u6_binds_no_keys_and_no_s_handler() -> None:
    """RD-e=A: U6 modules install no key bindings at all (so no ``[s]`` handler).

    The App (U1) owns the key map; the guided surfaces are pure models. Assert
    U6 imports no ``KeyBindings`` and registers no ``kb.add(...)`` — in
    particular there is no ``[s]`` status key.
    """

    for module_path in _U6_MODULES:
        source = module_path.read_text(encoding="utf-8")
        assert "KeyBindings" not in source, f"{module_path.name} references KeyBindings"
        assert ".add(" not in source, f"{module_path.name} registers a key binding"
        # No status-key binding of any spelling.
        assert '"s"' not in source and "'s'" not in source


def test_package_exports_no_status_symbol() -> None:
    """No status surface leaks via the package: U6 exposes no 'Status' symbol."""

    pkg_dir = Path(cli_agent_orchestrator.__file__).parent / "tui"
    for module_path in (pkg_dir / "profiles_view.py", pkg_dir / "navigation.py"):
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        top_level = [node.name for node in tree.body if isinstance(node, ast.ClassDef)]
        assert not any("Status" in name for name in top_level)


# --------------------------------------------------------------------------- #
# FR-7.2 — a 401 raised while listing profiles degrades to a NOTICE.             #
#                                                                                #
# This is the mutation target for the subclass constraint: ``load()`` catches      #
# ``(ServerUnavailable, ServerClientError)``. Make ``ServerAuthRequired`` a        #
# SIBLING of ``ServerUnavailable`` instead of a subclass and this test REDs,       #
# because the 401 escapes ``load()`` uncaught — onto the very screen FR-3.1 makes  #
# reachable for the first time.                                                   #
# --------------------------------------------------------------------------- #


def test_auth_required_on_load_degrades_to_a_notice_not_a_traceback() -> None:
    """FR-7.2 / FR-3.2: a 401 while listing profiles renders a notice, never raises."""

    client = mock.MagicMock()
    client.profiles.side_effect = ServerAuthRequired(
        "cao-server at /agents/profiles requires authentication (HTTP 401)"
    )
    browser = ProfilesBrowser(client=client, builder=mock.MagicMock(), preflight=mock.MagicMock())

    result = browser.load()  # must NOT raise

    assert result == []
    assert browser.unavailable is True
    assert "unavailable" in browser.list_text().lower()


def test_auth_required_on_detail_read_degrades_to_a_notice() -> None:
    """FR-7.2: the same holds for the detail read (``open_detail``'s catch site)."""

    client = mock.MagicMock()
    client.profiles.return_value = PROFILE_SUMMARIES
    client.profile.side_effect = ServerAuthRequired("requires authentication (HTTP 401)")
    browser = ProfilesBrowser(client=client, builder=mock.MagicMock(), preflight=mock.MagicMock())

    browser.load()
    detail = browser.open_detail("backend-dev")  # must NOT raise

    assert detail is None
    assert browser.unavailable is True


def test_auth_required_on_provider_readiness_degrades_to_text() -> None:
    """FR-7.2: and for the third catch site, provider readiness."""

    preflight = mock.MagicMock()
    preflight.rows.side_effect = ServerAuthRequired("requires authentication (HTTP 401)")
    browser = _browser(preflight=preflight)

    text = browser.provider_readiness("claude_code")  # must NOT raise

    assert "unavailable" in text.lower()
