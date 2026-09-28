---
sidebar_position: 1
---

# Handoff Pattern

The `handoff` MCP tool provides synchronous task delegation. The calling agent blocks until the delegated agent completes and returns its output.

## MCP Tool Signature

```
handoff(
  agent_profile: str,    # Required. Agent profile name
  message: str,          # Required. Task message
  timeout: int = 600,    # Optional. Seconds to wait (1-3600)
  working_directory?: str,  # Optional. Only when CAO_ENABLE_WORKING_DIRECTORY=true
  model?: str            # Optional. Override the agent's model
)
```

| Parameter | Required | Description |
|-----------|----------|-------------|
| `agent_profile` | Yes | The agent profile to launch for the delegate |
| `message` | Yes | The task description sent to the delegate |
| `timeout` | No | Seconds to wait for completion (1-3600, default 600) |
| `working_directory` | No | Set the working directory for the delegate (requires `CAO_ENABLE_WORKING_DIRECTORY=true`) |
| `model` | No | Override the agent's model |

## Behavior

1. The calling agent invokes `handoff` via the `cao-mcp-server`
2. CAO creates a **new terminal** with the specified agent profile
3. The task message is sent to that terminal
4. The calling agent **blocks** until the delegate completes
5. The delegate's output is returned to the calling agent
6. The calling agent continues execution with the result

## When to Use

- Tasks with sequential dependencies
- When the calling agent needs the result before proceeding
- Code review or refactoring workflows where output feeds the next step

## Example

An agent that needs a module refactored before continuing:

```json
{
  "tool": "handoff",
  "arguments": {
    "agent_profile": "developer",
    "message": "Refactor the database module to use connection pooling. Return the list of changed files."
  }
}
```

The calling agent waits for the refactoring to finish and receives the result before moving on.

:::note `working_directory` is off by default
The `working_directory` parameter is only registered on the tool when
`CAO_ENABLE_WORKING_DIRECTORY=true` is set for the MCP server. With the default
configuration it is absent from the tool schema, so passing it is a validation
error rather than an ignored argument.
:::
