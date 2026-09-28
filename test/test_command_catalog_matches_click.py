"""The Rust TUI's command catalog must match the live Click tree (issue #321).

PLACEMENT IS DELIBERATE. This is a top-level test with NO ``@pytest.mark.e2e`` marker,
because every pytest invocation across all 8 CI workflows applies both
``--ignore=test/e2e`` and ``-m "not e2e"``. A guard that never runs is worse than no
guard, since it reads as coverage.

# Why this test exists

``tui/src/catalog.rs`` holds one row per leaf command, and its docs claim the closed
``CommandId`` enum eliminated the "a new command lands as silence" failure mode. It does
not, and the gap is precise: the exhaustive ``match`` makes an **unclassified variant** a
compile error, so a command cannot be added to the enum and forgotten. Nothing made a
**missing** variant anything at all — a command added to the Python CLI simply did not
exist as far as the TUI was concerned, and every Rust test stayed green because they all
compare the table against itself.

That was not hypothetical. Review on PR #547 predicted it; walking the tree found it
already true. ``cao memory relationships`` {list, inspect, promote, reject} were added by
PR #524 (issue #511, commit ``8e8695a``, 2026-08-03), landed on ``main``, merged into the
TUI branch, and never reached the catalog. The Rust side read 22/16/23 = 61 commands,
internally consistent and fully green, while the CLI had 65 leaves.

So the check has to cross the language boundary, and it has to walk the tree rather than
scrape ``--help`` — help-scraping is design defect #1 of the superseded TUI (``project.md``
mandated rule; issue #321 FR-1.3).

# What is asserted, and in both directions

- Every Click leaf has a catalog row. A missing one is the silence above.
- Every catalog row has a Click leaf. A stale one is a TUI offering a command that no
  longer exists, which fails at run time with a confusing error.

Both matter, and a one-directional test would pass through half of the drift.
"""

from __future__ import annotations

import re
from pathlib import Path

import click
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOG = REPO_ROOT / "tui" / "src" / "catalog.rs"

# The `flow` group is `hidden=True` (a deprecated alias for `schedule`, issue #378) and its six
# leaves ARE in the catalog, classified HIDE. So hidden-ness is not a reason to exclude a command
# from the table — the catalog's rule is that a hidden command is present and classified HIDE,
# never absent. This test therefore walks hidden commands too, which is what keeps the `flow`
# rows accounted for rather than silently tolerated either way.
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _click_leaves() -> set[str]:
    """Every leaf command's full path, e.g. ``memory relationships list``.

    Walks the command tree programmatically — never ``--help`` output. Help-scraping cannot
    see a parameter's type (``--agents`` is declared Click ``TEXT``, not ``Choice``), which is
    why the superseded TUI could not offer a picker, and it is a mandated prohibition.
    """
    from cli_agent_orchestrator.cli.main import cli

    def walk(command: click.Command, path: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
        if isinstance(command, click.Group):
            found: list[tuple[str, ...]] = []
            for name, sub in command.commands.items():
                found.extend(walk(sub, path + (name,)))
            return found
        return [path]

    return {" ".join(path) for path in walk(cli)}


def _catalog_rows() -> dict[str, str]:
    """Each catalog row's full command path mapped to its ``CommandId`` variant.

    Parsed from the Rust source with a regex rather than by building the crate: the parse is
    what makes this test runnable in the Python CI job, which has no Rust toolchain. The
    parse is asserted non-empty below, so a regex that stops matching fails loudly instead of
    reporting an empty catalog as a perfect match — that is the vacuous-guard shape this
    repo's history is full of.
    """
    source = CATALOG.read_text(encoding="utf-8")
    rows: dict[str, str] = {}

    for variant, body in re.findall(
        r"CommandId::(\w+) => Command \{(.*?)\n        \},", source, re.S
    ):
        parent_match = re.search(r'parent:\s*(?:Some\("([^"]+)"\)|None)', body)
        leaf_match = re.search(r'leaf_name:\s*"([^"]+)"', body)
        assert parent_match is not None, f"{variant} has no `parent:` field"
        assert leaf_match is not None, f"{variant} has no `leaf_name:` field"

        parent = parent_match.group(1)
        leaf = leaf_match.group(1)
        rows[f"{parent} {leaf}" if parent else leaf] = variant

    return rows


def test_the_catalog_parse_is_not_vacuous():
    """The regex must actually find rows, or every comparison below is trivially satisfied.

    An empty parse would make "every catalog row has a Click leaf" pass perfectly while
    checking nothing, and would make the missing-command direction report the entire CLI as
    absent — noisy, but only after someone edits the Rust formatting. Pinning a floor and a
    couple of known rows keeps the parse honest.
    """
    rows = _catalog_rows()

    assert len(rows) >= 60, (
        f"parsed only {len(rows)} catalog rows, which means the regex has stopped matching the "
        "Rust source's shape — every assertion in this file would then be vacuous"
    )
    assert rows.get("launch") == "Launch", (
        "the top-level `cao launch` row must parse with no parent; got " f"{rows.get('launch')!r}"
    )
    assert (
        rows.get("memory list") == "MemoryList"
    ), f"a grouped row must parse as `<parent> <leaf>`; got {rows.get('memory list')!r}"


def test_every_click_command_has_a_catalog_row():
    """A command added to the CLI must not be invisible to the TUI.

    This is the direction that was actually broken: four `cao memory relationships *` leaves
    existed in the CLI and in no catalog row.

    The remedy is deliberately stated in the failure message, because "add it to the table" is
    not the whole answer — `project.md`'s mandated rule is that a new or unclassified command
    defaults to **HIDE** until it is deliberately classified IN-APP or HANDOFF. Someone hitting
    this failure should not have to go looking for that rule.
    """
    click_leaves = _click_leaves()
    catalog = _catalog_rows()

    missing = sorted(click_leaves - set(catalog))
    assert not missing, (
        f"{len(missing)} CAO command(s) exist in the Click tree but have no row in "
        f"tui/src/catalog.rs, so the TUI does not know they exist: {missing}\n"
        "Add a row for each, and classify it HIDE unless it has been deliberately reviewed as "
        "IN-APP or HANDOFF — an unclassified command defaults to HIDE so an unvetted command "
        "cannot surface half-working (project.md, mandated). Then update COMMAND_COUNT, "
        "DISPLAY_ORDER, the `route()` arm in tui/src/server.rs, and the policy-distribution test."
    )


def test_every_catalog_row_has_a_click_command():
    """A catalog row for a command that no longer exists is offered and then fails.

    The converse direction. A renamed or removed CLI command leaves a row that the TUI still
    lists — the operator selects it and gets an error from a command that is simply gone.
    """
    click_leaves = _click_leaves()
    catalog = _catalog_rows()

    stale = sorted(set(catalog) - click_leaves)
    assert not stale, (
        f"{len(stale)} catalog row(s) name a command that is not in the Click tree, so the TUI "
        f"would offer something that cannot run: {stale}\n"
        "Remove the row (and its CommandId variant, DISPLAY_ORDER entry, and `route()` arm), or "
        "correct its parent/leaf_name if the command was renamed."
    )


def test_the_declared_command_count_matches_the_click_tree():
    """`COMMAND_COUNT` must equal the number of Click leaves.

    The Rust side already asserts that `DISPLAY_ORDER` has `COMMAND_COUNT` distinct entries, so
    the count and the table cannot disagree with each other. What no Rust test could check is
    whether either agrees with the **CLI** — and that is the gap the missing four sat in for a
    day: 61 was a consistent, well-tested, wrong number.
    """
    source = CATALOG.read_text(encoding="utf-8")
    declared = re.search(r"const COMMAND_COUNT: usize = (\d+);", source)
    assert declared is not None, "COMMAND_COUNT is no longer declared in the expected form"

    expected = len(_click_leaves())
    assert int(declared.group(1)) == expected, (
        f"COMMAND_COUNT is {declared.group(1)} but the Click tree has {expected} leaf commands. "
        "The Rust tests cannot catch this: they compare the table against itself."
    )
