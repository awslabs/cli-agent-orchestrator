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
- `send_message(message, receiver_id=None)` for direct messages — `receiver_id` defaults to the terminal that created yours via handoff/assign
- `answer_user_prompt(terminal_id, answer)` for answering a Hermes worker that reports `waiting_user_answer`

Your own terminal ID is available in the `CAO_TERMINAL_ID` environment variable. CAO appends it to assigned task messages and records it on worker terminals automatically, so you rarely need to handle it yourself.

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

By default, CAO appends your terminal ID and callback instructions to every assigned message automatically, and records your terminal as the worker's caller — workers can reply with `send_message` without a `receiver_id`. You do not need to hand-write callback instructions.

You may still include an explicit callback ID in the task message for emphasis:

```text
Analyze dataset A. Send results back to terminal abc123 using send_message.
```

If your deployment disables the automatic suffix (`CAO_ENABLE_SENDER_ID_INJECTION=false`), the explicit pattern above is required: the structural caller record still works, but the worker gets no in-message reminder.

## Direct Supervisor Communication

Use `send_message` when you need to contact an existing terminal directly rather than spawning a new worker.

Examples:

- Relay follow-up instructions to a worker you already created.
- Forward a worker result to another coordinator terminal.
- Send a concise status update to a collaborating supervisor.

When sending direct messages, include enough context that the receiver can act without re-reading the full original task.

## Interactive Worker Prompts

Hermes workers can stop on approval prompts or clarify pickers and report `waiting_user_answer`. When a Hermes worker is in that state, do not use `assign`, `handoff`, or `send_message` to answer it. Use `answer_user_prompt(terminal_id, answer)` with the exact selection or text to submit, such as `1`, `o`, or a custom answer.

Other providers may still emit prompts in their terminal output without reporting `waiting_user_answer`. For those providers, treat the prompt as ordinary terminal output and answer it with `send_message` or direct input according to the workflow you are running.

## Practical Workflow

1. Dispatch asynchronous workers with `assign` — callback routing is automatic (your terminal ID is appended to the message and recorded as the worker's caller).
2. Use `handoff` only for steps that must finish before you can continue.
3. End the turn so asynchronous worker messages can be delivered.
4. When messages arrive, synthesize the results and continue the workflow.

## Reliability Guidelines

- Tell workers exactly what deliverable they should return.
- When workers create files, ask them to return absolute paths in their callback message.
- Do not assume results will be delivered while your terminal is still busy.
- Keep orchestration instructions separate from domain requirements so workers can parse both cleanly.
