"""Structural guard: an event subscription must be established BEFORE the state
snapshot it is meant to cover (#755).

Defect class: a long-lived subscription is opened *after* the snapshot/replay
that seeds the consumer's view, so every event emitted in the interval between
the two is observed by neither. #755 was this shape in
``HerdrInboxService._socket_loop`` (snapshot, then subscribe): herdr 0.8.x
replayed retained history on subscribe and masked it; herdr 0.9.0 removed the
replay and the lost ``pane.updated`` became a stalled handoff.

The repo already knows this class -- ``SseBus.register()`` exists precisely so a
caller "can start buffering live events *before* it takes a history snapshot"
(``services/sse_bus.py``) -- but nothing enforced it, so one stream path uses the
seam and another does not. This test enumerates every function in ``src/`` that
does both halves and pins the order, so a regression fails CI instead of waiting
for a reader.

The match is naming-convention based (see the token sets below) and therefore
approximate in both directions. ``KNOWN_SNAPSHOT_FIRST`` is the ratchet: the set
of offending sites must shrink, never grow, and an entry must be deleted when the
site is fixed.
"""

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "cli_agent_orchestrator"

# A call that opens or hands over a live event subscription. Matched as whole
# ``_``-delimited tokens so ``stop_watchdog`` is not mistaken for a watcher.
SUBSCRIBE_TOKENS = {"subscribe", "listen", "watch"}
# ``SseBus.register``/``drain`` is the seam built for this invariant; a bare
# ``register`` elsewhere is not a subscription, so it only counts paired with a
# snapshot in the same function, which is exactly what this test looks for.
SUBSCRIBE_NAMES = {"register", "drain"}

# A call that seeds the consumer's view from current/past state.
SNAPSHOT_TOKENS = {"snapshot", "reconcile", "replay", "history"}

# site -> why it is still snapshot-first. Shrink only.
KNOWN_SNAPSHOT_FIRST = {
    # Live sibling of #755, found by this guard, NOT fixed on the #755 branch:
    # run_plane_stream yields STATE_SNAPSHOT from snapshot_fn() and only then
    # calls bus_subscribe_fn(), whose SseBus subscriber is registered lazily on
    # the first __anext__ -- so fleet events in that window reach no client.
    # Filed separately; the fix is to use SseBus.register()/drain() the way
    # api/main.py:agui_stream already does. Delete this entry with the fix.
    ("services/agui/run_plane.py", "run_plane_stream"),
}


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return getattr(func, "id", "") or ""


def _matches(name: str, tokens: set, names: frozenset = frozenset()) -> bool:
    if name in names:
        return True
    return bool(tokens & set(name.lower().split("_")))


def _own_calls(fn):
    """Yield the calls in ``fn``'s OWN body, not in nested defs or lambdas.

    Source order only carries meaning within a single function body: a closure
    defined above its caller says nothing about when it runs.
    """
    stack = list(fn.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Call):
            yield node
        stack.extend(ast.iter_child_nodes(node))


def _sites() -> dict:
    """Map (relpath, function) -> (first subscribe line, first snapshot line)."""
    found = {}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            subs, snaps = [], []
            for node in _own_calls(fn):
                name = _call_name(node)
                if not name:
                    continue
                if _matches(name, SUBSCRIBE_TOKENS, frozenset(SUBSCRIBE_NAMES)):
                    subs.append(node.lineno)
                if _matches(name, SNAPSHOT_TOKENS):
                    snaps.append(node.lineno)
            if subs and snaps:
                rel = str(path.relative_to(SRC))
                found[(rel, fn.name)] = (min(subs), min(snaps))
    return found


def test_every_subscribe_snapshot_pair_subscribes_first():
    """No src/ function may snapshot before it subscribes (#755)."""
    offenders = {site: lines for site, lines in _sites().items() if lines[0] > lines[1]}
    new = {
        f"{site[0]}:{site[1]} (subscribe line {lines[0]} > snapshot line {lines[1]})"
        for site, lines in offenders.items()
        if site not in KNOWN_SNAPSHOT_FIRST
    }
    assert not new, (
        "snapshot taken before the subscription that must cover it -- events in "
        "the window between them are observed by nobody (#755 defect class). "
        "Subscribe first, then snapshot, then sweep for state that settled "
        "before the subscription existed:\n  " + "\n  ".join(sorted(new))
    )


def test_the_known_offender_list_has_no_stale_entries():
    """A fixed site must be removed from KNOWN_SNAPSHOT_FIRST, not left to rot."""
    sites = _sites()
    stale = {
        site
        for site in KNOWN_SNAPSHOT_FIRST
        if site not in sites or sites[site][0] < sites[site][1]
    }
    assert not stale, (
        "these sites no longer snapshot before subscribing (or no longer exist); "
        f"delete them from KNOWN_SNAPSHOT_FIRST: {sorted(stale)}"
    )


def test_the_guard_actually_sees_the_herdr_socket_loop():
    """Pin the detector against the #755 site, so a rename cannot silently
    empty this guard out."""
    assert ("services/herdr_inbox_service.py", "_socket_loop") in _sites(), (
        "HerdrInboxService._socket_loop is no longer recognised as a "
        "subscribe+snapshot site; the detector's token sets have drifted away "
        "from the code they guard."
    )
