# Terminal Lifecycle Management

CAO automatically manages the lifecycle of tmux windows and terminal records to provide a clean user experience while preserving debugging capabilities.

## Auto-Close on Exit

When a CLI agent process (kiro-cli, claude-code, etc.) exits or crashes, the tmux window automatically closes. This is achieved through tmux's `remain-on-exit off` option, which is configured automatically when creating terminals.

### Benefits

- **Clean tmux sessions** - No lingering windows after agent processes exit
- **Reduced clutter** - Only active agents remain visible in tmux
- **Better UX** - Users don't need to manually close windows

## Automatic Cleanup

When a tmux window closes (process exits), a tmux `pane-exited` hook automatically attempts to delete the terminal record from the database via the CAO API.

### Hook Design

The cleanup hook is designed to never disrupt tmux operations:

```bash
run-shell -b 'curl -sf --max-time 2 -X DELETE "http://localhost:9889/terminals/{terminal_id}" >/dev/null 2>&1 || true'
```

Key safety features:
- **Background execution** (`-b`) - Doesn't block tmux
- **Silent mode** (`-sf`) - No output on success
- **Hard timeout** (`--max-time 2`) - Fails fast if API unavailable
- **Output redirection** (`>/dev/null 2>&1`) - No tmux display messages
- **Always succeeds** (`|| true`) - Never causes tmux errors

### Failure Handling

If the cleanup hook fails (API down, network issue, etc.):
- The tmux window still closes normally
- The terminal record becomes orphaned in the database
- The retention-based cleanup service removes it after `RETENTION_DAYS` (default: 14 days)

## Log Preservation

Terminal logs are **NOT** deleted when the terminal record is removed. This ensures debugging capabilities are preserved:

- Logs remain at `~/.cao/logs/terminal/{terminal_id}.log`
- Logs are cleaned up by the retention service after `RETENTION_DAYS`
- You can inspect logs even after the agent process exits

## Retention-Based Cleanup

CAO runs a periodic cleanup service that removes old data:

- **Terminal records** - Deleted after `RETENTION_DAYS` of inactivity
- **Inbox messages** - Deleted after `RETENTION_DAYS`
- **Terminal logs** - Deleted after `RETENTION_DAYS`
- **Server logs** - Deleted after `RETENTION_DAYS`

Default retention period: 14 days (configurable in `constants.py`)

## Manual Cleanup

You can manually delete terminals and their records:

```bash
# Delete specific terminal (stops logging, removes DB record, keeps log file)
curl -X DELETE http://localhost:9889/terminals/\{terminal_id\}

# Shutdown entire session (kills tmux session, removes all terminal records)
cao shutdown --session cao-my-session

# Shutdown all CAO sessions
cao shutdown --all
```

## Architecture

```
Process Exit → tmux window closes (remain-on-exit off)
            ↓
            tmux pane-exited hook fires
            ↓
            curl DELETE /terminals/{id} (best-effort)
            ↓
            ┌─ Success: DB record deleted immediately
            └─ Failure: DB record cleaned up after RETENTION_DAYS
            
Log files preserved in both cases until RETENTION_DAYS
```

## Configuration

The lifecycle behavior is configured automatically when creating terminals. No user configuration is required.

To modify retention period, edit `RETENTION_DAYS` in `src/cli_agent_orchestrator/constants.py`.

## Troubleshooting

### Windows not closing automatically

Check if `remain-on-exit` is set correctly:

```bash
tmux show-options -t cao-session:window-name remain-on-exit
# Should show: remain-on-exit off
```

### Orphaned terminal records

If you see terminal records for closed windows:

```bash
# List all terminals
curl http://localhost:9889/terminals

# Manually delete orphaned terminal
curl -X DELETE http://localhost:9889/terminals/\{terminal_id\}
```

### Hook failures

Check server logs for hook execution errors:

```bash
tail -f ~/.cao/logs/server.log | grep DELETE
```

Common causes:
- CAO API server not running
- Network connectivity issues
- API server restarting during hook execution

These failures are expected and handled gracefully - the retention service will clean up orphaned records.
