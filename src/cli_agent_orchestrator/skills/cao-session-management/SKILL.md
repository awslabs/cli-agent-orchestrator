---
name: cao-session-management
description: Interact with CAO (CLI Agent Orchestrator) — launch multi-agent sessions,
  check status, send follow-up instructions, unblock stuck terminals, or shut down
  sessions. Use when working with CAO sessions in any capacity.
---

# CAO Session Management

## Overview

CAO runs multi-agent workflows in named sessions. A conductor agent inside each
session orchestrates the work.

## Core Concepts

- **Session**: A group of agent terminals working together
- **Conductor**: The supervisor terminal — receives instructions, delegates to workers
- **Provider**: LLM backend. Default `kiro_cli`, override with `--provider`

## Launching a Session

Every `cao launch` MUST include:

- `--agents PROFILE` — for `kiro_cli` provider, run `kiro-cli agent list` to discover
  profiles; otherwise ask the user
- `--headless` — required from an LLM agent; without it cao tries to attach tmux
- `--session-name NAME` — cao adds `cao-` prefix automatically
- `--working-directory DIR` — a wrong path silently breaks the session with no
  recovery short of shutdown and relaunch. Ask the user if unclear. Always wrap
  in single quotes to pass the literal path to the server (prevents local shell
  expansion of `~` or variables before the value reaches cao).

```bash
cao launch --agents <profile> --headless --yolo \
  --session-name <name> --working-directory '<path>' "<task>"
```

`--yolo` skips confirmation prompts. Required when launching from an agent — interactive
prompts will stall the session.

For SOP-driven workflows (Kiro provider): launch with `/prompts` to discover
available SOPs, then send the matched SOP name prefixed with `@` (e.g.,
`@my-sop-name`), then send the task — each as separate messages after
polling for `completed` status.

## Commands

| Command | Description |
|---------|-------------|
| `cao session list` | List active sessions |
| `cao session status SESSION` | Conductor status and last response |
| `cao session status SESSION --workers` | Include worker terminals |
| `cao session status SESSION --terminal ID` | Drill into a specific terminal |
| `cao session status SESSION --json` | Machine-readable output; use to extract terminal IDs |
| `cao session send SESSION "msg"` | Send and wait until completion (sync) |
| `cao session send SESSION "msg" --timeout N` | Send and wait up to N seconds |
| `cao session send SESSION "msg" --async` | Fire-and-forget without waiting |
| `cao session send SESSION "msg" --terminal ID` | Send to a specific terminal |
| `cao shutdown --session SESSION` | Shut down a session |
| `cao shutdown --all` | Shut down all sessions |

> `cao session send` waits for completion and returns output inline by default. With `--async`, it sends and returns immediately without waiting. With `--timeout N`, it waits up to N seconds — if the timeout expires, the agent is still running; check status later.
> Session names in commands use the `cao-` prefixed form (e.g. `--session-name mywork` → use `cao-mywork`).

## Worker Communication

Inside a session, the conductor talks to workers via two MCP tools:

**Prefer communicating through the conductor** (`cao session send SESSION "msg"`) rather
than directly to worker terminals. Bypassing the conductor leaves it without state on
what was asked or answered, which causes confusion. Two exceptions: unblocking a stuck
worker, and follow-up questions to a persistent async worker (see below).

**handoff** (blocking) — conductor sends task and waits for the worker to reach
`COMPLETED` status, then reads the output. If it times out, the worker is still
running — the conductor just stopped waiting.

**assign** (non-blocking) — conductor sends task and returns immediately. The worker
is expected to call `send_message` back to the conductor's terminal ID when done.
Each terminal only knows its own ID via `$CAO_TERMINAL_ID`.

By default the conductor uses sync (handoff). You can override this by explicitly
asking it to use async or sync protocol when sending it a task.

Async workers (assign) stay alive after completing their task and can answer follow-up
questions — useful for ongoing investigation where you want to keep querying the same
worker. In this case, sending directly to the worker terminal is appropriate:
```bash
cao session send SESSION "<follow-up question>" --terminal <worker-terminal-id>
```

## Common Mistakes

**Wrong working directory** — agents won't find files, builds fail with confusing errors.

**Stuck conductor** — conductor is waiting on a worker that stopped responding. Check
the worker's status first, then decide: prompt it to continue and send results back,
or ask it to resend if it already finished. Never re-delegate work that may still be
running — it risks duplicate work.
```bash
cao session status SESSION --workers
cao session status SESSION --terminal <worker-terminal-id>
cao session send SESSION "continue your work, then send results to terminal <conductor-terminal-id>" --terminal <worker-terminal-id>
```
Get the conductor's terminal ID from `cao session status SESSION --json`.
