# Terminal Lifecycle

## Overview

Each terminal created by CAO (via `assign` or `handoff`) occupies a tmux window
and a database record. In long-running sessions, terminals accumulate and can
exhaust system resources. CAO provides automatic and manual cleanup paths.

## Deletion paths

| How deleted | Snapshot saved? |
|-------------|----------------|
| Handoff completes successfully (auto-delete) | Yes |
| `delete_terminal` MCP tool | Yes |
| `DELETE /terminals/{id}` API | Yes |
| `cao shutdown --session <name>` | Yes |
| `cao shutdown --all` | Yes |
| Next `cao-server` start finds the session gone (crash, reboot, `tmux kill-server`) | Partly: metadata from creation, scrollback recovered from the log |

Individual deletion snapshots via `terminal_service.delete_terminal`.
Session-level shutdown (`delete_session`, which both `cao shutdown` modes reach
over `DELETE /sessions/{name}`) snapshots too: capturing each terminal's
scrollback is an explicit step of the teardown, and it deliberately runs
*before* the session kill, since scrollback only exists while the pane does.

A crash bypasses both paths, so no pane capture is taken. What survives it: the
`.snapshot.json` every terminal gets when it is created, and the `.log` the
output pipeline was writing. The next server start recovers a `.scrollback`
from that log when it finalizes the terminal (see
[Server restart](#server-restart)).

Capture is best-effort everywhere, not just at session level: a snapshot whose
write fails is logged and teardown continues regardless of which path took it.
So "Yes" above means the path attempts a snapshot, not that one is guaranteed.

## Snapshot files

Two files in `~/.cao/logs/terminal/` hold what a terminal leaves behind:

- `<terminal_id>.snapshot.json` — metadata for restore. Written when the
  terminal is created, so a crash still leaves it behind, and refreshed on
  deletion with the pane's live working directory. It is created owner-only
  (`0600`).
- `<terminal_id>.scrollback` — plain-text capture of the full pane scrollback,
  written on deletion. For a terminal finalized at server start it is recovered
  from `<terminal_id>.log` instead (see [Server restart](#server-restart)).

Snapshot JSON schema:

```json
{
  "terminal_id": "...",
  "session_name": "...",
  "window_name": "...",
  "agent_profile": "...",
  "provider": "...",
  "working_directory": "...",
  "allowed_tools": null,
  "caller_id": null
}
```

All three file types (`.log`, `.scrollback`, `.snapshot.json`) are purged after
`RETENTION_DAYS` (default: 7) by the cleanup service.

## Server restart

Agents run in tmux, so they outlive `cao-server`. But the output pipeline that
writes `<terminal_id>.log` and feeds status detection is armed by the server
that created the terminal, and dies with it. On startup, `cao-server` goes
through the terminals in its registry and settles each one. The rows are read
before the server starts serving requests, so a terminal created after the
restart is never touched, and the pass runs in a background thread. The herdr
backend is skipped: it delivers output over its own socket, with no pipeline to
re-arm.

| What tmux says | What happens |
|----------------|--------------|
| Session alive, window readable | **Re-adopted.** A fresh FIFO reader is started and `pipe-pane` is stopped and restarted into it, so `<terminal_id>.log` and status detection resume. No key is sent to the pane. |
| Session confirmed gone | **Finalized.** A `.scrollback` is recovered from `<terminal_id>.log` if none exists, then the terminal is torn down like any other: its runtime state (including provider cleanup) and then its registry row. A provider cleanup that has to be deferred keeps the row for a retry. |
| Could not tell (the tmux lookup failed, or the window cannot be read) | **Left untouched.** The next start re-evaluates it, and retention cleanup still applies. |

Things to know:

- **Status of a quiet agent.** An agent that is working reports its status as
  soon as it prints something. One that is quiet (idle, or waiting for an
  answer) prints nothing, so its status reads `unknown` until the pipe-pane
  liveness watchdog's cold-start check replays the pane's current content into
  the pipeline: after `CAO_PIPE_LIVENESS_COLD_START_GRACE_S` (default 3 s),
  checked every `CAO_PIPE_LIVENESS_CHECK_INTERVAL_S` (default 4 s; both in
  [Configuration](configuration.md#not-yet-routed-through-configservice)). The
  watchdog logs this as a cold-start re-arm. Nothing is typed into the pane to
  make it repaint: on a permission prompt, a keystroke would answer it.
- **The recovered scrollback is partial.** `<terminal_id>.log` stops growing
  when the server dies, so anything the agent printed after that is missing.
- **A dead window in a live session is not finalized.** There is no strict
  window-level existence check, and a window can be renamed while its agent
  keeps running, so that case counts as "could not tell". Delete the terminal
  explicitly, or let retention cleanup collect it.
- **Worktrees are kept.** Finalizing never removes a terminal's git worktree.

The startup log reports the outcome, for example
`Terminal re-adoption: 3 re-adopted, 1 finalized, 0 left untouched`.

## Restore

```bash
cao terminal restore <terminal_id>
```

This creates a **plain shell window** in the original session at the original
working directory, replaying the saved scrollback via `cat ... ; exec $SHELL -l`.

Constraints:

- The original session must still exist. If the session was shut down, restore
  will fail. You can still read the scrollback directly:
  `cat ~/.cao/logs/terminal/<terminal_id>.scrollback`
- Restore creates a shell window, not a re-launched agent. The window shows
  the old output but is not connected to any provider.

## Assign vs handoff cleanup

- **Handoff** terminals are deleted automatically on success. No action needed.
- **Assign** terminals are not auto-deleted. Call `delete_terminal(terminal_id)`
  when you no longer need the terminal, or wait for the 10-terminal nudge.

## Terminal count nudge

When a session reaches 10 terminals, `assign` and `handoff` responses include:

> NOTE: This session has N terminals. Consider calling delete_terminal on
> terminals you no longer need.
