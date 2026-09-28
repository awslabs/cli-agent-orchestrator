---
sidebar_position: 3
---

# Send Message Pattern

The `send_message` MCP tool enables direct inter-agent communication. It sends a message to an already-running agent session without creating a new terminal.

## MCP Tool Signature

```
send_message(
  message: str,                # Required. Message content
  receiver_id?: str = None     # Optional. Target terminal ID (8-char hex). When omitted, routes to the caller's parent.
)
```

| Parameter | Required | Description |
|-----------|----------|-------------|
| `message` | Yes | The message content to deliver |
| `receiver_id` | No | Target terminal ID (8-char hex). When omitted, routes to the caller's parent. |

## Behavior

1. The sending agent calls `send_message` via the `cao-mcp-server`
2. The message is delivered to the target agent's inbox
3. **No new terminal is created** — the target must already be running
4. The target agent receives the message in its context on its next inbox check
5. This is non-blocking for the sender

## When to Use

- Coordination between peer agents that are already running
- Providing context or status updates without delegating new work
- Real-time communication in multi-agent workflows
- Reporting results back to a supervisor

## Human CLI Equivalent

From a terminal, humans can send messages to running sessions using:

```bash
cao session send <session_name> "<message>"
```

## Example

A worker agent reporting results back to its supervisor:

```json
{
  "tool": "send_message",
  "arguments": {
    "message": "Integration tests passed. 42 tests run, 0 failures.",
    "receiver_id": "a1b2c3d4"
  }
}
```

When `receiver_id` is omitted, the message automatically routes to the terminal that created this worker (via `handoff` or `assign`):

```json
{
  "tool": "send_message",
  "arguments": {
    "message": "Task complete. Auth module implemented with OAuth2 support."
  }
}
```
