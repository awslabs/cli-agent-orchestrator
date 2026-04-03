---
name: cao-worker-protocols
description: Worker-side callback and completion rules for assigned and handed-off tasks in CAO
---

# CAO Worker Protocols

Use this skill when acting as a worker agent inside CLI Agent Orchestrator.

## Understand the Dispatch Mode

Workers receive tasks through one of two orchestration modes:

- `handoff`: blocking work where the orchestrator captures your final output automatically
- `assign`: non-blocking work where you must actively return results to the requesting terminal

## Rules for Handoff Tasks

When the task came through `handoff`, complete the task and present the result in your normal response. Do not call `send_message` unless the task explicitly asks for additional side-channel communication.

## Rules for Assigned Tasks

When the task came through `assign`, the task message should include a callback terminal ID. After you finish the work:

1. Extract the callback terminal ID from the task message.
2. Format the result clearly and concisely.
3. Call `send_message(receiver_id=..., message=...)` with the completed result.

Do not stop after writing a normal response if the assignment explicitly requires a callback. The requesting terminal depends on `send_message` to receive the result.

## Message Formatting

Return results that are easy for the supervisor to merge into a larger workflow:

- Identify what task or dataset the result belongs to
- Include the requested output or deliverable
- Keep the message specific enough to act on without re-reading the whole task

## Filesystem and Reporting Discipline

If the task asks you to create files, write them before reporting completion. When sending results back to a supervisor, include absolute file paths so the supervisor can continue the workflow without ambiguity.
