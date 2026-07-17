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

| CAO primitive | AG-UI event(s) |
|---|---|
| session / terminal launch | `RUN_STARTED` / `STEP_STARTED` |
| completion | `RUN_FINISHED` / `STEP_FINISHED` |
| handoff / delegation | `TOOL_CALL_START` / `TOOL_CALL_END` |
| message delivery | `TEXT_MESSAGE_CONTENT` (metadata only) |
| file modification | `STATE_DELTA` (RFC 6902) against a fleet `STATE_SNAPSHOT` |
| error | `RUN_ERROR` |
| agent-authored UI | `GENERATIVE_UI` |

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

[`examples/agui-dashboard/`](../examples/agui-dashboard) is a credentials-free,
runnable demonstration: `run.sh` starts a `cao-server` with the surface enabled
and `showcase.sh` drives all six allow-listed components plus an off-list
refusal through `emit_ui`, gating on the `GENERATIVE_UI` frames actually
arriving on the live SSE stream (so it doubles as a deployment smoke test). Teach
an agent the component vocabulary with the bundled
[`agui-author`](../skills/agui-author/SKILL.md) skill.

A **stock browser client** rendering the stream — the dependency-free
[`examples/agui-eventsource-viewer/`](../examples/agui-eventsource-viewer) — is the
adapter-level proof. Its demo GIF is **generated by the build** (shift-left): the
recorder drives the six components + the off-list refusal and asserts each renders
before exporting the GIF, so CI fails if the stream→component contract drifts.

![AG-UI EventSource viewer rendering the six generative-UI components live](media/agui-eventsource-viewer-demo.gif)

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
