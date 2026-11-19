## 11. CAO Shelf (not in the research)

1. **Name / pitch.** CAO Shelf: "Twelve folders on a shelf, the busy ones in front and the forgotten ones faded." Each CAO session is one card with its conductor as the title and its helpers as small chips; the whole card dims as `last_active` ages.

2. **Metaphor mapping.**
   - Agent: a chip on a folder. Session: the folder itself, titled by its conductor (always exactly one).
   - Handoff: a chip tucked under the conductor (from `caller_id`); a "handoff in flight" line while one is open. Departed helper: an entry in the folder's history drawer, rebuilt from inbox rows.
   - Approval: the folder moves to the front shelf, edged amber, with the question and two buttons. Failure: the same, edged red.
   - Idle agent: a folder that fades with age (warm under 1 h, parked under 48 h, stale beyond). Orphan: a dashed red chip. Memory: the shelf remembers nothing but the tier toggles you chose.

3. **Drawing.**
```
 CAO SHELF  12 sessions                       [ Quiet day ] [ Busy day ]
 [ Needs you 0 ] [ Live 0 ] [ Parked 3 ] [ Stale >48h 9 (hidden) ]
 20 terminals: 0 processing, 0 waiting for you, 20 idle. Idle is not running.
 PARKED (idle under 48 h) -----------------------------------------------
 +- payments-fix ------------+ +- docs-refresh -----------+
 | PARKED   conductor idle 2h| | PARKED  conductor idle 5h|
 |  (o) coder-1   idle 2h    | |  (o) writer-1  idle 5h   |
 |  (o) tester    idle 2h    | | [Ask] [History 2] [Stop] |
 |  (o) reviewer  idle 3h    | +--------------------------+
 | [Ask] [History 3] [Stop..]|
 +---------------------------+
 STALE (dimmed; opens with the toggle) ----------------------------------
 +- search-tuning ----------+ +- onboarding-flow --------+
 | idle 2d  conductor       | | idle 2d  conductor       |
 |  (o) probe-1 ORPHAN      | |  (o) yoda (peer)         |
 |  caller no longer exists | | [Ask] [History 0] [Stop] |
 +--------------------------+ +--------------------------+
 [ Clean up stale (9)... ] [ All history: 20 closed helpers ]  asks first
 Busy day: payments-fix jumps to NEEDS YOU, amber edge:
   tester asks "push to origin?" 4 min  [ Approve push ] [ Deny ]
```

4. **Interaction.**
   - Tier toggles show or hide Needs you, Live, Parked and Stale. Expand a card for its terminals with `status`, `last_active` and `caller_id`.
   - Ask: `send_session_message` to the card's conductor; the card then shows the conductor processing, completed, and back to idle (a visible "completed decays to idle").
   - Approve push or Deny: `send_session_message` to the waiting helper as typed input.
   - History drawer: closed helpers of that session with their last inbox row; "inbox rows" opens the three rows.
   - Stop and Clean up: `shutdown_session` per session, in a dialog that names every session and every terminal it ends, starts with nothing ticked, and warns when something is working.

5. **Data honesty.**
   - `list_sessions` once per poll, then `get_terminal_status` only for sessions whose conductor was not idle last time (poll per session, not per terminal). Fields: `status`, `last_active`, `caller_id`, profile name, `pending_inbox_count`.
   - `status` is inferred from the screen, so the card says "idle 2 h" (from `last_active`), never "running". Tier is `status` first, then `last_active`; the header counts processing, waiting and idle separately.
   - Vanished workers: a blocking handoff deletes its worker, so the history drawer is built from inbox rows whose sender no longer exists (94% of rows in the real data) and labelled "rebuilt from inbox rows". Orphans: a helper whose `caller_id` points at a gone terminal gets the dashed ORPHAN chip.
   - Fit with the real shape: one conductor per session, 0 to 3 helpers, all idle, 9 of 12 over 48 h. The default view is three parked cards with nine stale ones counted behind a toggle.

6. **Where it lives.** A `ui.pages` sidebar page (a `panelTabs` tab works too, with the grid collapsing to one column). It needs a small backend to poll CAO and keep the inbox-derived history, so the third-party trust grant applies. Limits: v0.8.0 dashboard has no base path and cannot be reached through the workshop code editor's forwarded port; whether the page can call `127.0.0.1:9889` directly is unverified, so it should go through its own backend.

7. **Build size.** M. Hardest piece: building the per-session history from inbox rows (one request per terminal, capped at 100 rows) without polling every terminal every few seconds.

8. **Best for / fails at.** Best for: a daily operator who wants to see which sessions are live, which are asking, and which are quietly rotting. Fails at: a timeline of what happened while away (cards show state, not events). Works with 50 agents? Yes at 50 sessions: stale ones stay hidden and cards cap helper chips at 5 then "+N"; 50 live ones in one session would crowd a card.

9. **Workshop fit.** Outside the workshop only. Inside it, the same grouping prints as a text list from a `/crew shelf` command (a launch prompt asking the agent to call `list_sessions` and print needs-you, live, parked, stale counts).
