# cao-session-monitor

A [herdr](https://herdr.dev) companion plugin: a read-only status pane showing CAO
sessions/terminals, flows, and workflows, inside the herdr `cao` session only. It
renders nothing in your default or personal herdr sessions.

This is a standalone herdr plugin, not a CAO server plugin — it makes no change to
`cao-server` or CAO's `src/`. It talks to CAO's existing HTTP API (`:9889`) and to
herdr's own socket API, both read-only.

## Status

Implemented: the pane script renders live CAO sessions (via AG-UI SSE with REST
polling fallback), flows, and workflows with focus-based bolding and graceful
degradation when AG-UI is disabled or CAO is unreachable.

## Prerequisites

- herdr `>= 0.7.5`.
- CAO running with the herdr backend (`terminal_backend: "herdr"` in
  `~/.aws/cli-agent-orchestrator/config.json`), launched as `herdr --session cao`
  (CAO's default for that backend).
- `python3` on PATH (the pane script is Python 3 stdlib-only, no install step).
- To see the sessions/terminals block, `cao-server` must be started with the AG-UI
  surface on: `CAO_AGUI_ENABLED=1` (or `CAO_MCP_APPS_ENABLED=1`). Without it, the
  plugin still renders — flows and workflows keep working via REST — but the
  sessions block shows a one-line hint to enable the flag instead of blanking.

## Install (link)

Plugins are global to your herdr user, not per-session — linking makes the plugin
available everywhere, but its own self-gate keeps it inert outside the `cao` session.

From the repository root:

```bash
herdr plugin link examples/cao-session-monitor --enabled
```

Confirm it registered:

```bash
herdr plugin list
# cao.session-monitor (CAO Session Monitor) enabled [local:.../examples/cao-session-monitor]
```

## Uninstall (unlink)

```bash
herdr plugin unlink cao.session-monitor
```

This is a clean removal — no herdr or CAO state is left behind.

## Opening the pane

**Auto-open:** a `[[startup]]` hook opens the pane automatically whenever herdr starts
or hands off, but only inside the `cao` session — elsewhere it is a no-op.

**Manual / reopen:** invoke the plugin action directly:

```bash
herdr plugin action invoke cao.session-monitor.open
```

**Keybinding:** plugins cannot ship keybindings — herdr requires you to add one to
your own `~/.config/herdr/config.toml`. Add:

```toml
[[keys.command]]
key = "prefix+alt+m"
type = "plugin_action"
command = "cao.session-monitor.open"
description = "CAO: open session monitor"
```

`prefix+alt+m` (`m` for **m**onitor) is a suggestion, free in herdr's default
keymap — pick any binding that doesn't collide with your own `[[keys.command]]`
entries. Reload with `herdr server reload-config` or restart herdr for the new
binding to take effect.

## What it shows

Three blocks, one crisp line per item, covering every workspace in the `cao` session
(not just ones with a `cao-` label prefix):

- **Sessions** — live CAO sessions and their terminals, sourced from the AG-UI
  stream (`GET /agui/v1/stream`).
- **Flows** — cron-scheduled sessions (`GET /flows`), polled roughly every 15s.
- **Workflows** — multi-step run definitions (`GET /workflows`), polled roughly every
  15s. Definitions only — no live run status (CAO has no endpoint to list runs).

The currently focused session/terminal is bolded, driven by herdr's pushed focus
events — no extra polling for focus.

## Scope and design notes

Full rationale lives in `openspec/changes/cao-session-monitor/design.md` and
`brainstorm.md`. Key points:

- herdr-only by design — no tmux backend support (the scoping, focus, and label
  bridge all depend on herdr primitives).
- No CAO API, model, or DB change. Bolding correlates the focused herdr pane to a
  CAO row using labels CAO already writes (session -> workspace label, terminal ->
  tab label) — no new bridge required.
- Never blanks silently: an unreachable CAO API shows a banner and last-known state
  with retry; a disabled AG-UI stream shows the enable-hint instead of an empty
  sessions block.
