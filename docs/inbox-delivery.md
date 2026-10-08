# Inbox Delivery

## Overview

When an agent calls `send_message(terminal_id, message)`, the message is queued in the database and delivered to the target terminal's input area as a bracketed paste. How the bracketing is applied depends on the host's tmux version (issue #413):

- **tmux < 3.7**: CAO wraps the buffer in hand-crafted `ESC [200~` / `ESC [201~` markers and pastes with `paste-buffer -r`. This guarantees bracketed framing even for TUIs that never enable bracketed paste mode (DECSET 2004) themselves — e.g. kiro-cli — so multi-line messages arrive as one input.
- **tmux >= 3.7**: pasted buffer content passes through `vis(3)` sanitization (hardening against bracket-end injection), so hand-crafted markers would render as literal `^[[200~` garbage. CAO loads only the raw message bytes and pastes with `paste-buffer -p`; tmux emits genuine markers conditionally on the pane's DECSET 2004 state. TUIs that never enable 2004 receive raw text and multi-line content submits per line — tmux-sanctioned semantics with no workaround short of `paste-buffer -S`, which CAO refuses because it bypasses the sanitization.

Delivery has two paths:

1. **Immediate**: the API endpoint attempts delivery right after persisting the message
2. **Watchdog**: a `PollingObserver` (5s interval) monitors terminal log files for changes and attempts delivery when idle patterns are detected

Both paths converge on `check_and_send_pending_messages()`, which gates delivery based on terminal status.

## Standard Delivery

By default, messages are only delivered when the terminal status is **IDLE** or **COMPLETED**. This ensures the provider's TUI is ready to accept input and the message won't be lost or corrupt the terminal state.

## Eager Delivery

Some providers (e.g., Claude Code) have TUIs that buffer pasted input even while processing. For these providers, waiting for IDLE introduces unnecessary latency between agent turns.

Eager delivery lets messages be delivered while the terminal is **PROCESSING**, eliminating the inter-turn gap. It is always on for providers that declare `accepts_input_while_processing = True`, and there is no setting for it. The former `CAO_EAGER_INBOX_DELIVERY` variable is removed and ignored if set.

### Never During WAITING_USER_ANSWER

A terminal in **WAITING_USER_ANSWER** never receives a message, even from a capable provider. That state means a dialog owns the input (for Claude Code, an AskUserQuestion or approval prompt). Pasted text would be consumed by the dialog, and the trailing Enter could select an option on the user's behalf. The message stays PENDING and is delivered after the dialog closes, on the next IDLE/COMPLETED status event or by the reconciliation sweep below.

This protection depends on the backend reporting WAITING_USER_ANSWER. On the herdr backend, status comes from herdr's own detection. With Claude Code 2.1.281 or later, a session started with `--agent` (as CAO launches Claude Code) or `--name` draws an agent-name rule under the dialog footer, and herdr then reports the open dialog as `idle` or `done` ([herdrdev/herdr#4573](https://github.com/herdrdev/herdr/issues/4573)). CAO then sees the terminal as ready and delivers. To check a pane, run `herdr agent explain <pane>`. An open dialog that reports `rule: osc_title_idle` is affected. Until herdr ships a fix, a local override of herdr's Claude detection manifest (`~/.config/herdr/agent-detection/claude.toml`) with the rule posted in that issue makes these dialogs report `blocked`. Remove the override once the upstream fix lands, because it shadows herdr's remote manifest updates.

### Provider Capability: `accepts_input_while_processing`

A property on `BaseProvider` (default `False`) that signals whether a provider's TUI safely buffers pasted input during processing. Override to `True` in providers that support this.

Currently enabled for:
- **Claude Code** (`ClaudeCodeProvider`): Ink TUI buffers input at all times
- **MiniMax Code** (`MinimaxCodeProvider`)

Other providers that may support this (contributions welcome):
- **Codex**: TUI-based, may buffer input
- **OpenCode**: TUI-based, may buffer input

To enable for a new provider, override the property:

```python
@property
def accepts_input_while_processing(self) -> bool:
    """This provider buffers pasted input during processing."""
    return self._initialized
```

The `_initialized` gate is important -- it prevents delivery during startup when `get_status()` returns PROCESSING but the REPL isn't actually ready.

### Risks

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Message delivered during PROCESSING gets lost (agent errors mid-turn) | Low | Message status is DELIVERED; acceptable for v1 |
| Watchdog fires every 5s during long turns | Medium (bounded) | One DB query + one tmux call per interval; no amplification |
| Feature causes regression in non-eager providers | None | Provider flag defaults to False; only opt-in providers affected |

## Reconciliation Sweep

The immediate and watchdog paths can both miss a message when the receiving terminal is *already idle* when the message is queued:

- the single immediate attempt may observe a momentarily stale status and skip delivery, and
- the watchdog only fires on log-file changes, which an already-idle agent that produces no further output never generates.

When both miss, the message would otherwise stay `PENDING` forever (issue #131).

A provider-agnostic background sweep closes this gap. Every `INBOX_RECONCILE_INTERVAL` (default 30s) it re-attempts delivery for any message left `PENDING` longer than `INBOX_RECONCILE_GRACE_SECONDS` (default 30s), routing it back through the same `check_and_send_pending_messages()` gate as the other paths. The work scales with the number of *backlogged* receivers, not the total agent count: when nothing is stuck the sweep runs one cheap query and returns.

### Grace Window

The sweep deliberately ignores messages younger than the grace window. The immediate and watchdog paths own delivery during that window; the sweep only adopts messages they have demonstrably had their chance at and missed. This keeps the sweep from competing with the fast paths on freshly queued messages and minimizes its overlap with them.

### Relationship to the OpenCode Poller

The sweep does not replace the OpenCode poller. They serve different roles: the OpenCode poller is a fast (5s) primary wakeup for a provider whose logs stop changing once its TUI settles, while the sweep is a slow, provider-agnostic safety net. Both reuse `check_and_send_pending_messages()` and so share its known duplicate-wakeup race; the grace window keeps the sweep from overlapping the fast paths in practice. GH #115 tracks unifying all of these wakeup sources into a single coordinated delivery engine that would make delivery atomic.
