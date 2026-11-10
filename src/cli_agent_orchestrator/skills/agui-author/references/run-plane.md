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
`payload` of `{"approved": true|false}`, plus `"editedArgs": "..."` (the edited
answer text) where the interrupt offers edit. A body that
is not a valid `RunAgentInput` gets HTTP 422 before the stream starts.

**Requires:** `CAO_AGUI_ENABLED=true`

## Capabilities

`GET /agui/v1/capabilities` returns the run plane's AG-UI 1.0
`AgentCapabilities` object, so a client can check what the run plane supports
before it sends a run. Like the other AG-UI routes it returns 404 unless
`CAO_AGUI_ENABLED` is set, and needs `cao:read` when auth is on. A field left
out is undeclared, not unsupported. The full object is in `docs/agui.md`
(Capabilities).

## Lifecycle Frames

Every run ends, and the response body closes right after its last frame (a
stock AG-UI client completes a run only when the body ends):

| Situation | Frames |
|---|---|
| Interrupts pending | `RUN_STARTED`, `STATE_SNAPSHOT`, `RUN_FINISHED` (`outcome: interrupt`) |
| Nothing pending | `RUN_STARTED`, `STATE_SNAPSHOT`, `RUN_FINISHED` (`outcome: success`) |
| `resume[]` delivered, nothing left pending | `RUN_STARTED`, `STATE_SNAPSHOT`, `RUN_FINISHED` (`outcome: success`) |
| `resume[]` delivered, other interrupts still pending | `RUN_STARTED`, `STATE_SNAPSHOT`, `RUN_FINISHED` (`outcome: interrupt`, the remaining ones) |
| `resume[]` names an unknown or expired interrupt | That entry is skipped with a server-side warning and listed in `RUN_FINISHED.result.skippedResume`; the run ends as one of the rows above |
| Resume failed (bad payload, delivery failure) | `RUN_STARTED`, `RUN_ERROR` |
| `resume[]` sent, but the server has no approval bridge | HTTP 404 "Approval bridge not initialized", no stream |

A `success` after `resume[]` means every entry that named a pending interrupt
was delivered; skipped entries are logged and listed in
`result.skippedResume`. It does not mean the agent finished its work. If the agent prompts again, the next run returns that
prompt as an interrupt. Live fleet activity is the ambient plane,
`GET /agui/v1/stream`.

### Frame Types

| Frame | When |
|-------|------|
| `RUN_STARTED` | Stream opens, run ID assigned |
| `STATE_SNAPSHOT` | Initial fleet state (always first after RUN_STARTED) |
| `STATE_DELTA` | Incremental fleet updates |
| `TOOL_CALL_START` | An orchestration tool was invoked |
| `TOOL_CALL_END` | Tool completed (`toolCallId` only) |
| `TOOL_CALL_RESULT` | Tool output |
| `CUSTOM` (`name: "cao.generative_ui"`) | Your `emit_ui` intents, with `value` = `{component, props, ...}` |
| `RUN_FINISHED` | Run ends: `outcome.type` is `success`, or `interrupt` with the pending approval prompts in `outcome.interrupts` (see Interrupts) |
| `RUN_ERROR` | Error (delivery failure, timeout, etc.) |

### Heartbeat

While projecting live events, the run plane emits `:keep-alive` SSE comments
at a configurable interval (default 15s, env `CAO_AGUI_HEARTBEAT_SECONDS`) to
prevent proxy timeouts. Live projection is available to embedders that inject
an event source into `run_plane_stream`; `POST /agui/v1/run` does not.

## Interrupts

When a terminal enters `WAITING_USER_ANSWER`, CAO records an interrupt. The
next run ends with `RUN_FINISHED` whose `outcome.interrupts` lists it. An
interrupt is not an event of its own and has no `type` field (the 1.0 schema
closes `Interrupt`, so an extra key is invalid). This is a real frame from the
run plane, with one interrupt raised by a `kiro_cli` terminal:

```json
{
  "type": "RUN_FINISHED",
  "threadId": "thread-m13",
  "runId": "run-m13",
  "outcome": {
    "type": "interrupt",
    "interrupts": [
      {
        "subagentRunId": "terminal:aaaa1111",
        "id": "262ec508-7e0e-4c0a-977b-51d9585bc63d",
        "reason": "kiro:permission_request",
        "message": "",
        "responseSchema": {
          "type": "object",
          "properties": {
            "approved": {"type": "boolean", "description": "true=approve, false=deny"},
            "editedArgs": {
              "type": "string",
              "description": "edited answer text, typed into the agent's prompt in place of yes/no"
            }
          },
          "required": ["approved"]
        },
        "metadata": {
          "provider": "kiro_cli",
          "terminalId": "aaaa1111",
          "sessionName": "s-1",
          "options": ["approve", "deny", "edit"]
        }
      }
    ]
  }
}
```

- `reason` is top-level: the classified prompt, `<provider>:<kind>`.
- `message` is the prompt text CAO could extract, which may be empty.
- `metadata` holds CAO's own detail: `provider`, `terminalId`, `sessionName`
  and the `options` the prompt offers.
- `subagentRunId` is `terminal:<id>`, the id the ambient stream
  (`GET`/`POST /agui/v1/stream`) gives that terminal's `SUBAGENT_STARTED`, so a
  client can tie the prompt to the invocation it already shows. It is absent,
  never null, when an interrupt has no terminal. `RUN_*` frames never carry it.
- `expiresAt` appears only when CAO knows a deadline, which today it does not.

`responseSchema` is built per interrupt. `editedArgs` appears only when that
interrupt offers `edit`, and it is the edited answer text typed into the
agent's prompt, not replacement tool arguments.

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
- Expired interrupt (terminal moved on) → an answer for it is skipped with a
  server-side warning, and a `status: "cancelled"` entry for it is skipped
  quietly. Either way the run proceeds and ends with `RUN_FINISHED`
  (`success`, or `interrupt` if others are pending). There is no `expired`
  outcome. A resolved record is evicted when a later interrupt is raised after
  the record is 5 minutes old, or sooner if the registry is over its cap. From
  then on the id is unknown: the entry is skipped with a server-side warning,
  the run proceeds, and anything still pending is reported again.
- Skipped entries are also reported in-band, on `RUN_FINISHED`:
  `"result": {"skippedResume": [{"interruptId": "<id>", "reason": "unknown" | "expired"}]}`.
  An evicted id is reported as `unknown`. Ids and reasons only, never prompt
  text. With nothing skipped there is no `result` key. `skippedResume` appears
  only on `RUN_FINISHED`; a run that exits with `RUN_ERROR` carries no list.
  A `cancelled` entry for
  an expired interrupt, and a re-sent answer for a resolved one, are not
  listed.
- Surface disabled → HTTP 404 (not 403)

## Replay Contract

Each connection to `GET`/`POST /agui/v1/stream` is a new AG-UI run. A cursor
(a CAO extension) only chooses which buffered records that run narrates first,
before its `STATE_SNAPSHOT`:
- `since` (`?since=` on GET, `forwardedProps.cao.since` on POST) must be valid
  ISO-8601 (datetime comparison, not lexicographic); malformed → HTTP 400
- `Last-Event-ID` (sent by a browser `EventSource` on its own) replays the
  records after that id
- Multi-frame records get derived ids (`<rid>.<i>`) on all but their last frame;
  a `<rid>.<i>` cursor replays that record whole
- `?framing=cao-legacy` serves the deprecated pre-1.0 wire, which
  `AguiStreamReader` requests

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
