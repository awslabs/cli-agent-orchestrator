# Inbox Delivery

## Overview

When an agent calls the `send_message` tool with a `receiver_id` and a `message`, the message is queued in the database and delivered to the target terminal's input area as a bracketed paste. On the tmux backend, how the bracketing is applied depends on the host's tmux version (issue #413):

- **tmux < 3.7**: CAO wraps the buffer in hand-crafted `ESC [200~` / `ESC [201~` markers and pastes with `paste-buffer -r`. This guarantees bracketed framing even for TUIs that never enable bracketed paste mode (DECSET 2004) themselves — e.g. kiro-cli — so multi-line messages arrive as one input.
- **tmux >= 3.7**: pasted buffer content passes through `vis(3)` sanitization (hardening against bracket-end injection), so hand-crafted markers would render as literal `^[[200~` garbage. CAO loads only the raw message bytes and pastes with `paste-buffer -p`; tmux emits genuine markers conditionally on the pane's DECSET 2004 state. TUIs that never enable 2004 receive raw text and multi-line content submits per line — tmux-sanctioned semantics with no workaround short of `paste-buffer -S`, which CAO refuses because it bypasses the sanitization.

Delivery has four main paths:

1. **Immediate**: the API endpoint (`POST /terminals/{id}/inbox/messages`) attempts delivery right after persisting the message
2. **Status events**: on the tmux backend, `InboxService.run()` consumes the `terminal.*.status` events that the StatusMonitor publishes from terminal output, and attempts delivery each time a terminal reports IDLE or COMPLETED. The two herdr backends (the herdr TUI backend, `terminal.backend: "herdr"`, and the Herdr app backend, `herdr-app`) do not feed that pipeline; their inbox services attempt delivery on herdr's own `idle` and `done` agent-status events instead
3. **OpenCode poller**: every `INBOX_POLLING_INTERVAL` (5s) attempts delivery for OpenCode terminals that have pending messages
4. **Reconciliation sweep**: every `INBOX_RECONCILE_INTERVAL` (30s) attempts delivery for messages the other paths missed (see [Reconciliation Sweep](#reconciliation-sweep))

A few other callers start a delivery attempt too, such as the delegation callback service for the replies it forwards and the herdr TUI inbox service's settle retries. All of them converge on `InboxService.deliver_pending()`, which gates delivery based on terminal status. By default one call delivers at most one message, the oldest PENDING one.

### One Delivery at a Time per Terminal

`deliver_pending()` reads the PENDING message, checks the terminal's status, marks the message DELIVERED, and then sends it. Two paths that ran that sequence for the same terminal at the same moment could both read the same message and deliver it twice (issue #406). Two locks prevent that:

- **Within one server**: `deliver_pending()` takes a per-terminal lock and runs the whole sequence (`_deliver_pending_locked()`) under it. Deliveries to one terminal run one after another; deliveries to different terminals do not wait for each other.
- **Across servers**: `cao-server` takes an exclusive `flock` on `$CAO_HOME_DIR/server.lock` at startup and refuses to start when another server holds it. Two servers on one data directory would each sweep the same pending messages, which the in-process lock cannot see.

## Standard Delivery

By default, messages are only delivered when the terminal status is **IDLE** or **COMPLETED**. This ensures the provider's TUI is ready to accept input and the message won't be lost or corrupt the terminal state.

The send itself (`dispatch_input`) has its own guard, the WAITING_USER_ANSWER guard. A terminal is in WAITING_USER_ANSWER when its agent has a question or approval dialog open for a human. For a provider that sets `blocks_orchestrated_input_while_waiting_user_answer`, `dispatch_input` refuses every orchestrated input (input CAO sends on an agent's behalf, which includes inbox messages) while the terminal is in that state (issue #896). This covers a dialog that opens between the status check and the paste: the send is refused, the message goes back to PENDING, and a later status event or the reconcile sweep delivers it once the dialog closes. A terminal whose status is ERROR (the provider process may have exited) is refused too; that message is marked FAILED.

## Submit Confirmation

A delivery is a bracketed paste, a fixed per-provider delay, then Enter. The delay only guesses when the TUI is ready; when it guesses wrong the TUI swallows the Enter and the message sits unsubmitted in the composer while the terminal still reads IDLE (issue #78). Nothing raises, and the message is already DELIVERED, so without a check the sender waits forever and the next message is pasted on top of the first.

After each paste the inbox therefore waits for evidence that the receiver began processing **this** delivery: a PROCESSING / COMPLETED / WAITING_USER_ANSWER status whose output-generation stamp is newer than the dispatch boundary (a status latched from the previous turn does not count). The output generation is a per-terminal counter that advances each time a chunk of terminal output arrives; the dispatch boundary is its value sampled inside `dispatch_input` just before any key reaches the pane. The Herdr app backend has no output generation, so there a started status counts when it is PROCESSING or differs from the status read just before the dispatch, and so does a `working` or `blocked` report from herdr-core made after the send.

The inbox holds the terminal's delivery lock while it waits, so the next message for the same terminal is not sent until the wait ends.

If no evidence arrives within `INBOX_SUBMIT_CONFIRM_TIMEOUT` (8s) the recovery runs, up to `INBOX_SUBMIT_MAX_RESUBMITS` (2) times. The recovery first reads the provider's composer, the editable input box of its TUI:

| Provider can... | Recovery |
|-----------------|----------|
| read its composer and the text is still there (`extract_current_composer`: codex, claude_code, kiro_cli) | bare Enter |
| read its composer, the text is gone, and it has a direct status probe (`supports_direct_status_probe`) | full re-send |
| not read its composer (all other providers) | nothing is sent; the drop is logged |

A full re-send is never attempted when the composer cannot be read: it would paste a second copy on top of the first and submit both.

A direct status probe is a live read of the pane that does not depend on the cached status. A provider that has one (kimi_cli, opencode_cli, mcode) is probed before either recovery, and a receiver found already working counts as confirmed with nothing sent. No provider has both a composer reader and a direct status probe today, so the full re-send row is not reached by any shipped provider.

The providers with no composer reader are antigravity_cli, copilot_cli, cursor_cli, grok_cli, hermes, kimi_cli, mcode, mock_cli, omp, opencode_cli and qwen_cli. `TestInboxRecoveryCoverageByProvider` in `test/services/test_inbox_service.py` pins that list (`NO_RECOVERY`), and issue #1494 tracks adding composer readers.

Neither recovery may answer an open dialog. The full re-send goes back through `dispatch_input`, so it inherits the WAITING_USER_ANSWER guard described under Standard Delivery. The bare Enter does not — it submits what is already in the composer — so it checks that guard itself and sends nothing when the receiver is parked on a prompt and its provider sets `blocks_orchestrated_input_while_waiting_user_answer`. Otherwise the one recovery that submits without pasting would be the one that can pick a highlighted option on a human's behalf (issue #896). Today every real dialog capture hides the composer box, so the bare-Enter branch is already out of reach there; the check makes that an invariant rather than a property of two providers' composer readers.

Cost: a healthy delivery pays one or two status reads. A dropped one holds **that terminal's** delivery lock for the first wait plus one more wait per recovery actually sent (up to 24s with the defaults). Other terminals are not affected: each delivery runs on its own worker thread, and the sweeps fan out. `POST /terminals/{id}/inbox/messages` answers once the paste has reached the pane (bounded by `INBOX_IMMEDIATE_DELIVERY_WAIT`, 5s, or by `INBOX_DELIVERY_OUTCOME_WAIT`, 45s, when the client sets `wait_for_outcome=true`), not after the confirmation.

The confirmation does not run in three cases:

- **The setting is off.** Set `CAO_INBOX_SUBMIT_CONFIRMATION=false` before starting `cao-server` to turn the confirmation off; deliveries then behave as before #78 (paste, Enter, done).
- **Eager deliveries** (see below). The receiver was already PROCESSING when the message was sent, or in WAITING_USER_ANSWER on a provider that does not set `blocks_orchestrated_input_while_waiting_user_answer` (a provider that sets it has the message held as PENDING instead). A status that left the ready state therefore proves nothing.
- **The herdr TUI backend.** Its inbox service delivers on herdr events from its asyncio event loop, and the blocking wait would stall event handling for the whole session. The skip is per backend, so it applies to every delivery there, including the immediate attempt and the sweeps. The Herdr app backend delivers on a worker thread and does run the confirmation.

In these cases a swallowed Enter is not detected, and the terminal's delivery lock is released as soon as the send returns.

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

### Which Paths Deliver Eagerly

The two-flag gate is checked inside `deliver_pending()`, so a path delivers eagerly only if it calls `deliver_pending()` while the terminal is PROCESSING. The immediate attempt on `POST` does, and so does the reconcile sweep for a message past its grace window. The status-event triggers fire only when a terminal reports IDLE or COMPLETED (`idle` or `done` on herdr), so they do not start a delivery mid-turn.

### Provider Capability: `accepts_input_while_processing`

A property on `BaseProvider` (default `False`) that signals whether a provider's TUI safely buffers pasted input during processing. Override to `True` in providers that support this.

Currently enabled for:
- **Claude Code** (`ClaudeCodeProvider`): Ink TUI buffers input at all times
- **MiniMax Code** (`MiniMaxCodeProvider`): returns `True` unconditionally, with no `_initialized` gate

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
| Enter swallowed on an eager delivery; the message sits unsubmitted | Not measured | None: submit confirmation does not run for eager deliveries |
| Feature causes regression in non-eager providers | None | Provider flag defaults to False; only opt-in providers affected |

## Reconciliation Sweep

The immediate and status-event paths can both miss a message when the receiving terminal is *already idle* when the message is queued:

- the single immediate attempt may observe a momentarily stale status and skip delivery, and
- a status event is only published when the terminal produces output, which an already-idle agent never does.

When both miss, the message would otherwise stay `PENDING` forever (issue #131).

A provider-agnostic background sweep closes this gap. Every `INBOX_RECONCILE_INTERVAL` (default 30s) it re-attempts delivery for any message left `PENDING` longer than `INBOX_RECONCILE_GRACE_SECONDS` (default 30s), routing it back through the same `deliver_pending()` gate as the other paths. The work scales with the number of *backlogged* receivers, not the total agent count: when nothing is stuck the sweep runs two cheap queries and returns.

The sweep also marks FAILED any message older than the grace window whose receiving terminal no longer exists (issue #708). No path can deliver such a message, so it is failed and logged instead of staying `PENDING`.

### Grace Window

The sweep deliberately ignores messages younger than the grace window. The immediate and status-event paths own delivery during that window; the sweep only adopts messages they have demonstrably had their chance at and missed. This keeps the sweep from competing with the fast paths on freshly queued messages and minimizes its overlap with them.

### Relationship to the OpenCode Poller

The sweep does not replace the OpenCode poller. They serve different roles: the OpenCode poller is a fast (5s) primary wakeup for a provider whose terminal output can stop changing once its TUI settles, while the sweep is a slow, provider-agnostic safety net. Both call `deliver_pending()` for each receiver, on up to `INBOX_SWEEP_FANOUT` (8) worker threads. When they overlap with each other or with the fast paths, the per-terminal lock keeps a message from being delivered twice (the duplicate-wakeup race, issue #406, now closed); the grace window keeps the sweep from overlapping the fast paths in practice. GH #115 (the event-driven architecture) is closed. It did not remove the OpenCode poller, which remains a separate, provider-specific wakeup.
