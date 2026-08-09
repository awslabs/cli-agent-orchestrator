---
created: 2026-05-30
lastUpdated: 2026-08-09
summary: "Reference for ordinary inbox delivery, managed generation binding, and the dedicated refusal-bound callback recovery protocol."
category: REFERENCE
status: CURRENT
note: "Current fork contract; callback recovery is not a general managed-message API."
changelog:
  - "2026-08-09: Generic inbox rows now capture the exact live managed-v2 receiver generation; parked, stale, and pre-M3 generationless rows are terminalized rather than retargeted."
  - "2026-07-30: Replaced generic bound messages with one-shot refusal/callback recovery."
  - "2026-07-30: Added exact-generation managed message binding."
---

# Inbox Delivery

## Refusal-bound callback recovery

`POST /terminals/{source}/callback-recoveries` is dedicated to recovering one
already-authored callback after an exact durable `managed-acp-pane` zero-byte
control refusal. It is not an ordinary message surface. Unknown request fields
are rejected. Admission binds project/run/task, source terminal/generation,
Codex provider and bridge session, supervisor caller/session, the refusal
occurrence, callback occurrence, report/source-head/manifest/finalization
digests, and publishing-lease state or absence inside one SQLite writer
transaction.

The refusal/callback identity has a unique one-shot key independent of the
caller operation id, so another id cannot repeat recovery. Refusals and
provider ambiguity are durable terminal outcomes. The exact provider and
session are checked again inside the generation-fence admission critical
section. A replaced generation is terminalized without bridge or pane
fallback, and stale rows do not starve later inbox work.

The recovery prompt's strict 14-field provider receipt is stored inside a
separate metadata envelope and revalidated against the immutable operation and
inbox row on every read. `POST /callback-recoveries/{key}/complete` closes the
operation only with the exact original callback inbox row. Open operations hold
terminal deletion and retention cleanup. Blocking database and bridge work is
offloaded from FastAPI's event loop.

## Generic managed inbox generation binding

`POST /terminals/{receiver}/inbox/messages` remains the ordinary generic
message surface, but a live managed-v2 receiver now captures its exact current
receiver generation while holding the terminal successor lock. Delivery
revalidates that generation before choosing a bridge or native pane writer.

A row for a parked generation, a row bound to an older generation, and a
pre-M3 generationless row observed on a managed-v2 receiver are terminalized
as visible failed history. They are never retargeted to a successor, never
fall back to raw pane paste, and never remain pending solely because a parked
head row preceded valid current-generation work. The park path eagerly applies
the same cleanup; delivery repeats it as crash-recovery backstop.

## Overview

When an agent calls `send_message(terminal_id, message)`, the message is queued in the database and delivered to the target terminal's input area via bracketed paste. Delivery has two paths:

1. **Immediate**: the API endpoint attempts delivery right after persisting the message
2. **Watchdog**: a `PollingObserver` (5s interval) monitors terminal log files for changes and attempts delivery when idle patterns are detected

Both paths converge on `check_and_send_pending_messages()`, which gates delivery based on terminal status.

## How the Paste Is Framed

Delivery loads the message into a tmux buffer verbatim and pastes it with `paste-buffer -p -r`. Each flag is load-bearing:

- `-p` asks **tmux** to emit the bracketed-paste markers (`ESC[200~` / `ESC[201~`). tmux emits them only for a pane that advertised `DECSET 2004`; a pane that never asked — a bare shell, say — correctly receives the message unframed.
- `-r` suppresses tmux's LF→CR rewrite. Without it a composer reads each newline as Enter and submits a multi-line message one line at a time, with every line but the last truncated.

CAO must never write the markers into the buffer itself. tmux sanitizes control bytes on their way out of a paste buffer, so an `ESC` placed there does not reach the pane as an escape at all — it arrives as the printable text `^[[200~`, which a composer types into the prompt and submits along with the message. Only markers that tmux generates for `-p` are written outside that sanitizing path.

Shell commands sent during terminal initialization use `-p` **without** `-r`, because there each newline *should* become the Enter that runs the line.

The argv is asserted in `test/clients/test_tmux_paste_framing.py`; what actually arrives at a real pane is measured in `test/e2e/test_ordinary_input_live.py`.

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
