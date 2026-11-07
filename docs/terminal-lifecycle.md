# Terminal Lifecycle

## Overview

Each terminal created by CAO (via `assign` or `handoff`) occupies a tmux window
and a database record. In long-running sessions, terminals accumulate and can
exhaust system resources. CAO provides automatic and manual cleanup paths.

If you want finished agents to stay around so people can look at what they did,
see [Keep finished agents](#keep-finished-agents-for-workshops-and-debugging).

## Deletion paths

| How deleted | Snapshot saved? |
|-------------|----------------|
| Handoff completes successfully (auto-delete) | Yes |
| `delete_terminal` MCP tool | Yes |
| `DELETE /terminals/{id}` API | Yes |
| `cao shutdown --session <name>` | Yes |
| `cao shutdown --all` | Yes |
| A kept agent reaped after its idle window, or to make room ([below](#keep-finished-agents-for-workshops-and-debugging)) | Yes, taken *before* the exit command |
| A flow's next run recycling the previous run's session | No |
| Process crash | No |

Individual deletion snapshots via `terminal_service.delete_terminal`.
Session-level shutdown (`delete_session`, which both `cao shutdown` modes reach
over `DELETE /sessions/{name}`) snapshots too: capturing each terminal's
scrollback is an explicit step of the teardown, and it deliberately runs
*before* the session kill, since scrollback only exists while the pane does. A
crash bypasses both paths, so nothing is captured. Flow recycling kills the old
session without a snapshot.

Capture is best-effort everywhere, not just at session level: a snapshot whose
write fails is logged and teardown continues regardless of which path took it.
So "Yes" above means the path attempts a snapshot, not that one is guaranteed.

## Snapshot files

On deletion, two files are written to `$CAO_HOME_DIR/logs/terminal/` (by default
`~/.aws/cli-agent-orchestrator/logs/terminal/`), both readable only by you (0600):

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
  "allowed_tools": null,
  "caller_id": "..."
}
```

All three file types (`.log`, `.scrollback`, `.snapshot.json`) are purged after
`RETENTION_DAYS` (14 days) by the cleanup service, which runs when the server
starts.

## Restore

```bash
cao terminal restore <terminal_id>
```

This creates a **plain shell window** in the original session at the original
working directory, replaying the saved scrollback via `cat ... ; exec $SHELL -l`.

Constraints:

- The original session must still exist. If the session was shut down, restore
  will fail. You can still read the scrollback directly:
  `cat ~/.aws/cli-agent-orchestrator/logs/terminal/<terminal_id>.scrollback`
- Restore creates a shell window, not a re-launched agent. The window shows
  the old output but is not connected to any provider.

## Assign vs handoff cleanup

- **Handoff** terminals are deleted automatically on success. No action needed.
- **Assign** terminals are not auto-deleted. Call `delete_terminal(terminal_id)`
  when you no longer need the terminal, or wait for the 10-terminal nudge.

Both change while [keeping finished agents](#keep-finished-agents-for-workshops-and-debugging)
is turned on.

## Terminal count nudge

When a session reaches 10 terminals, `assign` and `handoff` responses include:

> NOTE: This session has N terminals. Consider calling delete_terminal on
> terminals you no longer need.

Kept agents count toward those 10.

## Keep finished agents (for workshops and debugging)

### Why

When one agent hands work to another, CAO normally ends the child as soon as it is
done. That frees RAM and keeps sessions tidy, but it also throws away the one place
you could see **what the child did and which tools it called**: its own pane. All
that is left is the summary it sent back.

In a workshop or a demo, the trace is the point. Turn this setting on and finished
children stay open, idle, so anyone can open a pane, scroll back, and follow exactly
what happened. CAO still cleans them up later, on its own.

It is **off by default**. With it off, nothing on this page behaves any differently.

### Quick start

```bash
cao config set terminal.keep_finished.enabled true
```

That's it: no restart needed. The defaults are meant for a workshop: each kept
agent stays for an hour after it was last busy, and a session keeps at most 5. To
tune them, edit `settings.json` (`cao config path` prints where it is):

```json
{
  "terminal": {
    "keep_finished": {
      "enabled": true,
      "idle_window_minutes": 60,
      "max_per_session": 5,
      "on_cap": "teardown_oldest"
    }
  }
}
```

| Key | What it does | Allowed | Default |
|---|---|---|---|
| `enabled` | Turns keeping on for this machine | `true`, `false` | `false` |
| `idle_window_minutes` | How long a kept agent stays after CAO last saw it working | 1 – 10080 (a week) | `60` |
| `max_per_session` | How many kept agents one session may hold | 1 – 100 | `5` |
| `on_cap` | What happens when a session is full: `teardown_oldest` frees the oldest *idle* kept agent to make room; `dont_keep` tears the new one down as usual | `teardown_oldest`, `dont_keep` | `teardown_oldest` |

A bad value is refused: `cao config set` prints why and writes nothing, and a bad
value already in the file is logged at server start and treated as **off** until you
fix it.

To turn it off again:

```bash
cao config set terminal.keep_finished.enabled false
```

Agents already kept stay until their window ends (they are never stranded). To free
them now, delete them yourself (see [Operator deletes](#operator-deletes-still-delete-immediately)).

### What is kept

Only **children**: terminals another agent on this machine started with `assign` or
`handoff`. While the setting is on:

- **A `handoff` that succeeds** leaves its worker open. CAO sends no exit command and
  deletes nothing. Where the result would say "Worker terminal … was torn down", it
  says until when the worker is kept instead, for example:

  > Successfully handed off to developer (claude_code) in 41.20s. Worker terminal
  > 3f9a1c2e was kept until 2026-11-06 14:05 UTC. Keeping finished agents is on for
  > this machine (terminal.keep_finished): …

  The run-step response reports `"torn_down": false` for a kept worker, because
  nothing was deleted.

  `cao agent result 3f9a1c2e` still prints its last answer, and you can open its pane.
  `cao agent handoff` run from inside an agent's pane is kept the same way.
- **An agent's `delete_terminal` call** on an idle worker (a supervisor cleaning up
  after `assign`) keeps the worker instead, and the tool tells the agent so. There is
  nothing more for the agent to do.

The setting wins: in this first phase no call can opt out.

Not kept, exactly as before:

- workflow steps (YAML and script workflows), and flows;
- remote workers (`target_host`);
- a worker that **timed out or failed**: these were never torn down, and still aren't;
- a worker an agent deletes **while it is still working**: that's a cancellation, so
  it is deleted, and the agent is told why it wasn't kept;
- terminals no agent started, such as a session's own conductor, or a worker you
  started with `cao agent handoff` from your own shell;
- a terminal whose pane is already gone, such as a failed create whose record CAO
  kept so its cleanup can be retried. A handoff that fails that way names the
  terminal so you can delete it, and that delete is never turned into a keep.

When a worker isn't kept, the result says why (for example, the session is full),
after the usual "was torn down" or "was not torn down".

### Seeing kept agents

```bash
cao session status <session> --workers
```

```
ID           AGENT                PROVIDER        MODEL                HONORED  STATUS          KEPT UNTIL
--------------------------------------------------------------------------------------------------------------------
10000000     developer            kiro_cli        unknown              unknown  idle            -
20000000     developer            kiro_cli        unknown              unknown  idle            2026-11-06 14:05 UTC
```

The `KEPT UNTIL` column only appears when something is kept; with nothing kept the
table is exactly as before. `cao agent status <id>` adds `kept_until` for a kept
agent. The web UI does not show a badge yet.

Over the API, the terminal body (`GET /terminals/{id}`, and the body returned when a
terminal is created) now always has two more fields, `"kept"` and `"kept_until"`, and
each row of `GET /sessions/{name}/terminals` has `"kept_until"`. They are `false` and
`null` unless the terminal is kept, including while the setting is off. They are
additive: a client that ignores unknown fields sees no change. Handoff and agent-delete
responses can also include `keep_note`, explaining the keep or eviction outcome.

### The reaper: how kept agents go away

Once a minute, CAO looks at every kept agent:

- **Working** (busy, or waiting on a prompt you have to answer): it is never torn
  down, and its window starts again from now. So asking a kept agent a follow-up
  question keeps it around. **A kept agent stuck busy, for example on a prompt nobody
  answers, stays until you delete it**: there is no upper limit yet. Busy agents are
  never evicted for the cap either.
- **Idle past its window**: it is torn down the normal way. CAO saves its scrollback
  and `snapshot.json` **first**, while the pane still shows everything, then sends the
  exit command, closes the window, and records the end in its launch record
  (`kept_expired`). An ephemeral helper's name is released, and a Kiro helper's
  private home (with its transcripts) is removed only now, at this final teardown.
- **Status read raises**: CAO retries until one minute after the stored window ends,
  then tears the child down as it does for readable `unknown`. This bounds a broken
  status reader even across restarts; it can end work whose status CAO cannot read.

When a session is full, `teardown_oldest` tears down the oldest idle kept agent the
same way (`kept_evicted`), and the result says which one. If every kept agent in the
session is still working, the new one is not kept. If the agent chosen for eviction
starts working at that very moment, CAO leaves it running rather than kill live work;
it is then **no longer kept, and stays until you delete it**, and the result says so.
If eviction teardown fails or is deferred, the child is also no longer kept so the cap
can hold. CAO warns with its terminal ID, and `keep_note` says an operator delete is
required (`cao agent cancel --delete <id>`); the reaper cannot retry an unmarked child.

**After a server restart**, kept agents and their windows are remembered and the
reaper carries on. If the server was down when a window ended, the agent is torn down
when the server starts. One limit, on the **tmux** backend: after a restart CAO does
not re-attach to existing panes' output, so it cannot see that a kept agent is
working. Its status reads `unknown`, and CAO treats `unknown` as idle: the agent is
torn down when its stored window ends even if someone is typing in its pane, it can be
evicted for the cap, and an agent's `delete_terminal` on it is a keep, not a
cancellation. The same applies whenever a status read comes back `unknown`. On herdr,
CAO reads the status from herdr itself, so this restart limit doesn't apply.

### Operator deletes still delete immediately

You stay in control. These always delete at once, kept or not:

- `cao agent cancel --delete <id>`
- `cao shutdown --session <name>` and `cao shutdown --all`
- the web UI's close button
- `DELETE /terminals/{id}` and `DELETE /sessions/{name}`, and any other API call
  without the flag below

Only the agent side asks for a keep:

- the `delete_terminal` tool, and `cao agent handoff`'s own delete after a successful
  handoff, send a flag on their request: `DELETE /terminals/{id}?keep_if_enabled=true`;
- the `handoff` tool asks through the shape of its own run-step request.

The flag is sent whether or not keeping is on; with it off, the server ignores it and
deletes as before. The flag is a convenience, not a security boundary: an agent with a
shell can still delete a worker with `cao agent cancel --delete`. It can only turn a
delete into a keep, never stop one.

### Limits, RAM and tokens

- **RAM:** each kept agent is one idle CLI process. The window and `max_per_session`
  bound how many there are. Kept agents also count toward `server.max_terminals`, and
  a kept ephemeral helper toward `ephemeral.max_live_per_session`.
- **Tokens:** an idle CLI spends none. Asking a kept agent more questions does.
- **Nothing new is copied.** Keeping an agent archives nothing: its scrollback and
  logs are the same 0600 files as always, written at the final teardown.
- **A kept agent is still a live agent.** It keeps the tools it had (an ephemeral
  helper keeps its sealed tool ceiling), and other agents in its session can still
  message it.

### Troubleshooting

| What you see | Why | What to do |
|---|---|---|
| Handoff workers still disappear | The setting is off, or invalid | `cao config get terminal.keep_finished.enabled`; check the server log for `terminal.keep_finished is invalid` |
| `cao config set` says "must be a whole number from 1 to 10080" | A value out of range, or not a whole number | Use a whole number in the range shown |
| The result says "Not kept: this session already keeps 5" | The session is full and `on_cap` is `dont_keep`, or every kept agent is busy | Raise `max_per_session`, or delete kept agents you're done with |
| A kept agent vanished before its time | It was the oldest idle one when the session was full, or the window ended while the server was down | Raise `max_per_session` or `idle_window_minutes` |
| A worker deleted by an agent wasn't kept | It was still working, so the delete was a cancellation | Expected |
| A kept agent never goes away | It is stuck busy (for example on a prompt), or it started working as it was evicted | Delete it: `cao agent cancel --delete <id>` |

### FAQ

**Does this change workflows?** No. YAML and script workflow steps and flows tear
their terminals down exactly as before.

**Can a supervisor keep one particular child while the setting is off?** Not yet.
A per-call `keep` on `handoff` and `assign` is the next phase.

**Where do I read a kept agent's history after it's gone?** In its scrollback file,
`$CAO_HOME_DIR/logs/terminal/<terminal_id>.scrollback`, for 14 days.
