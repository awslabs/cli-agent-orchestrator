# AG-UI operator dashboard

A browser dashboard for the person supervising a CAO fleet. It reads two AG-UI
surfaces and nothing else:

- **Approvals**, from the run plane (`POST /agui/v1/run`). When a terminal is
  waiting on a permission prompt, the run ends `RUN_FINISHED(interrupt)` and the
  dashboard shows a card for each open interrupt. You answer Approve, Deny, or
  Edit where the prompt accepts it.
- **Generative UI**, from the ambient stream (`/agui/v1/stream`). Agents'
  `emit_ui` calls arrive as `CUSTOM cao.generative_ui` frames, and the panel
  lists them, attributed to the subagent that sent them.
- **Session timeline**, from the same ambient stream. A supervisor's `assign` or
  `handoff` arrives as `TOOL_CALL_START` and the end of the receiver's work as
  `TOOL_CALL_END`; the panel shows each call once: who called whom, and whether
  it is open, completed, or unconfirmed after a reconnect.

```
core/      TypeScript, no DOM and no framework: the run plane, the stream, and pure reducers
view/      React 19 + Vite 8 UI over core/
fixtures/  frames captured from a real cao-server (see fixtures/README.md)
e2e/       keyless end-to-end check: core/ driven headless against cao-server and mock_cli
```

## Run it

```bash
# 1. A cao-server with AG-UI on (default port 9889).
CAO_AGUI_ENABLED=1 uv run cao-server

# 2. The dashboard, from this directory. Node.js 22.18 or later.
npm ci
npm run dev        # http://localhost:5173
```

Open `http://localhost:5173/?server=http://127.0.0.1:9889`. `?server=` defaults
to `http://127.0.0.1:9889`.

**Origins.** cao-server allows browser requests from `http://localhost` and
`http://127.0.0.1` on ports **3000**, **5173** and **5174** by default, plus its
own origin (`constants.py`, `CORS_ORIGINS`). The dev server is pinned to 5173.
If you serve the dashboard anywhere else, add that origin with
`CAO_CORS_ORIGINS`. A refused origin shows as a network error, because the
browser hides the response.

**Auth.** With auth on, put a token in `sessionStorage.caoToken` before loading
the page. The dashboard sends it as an `Authorization` header, never in a URL:
on `POST /agui/v1/stream` for the stream (an `EventSource` cannot send headers)
and on every run.

The token is attached on its own only when `?server=` is on this machine
(`localhost`, `127.0.0.1` or `[::1]`). For any other server the page asks you to
confirm first, so a crafted link cannot send your token somewhere else
(`view/token.ts`). A `?server=` that is not an http or https URL is refused
outright: the page says why and connects to nothing. Answering an approval needs `cao:write`; with a read-only
token, the run plane answers 403 and the card says so.

**Stream status.** The header shows the ambient stream's state:
- `connecting`;
- `open`, once the server has accepted the stream;
- `reconnecting` after a drop. The token stream retries every 2 s and replays
  from the last frame it saw (`forwardedProps.cao.since`); the browser's
  `EventSource` replays through `Last-Event-ID`. When the stream opens again,
  the dashboard lists interrupts again, so a prompt that started during the
  drop is shown;
- `refused` when the server rejects the stream or the browser gives up on it,
  which is not retried;
- `closed`.

## What the dashboard does

**Approvals.**

- A card is created only from the run plane's `interrupts[]`. An ambient
  `approval_card` frame never creates, resolves or changes a card, because any
  agent can emit one with `emit_ui`. When a frame claims an interrupt is
  resolved or expired, the card shows it as an unverified hint and the dashboard
  runs the run plane again to see what is actually open. A frame announcing an
  open interrupt the dashboard does not list yet also triggers that run. The run
  carries no `resume[]`, so a forged frame can cost a run but cannot answer
  anything.
- One run at a time. A refresh asked for while a run is in flight is not
  dropped: such requests are combined into one refresh after that run.
- **Edit is offered only when the interrupt's `responseSchema` has
  `editedArgs`.** Today that is Kiro `permission_request` and Codex
  `approval_request`. Claude Code's permission dialog offers approve and deny
  only.
- An Edit is one line of at most 4000 characters (code points, as the server's
  `len()` counts them), typed into the agent's prompt in place of yes or no. The
  dashboard refuses a line break, which the server would cut without notice, and
  longer text, which the server refuses with `RUN_ERROR`, stopping the batch. **Blank or whitespace-only text is refused here and
  nothing is sent**, because the server would apply it as an approve. Edit always
  sends `approved: true`.
- **Decisions are held until every open interrupt has one**, then sent as one
  run whose `resume[]` covers all of them, each entry with `status: "resolved"`.
  The UI says how many approvals still need a decision. Every run ends by
  checking whether the held decisions now cover every open interrupt, so a
  decision made during another run, or an interrupt that disappears, cannot
  leave the others held. If a ready batch is waiting on a run in flight, the
  page says so; the answers go out when that run ends.
- A `RUN_FINISHED` whose outcome is neither `success` nor `interrupt` (for
  example `cancelled`) is treated as a failure, never as a delivery.
- The result is read per interrupt id: listed in `skippedResume` means skipped,
  listed again as open means still pending, and otherwise delivered.
- After a run that carried decisions ends in `RUN_ERROR`, an HTTP error, a
  network error, a timeout, or with no terminal event, the dashboard shows the
  error as the server gave it, marks the sent cards unknown, and runs the run
  plane again with no `resume[]`. Ids still listed go back to pending; ids no
  longer listed show as "delivered (unconfirmed)". If that check fails too,
  both errors are shown and "Retry the check" runs it again.

**Generative UI.** Frames are listed in arrival order and deduped by
`metadata.cao.eventId`. Props are shown as text: the app has no `innerHTML`, no
`dangerouslySetInnerHTML` and no `eval`. A `RAW` frame with
`event.rejected_component`, or a `CUSTOM` frame naming a component outside the
six, shows as a refusal with the component name and sender and without props.
A refusal the server folded others into adds "And N more refused." Refused
entries are capped apart, at the newest 20 (`MAX_REFUSED_ENTRIES`), so a flood of
refusals never pushes a real component out.
The panel has no controls, not even on an `approval_card`. It keeps the newest
200 entries (`MAX_PANEL_ENTRIES` in `core/genui.ts`), so a long session does not
grow without limit.

**Session timeline.** One entry per orchestration call, keyed by `toolCallId`
and deduped by `metadata.cao.eventId` (`core/timeline.ts`). The label comes from
`metadata.cao.orchestrationType`, sender and receiver; the wire's `toolCallName`
is not used as a key. Only metadata is shown: no message text. A `TOOL_CALL_END`
carries one of three `closedBy` values:

- `completion`: the receiver's work ended (a terminal that is closed, too). The
  call is "completed".
- `superseded`: a newer call went to the same receiver, so the server ended this
  one. It is shown as "ended: superseded by a newer call to the same receiver".
- `stream_closed`: the stream itself stopped. It is ignored, because the work may
  still be running and the next stream announces the call again.

Any other value ends the call too, shown as "ended: <value>", so a value added
later cannot leave a finished call shown as running. An end for a call the panel
never saw is ignored, and a call is never reopened. The panel keeps 200 calls
(`MAX_TIMELINE_ENTRIES`), dropping the oldest finished one before any live one,
and caps each string at 200 characters.

After a reconnect (an overflow, or any drop), each call that was still open is
shown as "open; its end was not seen across a reconnect". Each new stream is a
fresh run on the server, and its replay does not close a call whose start came
before the replay window, so an end that fell inside the gap is not sent again.
The panel does not guess: such a call completes (or ends) if an end for it
arrives later, and otherwise stays unconfirmed. A call whose start arrives in the
replay is listed as open only if it was not already listed; a listed unconfirmed
call whose replay holds its start but no end stays unconfirmed. A superseded or
ended call is not marked unconfirmed: the server has ended it. The end lost across a reconnect gap: #1292.

**Routes.** The app requests `/agui/v1/run` and `/agui/v1/stream` and
nothing else; `core/routes.ts` refuses any other path. It never calls `emit_ui` and never reads terminal output.

## What the run plane cannot tell you

A delivered card means the server accepted a decision for that interrupt id. It
does not mean your decision was the one applied:

- If another client's answer for the same id is already being typed, yours joins
  it and the first decision wins.
- A second answer for an id that is already resolved is skipped quietly, with no
  `skippedResume` entry.
- An interrupt the server has already evicted shows as `skipped(unknown)`, even
  though it was delivered.
- After a partial multi-key delivery, a retry stays pinned to the first
  decision. Today only an OMP deny sends several keys.

So a card says "delivered (decision applied: not reported by the run plane)".

## L2 constructs only (#386 AC6)

This app reads AC6's "composed from L2 constructs only" as: a TypeScript client
consumes only the wire surfaces the L2 constructs produce. Those are the approval
lifecycle of `AgentHandoffWithApproval`, served by `POST /agui/v1/run`, and the
allow-listed `CUSTOM cao.generative_ui` carrier on `/agui/v1/stream`, and the
`TOOL_CALL_START` / `TOOL_CALL_END` frames on that stream that
`MultiAgentSessionTimeline` (`services/agui/session_timeline.py`) folds into its
entries. The construct also closes on `TOOL_CALL_RESULT`; the ambient stream never
sends one, so the app reads only the end. The app
adds no protocol code and changes nothing on the server. If maintainers read
AC6 differently, the app is the place to change.

## Tests

```bash
npm run test:dependencies   # exact pins, no install scripts, lockfile integrity
npm run typecheck
npm run test:coverage       # vitest; core/ held to 100% lines, branches, functions, statements
npm run build
npm run e2e                 # needs tmux and a CAO checkout with `uv sync --all-extras`
```

The unit tests replay frames captured from a real server (`fixtures/`).

`npm run e2e` (`e2e/run.sh`) starts its own `cao-server` on port 9896, with a
throwaway `HOME` and its own tmux server, and drives `core/` against it.
[`e2e/README.md`](e2e/README.md) says what it checks and how it is isolated.

One part is simulated: an agent showing a prompt. The harness types the
marker `APPROVAL_REQUIRED: <text>` into a `mock_cli` pane without Enter,
standing in for a CLI that prints a permission prompt and waits. Covered for
real are the approval bridge, classification, the run plane, resume, and key
delivery to the pane. The harness also calls `emit_ui` as an agent would, once
for each of the six components and once for an off-list component, which gets
HTTP 400. The components are read on both stream transports at once:
`EventSource`, and the token `POST`.

CI runs all of these in the **AG-UI operator dashboard** job, which installs
tmux.

**Live check, opt-in and manual (not in CI).** `e2e/live-claude-code.sh`
approves and denies one real Claude Code permission prompt through `core/`
and prints the run-plane evidence (#1156). It needs a logged-in Claude Code
and spends two model turns on your account, so only a person runs it, by hand:

```bash
./e2e/live-claude-code.sh --dry-run    # say what it would do; start nothing
./e2e/live-claude-code.sh              # run it (CAO_LIVE_PORT, default 9897)
```

Read its header first: it says what it touches, including your real Claude
Code login and `~/.claude`. It refuses port 9889 and any argument it does not
know. It drives `core/` headless, not the browser view. Codecov reads only the Python suite's `coverage.xml`, so it does not see
this TypeScript; the job's coverage floor is this app's coverage gate.
