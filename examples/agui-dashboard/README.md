# AG-UI dashboard demo

A runnable, credentials-free tour of CAO's AG-UI surface: the full
generative-UI component set, the safety refusal, an optional mock fleet, and a
live SSE stream — in about a minute.

## What you'll see

- All six allow-listed generative-UI components rendering live:
  `agent_card`, `progress`, `metric`, `diff_summary`, `choice_prompt`,
  `approval_card`.
- The safety contract in action: an off-list `iframe` intent is **refused
  server-side (400)** and nothing renders.
- SSE reconnect resuming with no gap — automatically via `Last-Event-ID`
  (native `EventSource`) or explicitly via `?since=`.
- Optionally (`CAO_AGUI_DEMO_FLEET=1`): a real 2-worker fleet (deterministic
  `mock_cli` provider — no API keys, no real CLI binaries) appearing as
  `STATE_SNAPSHOT` / terminal events. Needs `tmux`; degrades gracefully
  without it.

## Quick start

```sh
# Terminal 1 — start a cao-server with the AG-UI surface enabled (self-contained)
./examples/agui-dashboard/run.sh
#   CAO_AGUI_DEMO_FLEET=1 …   adds the mock_cli fleet (needs tmux)
#   CAO_AGUI_RUN_SHOWCASE=1 … auto-runs showcase.sh once healthy

# Terminal 2 — drive the six components + the refusal against the live server
./examples/agui-dashboard/showcase.sh

# Terminal 3 — tail the raw AG-UI frames:
curl -N http://localhost:9889/agui/v1/stream
```

## Open a viewer (see it render in a browser)

![AG-UI EventSource viewer rendering the six generative-UI components live](../../docs/media/agui-eventsource-viewer-demo.gif)

For a rendered view instead of raw frames, open the **dependency-free
EventSource viewer** in [`../agui-eventsource-viewer/`](../agui-eventsource-viewer/):
a single HTML file (no npm/build) that renders the fleet projection and the six
generative-UI components live. Serve the repo over http and open it, then run
`showcase.sh`:

```sh
python3 -m http.server 8000   # then open http://localhost:8000/examples/agui-eventsource-viewer/
```

See [`../agui-eventsource-viewer/README.md`](../agui-eventsource-viewer/README.md)
for the CORS note and auth guidance. (This replaces the standalone PWA that was
removed from PR #436.)

`showcase.sh` also runs standalone against any reachable CAO
(`CAO_AGUI_BASE=… CAO_TOKEN=… ./showcase.sh`) — it exits non-zero unless all
six components are accepted, the off-list component is refused, **and** the
six `GENERATIVE_UI` frames actually arrive on the live SSE stream, so it
doubles as a smoke test for a deployment.

## How agents do this for real

The showcase drives `POST /agui/v1/emit_ui` with curl; a real agent calls the
`emit_ui` MCP tool (already registered on `cao-mcp-server`). Teach any agent
the component vocabulary with the bundled **`agui-author`** skill
(`skills/agui-author/SKILL.md`): when to emit which component, props shapes,
the 8 KB bound, and the refusal behavior.

## Auth

Default-off local runs need no tokens. When CAO has Auth0 enabled, pass
`CAO_TOKEN` (a `cao:write` JWT) to `showcase.sh` (it authenticates the POSTs
and the stream tail); a browser `EventSource` client passes a `cao:read` JWT
via `?access_token=<JWT>` — see the short-TTL guidance in
[docs/agui.md](../../docs/agui.md).

## Files

| File | Purpose |
|---|---|
| `run.sh` | Start a cao-server with AG-UI on (plus optional mock fleet / auto-showcase), clean up on exit |
| `showcase.sh` | Drive all six components + the refusal via `emit_ui`, gate on the live SSE frames (usable standalone as a smoke test) |
| `fleet_worker.md` | `mock_cli` agent profile the optional fleet runs on |
