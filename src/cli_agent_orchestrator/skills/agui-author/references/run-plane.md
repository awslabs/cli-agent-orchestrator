# AG-UI Run Plane Reference

The run plane (`POST /agui/v1/run`) provides a stock AG-UI wire dialect for
driving and observing a CAO session as a single SSE stream.

## Endpoint

```
POST /agui/v1/run
Content-Type: application/json

{
  "threadId": "<thread id>",
  "runId": "<run id>",
  "messages": [],
  "resume": [{"interruptId": "...", "status": "resolved", "payload": {"approved": true}}]
}
```

`threadId`, `runId` and `messages` are required (an empty `messages` array is
valid; it is not read). `resume[]` is optional. Each entry needs `interruptId`,
`status` (`resolved` or `cancelled`, where `cancelled` is a deny) and a
`payload` of `{"approved": true|false}` or `{"editedArgs": "..."}`. A body that
is not a valid `RunAgentInput` gets HTTP 422 before the stream starts.

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
| `resume[]` sent, but the server has no approval bridge | HTTP 404 "Approval bridge not initialized", no stream |

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
  "threadId": "t1",
  "runId": "r1",
  "messages": [],
  "resume": [{"interruptId": "abc123", "status": "resolved", "payload": {"approved": true}}]
}
```

**Via REST:** `POST /agui/v1/interrupts/{id}/resume`
```json
{"decision": "approve"}
{"decision": "deny"}
{"decision": "edit", "edited_text": "modified command"}
```

`edit` is offered only where the interrupt's `options` include it: `claude-code:permission_request` offers only approve and deny, and an `edit` decision on it is refused (422 on REST, `RUN_ERROR` on the run plane) before anything is typed, with the interrupt left pending. If an approve or deny delivery for the same interrupt is already in flight, a later `edit` joins it instead and returns that decision's outcome, which names the delivered decision; nothing is typed for the edit.

### Error handling

- Delivery failure → `RUN_ERROR` frame (retryable)
- Expired interrupt (terminal moved on) → the interrupt is already resolved, so
  the entry is skipped like a repeat resume and the run ends with `RUN_FINISHED`
  (`success`, or `interrupt` if others are pending). There is no `expired`
  outcome. A resolved record is evicted when a later interrupt is raised after
  the record is 5 minutes old, or sooner if the registry is over its cap. From
  then on the id is unknown and the run ends with `RUN_ERROR`.
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
