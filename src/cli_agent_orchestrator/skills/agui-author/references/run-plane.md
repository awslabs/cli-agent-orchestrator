# AG-UI Run Plane Reference

The run plane (`POST /agui/v1/run`) provides a stock AG-UI wire dialect for
driving and observing a CAO session as a single SSE stream.

## Endpoint

```
POST /agui/v1/run
Content-Type: application/json

{
  "agent_id": "<agent_profile>",
  "session_id": "<optional session name>",
  "input": "<task prompt>",
  "resume": [{"interrupt_id": "...", "value": {...}}]
}
```

**Requires:** `CAO_AGUI_ENABLED=true`

## Lifecycle Frames

Every run ends, and the response body closes right after its last frame (a
stock AG-UI client completes a run only when the body ends):

| Situation | Frames |
|---|---|
| Interrupts pending | `RUN_STARTED`, `STATE_SNAPSHOT`, `RUN_FINISHED` (`outcome: interrupt`) |
| Nothing pending | `RUN_STARTED`, `STATE_SNAPSHOT`, `RUN_FINISHED` (`outcome: success`) |
| `resume[]` delivered, nothing left pending | `RUN_STARTED`, `STATE_SNAPSHOT`, `RUN_FINISHED` (`outcome: success`) |
| `resume[]` delivered, other interrupts still pending | `RUN_STARTED`, `STATE_SNAPSHOT`, `RUN_FINISHED` (`outcome: interrupt`, the remaining ones) |
| Resume failed (unknown id, bad payload, delivery failure) | `RUN_STARTED`, `RUN_ERROR` |

A `success` after `resume[]` means the decision was delivered, not that the
agent finished its work. If the agent prompts again, the next run returns that
prompt as an interrupt. Live fleet activity is the ambient plane,
`GET /agui/v1/stream`.

### Frame Types

| Frame | When |
|-------|------|
| `RUN_STARTED` | Stream opens, run ID assigned |
| `STATE_SNAPSHOT` | Initial fleet state (always first after RUN_STARTED) |
| `STATE_DELTA` | Incremental fleet updates |
| `TOOL_CALL_START` | An orchestration tool was invoked |
| `TOOL_CALL_END` | Tool completed (with `closed_by` disposition) |
| `TOOL_CALL_RESULT` | Tool output |
| `GENERATIVE_UI` | Your `emit_ui` intents appear here |
| `Interrupt` | Approval prompt — stream pauses until resumed |
| `RUN_FINISHED` | Normal completion |
| `RUN_ERROR` | Error (delivery failure, timeout, etc.) |

### Heartbeat

While projecting live events, the run plane emits `:keep-alive` SSE comments
at a configurable interval (default 15s, env `CAO_AGUI_HEARTBEAT_SECONDS`) to
prevent proxy timeouts. Live projection is available to embedders that inject
an event source into `run_plane_stream`; `POST /agui/v1/run` does not.

## Interrupts

When a terminal enters `WAITING_USER_ANSWER`, CAO records an interrupt. The
next run ends with `RUN_FINISHED` whose `outcome.interrupts` lists it. Each
interrupt carries:

```json
{
  "type": "Interrupt",
  "id": "<interrupt_id>",
  "metadata": {
    "terminalId": "<terminal_id>",
    "sessionName": "<session_name>",
    "reason": "<classified_reason>"
  },
  "responseSchema": {
    "type": "object",
    "properties": {
      "approved": {"type": "boolean"},
      "editedArgs": {"type": "string"}
    }
  }
}
```

### Resuming an interrupt

**Via the run plane:** Include `resume` in a new `POST /agui/v1/run` request:
```json
{
  "resume": [{"interrupt_id": "abc123", "value": {"approved": true}}]
}
```

**Via REST:** `POST /agui/v1/interrupts/{id}/resume`
```json
{"decision": "approve"}
{"decision": "deny"}
{"decision": "edit", "edited_text": "modified command"}
```

### Error handling

- Delivery failure → `RUN_ERROR` frame (retryable)
- Expired interrupt (terminal moved on) → `RUN_FINISHED` with `outcome: expired`
- Surface disabled → HTTP 404 (not 403)

## Replay Contract

`GET /agui/v1/stream?since=<ISO-8601>` replays events from the given timestamp.
- `since` must be valid ISO-8601 (datetime comparison, not lexicographic)
- Malformed `since` → HTTP 400
- Reconnecting with `Last-Event-ID` deduplicates via `AguiStreamReader`
- Multi-frame records get derived ids (`<rid>.<i>`) for correct dedup

## Observing the stream (read-only)

```bash
# Watch live events
curl -N 'http://localhost:9889/agui/v1/stream'

# Replay from a timestamp
curl -N 'http://localhost:9889/agui/v1/stream?since=2026-07-21T00:00:00Z'
```

## See also

- `docs/agui.md` — full AG-UI reference documentation
- `examples/ag-ui/ag-ui-stock-client-live/` — stock `@ag-ui/client` HttpAgent demo
- `examples/ag-ui/ag-ui-meta-dogfood/` — real fleet captured through the stream
