# AG-UI event stream

CAO exposes its normalized fleet events as an
[AG-UI](https://github.com/ag-ui-protocol/ag-ui) typed-event stream over
Server-Sent Events at `GET /agui/v1/stream`. Any AG-UI-compatible client —
CopilotKit apps, the [AG-UI Dojo](https://docs.ag-ui.com/quickstart/applications),
or a plain `EventSource` — renders a live CAO fleet with no CAO-specific adapter
code. A producer endpoint, `POST /agui/v1/emit_ui`, lets agents author
allow-listed **generative UI** onto the same stream.

The surface is **default-off** and metadata-only (message bodies are never on
the wire).

## Enabling the AG-UI surface

`/agui/v1/stream` and `/agui/v1/emit_ui` return `404` unless the surface is
enabled:

```sh
export CAO_AGUI_ENABLED=true      # dedicated AG-UI flag
# (or CAO_MCP_APPS_ENABLED=true, which also enables it — both surfaces read the
#  same in-process event source with the same privacy boundary)
```

With neither flag set the endpoints are absent and the server is byte-identical
to a build without the feature.

## The stream

`GET /agui/v1/stream` maps CAO's six normalized event primitives onto AG-UI
typed events:

| CAO primitive | AG-UI event(s) | Notes |
|---|---|---|
| session / terminal launch | `RUN_STARTED` / `STEP_STARTED` | |
| completion | `RUN_FINISHED` / `STEP_FINISHED` | Also triggers synthesized `TOOL_CALL_END` for open receivers |
| handoff (`orchestration_type=handoff\|assign`) | `TOOL_CALL_START` | Agent-to-agent task delegation [1] |
| handoff (`orchestration_type=send_message` or absent) | `TEXT_MESSAGE_CONTENT` (metadata only) | Simple message dispatch |
| a2a_delegation | `TOOL_CALL_START` | Cross-agent A2A task; closer also synthesizes `TOOL_CALL_RESULT` |
| file modification | `STATE_DELTA` (RFC 6902) against a fleet `STATE_SNAPSHOT` | |
| error | `RUN_ERROR` | |
| agent-authored UI | `GENERATIVE_UI` | |

[1] **L1 Cleanup A (TOOL_CALL lifecycle):** Handoff records with
`orchestration_type` of `handoff` or `assign` now map to `TOOL_CALL_START`
(previously all handoffs mapped to `TEXT_MESSAGE_CONTENT`). The
`ToolCallLifecycleTracker` correlates these opens with receiver terminal
completions and synthesizes exactly one `TOOL_CALL_END` per open. For
`a2a_delegation` opens, a `TOOL_CALL_RESULT` is also synthesized before the
`TOOL_CALL_END`. This ensures AG-UI clients see a well-formed tool-call
lifecycle (open/result/end) rather than orphaned starts.

On connect the server emits a full `STATE_SNAPSHOT` so any client hydrates its
projection, then keeps it current with minimal RFC 6902 `STATE_DELTA` patches
after each fleet change.

## Consuming it

```sh
curl -N http://localhost:9889/agui/v1/stream
```

For a browser client on another origin, add that origin to CORS via
`CAO_CORS_ORIGINS` (comma-separated; appended to the localhost defaults):

```sh
export CAO_AGUI_ENABLED=true
export CAO_CORS_ORIGINS="https://dashboard.example.com"
uv run cao-server
```

## Auth (when CAO has Auth0 enabled)

If CAO runs with `AUTH0_DOMAIN` set, `/agui/v1/stream` requires a `cao:read`
JWT. Native `EventSource` can't send an `Authorization:` header, so the token
travels as the `?access_token=<JWT>` query parameter:

```
https://dashboard.example.com/?access_token=eyJhbGc...
```

**Keep these tokens short-lived.** A query-string credential can surface where
an `Authorization` header never would (browser history, proxy logs, `Referer`
headers) and stays replayable until `exp`. CAO scrubs `access_token` (and,
pre-emptively, `ticket`) values from its own access log, but that doesn't cover
intermediaries — so mint short-TTL tokens (minutes, not hours). A short-lived
single-use ticket handshake (`POST /agui/v1/ticket` with header auth →
`?ticket=`) is a follow-up.

## Connection resilience

A client that drops the connection resumes **without a gap** by one of two
cursors:

- **`Last-Event-ID` (automatic).** Every event frame carries an `id:` cursor, so
  a native browser `EventSource` resends the last id it saw as the
  `Last-Event-ID` request header on its automatic reconnect. The server replays
  the buffered records **after that id** before the live stream — no client code
  required.
- **`?since=<last event timestamp>` (explicit).** A non-`EventSource` client (or
  one resuming across a fresh connection) can pass an ISO-8601 lower bound to
  replay buffered events after that time.

`?since=` takes precedence when both are supplied. Either way the server
registers the live subscription **before** replaying history, then drains both
with event-id deduplication, so events published during the reconnect handoff
are neither lost nor double-delivered. Pair this with a capped exponential
backoff on the client.

**Overflow is a gap *signal*, not a silent drop.** Each subscriber has a bounded
queue so one slow client can never back-pressure the orchestration core. If that
queue fills, the AG-UI stream does **not** quietly drop events on an open
connection — it marks the overflow and **closes the stream**. The browser then
reconnects automatically and the dropped records are replayed exactly once via
`Last-Event-ID` (above). The durable record behind the replay is the in-process
ring buffer (`event_log_service`). (The MCP-Apps `/events` stream keeps its
legacy drop-on-slow behaviour and backfills via `cao_fetch_history`.)

## Replay contract (full specification)

The replay mechanism is deterministic and designed for safe, idempotent reconnects:

### Cursor precedence

1. **`?since=<ISO-8601>`** -- explicit timestamp lower bound. Must be a valid
   ISO-8601 string; malformed values produce HTTP 400 before any streaming starts.
2. **`Last-Event-ID` header** -- native EventSource automatic cursor. Used only
   when `?since=` is absent.
3. **Neither** -- no replay; only the live stream is emitted.

### Validation

The `?since=` parameter is validated as ISO-8601 using `datetime.fromisoformat()`
**before** the streaming response begins. Invalid values return `400 Bad Request`
with a descriptive error. This prevents malformed cursors from being silently
swallowed inside the failure-isolated replay block.

### Over-delivery and deduplication (Seen-Set Dedup)

The live subscription is registered **before** history replay begins. This means
events published during the replay-to-live handoff are buffered in the subscriber
queue. The stream maintains a `replayed_ids` set to deduplicate the overlap:
events that appear in both the replay batch and the live queue are emitted only
once (from replay). This guarantees neither gaps nor duplicates.

### Snapshot ordering

On every connection (fresh or reconnect), the server emits a full
`STATE_SNAPSHOT` after the replay batch and before the first live `STATE_DELTA`.
A client must receive the snapshot to hydrate its projection before it can
correctly apply RFC-6902 patches.

### Tool-call lifecycle across reconnects

The `ToolCallLifecycleTracker` is instantiated per-connection. On reconnect with
replay, the tracker processes replayed records first (rebuilding its open-call
map), then continues with live events. Because the tracker is deterministic
(same inputs produce same outputs), a replay produces the same synthesized frames
as the original stream -- no duplicate closers, no orphan frames.

### CAO extension status

| Feature | Status | Notes |
|---|---|---|
| `?since=` ISO-8601 validation | Shipped | 400 on malformed |
| `Last-Event-ID` replay | Shipped | Native EventSource compatible |
| Seen-Set dedup | Shipped | Zero duplicates on reconnect |
| Overflow-as-gap signal | Shipped | Stream closes; client reconnects |
| `TOOL_CALL_END` lifecycle synthesis | Shipped | Deterministic under replay |
| Short-lived ticket handshake | Follow-up | `POST /agui/v1/ticket` TBD |

## Generative UI

Any agent in the fleet — regardless of provider — can author a UI intent and
have it rendered uniformly by any AG-UI client. The safety model is what makes
that shippable over *untrusted* agents: an agent may only emit a **closed
allow-list of named components with JSON props** — no HTML, no script, no
`eval`, no iframe. An off-list component is **refused, never rendered**.

**Wire path.** An agent calls the `emit_ui` MCP tool (or
`POST /agui/v1/emit_ui`) → the intent rides a CAO event as a `ui` block
(`{component, props}`) → the AG-UI adapter maps it to a typed `GENERATIVE_UI`
frame → `GET /agui/v1/stream` emits it → any AG-UI client renders it.

**Server-side validation** (`services/agui_stream.py`): the closed allow-list
`GENERATIVE_UI_COMPONENTS = {approval_card, choice_prompt, diff_summary,
progress, metric, agent_card}`; unknown components are refused (mapped to `RAW`
with `rejected_component`); props must be JSON-serializable and are size-bounded
(8 KB), degrading safely. `POST /agui/v1/emit_ui` rejects off-list components and
oversized/non-serializable props with `400` before anything reaches the bus.

### Safety model

| Threat | Mitigation | Verified by |
|---|---|---|
| Agent emits arbitrary HTML/script | No HTML on the wire — only named components + JSON props | `TestGenerativeUI` |
| Agent names an off-list component (e.g. `iframe`) | Refused server-side (→ RAW / 400) | `test_off_list_component_is_refused` |
| Agent floods the bus with a huge payload | Props capped at 8 KB → `{_truncated: true}` | `test_oversized_props_are_truncated` |
| Non-serializable props | Degrade to `{}` | `test_non_serializable_props_degrade_to_empty` |
| Message-body leakage | Bodies never in the props path (metadata-only contract) | privacy tests |

A conformant client SHOULD mirror the allow-list as defense in depth and render
each component from JSON props only (no `dangerouslySetInnerHTML`, no `eval`),
falling back to an inert placeholder for anything unknown.

### Live proof

[`examples/ag-ui/ag-ui-dashboard/`](../examples/ag-ui/ag-ui-dashboard) is a credentials-free,
runnable demonstration: `run.sh` starts a `cao-server` with the surface enabled
and `showcase.sh` drives all six allow-listed components plus an off-list
refusal through `emit_ui`, gating on the `GENERATIVE_UI` frames actually
arriving on the live SSE stream (so it doubles as a deployment smoke test). Teach
an agent the component vocabulary with the bundled
[`agui-author`](../skills/agui-author/SKILL.md) skill.

A **stock browser client** rendering the stream — the dependency-free
[`examples/ag-ui/ag-ui-eventsource-viewer/`](../examples/ag-ui/ag-ui-eventsource-viewer) — is the
adapter-level proof. Its demo GIF is **generated by the build** (shift-left): the
recorder drives the six components + the off-list refusal and asserts each renders
before exporting the GIF, so CI fails if the stream→component contract drifts.

![AG-UI EventSource viewer rendering the six generative-UI components live](media/ag-ui-eventsource-viewer-demo.gif)

## Two-plane model

CAO exposes its AG-UI surface through two complementary planes that differ in
wire dialect, consumer shape, and intended use:

### Ambient dialect (GET /agui/v1/stream)

The **ambient plane** is the `GET /agui/v1/stream` SSE endpoint. It speaks
CAO's own AG-UI dialect: `event:` + `data:` frames with UPPER_SNAKE event types
(`STATE_SNAPSHOT`, `STATE_DELTA`, `STEP_STARTED`, `GENERATIVE_UI`, etc.) and an
`id:` cursor for replay. Any `EventSource`-compatible client (browser-native or
library-based) connects, hydrates from the initial `STATE_SNAPSHOT`, then folds
live frames into its projection.

This plane is optimized for long-lived dashboard consumers and the L2 construct
library. No request body is required; the client is a pure observer. Replay is
`Last-Event-ID`-based (browser-automatic) or `?since=`-based (explicit).

### Stock run plane (POST /agui/v1/run)

The **run plane** is the `POST /agui/v1/run` endpoint. It speaks the stock
[ag-ui-protocol](https://github.com/ag-ui-protocol/ag-ui) wire dialect:
`data:`-only camelCase JSON frames with a `type` field, produced by the official
`EventEncoder`. This is what CopilotKit, the AG-UI Dojo, and other stock AG-UI
clients expect.

A client POSTs a `RunAgentInput` (threadId, runId, optional resume[]) and
receives a lifecycle-legal stream: `RUN_STARTED` then `STATE_SNAPSHOT` then live
projection frames (`STATE_DELTA`, `STEP_STARTED/FINISHED`, `TOOL_CALL_START/END`,
`CUSTOM`) then `RUN_FINISHED`. If open approval interrupts exist, the stream
closes with a `RUN_FINISHED` whose outcome is `{type: "interrupt", interrupts: [...]}`,
and the client re-POSTs with a `resume[]` array to resolve them.

The run plane requires the `[agui]` optional extra (`ag-ui-protocol` package).
Without it the endpoint returns HTTP 501.

### Choosing a plane

| Use case | Plane | Why |
|---|---|---|
| Long-lived supervisory dashboard | Ambient | EventSource auto-reconnect, replay, no request body |
| CopilotKit / AG-UI Dojo integration | Run | Stock wire dialect, interrupt/resume lifecycle |
| L2 construct composition (Python) | Ambient | AguiStreamReader + fold constructs |
| Raw HTTP client proving connectivity | Run | Single POST, verifiable frame output |
| CI smoke tests | Either | Both exit non-zero on unmet preconditions |

---

## L2 construct library

The `services/agui/` package provides fold-based constructs that compose on top
of the ambient stream. Each construct consumes AG-UI frames (via `handle_frame`)
and derives a `projection()` view without I/O.

### Foundation

| Class | Role |
|---|---|
| `AguiConstruct` | Abstract base: `handle_frame`, `projection`, `emit` (validated) |
| `UiEmitter` / `RecordingUiEmitter` / `InProcessUiEmitter` / `HttpUiEmitter` | Emitter family (protocol + implementations) |
| `AguiStreamReader` | `requests`-based SSE reader yielding `(event_id, agui_type, data)` tuples |
| `ToolCallLifecycleTracker` | Correlates TOOL_CALL opens with terminal completions |
| `apply_json_patch_strict` | Pure RFC-6902 subset (add/remove/replace), never mutates inputs |

### Fold-based constructs

#### SupervisorDashboardStream

Folds `STATE_SNAPSHOT` and `STATE_DELTA` frames to maintain a local copy of the
fleet topology. Derives:

- `hierarchy()`: session-name keyed dict with terminal lists and counts
- `supervisor_snapshot()`: active sessions, provider distribution, waiting terminals, rollup counters

**Frames folded:** STATE_SNAPSHOT, STATE_DELTA, STEP_STARTED/FINISHED,
RUN_STARTED/FINISHED, TOOL_CALL_START/END/RESULT (rollup counters).

**Seen-Set Dedup:** id-bearing frames already processed are skipped; state
frames (event_id=None) are always folded.

#### MultiAgentSessionTimeline

Tracks delegations and messages as an ordered timeline:

- `TOOL_CALL_START` opens a delegation entry (keyed by tool_call_id)
- `TOOL_CALL_END`/`TOOL_CALL_RESULT` closes matching entries
- `TEXT_MESSAGE_CONTENT` appends message entries (metadata only, no body)

**Projection:** `entries()` returns `TimelineEntry` list sorted by `(started_at, id)`.

**Retention cap:** configurable (default 1000), evicts oldest first.

#### CrossProviderStateSync

Maintains a shared state view by folding snapshots/deltas:

- `shared_state()`: the current state (or None before first snapshot)
- `providers_seen()`: sorted list of unique providers from terminals
- `converges_with(authoritative)`: deep-equality check against a reference

Useful for verifying that a client-side projection has converged with the
server's authoritative state.

#### AgentHandoffWithApproval

Human-in-the-loop approval lifecycle:

- `on_provider_waiting(terminal_id, provider, raw_prompt)`: classifies the
  prompt via `classify_reason` and creates an `Interrupt` record
- `resume(interrupt_id, decision, edited_text)`: lock-guarded exactly-once
  resolution with per-provider answer translation
- `expire(terminal_id)`: zero-keystroke expiration on state transition
- `pending()`: list of unresolved interrupts

**Interrupt options** derive from the classified reason (permission_request gets
approve/deny/edit; trust_prompt gets approve/deny).

### Subclass extension points

All constructs expose their state through `projection()` (JSON-serializable dict).
Subclasses can:

1. Override `handle_frame` to fold additional frame types
2. Call `self.emit(component, props)` to author generative-UI intents (validated
   against the allow-list and 8 KB size bound)
3. Call `self.assert_no_body(data)` to enforce the metadata-only contract
4. Access `self._emitter` for custom transport needs

### Composition pattern

```python
from cli_agent_orchestrator.services.agui import (
    AguiStreamReader,
    RecordingUiEmitter,
    SupervisorDashboardStream,
)

reader = AguiStreamReader("http://localhost:9889")
emitter = RecordingUiEmitter()
dashboard = SupervisorDashboardStream(emitter)

for event_id, agui_type, data in reader.frames():
    dashboard.handle_frame(agui_type, data, event_id)

print(dashboard.hierarchy())
print(dashboard.supervisor_snapshot())
```

### Runnable examples

Each construct has a runnable example in `examples/`:

- [`agui-supervisor-dashboard/`](../examples/ag-ui/ag-ui-supervisor-dashboard/) - SupervisorDashboardStream composition
- [`agui-session-timeline/`](../examples/ag-ui/ag-ui-session-timeline/) - MultiAgentSessionTimeline delegation tracking
- [`agui-handoff-approval/`](../examples/ag-ui/ag-ui-handoff-approval/) - Human-in-the-loop approval (CI + live procedure)
- [`agui-cross-provider-sync/`](../examples/ag-ui/ag-ui-cross-provider-sync/) - CrossProviderStateSync convergence
- [`agui-stock-client-live/`](../examples/ag-ui/ag-ui-stock-client-live/) - Stock AG-UI run plane (POST /agui/v1/run)

### Shift-left video evidence (per feature)

Each new L2 construct ships a **gated** demo recording — proof-of-work, not
decoration. The recorder in
[`examples/ag-ui/ag-ui-construct-demos/tools/`](../examples/ag-ui/ag-ui-construct-demos/tools/)
runs the construct's own asserting example (the `run.sh` above, in
offline/synthetic mode — no live provider, network, or secrets), and **only**
produces a GIF if the example exits `0` and prints its `PASS` marker. If a
construct regresses, the example exits non-zero, the recorder fails, and CI goes
red (see the `AG-UI construct demos (shift-left recordings)` job) — so a broken
construct cannot yield a green recording. Regenerate locally with:

```sh
cd examples/ag-ui/ag-ui-construct-demos/tools
npm ci && npm run playwright:install && npm run record   # ONLY=<slug> for one
```

The GIFs are committed under [`docs/media/`](media/) and re-generated + uploaded
as CI artifacts (`agui-construct-demos`) on every run.

**SupervisorDashboardStream** — folds `STATE_SNAPSHOT`/`STATE_DELTA` into a live
fleet view (active sessions, provider distribution, waiting terminals):

![SupervisorDashboardStream example asserting its fold logic](media/ag-ui-supervisor-dashboard-demo.gif)

**MultiAgentSessionTimeline** — reconstructs the delegation + message timeline
from tool-call frames and closes entries on `TOOL_CALL_END`/`RESULT`:

![MultiAgentSessionTimeline example asserting delegation tracking](media/ag-ui-session-timeline-demo.gif)

**AgentHandoffWithApproval** — human-in-the-loop lifecycle: classify the prompt,
raise an interrupt, resume exactly-once (idempotent), and expire on transition:

![AgentHandoffWithApproval example asserting the approval lifecycle](media/ag-ui-handoff-approval-demo.gif)

**CrossProviderStateSync** — convergent shared state across `claude_code`,
`kiro_cli`, and `codex`, verified with `converges_with()` before and after a delta:

![CrossProviderStateSync example asserting convergence](media/ag-ui-cross-provider-sync-demo.gif)

---

## Privacy boundary

`/agui/v1/stream` redacts message bodies (same contract as the SSE bus and the
in-host MCP Apps iframe). `TEXT_MESSAGE_CONTENT` events carry metadata (sender,
receiver, orchestration_type) but never the message text.

## Troubleshooting

**Connection shows an error** — check that `CAO_AGUI_ENABLED` is set (else the
endpoint 404s) and, for a cross-origin browser client, that the origin is allowed
via `CAO_CORS_ORIGINS` (exact scheme + host + port), then restart CAO.

**401 on connect with auth enabled** — token missing or expired. Get a fresh
`cao:read` token and pass it via `?access_token=`.

**Events stop after a proxy idle timeout** — reconnect with backoff. A native
`EventSource` resumes automatically via `Last-Event-ID`; other clients resume
via `?since=`. No state is lost.
