"""#735 guard: a turn-identity gate that is opt-in and fail-open.

#566 built the recency token that says WHICH turn a status belongs to
(``StatusMonitor.output_generation`` / ``status_observation``), but it is
*opt-in per call site* and *fail-open on a missing field*. Both halves bite:

1. A dispatch-then-wait site that never asks for the boundary gets the pre-#566
   contract and is called done by the previous turn's latched COMPLETED. That was
   #735 on ``cao session send``, and before it #407.
2. The gate degrades deliberately to the old frame-based wait when the reading
   carries no generation (older cao-server, event-inbox backends). So if any link
   in ``get_terminal`` -> ``Terminal`` -> ``GET /terminals/{id}`` ->
   ``poll_until_done`` stops carrying the field, the gate silently switches off
   and every per-side test keeps passing: each asserts its own literal key name.

The first test below is a call-shape lint, not coverage of the defect class: it
pins every dispatch-then-wait *call shape* it can see in ``src/`` -- a dispatch
and a wait reachable from the same function body. A site that is ungated on
purpose says so where it lives, with a ``turn-boundary: ungated -- <reason>``
comment inside the function (see ``UNGATED_MARKER``); the lint matches that
comment, not the file path or the function name, so a rename or a move changes
nothing here (round-1 review of PR #999, P3-1). It does not see a dispatch
helper split across two functions, or a bespoke inline poll loop that never
calls one of ``WAITS`` by name. One live sibling of exactly that uncaught shape:
``services/memory_service.py``'s reused-curator path calls ``send_input`` and
then polls with its own inline ``for _ in range(30)`` loop instead of
``poll_until_done`` -- the same defect, impact bounded (not eliminated) by the
``output_before`` watermark above it. The lint never considers it a
dispatch-then-wait site in the first place; recorded here instead, pending
either extending the lint to see bespoke loops or filing it as its own issue.

The lint core (``_ungated_sites``) takes source text, so the shapes it must
accept and refuse are pinned on snippets in ``test_lint_core_judges_each_shape``
rather than only on whatever ``src/`` happens to contain today.

The last test closes the other half structurally: it deliberately never names
the wire field, so it cannot be satisfied by a rename on both sides of one seam.
"""

from __future__ import annotations

import ast
import textwrap
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest

from cli_agent_orchestrator.models.terminal import Terminal, TerminalStatus

SRC = Path(__file__).resolve().parents[1] / "src" / "cli_agent_orchestrator"

# Names that start a new turn on a terminal. ``INPUT_ROUTE`` covers the callers
# that dispatch over HTTP instead (the CLI), which is where #735 was reported.
DISPATCH = {"send_input", "dispatch_input", "send_message"}
INPUT_ROUTE = "/input"
# Calls that block until that turn is "done".
WAITS = {"poll_until_done", "_wait_for_completion", "wait_until_terminal_status"}
# Keyword names that carry a turn boundary into a wait.
BOUNDARY_KWARGS = {"min_generation", "dispatch_generation", "min_output_generation"}
# A dispatch-then-wait site that passes no boundary ON PURPOSE carries this
# comment inside the function, followed by the reason (or the issue tracking
# it). Living at the site, it moves with a rename and dies with a deletion, so
# there is no allowlist here to go stale. A NEW ungated site fails the lint
# until it is either gated or marked -- that is the whole point.
UNGATED_MARKER = "turn-boundary: ungated"


def _name_of(node):
    if isinstance(node, ast.Attribute):
        return node.attr
    return getattr(node, "id", None)


def _body(node):
    """This function's own nodes, excluding nested function definitions."""
    nested = [
        inner
        for child in ast.iter_child_nodes(node)
        for inner in ast.walk(child)
        if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    skip = {n for f in nested for n in ast.walk(f)}
    return [n for n in ast.walk(node) if n not in skip]


def _referenced(nodes):
    """Every name mentioned, called or not.

    A dispatch is often handed to ``asyncio.to_thread`` rather than called
    (``to_thread(terminal_service.send_input, ...)``), so a call-only walk misses
    exactly the sites this guard exists for.
    """
    return {_name_of(n) for n in nodes if isinstance(n, (ast.Name, ast.Attribute))}


def _dispatches(nodes):
    """True if this function starts a turn, in-process or over HTTP."""
    if _referenced(nodes) & DISPATCH:
        return True
    return any(
        isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.endswith(INPUT_ROUTE)
        for n in nodes
    )


def _wait_calls(nodes):
    """(wait name, the Call whose keywords reach it) for each wait in this body.

    ``to_thread(wait, ..., min_generation=N)`` forwards its keywords to the wait,
    so the ``to_thread`` call is the one to inspect.
    """
    for n in nodes:
        if not isinstance(n, ast.Call):
            continue
        name = _name_of(n.func)
        if name in WAITS:
            yield name, n
        elif name == "to_thread" and n.args:
            inner = _name_of(n.args[0])
            if inner in WAITS:
                yield inner, n


def _names_a_live_boundary(call):
    """True if ``call`` passes a boundary keyword whose value is computed, not written.

    A turn boundary is sampled at dispatch time, so it can only ever arrive as a
    name or an expression. Any literal -- ``None`` (the gate's off switch), ``0``,
    ``8`` -- spells the keyword without naming a turn and must not read as gated
    (review P3-1: ``min_generation=0`` used to pass).
    """
    for kw in call.keywords:
        if kw.arg not in BOUNDARY_KWARGS:
            continue
        if isinstance(kw.value, ast.Constant):
            continue
        return True
    return False


def _ungated_sites(rel, source):
    """Lint core: (offenders, sites seen) for one module's source text.

    An offender is a function that dispatches and waits in the same body, passes
    no live boundary to any of its waits, and carries no ``UNGATED_MARKER``
    comment anywhere in its own lines.
    """
    tree = ast.parse(source, filename=rel)
    lines = source.splitlines()
    offenders = []
    seen = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        nodes = _body(fn)
        waits = list(_wait_calls(nodes))
        if not waits or not _dispatches(nodes):
            continue
        seen.append(f"{rel}:{fn.lineno} {fn.name}()")
        if any(_names_a_live_boundary(call) for _, call in waits):
            continue
        own_lines = lines[fn.lineno - 1 : fn.end_lineno]
        if any(UNGATED_MARKER in line for line in own_lines):
            continue
        offenders.append(f"{rel}:{fn.lineno} {fn.name}()")
    return offenders, seen


def test_every_dispatch_then_wait_site_consults_a_turn_boundary():
    """Enumerate the dispatch-then-wait sites in src/ and require a boundary.

    This is the shape of #735 itself: ``cao session send`` posted a message and
    then waited on a status that describes the *current frame*. Any site that
    sends and then waits without naming the turn has the same defect.
    """
    offenders = []
    seen = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        found, sites = _ungated_sites(rel, path.read_text(encoding="utf-8"))
        offenders.extend(found)
        seen.extend(sites)

    assert not offenders, (
        "These sites dispatch input and then wait for the turn to finish without "
        "passing a turn boundary to the wait, so the previous turn's latched "
        "COMPLETED -- which is genuinely the terminal's status until this turn's "
        "first PROCESSING is applied -- ends the wait at once (#735, #407):\n  "
        + "\n  ".join(offenders)
        + "\nPass the dispatch boundary (e.g. min_generation=) or, if the site is "
        f"sound without one, say why in a `# {UNGATED_MARKER} -- <reason>` comment "
        "inside the function."
    )
    # The lint must actually be looking at something.
    assert seen, "found no dispatch-then-wait sites at all -- the call-name sets are stale"


_SNIPPET = """
def ungated(tid):
    send_input(tid, "go")
    poll_until_done(tid, 30)

def marked(tid):
    send_input(tid, "go")
    # {marker} -- the status here comes from a route with no generation
    poll_until_done(tid, 30)

def literal_zero(tid):
    send_input(tid, "go")
    poll_until_done(tid, 30, min_generation=0)

def literal_none(tid):
    send_input(tid, "go")
    poll_until_done(tid, 30, min_generation=None)

def gated(tid):
    boundary = dispatch_input(tid, "go")
    poll_until_done(tid, 30, min_generation=boundary)

async def gated_via_to_thread(tid):
    boundary = await asyncio.to_thread(dispatch_input, tid, "go")
    await asyncio.to_thread(poll_until_done, tid, 30, min_generation=boundary)

def only_waits(tid):
    poll_until_done(tid, 30)

def marked_outside(tid):
    send_input(tid, "go")
    poll_until_done(tid, 30)
# {marker} -- a comment after the function does not mark it
"""


def test_lint_core_judges_each_shape():
    """Pin what the lint refuses and accepts, independent of today's src/.

    The review found two soft spots: a literal ``min_generation=0`` read as
    gated, and the exemption list was keyed by file path and function name so a
    rename failed the lint with no behaviour change. Both are pinned here: a
    literal never gates, and the marker exempts a site only from inside it.
    """
    source = textwrap.dedent(_SNIPPET.format(marker=UNGATED_MARKER))
    offenders, seen = _ungated_sites("snippet.py", source)
    offending = {o.split()[1] for o in offenders}
    considered = {s.split()[1] for s in seen}

    assert offending == {"ungated()", "literal_zero()", "literal_none()", "marked_outside()"}
    assert considered == offending | {"marked()", "gated()", "gated_via_to_thread()"}
    assert "only_waits()" not in considered, "a wait with no dispatch is not a site"


def test_a_renamed_or_moved_marked_site_is_still_exempt():
    """The exemption travels with the code, not with a (path, name) key."""
    marked = textwrap.dedent(f"""
        def {{name}}(tid):
            send_input(tid, "go")
            # {UNGATED_MARKER} -- reason lives here
            poll_until_done(tid, 30)
        """)
    for rel, name in (("cli/commands/worker.py", "send"), ("elsewhere/renamed.py", "deliver")):
        offenders, seen = _ungated_sites(rel, marked.format(name=name))
        assert not offenders, (rel, name)
        assert len(seen) == 1


@pytest.fixture
def _finished_turn():
    """A status monitor sitting on a COMPLETED earned before the next send."""
    monitor = MagicMock()
    observation = MagicMock()
    observation.status = TerminalStatus.COMPLETED
    observation.output_generation = 8
    monitor.status_observation.return_value = observation
    monitor.get_status.return_value = TerminalStatus.COMPLETED
    # Nothing has landed since the send: the gate's unbounded state (P2-2). A bare
    # MagicMock here coerces to int 1 through the Terminal model and reads as a
    # server restart instead.
    monitor.output_generation.return_value = 8
    return monitor


def test_the_status_a_client_reads_still_carries_which_turn_earned_it(_finished_turn):
    """The gate must not be switchable off by dropping a field on the way out.

    Builds the GET body the way production does -- ``terminal_service.get_terminal``
    through the real ``Terminal`` model -- and feeds it to the real
    ``poll_until_done`` with a boundary. This test never names the field that
    carries the generation, so a rename or a drop anywhere along that chain makes
    the gate degrade to the pre-#735 wait and fails here, where the per-side tests
    (each asserting its own literal key) all still pass.
    """
    from cli_agent_orchestrator.services import terminal_service
    from cli_agent_orchestrator.utils.terminal import poll_until_done

    with (
        patch.object(terminal_service, "status_monitor", _finished_turn),
        patch.object(
            terminal_service,
            "get_terminal_metadata",
            return_value={
                "id": "term735",
                "tmux_window": "developer-term735",
                "tmux_session": "s-735",
                "provider": "claude_code",
                "agent_profile": "developer",
                "last_active": datetime.now(),
            },
        ),
        patch.object(terminal_service, "get_deferred_init_failure", return_value=None),
        patch.object(terminal_service, "callback_terminal_id_for_session", return_value=None),
    ):
        body = Terminal(**terminal_service.get_terminal("term735")).model_dump()

    assert body["status"] == TerminalStatus.COMPLETED.value, "fixture no longer reads COMPLETED"
    # The pair must come from ONE lock read. Two separate reads (get_status(),
    # then a generation accessor) let a detector apply this turn's PROCESSING in
    # between and pair the previous turn's COMPLETED with the new turn's stamp,
    # which is a reading the gate below would wrongly accept (review P3-2; the
    # separate accessor was removed so no caller can pair them again).
    _finished_turn.status_observation.assert_called_once()
    _finished_turn.get_status.assert_not_called()

    reads = []

    def get(url, timeout=None):
        reads.append(url)
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = body
        return response

    # An ungated wait returns on this COMPLETED at the first read. The gate, fed
    # the body above, must refuse it for the whole (short) timeout instead: that
    # raise IS the assertion, so it is spelled out rather than left to
    # ``pytest.raises`` with a read count beside it (review P3-1).
    with patch("cli_agent_orchestrator.utils.terminal.api_http.get", side_effect=get):
        try:
            poll_until_done("term735", timeout=0.05, polling_interval=0.01, min_generation=8)
        except click.ClickException as exc:
            assert "Timed out" in exc.message, exc.message
        else:
            pytest.fail(
                "GET /terminals/{id}'s body no longer tells poll_until_done which turn "
                "the status was earned from, so the #735 gate silently degraded to the "
                "frame-based wait and finished on the previous turn's COMPLETED after "
                f"{len(reads)} read(s)"
            )
