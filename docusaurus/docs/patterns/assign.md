---
sidebar_position: 2
---

# Assign Pattern

The `assign` MCP tool provides asynchronous task delegation. A supervisor agent dispatches a task to a new worker agent and continues working without waiting for the result.

## MCP Tool Signature

```
assign(
  agent_profile: str,    # Required. Agent profile name
  message: str,          # Required. Task message
  working_directory?: str,  # Optional. Only when CAO_ENABLE_WORKING_DIRECTORY=true
  model?: str            # Optional. Override the agent's model
)
```

| Parameter | Required | Description |
|-----------|----------|-------------|
| `agent_profile` | Yes | The agent profile to launch for the worker |
| `message` | Yes | The task description sent to the new worker |
| `working_directory` | No | Set the working directory for the worker (requires `CAO_ENABLE_WORKING_DIRECTORY=true`) |
| `model` | No | Override the agent's model |

## Behavior

1. The supervisor agent calls `assign` via the `cao-mcp-server`
2. CAO creates a **new terminal** with the specified agent profile
3. The task message is sent to that terminal
4. The supervisor **continues immediately** without waiting
5. The worker runs asynchronously in the same session as the supervisor (as a separate tmux window)
6. The worker reports back via `send_message` — CAO appends an instruction to the
   task message telling the worker which terminal to reply to

## When to Use

- Independent tasks that can run in parallel
- Long-running operations where blocking the supervisor is undesirable
- Fan-out workflows (one supervisor, many workers)

## Example

A supervisor agent orchestrating a code review might call:

```json
{
  "tool": "assign",
  "arguments": {
    "agent_profile": "developer",
    "message": "Run integration tests for the auth module and report results"
  }
}
```

The supervisor continues its own work while the worker executes independently.
