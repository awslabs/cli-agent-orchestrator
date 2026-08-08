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
| `cao session stop` / `POST /sessions/{name}/lifecycle/stop` (lifecycle stop) | Yes |
| `cao session lifecycle` archive (`POST .../lifecycle/archived`) | Yes |
| `DELETE /sessions/{name}` (destructive session deletion) | No |
| `cao shutdown --session <name>` | No |
| `cao shutdown --all` | No |
| Process crash | No |

Snapshots are only saved when a terminal is torn down individually via
`terminal_service.delete_terminal`. Session-level shutdown and destructive
deletion (`delete_session`, `cao shutdown`) kill windows directly and do not
snapshot. **Lifecycle stop and archive are different:** they collect each pane
through that same snapshotting `delete_terminal` path, so every pane is
snapshotted even though the operation is session-level. They are not a
deletion — the lifecycle row, the forwarded environment, and the restore
target are all preserved for a future resume; only the destructive
`DELETE /sessions/{name}` (or shutdown) releases the name and discards them. If
you want scrollback preserved, prefer lifecycle stop/archive over shutdown, or
delete terminals individually before shutting down the session.

## Snapshot files

On deletion, two files are written to `~/.cao/logs/terminal/`:

- `<terminal_id>.scrollback` — plain-text capture of the full pane scrollback
- `<terminal_id>.snapshot.json` — metadata for restore

Snapshot JSON schema:

```json
{
  "terminal_id": "...",
  "session_name": "...",
  "window_name": "...",
  "agent_profile": "...",
  "provider": "...",
  "working_directory": "...",
  "allowed_tools": null
}
```

All three file types (`.log`, `.scrollback`, `.snapshot.json`) are purged after
`RETENTION_DAYS` (default: 7) by the cleanup service.

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
