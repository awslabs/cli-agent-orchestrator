"""Unit tests for the shared call-site lint helper (#1005).

The three rules in ``test_callsite_lint_rules.py`` are only as trustworthy as
this helper. Each test below feeds it synthetic source text so the rule
primitives are exercised against both the shape they must catch and the shapes
they must *not* catch -- a structural guard that silently matches nothing is
worse than no guard, because it reads as coverage.
"""

from __future__ import annotations

import ast
import textwrap
from test._support import callsite_lint as cl
from typing import List

import pytest


def _module(source: str, relpath: str = "pkg/mod.py") -> cl.Module:
    """Build a :class:`cl.Module` from inline source, as the parser would."""
    text = textwrap.dedent(source)
    return cl.Module(
        path=cl.PACKAGE_ROOT / relpath,
        relpath=relpath,
        tree=ast.parse(text),
        lines=tuple(text.splitlines()),
    )


# ---------------------------------------------------------------------------
# Source discovery
# ---------------------------------------------------------------------------


def test_parse_sources_covers_the_package_and_is_cached() -> None:
    """The scan reaches the real tree, and repeated calls reuse one parse."""
    first = cl.parse_sources()
    second = cl.parse_sources()
    assert first is second, "parse_sources must be cached; three rules share it"
    assert len(first) > 100, f"only {len(first)} modules found under {cl.PACKAGE_ROOT}"
    relpaths = {m.relpath for m in first}
    assert "cli_agent_orchestrator/utils/terminal.py" in relpaths
    assert all(m.relpath.endswith(".py") for m in first)
    # Source lines must be carried alongside the AST or inline markers cannot
    # be read.
    assert all(m.lines or m.path.stat().st_size == 0 for m in first)


# ---------------------------------------------------------------------------
# Enum discovery
# ---------------------------------------------------------------------------


def test_enum_members_reads_members_and_skips_methods_and_privates() -> None:
    mod = _module("""
        from enum import Enum

        class Colour(str, Enum):
            RED = "red"
            BLUE = "blue"
            _ignore_ = "skip me"

            def pretty(self) -> str:
                return self.value
        """)
    assert cl.enum_members([mod]) == {"Colour": frozenset({"RED", "BLUE"})}


def test_enum_members_matches_dotted_and_annotated_forms() -> None:
    """``enum.IntEnum`` and an annotated member both count."""
    mod = _module("""
        import enum

        class Level(enum.IntEnum):
            LOW: int = 1
            HIGH: int = 2
        """)
    assert cl.enum_members([mod]) == {"Level": frozenset({"LOW", "HIGH"})}


def test_enum_members_ignores_a_plain_class() -> None:
    mod = _module("""
        class NotAnEnum:
            RED = "red"
            BLUE = "blue"
        """)
    assert cl.enum_members([mod]) == {}


def test_real_tree_exposes_the_enums_the_rules_depend_on() -> None:
    """Guard the guard: if discovery breaks, rule 1 matches nothing."""
    table = cl.enum_members()
    assert table["TerminalStatus"] >= {
        "COMPLETED",
        "IDLE",
        "PROCESSING",
        "WAITING_USER_ANSWER",
        "ERROR",
        "UNKNOWN",
    }
    assert table["OrchestrationType"] == frozenset({"ASSIGN", "HANDOFF", "SEND_MESSAGE"})


# ---------------------------------------------------------------------------
# Rule 1 primitive: enum subsets
# ---------------------------------------------------------------------------


_ENUM_PRELUDE = """
from enum import Enum

class Status(str, Enum):
    A = "a"
    B = "b"
    C = "c"
"""


def _subset_details(source: str) -> List[str]:
    mod = _module(_ENUM_PRELUDE + textwrap.dedent(source))
    return [s.detail for s in cl.enum_subset_sites([mod])]


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("x = s in {Status.A, Status.B}", id="set"),
        pytest.param("x = s in (Status.A, Status.B)", id="tuple"),
        pytest.param("x = s in [Status.A, Status.B]", id="list"),
        pytest.param("x = s not in {Status.A, Status.B}", id="not-in"),
        pytest.param("x = s in {Status.A.value, Status.B.value}", id="dot-value"),
    ],
)
def test_enum_subset_sites_flag_every_literal_container_shape(body: str) -> None:
    """Set, tuple, list, ``not in`` and ``.value`` all hide the same hole."""
    details = _subset_details(body)
    assert len(details) == 1, details
    assert "Status subset {A, B}" in details[0]
    assert "2 of 3 members" in details[0]


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("x = s in {Status.A, Status.B, Status.C}", id="exhaustive"),
        pytest.param("x = s in ALLOWED", id="named-collection"),
        pytest.param("x = s in {Other.A, Other.B}", id="unknown-class"),
        pytest.param("x = s in {Status.A, Other.B}", id="mixed-classes"),
        pytest.param('x = s in {"a", "b"}', id="string-literals"),
        pytest.param("x = s == Status.A", id="equality-not-membership"),
        pytest.param("x = s in set()", id="empty"),
    ],
)
def test_enum_subset_sites_do_not_flag_the_shapes_that_are_fine(body: str) -> None:
    """A complete literal, a derived name, or a non-Enum set is not a finding.

    ``string-literals`` is the honest limitation: the two sites #1005 named as
    seeds compare strings, and this rule cannot see them.
    """
    assert _subset_details(body) == []


def test_an_exhaustive_literal_becomes_a_finding_when_a_member_is_added() -> None:
    """The rule fires at the moment the enum grows -- the #896 trigger."""
    complete = _module(_ENUM_PRELUDE + "x = s in {Status.A, Status.B, Status.C}")
    assert cl.enum_subset_sites([complete]) == []

    grown = _module(
        _ENUM_PRELUDE.replace('    C = "c"', '    C = "c"\n    D = "d"')
        + "x = s in {Status.A, Status.B, Status.C}"
    )
    found = cl.enum_subset_sites([grown])
    assert len(found) == 1
    assert "3 of 4 members" in found[0].detail


def test_enum_subset_site_key_is_line_free() -> None:
    """Keys must survive reformatting, or every reflow churns the allowlist."""
    mod = _module(_ENUM_PRELUDE + "x = s in {Status.A, Status.B}")
    (site,) = cl.enum_subset_sites([mod])
    assert site.key == ("pkg/mod.py", "Status", "A, B")
    assert str(site.line) not in "".join(site.key)
    assert site.where == f"pkg/mod.py:{site.line}"


# ---------------------------------------------------------------------------
# Inline markers
# ---------------------------------------------------------------------------


def test_inline_marker_reads_the_reason_off_the_flagged_line() -> None:
    mod = _module("""
        a = 1
        b = 2  # subset-ok: only these two can be retried
        """)
    assert cl.inline_marker(mod, 3, "subset-ok") == "only these two can be retried"


def test_inline_marker_looks_two_lines_back_for_a_wrapped_statement() -> None:
    """``black`` pushes a long set literal off the ``if`` line."""
    mod = _module("""
        if status in {  # subset-ok: wrapped by black
            Status.A,
            Status.B,
        }:
            pass
        """)
    assert cl.inline_marker(mod, 4, "subset-ok") == "wrapped by black"
    # Three lines back is out of range on purpose: an unbounded lookback would
    # let a marker on an unrelated statement excuse the site below it.
    assert cl.inline_marker(mod, 5, "subset-ok") is None


def test_inline_marker_rejects_a_marker_with_no_reason() -> None:
    mod = _module("b = 2  # subset-ok:")
    assert cl.inline_marker(mod, 1, "subset-ok") is None


def test_inline_marker_is_prefix_scoped() -> None:
    mod = _module("b = 2  # timeout-ok: local give-up")
    assert cl.inline_marker(mod, 1, "subset-ok") is None
    assert cl.inline_marker(mod, 1, "timeout-ok") == "local give-up"


def test_inline_marker_tolerates_an_out_of_range_line() -> None:
    mod = _module("b = 2")
    assert cl.inline_marker(mod, 99, "subset-ok") is None


# ---------------------------------------------------------------------------
# Rule 2 primitive: bare timeout literals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param("run(cmd, timeout=30)", "run(timeout=30)", id="int"),
        pytest.param("run(cmd, timeout=1.5)", "run(timeout=1.5)", id="float"),
        pytest.param("c.get(url, timeout=5)", "get(timeout=5)", id="attribute-callee"),
        pytest.param("run(cmd, read_timeout=5)", "run(read_timeout=5)", id="name-variant"),
        pytest.param("run(cmd, timeout=-1)", "run(timeout=-1)", id="negated"),
    ],
)
def test_bare_timeout_sites_flag_literal_values(body: str, expected: str) -> None:
    (site,) = cl.bare_timeout_sites([_module(body)])
    assert site.key == ("pkg/mod.py", expected)
    assert "bare literal" in site.detail


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("run(cmd, timeout=HTTP_TIMEOUT_S)", id="named-constant"),
        pytest.param("run(cmd, timeout=settings.mcp_request_timeout)", id="setting"),
        pytest.param("run(cmd, timeout=_mcp_timeout())", id="call"),
        pytest.param("run(cmd, timeout=budget - elapsed)", id="arithmetic"),
        pytest.param("run(cmd, timeout=None)", id="none"),
        pytest.param("run(cmd, timeout=True)", id="bool"),
        pytest.param("run(cmd, **kwargs)", id="splat"),
        pytest.param("run(cmd, 30)", id="positional"),
        pytest.param("run(cmd, deadline=30)", id="unrelated-kwarg"),
    ],
)
def test_bare_timeout_sites_pass_a_derived_or_absent_value(body: str) -> None:
    """Deriving the number is the asked-for behaviour, so it must be silent."""
    assert cl.bare_timeout_sites([_module(body)]) == []


# ---------------------------------------------------------------------------
# Rule 3 primitives: declared parameters, census, missing kwarg
# ---------------------------------------------------------------------------


def test_declared_parameters_covers_positional_keyword_only_and_splats() -> None:
    mod = _module("""
        def poll(a, /, b, *args, c=1, **kw):
            pass
        """)
    assert cl.declared_parameters("poll", [mod]) == frozenset({"a", "b", "args", "c", "kw"})


def test_declared_parameters_sees_an_async_def() -> None:
    mod = _module("""
        async def poll(terminal_id, min_generation=None):
            pass
        """)
    assert cl.declared_parameters("poll", [mod]) == frozenset({"terminal_id", "min_generation"})


def test_declared_parameters_is_empty_for_an_unknown_name() -> None:
    assert cl.declared_parameters("nope", [_module("x = 1")]) == frozenset()


def test_poll_until_done_has_no_min_generation_on_this_tree() -> None:
    """Pins the fact rule 3's conditional rests on (#735 / PR #812).

    If this starts failing, ``min_generation`` has landed and
    ``test_every_poll_until_done_passes_a_generation_floor`` has stopped
    skipping -- which is the intended handover, not a breakage.
    """
    declared = cl.declared_parameters("poll_until_done")
    assert declared, "poll_until_done vanished from src/; rule 3 needs rewriting"
    assert "min_generation" not in declared


def test_missing_kwarg_sites_flags_an_omission_and_a_splat_but_not_an_explicit_pass() -> None:
    mod = _module("""
        poll_until_done(tid)
        poll_until_done(tid, min_generation=4)
        poll_until_done(tid, **opts)
        """)
    sites = cl.missing_kwarg_sites("poll_until_done", "min_generation", [mod])
    # Line 1 is the blank line the dedented triple-quote leaves behind, so the
    # bare call is on line 2 and the splat is on line 4.
    assert [s.line for s in sites] == [2, 4], "only the explicit pass on line 3 may be silent"
    assert sites[0].detail == "poll_until_done(...) does not pass min_generation="
    assert "cannot verify" in sites[1].detail


def test_call_sites_records_the_keywords_actually_passed() -> None:
    mod = _module("poll_until_done(tid, timeout=5)")
    (site,) = cl.call_sites("poll_until_done", [mod])
    assert site.key == ("pkg/mod.py", "poll_until_done")
    assert "timeout" in site.detail


# ---------------------------------------------------------------------------
# The ratchet
# ---------------------------------------------------------------------------


def _site(key: str, line: int = 7) -> cl.Site:
    return cl.Site(relpath="pkg/mod.py", line=line, key=("pkg/mod.py", key), detail=f"<{key}>")


def test_check_against_allowlist_passes_when_every_site_has_a_row() -> None:
    cl.check_against_allowlist(
        rule="r",
        sites=[_site("a")],
        allowlist={("pkg/mod.py", "a"): "reviewed"},
        remedy="do the thing",
    )


def test_check_against_allowlist_passes_on_an_empty_tree() -> None:
    cl.check_against_allowlist(rule="r", sites=[], allowlist={}, remedy="do the thing")


def test_check_against_allowlist_fails_on_a_new_unexplained_site() -> None:
    with pytest.raises(AssertionError) as exc:
        cl.check_against_allowlist(
            rule="enum-subset",
            sites=[_site("a", line=42)],
            allowlist={},
            remedy="add a marker or a row",
        )
    message = str(exc.value)
    assert "call-site lint [enum-subset] failed" in message
    assert "pkg/mod.py:42" in message, "the message must be clickable"
    assert "<a>" in message, "the message must say what was flagged"
    assert "add a marker or a row" in message, "the message must say what to do"


def test_check_against_allowlist_fails_on_a_row_that_matches_nothing() -> None:
    """The ratchet: a fixed site must force its row out of the table."""
    with pytest.raises(AssertionError) as exc:
        cl.check_against_allowlist(
            rule="bare-timeout",
            sites=[],
            allowlist={("pkg/mod.py", "a"): "was reviewed"},
            remedy="irrelevant",
        )
    message = str(exc.value)
    assert "no longer match any call site" in message
    assert "was reviewed" in message
    assert "can only shrink" in message


def test_check_against_allowlist_accepts_an_inline_marker_instead_of_a_row() -> None:
    cl.check_against_allowlist(
        rule="r",
        sites=[_site("a")],
        allowlist={},
        marked={("pkg/mod.py", "a"): "deliberate"},
        remedy="do the thing",
    )


def test_check_against_allowlist_fails_on_a_stale_inline_marker() -> None:
    with pytest.raises(AssertionError) as exc:
        cl.check_against_allowlist(
            rule="r",
            sites=[],
            allowlist={},
            marked={("pkg/mod.py", "gone"): "deliberate"},
            remedy="do the thing",
        )
    assert "no longer sit on a flagged" in str(exc.value)


def test_check_against_allowlist_reports_every_problem_at_once() -> None:
    """One run, one full list -- not a fix-rerun-fix loop per site."""
    with pytest.raises(AssertionError) as exc:
        cl.check_against_allowlist(
            rule="r",
            sites=[_site("new1", line=1), _site("new2", line=2)],
            allowlist={("pkg/mod.py", "old"): "stale row"},
            remedy="do the thing",
        )
    message = str(exc.value)
    assert "2 unexplained call site(s)" in message
    assert "pkg/mod.py:1" in message and "pkg/mod.py:2" in message
    assert "1 allowlist row(s)" in message


def test_one_row_covers_repeated_sites_up_to_its_reviewed_count() -> None:
    """Three copies of one idiom in one module need one row, not three --

    but the row must say ``3``, not just a bare reason.
    """
    cl.check_against_allowlist(
        rule="r",
        sites=[_site("a", line=n) for n in (1, 2, 3)],
        allowlist={("pkg/mod.py", "a"): (3, "reviewed as three sites")},
        remedy="do the thing",
    )


def test_a_bare_reason_row_implies_a_reviewed_count_of_one() -> None:
    """A plain string reason is shorthand for ``(1, reason)``, not "any number"."""
    cl.check_against_allowlist(
        rule="r",
        sites=[_site("a", line=1)],
        allowlist={("pkg/mod.py", "a"): "reviewed once"},
        remedy="do the thing",
    )


def test_a_second_unreviewed_site_under_an_existing_key_is_not_silent() -> None:
    """The gap #1005's reviewers found: a key collision must not hide growth.

    ``worker.py`` holding two ``poll_until_done`` calls behind one row is the
    live example this guards against -- a third call there must fail until the
    row's count is bumped, not pass because the key already has a row.
    """
    with pytest.raises(AssertionError) as exc:
        cl.check_against_allowlist(
            rule="r",
            sites=[_site("a", line=1), _site("a", line=2)],
            allowlist={("pkg/mod.py", "a"): "reviewed once"},
            remedy="bump the reviewed count or split the site",
        )
    message = str(exc.value)
    assert "exceed their allowlist row's reviewed occurrence count" in message
    assert "pkg/mod.py:2" in message
    assert "row allows 1" in message


def test_an_explicit_count_row_still_fails_past_its_reviewed_count() -> None:
    with pytest.raises(AssertionError) as exc:
        cl.check_against_allowlist(
            rule="r",
            sites=[_site("a", line=n) for n in (1, 2, 3)],
            allowlist={("pkg/mod.py", "a"): (2, "reviewed as two sites")},
            remedy="bump the reviewed count or split the site",
        )
    message = str(exc.value)
    assert "1 call site(s) exceed" in message
    assert "pkg/mod.py:3" in message
    assert "row allows 2" in message
