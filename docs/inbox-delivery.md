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

Eager delivery allows messages to be delivered during **PROCESSING** and **WAITING_USER_ANSWER** states, eliminating the inter-turn gap.

### Enabling

Set the environment variable before starting the CAO server:

```bash
export CAO_EAGER_INBOX_DELIVERY=true
cao-server
```

When disabled (default), delivery behavior is unchanged -- messages wait for IDLE or COMPLETED.

### Two-Flag Gate

Eager delivery requires both conditions to be true:

1. **Environment variable** (`CAO_EAGER_INBOX_DELIVERY=true`): global kill-switch for operators
2. **Provider capability** (`accepts_input_while_processing = True`): per-provider opt-in

This prevents accidental delivery to providers whose TUIs would be corrupted by unsolicited input during processing.

### How the Watchdog Path Changes

Without eager delivery, the watchdog uses a fast `_has_idle_pattern()` check before attempting delivery. For eager-capable providers, this check is skipped (there is no idle pattern during PROCESSING), and the watchdog proceeds directly to `check_and_send_pending_messages()` where the full status gate applies.

### Provider Capability: `accepts_input_while_processing`

A property on `BaseProvider` (default `False`) that signals whether a provider's TUI safely buffers pasted input during processing. Override to `True` in providers that support this.

Currently enabled for:
- **Claude Code** (`ClaudeCodeProvider`): Ink TUI buffers input at all times

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
