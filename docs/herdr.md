# herdr Backend

Using the Herdr desktop app? See [Herdr app backend](herdr-app.md).

CAO's default backend is [tmux](tmux.md). For agentic workloads, CAO also supports [herdr](https://herdr.dev/) -- a terminal-native agent runtime and multiplexer designed specifically for AI coding agents.

The key difference: tmux has no concept of "agent state," so CAO must poll terminal output and match regex patterns to detect when an agent is idle. herdr exposes a Unix socket API that emits real-time status events (`working`, `idle`, `done`, `blocked`). This eliminates polling entirely and enables instant inbox delivery.

## tmux vs herdr

| | tmux | herdr |
|---|---|---|
| Status detection | Poll `capture-pane` output + regex matching | Native socket events (`idle`, `done`, `blocked`) |
| Inbox delivery | Watchdog polls every 5s, delivers on idle pattern | Event-driven: delivers immediately on status change |
| Session identity | Inferred from pane content | Tracked natively per pane |
| Attach to agents | `tmux attach -t <session>` | `herdr attach` or herdr TUI |
| Maturity | Stable, well-tested default | Experimental |

Choose herdr if you want lower-latency inter-agent messaging and native agent lifecycle tracking. Choose tmux if you want the stable, battle-tested default.

## Prerequisites

1. **herdr installed** and available on `$PATH`. See [herdr.dev](https://herdr.dev/) for installation instructions.
2. **herdr server** for the configured session. CAO starts it automatically if
   the session socket is absent, so pre-starting is optional. Set
   `terminal.herdr_auto_start` to `false` in `settings.json` when systemd manages
   herdr; CAO then refuses a
   missing socket instead of starting its own server. To start (or attach to) it
   yourself:

```bash
herdr                        # default session name
herdr --session cao          # explicit session name (recommended)
```

## Configuration

Set `terminal.backend` in `~/.aws/cli-agent-orchestrator/settings.json`:

```json
{
  "terminal": {
    "backend": "herdr"
  }
}
```

Optionally specify the herdr session name (defaults to `"cao"`):

```json
{
  "terminal": {
    "backend": "herdr",
    "herdr_session": "my-session"
  }
}
```

See [configuration.md](configuration.md#terminal-backend-terminal) for the full schema, the `CAO_TERMINAL_BACKEND` / `CAO_HERDR_SESSION` env vars, and the `cao config` CLI.

## Launching

With `terminal.backend` set in `settings.json`, start the server the same way as
with tmux -- CAO detects the backend and connects to herdr automatically:

```bash
cao-server
```

To select herdr without editing `settings.json`, pass `--terminal`:

```bash
cao-server --terminal herdr
```

The `--terminal` flag (`tmux` or `herdr`) overrides `terminal.backend` from
`settings.json` for that run. The `herdr_session` name, if set, is still read from
`settings.json`.

## Viewing and Attaching

```bash
# List active CAO sessions
cao session list

# Attach to the herdr TUI (shows all workspaces and tabs)
herdr --session cao

# Attach to a specific CAO session
cao session attach <session-name>
```

## How It Works

CAO maps its concepts to herdr primitives:

| CAO concept | herdr primitive |
|---|---|
| Session (e.g. `cao-my-task`) | Workspace (labeled with session name) |
| Terminal / window (e.g. `conductor-a1b2`) | Tab within workspace (labeled with window name) |

### Event-driven inbox delivery

`HerdrInboxService` connects to the herdr Unix socket at startup and subscribes, in one `events.subscribe` call, to `pane.agent_status_changed` for each managed pane (validated against a live snapshot) plus a broadcast `pane.updated` for older herdr versions. When a pane transitions to `idle` or `done`, pending inbox messages are delivered immediately. herdr permits one `events.subscribe` per connection, so a pane registered later is picked up by an intentional reconnect; the subscribe response is read, and a rejected subscription fails the connection attempt loudly instead of silently degrading delivery to the 30s reconciliation sweep. Delivery log lines carry the trigger (`trigger: herdr-event`, `herdr-settle`, `settled-sweep`, `reconcile-sweep`, `opencode-poll`), so a dead event path is visible in the logs.

> Note on `pane.updated`: as of herdr 0.9.x it is still emitted for other pane changes (e.g. agent name/title), but no longer on agent-status transitions, which is why the per-pane `pane.agent_status_changed` subscription is the one that carries delivery. The broadcast subscription is kept only for herdr ≤0.8, where it did carry status.

herdr's state is trusted for status, with one exception. herdr can report `idle` or `done` while a Claude Code dialog (AskUserQuestion, a permission prompt, plan approval) is open, and a message typed there is swallowed while its Enter answers the dialog. So before each orchestrated delivery to a provider that blocks input at dialogs, CAO reads the pane once (`herdr pane read`, the last 50 lines as text) and holds the message as PENDING if the provider sees a dialog (issue #896). See [Inbox Delivery](inbox-delivery.md#standard-delivery).

### Startup and reconnect behavior

On server start (or reconnect after socket disconnect):

1. **Startup cleanup** -- cross-checks all DB terminal records against live herdr tabs. Removes ghost records left by prior server runs that exited uncleanly.
2. **Reconnect reconcile** -- prunes stale pane subscriptions from the in-memory map and re-subscribes only to panes that are still alive.
3. **Lifecycle events** -- subscribes to `pane.closed` and `workspace.closed` events for real-time cleanup when agents exit or sessions end.

The socket connection uses exponential backoff (1s to 30s) on disconnect.

## Switching Back to tmux

Remove `terminal` from `settings.json`, or set it explicitly:

```json
{
  "terminal": {
    "backend": "tmux"
  }
}
```

Restart the CAO server. Existing herdr sessions are unaffected (they remain running in herdr). New sessions will be created in tmux.

## Troubleshooting

### `cao session list` shows no sessions after server restart

Ghost DB records from a prior run were cleaned up. This is expected. Restart the server and check the log for:

```
Startup DB cleanup: removed N ghost terminal(s)
```

If sessions are genuinely running in herdr, they will be re-discovered on the next `cao launch`.

### Session visible in herdr but not in CAO

The CAO server may be connected to the wrong herdr session. Verify `terminal.herdr_session` in `settings.json` matches the session herdr is running under:

```bash
# Check which session herdr is using
herdr workspace list                        # default session
herdr --session cao workspace list          # named session
```

### Socket connection errors in CAO logs

herdr must be running before the CAO server starts. If you see socket errors:

1. Confirm herdr is running: `herdr --session cao workspace list`
2. If not running, start it: `herdr --session cao`
3. Restart the CAO server: `cao-server`

The default socket path is derived from the session name. If you use a non-default `herdr_session`, ensure herdr was started with the matching `--session` flag.
