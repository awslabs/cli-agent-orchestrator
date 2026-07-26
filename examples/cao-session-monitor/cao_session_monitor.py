"""Core self-gate and socket-path logic for the CAO session monitor plugin.

Standalone herdr companion plugin (see README.md) -- Python 3 stdlib only, no
import from CAO's own ``src/`` and no third-party runtime dependencies. Later
tasks extend this module with the AG-UI stream consumer, the flows/workflows
REST poller, the focus listener, and the renderer; this file currently
provides the self-gate that decides whether this pane process should render
at all.

herdr socket path convention (mirrors
``cli_agent_orchestrator.backends.herdr_backend.HerdrBackend._session_socket_path``
and ``cli_agent_orchestrator.services.herdr_inbox_service.HerdrInboxService.
_default_socket_path``, duplicated here rather than imported so this plugin
keeps zero CAO code dependency):

- default (unnamed) session: ``~/.config/herdr/herdr.sock``
- named session "<name>":    ``~/.config/herdr/sessions/<name>/herdr.sock``

``HERDR_SOCKET_PATH`` is inherited by every pane herdr spawns and always
points at the socket for the session the pane actually lives in. When set it
takes precedence over the convention above; the convention is only a
fallback for running this script outside herdr (e.g. by hand, in tests).
"""

import json
import os
import signal
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path
from typing import Generator, List, Optional, Tuple, Union

#: Sentinel session name for herdr's own default/unnamed session, matching
#: the convention used throughout cli-agent-orchestrator's herdr backend.
DEFAULT_SESSION = "default"


def expected_socket_path(session_name: str) -> str:
    """Derive the herdr socket path for ``session_name`` by convention.

    Pure function: the only environment input is ``XDG_CONFIG_HOME``, which
    is part of the path convention itself -- herdr and the rest of CAO
    resolve it the same way.

    Args:
        session_name: A herdr session name, or the ``"default"`` sentinel
            for herdr's unnamed session.

    Returns:
        The absolute socket path herdr uses for that session.
    """
    config_home = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    if session_name == DEFAULT_SESSION:
        return f"{config_home}/herdr/herdr.sock"
    return f"{config_home}/herdr/sessions/{session_name}/herdr.sock"


def resolve_socket_path() -> str:
    """Resolve the socket path this process is actually attached to.

    ``HERDR_SOCKET_PATH`` is inherited from the parent herdr pane and always
    wins when set. It is absent only when this script runs outside a herdr
    pane (manual invocation, tests), in which case herdr's default-session
    path is the correct fallback.

    Returns:
        The socket path in effect for this process.
    """
    return os.environ.get("HERDR_SOCKET_PATH") or expected_socket_path(DEFAULT_SESSION)


def should_render(socket_path: str, session_name: str) -> bool:
    """Self-gate: should this pane render for ``session_name``?

    Pure function -- no env access, no hidden global state. herdr plugins
    install once per user and load in every session, so this is what keeps
    the monitor inert everywhere except the one session it is meant to
    watch (typically CAO's, ``"cao"``).

    Args:
        socket_path: The socket path this process is actually attached to
            (see ``resolve_socket_path``).
        session_name: The session this monitor instance is configured to
            render for.

    Returns:
        True iff ``socket_path`` is the socket for ``session_name``, i.e.
        this process is running inside the session it is meant to monitor.
    """
    return socket_path == expected_socket_path(session_name)


# ---------------------------------------------------------------------------
# RFC-6902 JSON Patch (shallow, add/replace/remove only)
# ---------------------------------------------------------------------------


def apply_patch(doc: dict, ops: List[dict]) -> dict:
    """Apply RFC-6902 ops (add/replace/remove) to ``doc``, returning a new dict.

    Shallow only: arrays are whole-key replaced, never deep-merged. Paths are
    JSON Pointer strings (``/key`` or ``/key/subkey``). Only one or two segments
    are supported — matching the granularity ``ui_state_service.diff_snapshot``
    actually emits.

    The original ``doc`` (and its nested dicts) are never mutated — intermediate
    containers on the path are copied on write.

    Args:
        doc: The document to patch (not mutated).
        ops: A list of RFC-6902 operation dicts, each with ``op``, ``path``,
            and (for add/replace) ``value``.

    Returns:
        A new dict with all ops applied in order.
    """
    result = json.loads(
        json.dumps(doc)
    )  # ponytail: deep copy via json round-trip; fine for JSON-native data
    for op_obj in ops:
        op = op_obj["op"]
        path = op_obj["path"]
        tokens = _pointer_tokens(path)
        if op in ("add", "replace"):
            _set_at(result, tokens, op_obj["value"])
        elif op == "remove":
            _remove_at(result, tokens)
    return result


def _pointer_tokens(path: str) -> List[str]:
    """Split a JSON Pointer path into unescaped reference tokens (RFC 6901)."""
    # Leading '/' is mandatory; split produces an empty first element.
    parts = path.split("/")[1:]
    return [p.replace("~1", "/").replace("~0", "~") for p in parts]


def _set_at(doc: dict, tokens: List[str], value: object) -> None:
    """Set a value at the location described by ``tokens`` (mutates ``doc``)."""
    target = doc
    for token in tokens[:-1]:
        target = target[token]
    target[tokens[-1]] = value


def _remove_at(doc: dict, tokens: List[str]) -> None:
    """Remove the key at the location described by ``tokens`` (mutates ``doc``)."""
    target = doc
    for token in tokens[:-1]:
        target = target[token]
    del target[tokens[-1]]


# ---------------------------------------------------------------------------
# Snapshot -> tree projection
# ---------------------------------------------------------------------------


def build_tree(snapshot: dict) -> dict:
    """Transform a ``DashboardSnapshot`` dict into a session-keyed tree.

    Returns::

        {"sessions": {<session_name>: {"terminals": [<terminal_view>, ...]}}}

    Terminal assignment uses ``terminal["session_name"]``. The upstream
    ``ui_state_service`` projects the window field into the ``"window"`` key
    before this function receives it; terminals are passed through
    unchanged.

    The workspace label is the bare ``session_name`` string (it already carries
    one ``cao-`` prefix internally; we do NOT add another).
    """
    sessions: dict = {}
    for s in snapshot.get("sessions", []):
        name = s.get("name", s.get("id", ""))
        sessions[name] = {"terminals": []}
    for t in snapshot.get("terminals", []):
        sess_name = t.get("session_name", "")
        if sess_name not in sessions:
            sessions[sess_name] = {"terminals": []}
        sessions[sess_name]["terminals"].append(t)
    return {"sessions": sessions}


# ---------------------------------------------------------------------------
# SSE stream consumer (stdlib-only)
# ---------------------------------------------------------------------------


def iter_sse(
    url: str, last_event_id: Optional[str] = None
) -> Generator[Tuple[str, str, Optional[str]], None, None]:
    """Yield ``(event_name, data_str, event_id)`` from a Server-Sent Events stream.

    Uses only ``urllib.request`` (no third-party SSE libs). Supports named
    events (``event:`` field) and multi-line ``data:`` concatenation per the
    SSE spec. Sends ``Last-Event-ID`` header on reconnect when provided.

    The server emits named events ``STATE_SNAPSHOT`` / ``STATE_DELTA`` on the
    wire; unnamed events (no ``event:`` field) default to ``"message"``.

    Args:
        url: The SSE endpoint URL (e.g. ``http://localhost:9889/agui/v1/stream``).
        last_event_id: If set, sent as the ``Last-Event-ID`` header (reconnect
            cursor).

    Yields:
        ``(event_name, data_str, event_id)`` tuples. ``data_str`` is the raw
        joined data (not parsed) — the caller is responsible for JSON decoding.
        ``event_id`` is the last ``id:`` field seen up to this event (or None).
    """
    req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
    if last_event_id:
        req.add_header("Last-Event-ID", last_event_id)

    with urllib.request.urlopen(req) as resp:  # noqa: S310 — URL is operator-controlled
        yield from _parse_sse_stream(resp)


def _parse_sse_stream(stream) -> Generator[Tuple[str, str, Optional[str]], None, None]:
    """Parse an SSE byte stream into ``(event_name, data_str, event_id)`` tuples.

    ``event_id`` is the last ``id:`` field value seen up to and including this
    event (per SSE spec, the id persists across events until a new ``id:`` line
    updates it). Callers use this to track the reconnect cursor.
    """
    current_event: Optional[str] = None
    current_data: Optional[str] = None
    last_event_id: Optional[str] = None

    for raw_line in stream:
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")

        if line == "":
            # End of event — dispatch if we have data.
            if current_data is not None:
                event_name = current_event if current_event else "message"
                yield (event_name, current_data, last_event_id)
            current_event = None
            current_data = None
            continue

        # SSE comment
        if line.startswith(":"):
            continue

        colon_idx = line.find(":")
        if colon_idx < 0:
            # Field with no value — ignore per spec.
            continue

        field = line[:colon_idx]
        value = line[colon_idx + 1 :]
        if value.startswith(" "):
            value = value[1:]

        if field == "event":
            current_event = value
        elif field == "data":
            if current_data is None:
                current_data = value
            else:
                current_data = f"{current_data}\n{value}"
        elif field == "id":
            last_event_id = value
        # 'retry' and unknown fields: ignored.

    # Stream ended — dispatch any trailing event without a final blank line.
    if current_data is not None:
        event_name = current_event if current_event else "message"
        yield (event_name, current_data, last_event_id)


# ---------------------------------------------------------------------------
# REST polling: flows and workflows (stdlib-only GET + parse)
# ---------------------------------------------------------------------------


def fetch_json(url: str) -> Union[dict, list]:
    """GET ``url`` and return the parsed JSON response body.

    Uses only ``urllib.request`` (no third-party HTTP client), matching
    ``iter_sse``'s stdlib-only rule. Transport errors (``URLError``,
    ``HTTPError``) and a non-JSON body (``json.JSONDecodeError``) propagate
    unchanged -- this function does not swallow either.

    Args:
        url: The REST endpoint to GET (e.g. ``http://localhost:9889/flows``).

    Returns:
        The parsed JSON body -- a dict or list depending on the endpoint.
    """
    with urllib.request.urlopen(url) as resp:  # noqa: S310 — URL is operator-controlled
        return json.loads(resp.read().decode("utf-8"))


def parse_flows(data: List[dict]) -> List[dict]:
    """Project ``GET /flows`` rows down to the fields the monitor renders.

    Source rows are ``Flow``-model-shaped (see fixtures/flows.json) and carry
    more fields than the monitor shows (``file_path``, ``script``,
    ``prompt_template``); a missing required key raises ``KeyError`` rather
    than silently defaulting, matching ``apply_patch``'s no-silent-gaps rule.

    Args:
        data: The parsed JSON body of ``GET /flows`` -- a list of flow dicts.

    Returns:
        A list of dicts, one per flow, each with exactly: ``name``,
        ``schedule``, ``agent_profile``, ``provider``, ``enabled``,
        ``last_run``, ``next_run``.
    """
    return [
        {
            "name": row["name"],
            "schedule": row["schedule"],
            "agent_profile": row["agent_profile"],
            "provider": row["provider"],
            "enabled": row["enabled"],
            "last_run": row["last_run"],
            "next_run": row["next_run"],
        }
        for row in data
    ]


def parse_workflows(data: List[dict]) -> List[dict]:
    """Parse ``GET /workflows`` rows into the monitor's internal representation.

    Source rows are ``WorkflowIndexRow``-shaped (see fixtures/workflows.json)
    -- every field is kept, since the row already matches what the monitor
    shows (unlike ``parse_flows``, there is nothing to drop).

    Args:
        data: The parsed JSON body of ``GET /workflows`` -- a list of
            workflow-index dicts.

    Returns:
        A list of dicts, one per workflow, each with exactly: ``name``,
        ``source_path``, ``mode``, ``step_count``, ``description``,
        ``indexed_at``.
    """
    return [
        {
            "name": row["name"],
            "source_path": row["source_path"],
            "mode": row["mode"],
            "step_count": row["step_count"],
            "description": row["description"],
            "indexed_at": row["indexed_at"],
        }
        for row in data
    ]


# ---------------------------------------------------------------------------
# Focus bolding
# ---------------------------------------------------------------------------


def bold_set(tree: dict, ws_label: Optional[str], tab_label: Optional[str]) -> dict:
    """Mark the focused session/terminal in ``tree``, returning a fresh copy.

    ``ws_label`` matches a session via direct membership in ``tree["sessions"]``
    -- the workspace label is the bare ``session_name`` with no ``cao-`` prefix
    to strip (confirmed in Task 1/4; see ``build_tree``'s docstring). ``tab_label``
    then matches a terminal's ``window`` field (not ``name``) within that one
    session -- the herdr tab label CAO writes, format ``<agent_profile>-<4hex>``
    (confirmed in ``fixtures/README.md`` section 1).

    Covers focused (labels resolve to a real entry -> that entry is marked) and
    no-match (stale/racy focus data, or focus outside this CAO session -> no
    marker set, tree shape otherwise unchanged) without raising either way. A
    "focus moved" call is not a distinct code path: this function is stateless
    and recomputes from scratch on every call, so calling it again with the new
    labels naturally produces a copy where only the new entry is marked -- the
    caller doesn't need to un-mark a previous result.

    ``tab_label is None`` (a valid ``focused_labels()`` return when the
    workspace resolves but the tab does not) is treated as no-match and short
    circuits before the terminal loop -- it is never compared against a
    terminal's ``window`` field. Without this guard, a terminal with no
    ``window`` key (``dict.get`` -> None) would false-match ``None == None``
    and get spuriously marked focused. This also means a resolved workspace
    with no resolved tab does not mark the session alone: the spec's bold
    pairing (design.md D4) is workspace label -> session row *and* tab label
    -> worker row together, with no session-only-bold case, so treating a
    missing tab label as a full no-match (session included) matches the spec
    rather than inventing a new partial-bold state.

    Like ``build_tree`` (builds a fresh dict rather than mutating its input)
    and ``apply_patch`` (explicit "ponytail: deep copy via json round-trip"),
    this never mutates ``tree`` -- it returns an independent deep copy, marked
    on write.

    Args:
        tree: A tree dict as returned by ``build_tree``/``apply_patch``.
        ws_label: The focused herdr workspace label, or None if focus is
            undeterminable.
        tab_label: The focused herdr tab label, or None if focus is
            undeterminable.

    Returns:
        A deep copy of ``tree``. When ``ws_label`` names a session and
        ``tab_label`` matches one of that session's terminals' ``window``
        field, that session dict and that terminal dict each get
        ``"focused": True`` added. On any no-match, the copy carries no
        ``"focused"`` key at all -- callers should treat a missing key as
        not-focused.
    """
    result = json.loads(json.dumps(tree))  # ponytail: deep copy via json round-trip
    sessions = result.get("sessions", {})
    if ws_label not in sessions or tab_label is None:
        return result
    session = sessions[ws_label]
    for terminal in session.get("terminals", []):
        if terminal.get("window") == tab_label:
            session["focused"] = True
            terminal["focused"] = True
            break
    return result


def focused_labels() -> Tuple[Optional[str], Optional[str]]:
    """``(workspace_label, tab_label)`` of the currently focused herdr pane.

    Queries live focus state via a one-shot ``herdr api snapshot`` subprocess
    call -- the same envelope-wrapped JSON API and subprocess pattern already
    used by ``HerdrBackend._refresh_pane_id_map`` in
    ``cli_agent_orchestrator.backends.herdr_backend`` (``self._run_herdr(["api",
    "snapshot"], check=False)``), reproduced here rather than imported so this
    plugin keeps zero CAO code dependency (matching this module's other
    self-contained herdr conventions -- see the module docstring).

    No ``--session`` flag is needed: this pane script always runs as a child
    process of the herdr session it monitors, so it inherits
    ``HERDR_SOCKET_PATH`` and the CLI resolves that same live session
    automatically (verified live: querying with only ``HERDR_SOCKET_PATH`` set
    reproduces the identical ``focused_workspace_id`` as the full inherited
    pane environment).

    Resolves ``workspace_label`` from top-level ``focused_workspace_id``
    against ``snapshot["workspaces"]``, and ``tab_label`` from
    ``focused_tab_id`` against ``snapshot["tabs"]``. This is the mechanism
    ``brainstorm.md`` fact 4 recorded as verified live during the Task 1 spike
    ("``herdr api snapshot`` carries ``workspaces[].label/focused``,
    ``tabs[].label/focused``, and top-level ``focused_workspace_id/tab_id``").
    Note: ``fixtures/README.md`` itself only documents the window-name and
    workspace-label *format* (sections 1-2) -- the focus-query mechanism is
    recorded in ``design.md`` D4 and ``brainstorm.md`` fact 4 instead, and
    corroborated live against this process's own herdr pane during
    implementation.

    Never raises: a missing ``herdr`` binary, a non-zero exit, a timeout, or an
    unparseable/malformed snapshot all fall through to ``(None, None)`` -- the
    "keep last-known bold" signal a focus read failure calls for.

    Returns:
        ``(workspace_label, tab_label)``, each independently ``None`` if that
        half of the focus state could not be resolved, or ``(None, None)`` if
        focus state could not be read at all.
    """
    try:
        proc = subprocess.run(
            ["herdr", "api", "snapshot"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if proc.returncode != 0:
            return (None, None)
        payload = json.loads(proc.stdout)
        snapshot = payload["result"] if "result" in payload else payload
        snapshot = snapshot.get("snapshot", snapshot)
        focused_workspace_id = snapshot.get("focused_workspace_id")
        focused_tab_id = snapshot.get("focused_tab_id")
        ws_label = next(
            (
                w["label"]
                for w in snapshot.get("workspaces", [])
                if w.get("workspace_id") == focused_workspace_id
            ),
            None,
        )
        tab_label = next(
            (t["label"] for t in snapshot.get("tabs", []) if t.get("tab_id") == focused_tab_id),
            None,
        )
        return (ws_label, tab_label)
    except (
        subprocess.SubprocessError,
        OSError,
        json.JSONDecodeError,
        KeyError,
        AttributeError,
        TypeError,
        ValueError,
    ):
        return (None, None)


# ---------------------------------------------------------------------------
# Render (three-block text readout: sessions, flows, workflows)
# ---------------------------------------------------------------------------


def _bold(text: str, is_bold: bool) -> str:
    """Wrap ``text`` in ANSI bold escapes when ``is_bold``, else return it unchanged."""
    return f"\033[1m{text}\033[0m" if is_bold else text


def _format_terminal_row(terminal: dict) -> str:
    """One line for a single terminal, tolerant of missing fields.

    ``build_tree`` passes terminal dicts through unchanged from whatever shape
    the live snapshot/delta stream happens to carry (e.g. a still-INITIALIZING
    terminal may lack ``provider``/``window``), so every field read here is
    ``.get``-defaulted rather than direct-indexed.
    """
    agent_profile = terminal.get("agent_profile", "?")
    provider = terminal.get("provider", "?")
    window = terminal.get("window", "?")
    status = terminal.get("status") or "-"
    return f"    {agent_profile} [{provider}] {window} ({status})"


def _render_sessions_block(tree: dict) -> str:
    """Full sessions/terminals readout: one line per session, one per terminal.

    Rows with ``"focused": True`` (set by ``bold_set``) render wrapped in ANSI
    bold; every other row renders as plain text with no escape codes.
    """
    lines = ["SESSIONS"]
    sessions = tree.get("sessions", {})
    if not sessions:
        lines.append("  (none)")
        return "\n".join(lines)
    for name, session in sessions.items():
        lines.append(_bold(f"  {name}", session.get("focused", False)))
        for terminal in session.get("terminals", []):
            lines.append(_bold(_format_terminal_row(terminal), terminal.get("focused", False)))
    return "\n".join(lines)


def _render_agui_disabled_hint() -> str:
    """Sessions-block replacement for when the AG-UI stream surface is off.

    No session data is available in this mode -- the stream that would carry
    it was never connected -- so this renders only the hint, never a partial
    or stale tree.
    """
    return "SESSIONS\n  AG-UI streaming is off -- set CAO_AGUI_ENABLED=1 to enable this block."


def _render_flows_block(flows: List[dict]) -> str:
    """One line per flow: name, cron schedule, enabled state.

    ``flows`` is ``parse_flows``'s output -- every dict is guaranteed to carry
    exactly its 7 named keys, so direct indexing (not ``.get``) is safe here,
    matching ``parse_flows``'s own no-silent-gaps convention.
    """
    lines = ["FLOWS"]
    if not flows:
        lines.append("  (none)")
        return "\n".join(lines)
    for flow in flows:
        state = "enabled" if flow["enabled"] else "disabled"
        lines.append(f"  {flow['name']}  {flow['schedule']}  {state}")
    return "\n".join(lines)


def _render_workflows_block(workflows: List[dict]) -> str:
    """One line per workflow: name and step count -- definitions only, no
    per-run status (no CAO endpoint exposes live workflow runs).

    ``workflows`` is ``parse_workflows``'s output, so direct indexing is safe
    for the same reason as ``_render_flows_block``. ``step_count`` is the one
    field documented ``Optional[int]`` on the real ``WorkflowIndexRow`` model,
    so it alone gets a None-safe check.
    """
    lines = ["WORKFLOWS"]
    if not workflows:
        lines.append("  (none)")
        return "\n".join(lines)
    for workflow in workflows:
        step_count = workflow["step_count"]
        steps = f"{step_count} steps" if step_count is not None else "steps unknown"
        lines.append(f"  {workflow['name']} ({steps})")
    return "\n".join(lines)


def render(
    tree: dict,
    flows: List[dict],
    workflows: List[dict],
    agui_enabled: bool,
    *,
    stream_disconnected: bool = False,
    unreachable: bool = False,
) -> str:
    """Render the monitor's three-block text readout: sessions, flows, workflows.

    Pure formatting -- no I/O, no env access. ``main()`` (a later task) owns
    fetching ``tree``/``flows``/``workflows`` and resolving ``agui_enabled``
    (e.g. from ``CAO_AGUI_ENABLED`` or a 404 on the stream connect); this
    function only turns already-fetched data into text. The three blocks
    always appear in this fixed order regardless of any argument's content,
    so callers can rely on sessions coming first, then flows, then workflows.

    Args:
        tree: The session/terminal tree, shaped like ``build_tree``/
            ``apply_patch``/``bold_set``'s output --
            ``{"sessions": {name: {"terminals": [...], "focused"?: True}}}``.
            A session or terminal dict with ``"focused": True`` renders
            wrapped in ANSI bold (``\033[1m...\033[0m``); every other row
            renders plain, with no escape codes. Ignored entirely when
            ``agui_enabled`` is False.
        flows: Flow rows as returned by ``parse_flows``.
        workflows: Workflow rows as returned by ``parse_workflows``.
        agui_enabled: Whether the AG-UI stream surface is on. False replaces
            the sessions block with an enable-hint (degradation mode) instead
            of attempting to render any session data; flows and workflows
            still render from their own data either way.
        stream_disconnected: When True, appends a disconnected indicator to
            the sessions block (spec R3: shows disconnected indicator until
            state is restored).
        unreachable: When True, prepends a one-line CAO unreachable banner
            (spec R6: CAO unreachable banner while retaining last-known data).

    Returns:
        The full text readout, with a blank line between blocks.
    """
    blocks: List[str] = []

    # CAO unreachable banner (spec R6)
    if unreachable:
        blocks.append("CAO unreachable (:9889)")

    sessions_block = _render_sessions_block(tree) if agui_enabled else _render_agui_disabled_hint()
    if stream_disconnected and agui_enabled:
        sessions_block += "\n  [disconnected -- reconnecting]"
    blocks.append(sessions_block)

    blocks.append(_render_flows_block(flows))
    blocks.append(_render_workflows_block(workflows))
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# main() entry point — integration wiring (Task 8)
# ---------------------------------------------------------------------------

#: Base URL for the CAO API server. Matches `CAO_AGUI_BASE` convention used
#: by sibling examples (ag-ui-dashboard/showcase.sh, ag-ui-eventsource-viewer).
_DEFAULT_BASE_URL = "http://localhost:9889"

#: Poll interval for REST endpoints (flows/workflows), in seconds.
_REST_POLL_INTERVAL = 15.0

#: Focus-label refresh interval, in seconds.
_FOCUS_POLL_INTERVAL = 2.0

#: Main render-loop tick interval, in seconds.
_RENDER_INTERVAL = 1.0


def _sse_reader(
    base_url: str,
    state: dict,
    lock: threading.Lock,
    stop: threading.Event,
) -> None:
    """Daemon thread: consume the AG-UI SSE stream, maintaining tree state.

    On HTTP 404 (surface disabled), sets ``agui_enabled=False`` and exits.
    On other errors, sets ``stream_disconnected=True``, retries after a short
    backoff until ``stop`` is set, and clears the flag on successful reconnect.
    """
    url = f"{base_url}/agui/v1/stream"
    last_event_id: Optional[str] = None
    while not stop.is_set():
        try:
            for event_name, data_str, event_id in iter_sse(url, last_event_id=last_event_id):
                if stop.is_set():
                    return
                if event_id is not None:
                    last_event_id = event_id
                payload = json.loads(data_str)
                with lock:
                    if event_name == "STATE_SNAPSHOT":
                        snapshot = payload.get("snapshot", payload)
                        state["_snapshot"] = snapshot
                        state["tree"] = build_tree(snapshot)
                    elif event_name == "STATE_DELTA":
                        ops = payload.get("delta", payload)
                        if isinstance(ops, list) and state["_snapshot"]:
                            # Rebuild tree from patched snapshot; deltas target
                            # the flat snapshot shape, not the tree shape.
                            state["_snapshot"] = apply_patch(state["_snapshot"], ops)
                            state["tree"] = build_tree(state["_snapshot"])
                    state["agui_enabled"] = True
                    state["stream_disconnected"] = False
        except Exception as exc:
            # urllib raises HTTPError(404) when the AG-UI surface is off.
            if _is_http_404(exc):
                with lock:
                    state["agui_enabled"] = False
                return
            # Connection refused / timeout / parse error: show disconnected, retry.
            with lock:
                state["stream_disconnected"] = True
            stop.wait(5.0)


def _rest_poller(
    base_url: str,
    state: dict,
    lock: threading.Lock,
    stop: threading.Event,
) -> None:
    """Daemon thread: poll /flows and /workflows on a timer."""
    while not stop.is_set():
        try:
            flows_data = fetch_json(f"{base_url}/flows")
            workflows_data = fetch_json(f"{base_url}/workflows")
            with lock:
                state["flows"] = parse_flows(flows_data)
                state["workflows"] = parse_workflows(workflows_data)
                state["unreachable"] = False
        except Exception:
            # ponytail: keep last-known on failure; set unreachable banner, retry next tick
            with lock:
                state["unreachable"] = True
        stop.wait(_REST_POLL_INTERVAL)


def _focus_poller(
    state: dict,
    lock: threading.Lock,
    stop: threading.Event,
) -> None:
    """Daemon thread: refresh focused workspace/tab labels.

    On a read failure (both labels None), preserves the last-known bold set
    rather than clearing all bolding (spec R5 scenario: focus read failure).
    """
    while not stop.is_set():
        ws_label, tab_label = focused_labels()
        if ws_label is not None or tab_label is not None:
            # At least one label resolved — update state.
            with lock:
                state["ws_label"] = ws_label
                state["tab_label"] = tab_label
        # (None, None) means read failure — keep previous state unchanged.
        stop.wait(_FOCUS_POLL_INTERVAL)


def _render_loop(
    state: dict,
    lock: threading.Lock,
    stop: threading.Event,
) -> None:
    """Main-thread render loop: snapshot state, render, print."""
    last_output = ""
    while not stop.is_set():
        with lock:
            tree = state.get("tree", {"sessions": {}})
            flows = state.get("flows", [])
            workflows = state.get("workflows", [])
            agui_enabled = state.get("agui_enabled", False)
            ws_label = state.get("ws_label")
            tab_label = state.get("tab_label")
            stream_disconnected = state.get("stream_disconnected", False)
            unreachable = state.get("unreachable", False)

        marked_tree = bold_set(tree, ws_label, tab_label)
        output = render(
            marked_tree,
            flows,
            workflows,
            agui_enabled,
            stream_disconnected=stream_disconnected,
            unreachable=unreachable,
        )

        if output != last_output:
            # Clear screen + home cursor, then print fresh output.
            sys.stdout.write(f"\033[2J\033[H{output}\n")
            sys.stdout.flush()
            last_output = output

        stop.wait(_RENDER_INTERVAL)


def _is_http_404(exc: BaseException) -> bool:
    """True if ``exc`` is an urllib HTTPError with status 404."""
    return hasattr(exc, "code") and getattr(exc, "code", None) == 404


def _install_signal_handler(stop: threading.Event) -> None:
    """Set SIGTERM (and SIGINT as backup) to trigger the stop event."""

    def _handler(signum: int, frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def main() -> int:
    """Entry point: self-gate, then run the three-thread monitor loop.

    Returns 0 on clean exit (including self-gate rejection).
    """
    session = os.environ.get("CAO_MONITOR_SESSION", "cao")
    if not should_render(resolve_socket_path(), session):
        return 0

    base_url = os.environ.get("CAO_AGUI_BASE", _DEFAULT_BASE_URL)

    stop = threading.Event()
    _install_signal_handler(stop)

    state: dict = {
        "tree": {"sessions": {}},
        "_snapshot": {},
        "flows": [],
        "workflows": [],
        "agui_enabled": os.environ.get("CAO_AGUI_ENABLED") == "1",
        "ws_label": None,
        "tab_label": None,
        "stream_disconnected": False,
        "unreachable": False,
    }
    lock = threading.Lock()

    threads = [
        threading.Thread(target=_sse_reader, args=(base_url, state, lock, stop), daemon=True),
        threading.Thread(target=_rest_poller, args=(base_url, state, lock, stop), daemon=True),
        threading.Thread(target=_focus_poller, args=(state, lock, stop), daemon=True),
    ]
    for t in threads:
        t.start()

    try:
        _render_loop(state, lock, stop)
    except KeyboardInterrupt:
        stop.set()

    # Give daemon threads a moment to notice the stop event and exit cleanly.
    for t in threads:
        t.join(timeout=1.0)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
