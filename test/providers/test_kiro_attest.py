"""The Kiro v3 ``/tools`` and ``/mcp list`` attestation parsers (#1167 Phase 1, gate G8).

Fixtures are kiro-cli 2.35.1 v3 panes captured in the #1167 measurements, with
the scratch path redacted at the same width. Every row of the design's D14
table gives its verdict here; v2's panels and the slash palette refuse. A
``/tools`` list longer than the pane is drawn by ``kiro_tools_panel`` as
kiro-cli 3.0.0's TUI draws it, one screen per Down.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from test.providers.kiro_tools_panel import LIVE_ROWS, KiroPane, frame, last_offset, render
from types import MappingProxyType

import pytest

from cli_agent_orchestrator.providers.kiro_attest import (
    KiroAttestationError,
    McpServerRow,
    ToolsPage,
    check_mcp_panel,
    check_tools_page,
    check_tools_panel,
    granted_tags,
    join_tools_pages,
    palette_shows_only,
    parse_mcp_panel,
    parse_tools_page,
    parse_tools_panel,
)
from cli_agent_orchestrator.providers.kiro_v3_schema import KAS_0_66_70

FIXTURES = Path(__file__).parent / "fixtures"
READ_ONLY_MCP = ["fs_read", "fs_list", "@cao-mcp-server"]
READ_ONLY = ["fs_read", "fs_list"]


def _screen(name: str) -> str:
    return (FIXTURES / f"kiro_cli_tui_2_35_1_v3_{name}.txt").read_text(encoding="utf-8")


def _tools_verdict(name: str, grant):
    check_tools_panel(parse_tools_panel(_screen(name)), grant)


@pytest.mark.parametrize(
    ("fixture", "grant"),
    [
        ("tools_readonly_mcp", READ_ONLY_MCP),
        ("tools_trustall", READ_ONLY_MCP),
        ("tools_web_write", ["fs_read", "fs_write", "web_fetch"]),
    ],
)
def test_matching_tools_panels_pass(fixture, grant):
    _tools_verdict(fixture, grant)


@pytest.mark.parametrize(
    ("fixture", "grant", "why"),
    [
        ("tools_read_tag_write", READ_ONLY, "write"),
        ("tools_shell_visible", READ_ONLY, "shell"),
        ("tools_default", READ_ONLY_MCP, "shell"),
        ("tools_none", READ_ONLY_MCP, "no tools"),
        ("tools_web_write", READ_ONLY, "web"),
        ("tools_universal", READ_ONLY, "write"),
        ("tools_readonly_mcp", READ_ONLY, "not granted"),
        ("palette_tools_tab", READ_ONLY_MCP, "no complete /tools panel"),
        ("palette_slash_menu", READ_ONLY_MCP, "no complete /tools panel"),
        ("v2_tools_panel", READ_ONLY_MCP, "no complete /tools panel"),
        ("idle", READ_ONLY_MCP, "no complete /tools panel"),
    ],
)
def test_mismatching_or_unrecognized_tools_panels_refuse(fixture, grant, why):
    with pytest.raises(KiroAttestationError) as excinfo:
        _tools_verdict(fixture, grant)
    assert excinfo.value.code == "kiro_v3_tools_mismatch"
    assert why in str(excinfo.value)


def test_a_clipped_tools_list_refuses():
    screen = _screen("tools_readonly_mcp")
    clipped = "\n".join(line for line in screen.splitlines() if "/workflow_wait" not in line)
    with pytest.raises(KiroAttestationError, match="declares 35 tags but 34"):
        parse_tools_panel(clipped)


def test_a_per_tool_grant_covers_only_its_tool():
    panel = parse_tools_panel(_screen("tools_readonly_mcp"))
    with pytest.raises(KiroAttestationError, match="is not granted"):
        check_tools_panel(panel, ["fs_read", "@cao-mcp-server/send_message"])


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [("mcp_running", ["cao-mcp-server"]), ("mcp_decoys_hidden", ["cao-mcp-server"])],
)
def test_matching_mcp_panels_pass(fixture, expected):
    assert check_mcp_panel(parse_mcp_panel(_screen(fixture)), expected) == ()


def test_decoy_servers_shown_refuse():
    servers = parse_mcp_panel(_screen("mcp_decoys_shown"))
    assert set(servers) == {"kv3-user-decoy", "kv3-ws-decoy", "cao-mcp-server"}
    with pytest.raises(KiroAttestationError) as excinfo:
        check_mcp_panel(servers, ["cao-mcp-server"])
    assert excinfo.value.code == "kiro_v3_mcp_not_ready"
    assert "extra" in str(excinfo.value)


@pytest.mark.parametrize(
    "fixture", ["v2_mcp_panel", "palette_slash_menu", "idle", "tools_readonly_mcp"]
)
def test_unrecognized_mcp_layouts_refuse(fixture):
    with pytest.raises(KiroAttestationError) as excinfo:
        parse_mcp_panel(_screen(fixture))
    assert excinfo.value.code == "kiro_v3_mcp_not_ready"


@pytest.mark.parametrize(
    ("row", "outcome"),
    [
        (McpServerRow("cao-mcp-server", "● running", 34), ()),
        # Kiro's bundled TUI draws ◌ (o with icons off); only the word is relied on.
        (McpServerRow("cao-mcp-server", "◌ loading", 0), ("cao-mcp-server",)),
        (McpServerRow("cao-mcp-server", "o loading", 0), ("cao-mcp-server",)),
        (McpServerRow("cao-mcp-server", "✗ failed", 0), "failed"),
        (McpServerRow("cao-mcp-server", "● running", 0), "0 tools"),
    ],
)
def test_server_states(row, outcome):
    servers = {row.name: row}
    if isinstance(outcome, tuple):
        assert check_mcp_panel(servers, [row.name]) == outcome
    else:
        with pytest.raises(KiroAttestationError, match=outcome):
            check_mcp_panel(servers, [row.name])


def test_a_missing_expected_server_refuses():
    with pytest.raises(KiroAttestationError, match="missing"):
        check_mcp_panel(parse_mcp_panel(_screen("mcp_running")), ["cao-mcp-server", "other"])


@pytest.mark.parametrize(
    ("grant", "tags"),
    [
        (READ_ONLY_MCP, {"read"}),
        (["@cao-mcp-server"], {"read"}),
        # ``code`` comes with fs_write and carries read as well as write.
        (["fs_write"], {"read", "write"}),
        (["fs_write", "execute_bash"], {"read", "write", "shell", "subagent"}),
        (["fs_*"], {"read", "write"}),
        (["execute_bash"], {"shell", "subagent"}),
        (["web_fetch"], {"web"}),
        # ``introspect`` carries read.
        (["@builtin"], {"read"}),
        ([], set()),
    ],
)
def test_granted_tags(grant, tags):
    """The tags of the grant's exact v3 tools, read from the pinned schema
    (maintainer review on #1179)."""
    assert granted_tags(grant) == tags


def _schema(engine, **changed):
    tags = {tag: frozenset(ids) for tag, ids in KAS_0_66_70.tags.items()}
    tags.update({tag: frozenset(ids) for tag, ids in changed.items()})
    return replace(KAS_0_66_70, engine=engine, tags=MappingProxyType(tags))


def test_granted_tags_follow_the_schema_not_a_second_table():
    plain_read = KAS_0_66_70.tags["read"] - {"code", "introspect"}
    schema = _schema("kas-test-plain-read", read=plain_read)
    assert granted_tags(["fs_write"], [schema]) == {"write"}
    assert granted_tags(["@builtin"], [schema]) == set()
    assert granted_tags(["fs_read"], [schema]) == {"read"}


def test_engines_that_disagree_allow_only_the_tags_every_one_implies():
    plain_read = _schema("kas-test-plain-read", read=KAS_0_66_70.tags["read"] - {"code"})
    assert granted_tags(["fs_write"], [KAS_0_66_70, plain_read]) == {"write"}
    assert granted_tags(["fs_write"], [KAS_0_66_70]) == {"read", "write"}


def test_a_write_grant_without_a_read_atom_passes_the_read_tag_code_brings():
    """Measured: a file whose tools bring ``code`` shows its read and write tags."""
    panel = parse_tools_panel(_screen("tools_read_tag_write"))
    assert panel.tags == {"read", "write"}
    check_tools_panel(panel, ["fs_write", "execute_bash"])
    with pytest.raises(KiroAttestationError) as excinfo:
        check_tools_panel(panel, ["execute_bash"])
    assert excinfo.value.code == "kiro_v3_tools_mismatch"


def test_the_typed_palettes_are_recognized_and_tab_is_not():
    assert palette_shows_only(_screen("typed_tools"), "/tools")
    assert palette_shows_only(_screen("typed_mcp"), "/mcp")
    assert not palette_shows_only(_screen("palette_tools_tab"), "/tools")
    assert not palette_shows_only(_screen("typed_mcp"), "/tools")
    assert not palette_shows_only(_screen("idle"), "/tools")


@pytest.mark.parametrize("selected", ["❯ trust-all    Trust every tool", "  ❯ /tools trust-all"])
def test_a_palette_with_a_selected_row_is_refused_on_its_own(selected):
    """The ``❯`` clause alone: the completion row and the cancel/select footer are
    both present, so only the selected row (``trust-all`` after a Tab) refuses."""
    typed = _screen("typed_tools")
    completion = "/tools    List available tools (tab for sub-commands)"
    assert completion in typed and palette_shows_only(typed, "/tools")
    with_selection = typed.replace(completion, f"{completion}\n{selected}")
    assert "esc to cancel · ↵ to select" in with_selection
    assert not palette_shows_only(with_selection, "/tools")


# A /tools list longer than the pane (kiro-cli 3.0.0's TUI) ---------------------

#: A helper with ``fs_read`` and ``@cao-mcp-server``, as on the live 3.0.0 run.
HELPER = ["fs_read", "@cao-mcp-server"]


def _pages(rows=LIVE_ROWS, *, height=24, **drawn):
    """Every screen of the list, one Down apart, parsed."""
    return [
        parse_tools_page(render(rows, height=height, offset=offset, **drawn))
        for offset in range(last_offset(len(rows), height) + 1)
    ]


@pytest.mark.parametrize(("height", "shown", "screens"), [(30, 21, 16), (24, 15, 22), (14, 5, 32)])
def test_a_scrolling_list_read_screen_by_screen_joins_into_the_whole_panel(height, shown, screens):
    """36 tags on two, three or eight screenfuls: every tag is seen, each once."""
    pages = _pages(height=height)
    assert len(pages) == screens
    assert all(len(page.rows) == shown for page in pages)
    panel = join_tools_pages(pages)
    assert panel.tags == {"read"}
    assert len(panel.mcp_tools) == 35
    assert {name for name, _, _ in LIVE_ROWS} == panel.tags | panel.mcp_tools
    check_tools_panel(panel, HELPER)


def test_the_scroll_rows_are_read_only_where_kiro_draws_them():
    first, middle, last = (
        parse_tools_page(render(LIVE_ROWS, height=24, offset=offset)) for offset in (0, 7, 21)
    )
    assert (first.more_above, first.more_below) == (False, True)
    assert (middle.more_above, middle.more_below) == (True, True)
    assert (last.more_above, last.more_below) == (True, False)
    assert first.rows[0] == ("read", "builtin")
    assert last.rows[-1] == ("@cao-mcp-server/workflow_wait", "mcp")
    assert all(page.declared == 36 for page in (first, middle, last))


def test_regression_a_scrolling_list_on_one_screen_refuses_but_not_on_its_scroll_row():
    """The live 3.0.0 refusal was ``unrecognized row '↓ more'``. One screen of a
    longer list still refuses, but because rows are left below it."""
    screen = render(LIVE_ROWS, height=30)
    assert "   ↓ more" in screen.splitlines()
    with pytest.raises(KiroAttestationError) as excinfo:
        parse_tools_panel(screen)
    assert excinfo.value.code == "kiro_v3_tools_mismatch"
    assert "rows below the last screen read" in str(excinfo.value)
    assert "unrecognized row" not in str(excinfo.value)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("   ↓ more", "   v more"),  # icons off
        ("   ↓ more", "   down more"),  # screen-reader glyphs
        ("   ↓ more", "   ↓ 21 more below"),
        ("   ↑ more", "   ^ more"),
    ],
)
def test_other_scroll_rows_refuse(old, new):
    """Above the column header an unknown row breaks the layout; below the last
    tool row it is an unknown row."""
    screen = render(LIVE_ROWS, height=24, offset=7)
    assert old in screen
    with pytest.raises(KiroAttestationError, match="unrecognized (row|/tools layout)"):
        parse_tools_page(screen.replace(old, new))


@pytest.mark.parametrize("row", ["   ↓ more", "   ↑ more"])
def test_a_scroll_row_among_the_tool_rows_refuses(row):
    lines = render(LIVE_ROWS, height=24, offset=7).splitlines()
    at = next(i for i, line in enumerate(lines) if "@cao-mcp-server/handoff " in line)
    lines.insert(at, row)
    with pytest.raises(KiroAttestationError, match="unrecognized row"):
        parse_tools_page("\n".join(lines))


def test_a_scroll_row_with_no_tool_rows_refuses():
    screen = render(LIVE_ROWS[:0], height=24, more_below=True)
    with pytest.raises(KiroAttestationError, match="unrecognized /tools layout"):
        parse_tools_page(screen)


def test_a_missing_screen_refuses():
    pages = _pages()
    with pytest.raises(KiroAttestationError, match="did not move down by one row"):
        join_tools_pages(pages[:5] + pages[6:])


def test_a_missing_last_screen_refuses():
    pages = _pages()
    with pytest.raises(KiroAttestationError, match="rows below the last screen read"):
        join_tools_pages(pages[:-1])


def test_a_list_read_from_its_middle_refuses():
    with pytest.raises(KiroAttestationError, match="did not start at its first row"):
        join_tools_pages(_pages()[3:])


def test_a_tag_listed_twice_refuses():
    """Kiro counts both rows in its title, so the count alone would pass."""
    rows = list(LIVE_ROWS)
    rows[30] = rows[2]
    with pytest.raises(KiroAttestationError, match="more than once"):
        join_tools_pages(_pages(rows))


@pytest.mark.parametrize(("declared", "visible"), [(37, 36), (35, 36)])
def test_a_title_count_the_joined_rows_do_not_match_refuses(declared, visible):
    with pytest.raises(KiroAttestationError) as excinfo:
        join_tools_pages(_pages(declared=declared))
    assert str(excinfo.value) == (
        f"kiro_v3_tools_mismatch: the panel declares {declared} tags but {visible} are visible"
    )


def test_a_row_the_panel_never_scrolls_to_refuses():
    """Its last row is never drawn and its last screen shows no ``↓ more``."""
    pages = _pages()[:-1]
    hidden = render(LIVE_ROWS, height=24, offset=20, more_below=False)
    pages[-1] = parse_tools_page(hidden)
    with pytest.raises(KiroAttestationError) as excinfo:
        join_tools_pages(pages)
    assert str(excinfo.value) == (
        "kiro_v3_tools_mismatch: the panel declares 36 tags but 35 are visible"
    )


def test_the_whole_joined_list_is_held_to_the_grant():
    """A tool only the last screen shows is checked like the first screen's."""
    panel = join_tools_pages(_pages())
    names = [name for name, source, _ in LIVE_ROWS if source == "mcp"]
    check_tools_panel(panel, ["fs_read", *names])
    with pytest.raises(KiroAttestationError, match="workflow_wait is not granted"):
        check_tools_panel(panel, ["fs_read", *names[:-1]])


def _page(rows, *, above=True, below=True, declared=36):
    return ToolsPage(declared, tuple((name, "mcp") for name in rows), above, below)


@pytest.mark.parametrize(
    "page",
    [
        _page("bcdef"),
        _page("abcde"),  # the old screen, not yet redrawn
        _page("cdefg"),  # moved by two rows
        _page("bcdee"),  # drawn halfway: an old row at the bottom
        _page("bcdea"),  # a row already read
        _page("bcde"),  # fewer rows
        _page("bcdef", declared=37),  # the list changed
        _page("bcdef", above=False),
    ],
    ids=["follows", "stale", "two_rows", "halfway", "seen_row", "fewer", "count", "no_up_row"],
)
def test_each_screen_must_be_the_last_one_moved_down_by_one_row(page):
    previous = _page("abcde", above=False)
    if page == _page("bcdef"):
        check_tools_page(page, previous)
        return
    with pytest.raises(KiroAttestationError, match="did not move down by one row"):
        check_tools_page(page, previous)


def test_a_screen_after_the_last_one_refuses():
    with pytest.raises(KiroAttestationError, match="did not move down by one row"):
        check_tools_page(_page("bcdef"), _page("abcde", below=False))


@pytest.mark.parametrize("height", [5, 14, 24, 30])
def test_a_scrolling_frame_parses_only_with_the_rows_above_the_pane(height):
    """Kiro draws a scrolling panel taller than the pane: its title is above the
    pane's top row. The pane alone refuses; the pane plus its recent scrollback
    parses, on either backend's read."""
    pane = KiroPane(height)
    pane.draw(frame(LIVE_ROWS, height=height, offset=1))
    with pytest.raises(KiroAttestationError, match="no complete /tools panel on screen"):
        parse_tools_page(pane.capture(0))
    for screen in (pane.capture(-100), pane.recent(100)):
        page = parse_tools_page(screen)
        assert (page.more_above, page.more_below) == (True, True)
        assert page.rows[0] == (LIVE_ROWS[1][0], LIVE_ROWS[1][1])


def test_old_frames_left_in_the_scrollback_refuse():
    """A terminal that kept every frame's overflow would hold two titles."""
    pane = KiroPane(24, redraw="keep_old")
    pane.draw(frame(LIVE_ROWS, height=24, offset=0))
    pane.draw(frame(LIVE_ROWS, height=24, offset=1))
    with pytest.raises(KiroAttestationError, match="no complete /tools panel on screen"):
        parse_tools_page(pane.capture(-100))


@pytest.mark.parametrize("below", ["• Kiro is checking for updates", "  ❯ Allow once"])
def test_anything_but_blank_rows_below_the_footer_refuses(below):
    """The panel must be the last thing drawn: Kiro keeps only blank rows under it."""
    screen = render(LIVE_ROWS, height=24, offset=3)
    parse_tools_page(screen)
    with pytest.raises(KiroAttestationError) as excinfo:
        parse_tools_page(f"{screen}\n\n{below}\n")
    assert str(excinfo.value) == (
        "kiro_v3_tools_mismatch: something is drawn below the /tools panel"
    )


@pytest.mark.parametrize("height", [11, 9, 5])
def test_on_kiros_default_redraw_stale_rows_above_a_small_pane_are_caught_by_the_shift_check(
    height,
):
    """What rejects stale rows. Kiro's default redraw keeps the scrollback: below
    12 rows the first Down changes a row above the pane, so only the pane is
    repainted. The read still holds exactly one title and parses as a panel,
    so the one-title rule doesn't catch it; the one-row check does."""
    pane = KiroPane(height)
    pane.draw(frame(LIVE_ROWS, height=height, offset=0))
    first = parse_tools_page(pane.capture(-100))
    pane.draw(frame(LIVE_ROWS, height=height, offset=1))
    for screen in (pane.capture(-100), pane.recent(100)):
        assert screen.count("/tools · 36 tags") == 1
        stale = parse_tools_page(screen)
        # Not the screen Kiro drew: old rows, or the old frame's header with no ``↑ more``.
        assert (stale.rows, stale.more_above) != (tuple((n, s) for n, s, _ in LIVE_ROWS[1:6]), True)
        with pytest.raises(KiroAttestationError, match="did not move down by one row"):
            check_tools_page(stale, first)
    # A clearing redraw writes the whole frame, and the same screen follows.
    clearing = KiroPane(height, redraw="clear")
    clearing.draw(frame(LIVE_ROWS, height=height, offset=0))
    clearing.draw(frame(LIVE_ROWS, height=height, offset=1))
    check_tools_page(parse_tools_page(clearing.capture(-100)), first)
