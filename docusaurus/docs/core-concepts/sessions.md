---
sidebar_position: 2
---

# Sessions

Sessions are the fundamental runtime unit in CAO. Each session is a **tmux session** containing one or more terminal windows, where each window runs an isolated AI agent process.

## Structure

```
tmux session: cao-my-project
├── window 0: supervisor  (code_supervisor profile, kiro_cli provider)
├── window 1: worker-a    (developer profile, claude_code provider)
└── window 2: worker-b    (reviewer profile, codex provider)
```

- A **session** groups related agent terminals under one tmux session (prefixed with `cao-`).
- Each **terminal** (tmux window) has a unique 8-character hex `CAO_TERMINAL_ID` set as an environment variable.
- The server uses `CAO_TERMINAL_ID` to route messages, track status, and coordinate orchestration.

## Session Lifecycle

```
cao launch ──▶ Session Created ──▶ Active (agents running)
                                        │
                            ┌────────────┼────────────┐
                            ▼            ▼            ▼
                       Workers      Workers      Supervisor
                       spawned      complete     completes
                            │            │            │
                            └────────────┼────────────┘
                                         ▼
                              cao shutdown ──▶ Terminated
```

1. **Created** -- `cao launch --agents <profile>` spawns a tmux session and initializes the supervisor terminal.
2. **Active** -- the supervisor is running, can spawn workers via `handoff`/`assign`, and processes tasks.
3. **Terminated** -- `cao shutdown --session cao-<name>` kills the tmux session and cleans up resources.

## Terminal Status Detection

Each terminal's status is detected by the **StatusMonitor**, which analyzes output patterns from the running CLI provider. Status values:

| Status | Meaning |
|--------|---------|
| `IDLE` | Agent is waiting for input (shows prompt) |
| `PROCESSING` | Agent is generating a response (spinner visible) |
| `WAITING_USER_ANSWER` | Agent is showing a permission/selection prompt |
| `COMPLETED` | Agent finished its response and returned to prompt |
| `ERROR` | Agent encountered an error or produced unrecognizable output |
| `UNKNOWN` | Status cannot be determined (e.g., terminal just created or output not yet captured) |

Status detection is **provider-specific** -- each provider implements pattern matching for its CLI's output format. The StatusMonitor maintains a rolling 8 KB buffer of terminal output and re-evaluates status at quiescence (after output stops) and on rising edges (when output resumes after quiet), using debounced edge detection to avoid false transitions during TUI redraws.

### Sticky status

Once a terminal reaches a "ready" state (IDLE, COMPLETED, WAITING_USER_ANSWER, ERROR), the status is **latched** -- it will not regress to PROCESSING until new input is explicitly sent. This prevents false transitions caused by TUI redraws.

## Session Management Commands

```bash
# List active sessions
cao session list

# Show conductor status and last output
cao session status cao-<session-name>

# Include worker terminal statuses
cao session status cao-<session-name> --workers

# Send a message and wait for completion
cao session send cao-<session-name> "Implement the login page"

# Fire-and-forget (don't wait)
cao session send cao-<session-name> "Run the tests" --async

# Wait with timeout
cao session send cao-<session-name> "Deploy to staging" --timeout 120

# Launch a new session
cao launch --agents code_supervisor

# Launch headless (no tmux attach, returns when done)
cao launch --agents code_supervisor --headless --yolo \
  --session-name my-task "Build the authentication module"

# Shut down a specific session
cao shutdown --session cao-my-task

# Shut down all CAO sessions
cao shutdown --all
```

## Roles

Terminals are assigned roles through their agent profile's `role` field:

| Role | Purpose | Default Tool Access |
|------|---------|---------------------|
| **supervisor** | Coordinates other agents, delegates work | `@cao-mcp-server`, `fs_read`, `fs_list` |
| **developer** | Implements tasks, writes code | `@builtin`, `fs_*`, `execute_bash`, `web_fetch`, `@cao-mcp-server` |
| **reviewer** | Reviews code, provides feedback | `@builtin`, `fs_read`, `fs_list`, `@cao-mcp-server` |

- `@cao-mcp-server` bundles all orchestration tools (`handoff`, `assign`, `send_message`, `delete_terminal`, `memory_store`, `memory_recall`, `memory_forget`, `load_skill`, `workflow_run`, `workflow_resume`, `workflow_return`, `emit_ui`, `find_profiles`, `workflow_cancel`, etc.)
- `@builtin` maps to all provider-native tools (the tools the underlying CLI agent exposes natively).

Custom roles can be defined in `settings.json` under `agents.roles`.

## Attaching to Sessions

Because sessions are standard tmux, you can attach at any time:

```bash
# List all CAO sessions
tmux list-sessions | grep cao-

# Attach to watch agents in real time
tmux attach -t cao-my-project

# Switch between windows inside tmux
# Ctrl-b w  (window selector)
# Ctrl-b n  (next window)
# Ctrl-b p  (previous window)
```

This enables **human-in-the-loop** workflows -- you can type directly into a worker's terminal to steer it, answer permission prompts, or provide additional context.

## Terminal Restoration

When a `handoff` completes successfully, the worker terminal is automatically deleted. However, its scrollback and metadata are saved to `~/.aws/cli-agent-orchestrator/logs/terminal/` so you can restore it for debugging:

```bash
cao terminal restore <terminal_id>
```

This recreates the window in the original session with the saved scrollback content.
