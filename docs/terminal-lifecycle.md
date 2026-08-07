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
| Process crash | No |

Individual deletion snapshots via `terminal_service.delete_terminal`.
Session-level shutdown (`delete_session`, which both `cao shutdown` modes reach
over `DELETE /sessions/{name}`) snapshots too: capturing each terminal's
scrollback is an explicit step of the teardown, and it deliberately runs
*before* the session kill, since scrollback only exists while the pane does. A
crash bypasses both paths, so nothing is captured.

Capture is best-effort everywhere, not just at session level: a snapshot whose
write fails is logged and teardown continues regardless of which path took it.
So "Yes" above means the path attempts a snapshot, not that one is guaranteed.

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
