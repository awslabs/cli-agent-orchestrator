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

## Submit Confirmation

A delivery is a bracketed paste, a fixed per-provider delay, then Enter. The delay only guesses when the TUI is ready; when it guesses wrong the TUI swallows the Enter and the message sits unsubmitted in the composer while the terminal still reads IDLE (issue #78). Nothing raises, and the message is already DELIVERED, so without a check the sender waits forever and the next message is pasted on top of the first.

After each paste the inbox therefore waits for evidence that the receiver began processing **this** delivery: a PROCESSING / COMPLETED / WAITING_USER_ANSWER status whose output-generation stamp is newer than the dispatch boundary (a status latched from the previous turn does not count). If none arrives within `INBOX_SUBMIT_CONFIRM_TIMEOUT` (8s) the recovery runs, up to `INBOX_SUBMIT_MAX_RESUBMITS` (2) times:

| Provider can... | Recovery |
|-----------------|----------|
| read its composer and the text is still there (`extract_current_composer`: codex, claude_code, kiro_cli) | bare Enter |
| read its composer, the text is gone, and it has a direct status probe | full re-send |
| neither read its composer nor probe (others) | nothing; the drop is logged |

A full re-send is never attempted when the composer cannot be read: it would paste a second copy on top of the first and submit both.

Neither recovery may answer an open dialog. The full re-send goes back through `dispatch_input`, so it inherits the WAITING_USER_ANSWER guard below. The bare Enter does not — it submits what is already in the composer — so it checks that guard itself and sends nothing when the receiver is parked on a prompt and its provider sets `blocks_orchestrated_input_while_waiting_user_answer`. Otherwise the one recovery that submits without pasting would be the one that can pick a highlighted option on a human's behalf (issue #896). Today every real dialog capture hides the composer box, so the bare-Enter branch is already out of reach there; the check makes that an invariant rather than a property of two providers' composer readers.

Cost: a healthy delivery pays one or two status reads. A dropped one holds **that terminal's** delivery lock for the first wait plus one more wait per recovery actually sent (up to 24s with the defaults). Other terminals are not affected: each delivery runs on its own worker thread, and the sweeps fan out. `POST /terminals/{id}/inbox/messages` answers once the paste has reached the pane (bounded by `INBOX_IMMEDIATE_DELIVERY_WAIT`, 5s), not after the confirmation.

Set `CAO_INBOX_SUBMIT_CONFIRMATION=false` before starting `cao-server` to turn the confirmation off; deliveries then behave as before #78 (paste, Enter, done). Event-inbox backends (herdr) skip it regardless.

## Eager Delivery

Some providers (e.g., Claude Code) have TUIs that buffer pasted input even while processing. For these providers, waiting for IDLE introduces unnecessary latency between agent turns.

Eager delivery allows messages to be delivered during **PROCESSING**, eliminating the
inter-turn gap. It also covers **WAITING_USER_ANSWER**, but only for providers whose
`blocks_orchestrated_input_while_waiting_user_answer` is False.

A provider that sets that property (e.g. Claude Code, Codex, MiniMax Code) is declaring that its
choice widget consumes pasted text as the answer to the open dialog. Delivering there
loses the message — it is marked DELIVERED but never reaches the agent — and the
trailing Enter submits whichever option is highlighted, so an inbox message would
answer a question reserved for a human (issue #896). For those providers the message
stays PENDING and is delivered by the normal IDLE/COMPLETED consumer, or by the
reconcile sweep, once the dialog closes.

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
