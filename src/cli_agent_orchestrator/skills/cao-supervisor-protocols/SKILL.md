---
name: cao-supervisor-protocols
description: Supervisor-side orchestration patterns for assign, handoff, and idle inbox delivery in CAO
---

# CAO Supervisor Protocols

Use this skill when supervising worker agents through CLI Agent Orchestrator.

This skill covers how supervisors should dispatch work, decide between `assign` and `handoff`, and receive worker results without blocking inbox delivery.

## Core MCP Tools

From `cao-mcp-server`, supervisors orchestrate work with:

- `assign(agent_profile, message)` for asynchronous work that returns immediately
- `handoff(agent_profile, message)` for synchronous work that blocks until the worker finishes
- `send_message(receiver_id, message)` for direct messages to an existing terminal

Your own terminal ID is available in the `CAO_TERMINAL_ID` environment variable. Use it when you need workers to send results back to you.

## Choosing Between Assign and Handoff

Use `assign` when the worker should continue independently and report back later. This is the normal pattern for fan-out work or parallel execution.

Use `handoff` when the next step is blocked on the worker result. The orchestrator waits for completion, captures the worker output, and returns it directly to the supervisor.

Typical pattern:

- Use `assign` for analysis, research, or code changes that can run in parallel.
- Use `handoff` for report generation, blocking review steps, or any task where you need the result before you can continue.

## Idle-Based Message Delivery

Assigned workers usually return results through `send_message`. Those inbox messages are delivered to the supervisor automatically when the supervisor terminal becomes idle.

This means supervisors should:

- Dispatch all planned worker tasks first
- Finish the turn after dispatching work
- Avoid running placeholder shell commands just to wait

Do not keep the terminal busy with `sleep`, `echo`, or similar commands while waiting. A busy terminal delays inbox delivery.

If you need multiple worker results, dispatch them all first, then end the turn. Do not poll manually in a loop.

## Callback Pattern

When you use `assign`, include the callback terminal ID in the task message. Tell the worker exactly which terminal should receive the result and instruct the worker to use `send_message`.

Example pattern:

```text
Analyze dataset A. Send results back to terminal abc123 using send_message.
```

Some CAO deployments also append an automatic callback suffix to assigned messages. Treat that appended context as helpful reinforcement, but still write task messages that are explicit and self-contained.

## Direct Supervisor Communication

Use `send_message` when you need to contact an existing terminal directly rather than spawning a new worker.

Examples:

- Relay follow-up instructions to a worker you already created.
- Forward a worker result to another coordinator terminal.
- Send a concise status update to a collaborating supervisor.

When sending direct messages, include enough context that the receiver can act without re-reading the full original task.

## Practical Workflow

1. Read or determine your terminal ID.
2. Dispatch asynchronous workers with `assign` and include callback instructions.
3. Use `handoff` only for steps that must finish before you can continue.
4. End the turn so asynchronous worker messages can be delivered.
5. When messages arrive, synthesize the results and continue the workflow.

## Reliability Guidelines

- Tell workers exactly what deliverable they should return.
- When workers create files, ask them to return absolute paths in their callback message.
- Do not assume results will be delivered while your terminal is still busy.
- Keep orchestration instructions separate from domain requirements so workers can parse both cleanly.
