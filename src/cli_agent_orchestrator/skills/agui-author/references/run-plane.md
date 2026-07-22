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

The SSE stream emits frames in this order:

```
RUN_STARTED
  → STATE_SNAPSHOT (full fleet state)
  → [live frames: STATE_DELTA, TOOL_CALL_START, TOOL_CALL_END, GENERATIVE_UI, ...]
  → [optional: Interrupt (approval prompt, pauses stream)]
  → [on resume: remaining lifecycle frames]
RUN_FINISHED | RUN_ERROR
```

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

The run plane emits `:keep-alive` SSE comments at a configurable interval
(default 15s, env `CAO_AGUI_HEARTBEAT_SECONDS`) to prevent proxy timeouts.

## Interrupts

When a terminal enters `WAITING_USER_ANSWER`, the run plane emits an `Interrupt`
frame and pauses. The interrupt carries:

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
