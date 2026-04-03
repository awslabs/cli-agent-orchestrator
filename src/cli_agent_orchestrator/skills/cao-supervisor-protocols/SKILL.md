---
name: cao-supervisor-protocols
description: Supervisor-side orchestration patterns for assign, handoff, and idle inbox delivery in CAO
---

# CAO Supervisor Protocols

Use this skill when supervising worker agents through CLI Agent Orchestrator.

## Core MCP Tools

From `cao-mcp-server`, supervisors orchestrate work with:

- `assign(agent_profile, message)` for asynchronous work that returns immediately
- `handoff(agent_profile, message)` for synchronous work that blocks until the worker finishes
- `send_message(receiver_id, message)` for direct messages to an existing terminal

## Choosing Between Assign and Handoff

Use `assign` when the worker should continue independently and report back later. This is the normal pattern for fan-out work or parallel execution.

Use `handoff` when the next step is blocked on the worker result. The orchestrator waits for completion, captures the worker output, and returns it directly to the supervisor.

## Idle-Based Message Delivery

Assigned workers usually return results through `send_message`. Those inbox messages are delivered to the supervisor automatically when the supervisor terminal becomes idle.

This means supervisors should:

- Dispatch all planned worker tasks first
- Finish the turn after dispatching work
- Avoid running placeholder shell commands just to wait

Do not keep the terminal busy with `sleep`, `echo`, or similar commands while waiting. A busy terminal delays inbox delivery.

## Callback Pattern

When you use `assign`, include the callback terminal ID in the task message. Tell the worker exactly which terminal should receive the result and instruct the worker to use `send_message`.

Example pattern:

```text
Analyze dataset A. Send results back to terminal abc123 using send_message.
```

## Practical Workflow

1. Read or determine your terminal ID.
2. Dispatch asynchronous workers with `assign` and include callback instructions.
3. Use `handoff` only for steps that must finish before you can continue.
4. End the turn so asynchronous worker messages can be delivered.
5. When messages arrive, synthesize the results and continue the workflow.
