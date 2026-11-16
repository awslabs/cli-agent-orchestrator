# CAO Dojo: live fleet view with an approval card you can answer

A **single HTML file with zero dependencies** (no npm, no framework, no build step). It
connects to your own local `cao-server`, shows the fleet and the six allow-listed
generative-UI components, and renders `approval_card` with working **Approve** and
**Reject** buttons. When an agent hits a permission prompt, you answer it here and CAO
types the answer into that agent's terminal.

It is built from [`../ag-ui-eventsource-viewer/index.html`](../ag-ui-eventsource-viewer/index.html)
and adds the answerable approval card described in #1109.

## Requirements

- A `cao-server` that includes the run-plane fix from #1107
  (`/agui/v1/run` always ends with `RUN_FINISHED` or `RUN_ERROR`), installed with the
  `[agui]` extra. Older servers keep the run stream open and the page waits for its
  150 s backstop.
- For a real permission prompt: Claude Code 2.1.310 or later, which CAO detects as a
  `claude-code:permission_request` interrupt (fixed by #1107).

## Run it

1. **Serve the page over http.** A `file://` page sends `Origin: null`, which CAO's
   Origin check rejects for the resume `POST`.

   ```bash
   cd examples/ag-ui/cao-dojo
   python3 -m http.server 8790 --bind 127.0.0.1
   ```

2. **Start `cao-server` with AG-UI on and the page's origin allowed.** Give it its own
   `CAO_HOME_DIR` and a port that is not your working fleet's, and keep it on loopback:
   `cao-server` runs commands for whoever can reach it. The port alone is not enough:
   `cao-server` locks `$CAO_HOME_DIR/server.lock`, so a second server on your default
   home refuses to start next to your fleet.

   ```bash
   export CAO_HOME_DIR="$(mktemp -d)"; echo "$CAO_HOME_DIR"   # reuse this in step 3
   CAO_AGUI_ENABLED=1 CAO_CORS_ORIGINS=http://localhost:8790 \
     cao-server --host 127.0.0.1 --port 9890
   ```

3. **Give an agent something to ask about, in a throwaway directory.** The bundled
   `dojo-approver` profile asks permission to write one file and does nothing else.

   Run this in a second shell with the **same** `CAO_HOME_DIR` as step 2, so the profile
   lands in that server's agent store and not in your real one.

   ```bash
   export CAO_HOME_DIR=<the directory step 2 printed>
   export CAO_API_HOST=127.0.0.1 CAO_API_PORT=9890
   cao install examples/ag-ui/cao-dojo/dojo-approver.md
   cao launch --agents dojo-approver --session-name dojo-demo \
     --working-directory "$(mktemp -d)" --headless --auto-approve --async "Do your task now."
   ```

   `--auto-approve` only skips `cao launch`'s own confirmation. The profile sets
   `permissionMode: default`, so the file write still prompts.

4. **Open** `http://localhost:8790/index.html`, set the endpoint to
   `http://127.0.0.1:9890`, and click **Connect**. When the agent's prompt is up
   (about 20 s), the card appears. If it does not, click **Send run**: the run answers
   with every open approval.

5. **Approve or Reject.** The page posts the decision and shows the run's result.
   Approve writes `approved-by-dojo.txt` in the agent's directory; Reject leaves none.

6. **Tear down.** `cao shutdown --session cao-dojo-demo`, then stop `cao-server` and the
   http server (Ctrl-C, or `kill <pid>`), and remove the `CAO_HOME_DIR` from step 2.

The endpoint field defaults to `http://localhost:9889`, CAO's default port. You can also
pass it in the URL: `index.html?endpoint=http://127.0.0.1:9890`. The page accepts a URL endpoint only for a loopback host (`127.0.0.1`, `localhost`, `[::1]`), so a crafted link cannot point it, and a token you type, at another server.

## What goes over the wire

| Transport | Request | Used for |
|---|---|---|
| Live (`EventSource`) | `GET /agui/v1/stream?since=<10 min ago>` | Fleet state and `CUSTOM` `cao.generative_ui` frames. `since` replays a card raised before the page opened. |
| Run (`fetch`, SSE body) | `POST /agui/v1/run` with a `RunAgentInput` that declares `protocolVersion: "1.0"` | Collects open approvals. Ends `RUN_FINISHED` with outcome `interrupt` (and the interrupts) or `success`. |
| Run, resuming | same, plus `resume: [{interruptId, status: "resolved", payload: {approved: true \| false}}, …]` | Answers the approvals of the last interrupted run. AG-UI 1.0 says the resume list MUST cover every interrupt of the run it continues, so when that run listed several, the page holds each decided card (`decided — waiting for your answer to N other prompt(s)`) and sends one resume once all of them have an answer. It never abandons a prompt for you: on CAO an abandoned entry is a deny. The resume goes on the interrupted run's `threadId`; a run that only collects starts a fresh thread, so it never continues past an interrupt it does not answer. Ends `RUN_FINISHED` once CAO has delivered it: outcome `success` when nothing else is waiting, or `interrupt` listing the approvals still open. Ends `RUN_ERROR` for a resume CAO cannot process (for example a bad payload or a failed delivery). An entry for an interrupt CAO did not raise, or one that has expired, is skipped with a server-side warning and the run proceeds. It is listed in `RUN_FINISHED.result.skippedResume`, and the page shows that card as `skipped` ("answer not used — this prompt was no longer pending"), not approved or rejected. |

The run list can also show two results that are not AG-UI events, both raised by the
page itself:

| Result | Meaning |
|---|---|
| `HTTP_ERROR` | The run never streamed: no endpoint set, a non-2xx answer (shown with its status and `detail`), a network or CORS failure, or no `RUN_FINISHED` within 150 s. |
| `NO_END` | The stream closed without `RUN_FINISHED` or `RUN_ERROR`. |

On either, or on `RUN_ERROR`, a card you just answered goes back to pending.

- The card shows the `approval_card` props `reason`, `provider`, `terminal_id`, `options`
  and `interrupt_id`, as plain text. CAO never puts the prompt body on the wire.
- `RUN_FINISHED` after a resume means the answer reached the terminal, not that the agent
  finished. A later prompt arrives as a new interrupt on the next run.
- The message box is sent as the run's user message. CAO's run plane uses the run to
  collect and answer approvals; it does not deliver that text to an agent.
- With auth enabled, the access token goes to the stream as `?access_token=` (EventSource
  cannot set headers) and to the run as `Authorization: Bearer`. A resume needs
  `cao:write`. The token is held in memory only.
- Caution: a token in the stream URL can be written to proxy and server access logs.
  Use a short-lived token.

## End-to-end check (local only)

`tools/e2e-local.sh` starts its own `cao-server` and static server, launches the
`dojo-approver` agent, and drives the page with Playwright through five phases:

- **page**: no server or agent. A scripted `EventSource` and a stubbed run check the
  page's own state handling: pending cards survive the 40-card cap, a card answered
  with no endpoint goes back to pending, an answered card stays answered when a
  reconnect replays it, a patch with a failing op changes nothing, a new connection
  clears the old server's state, a non-finite progress value is not shown, a
  transient stream error reads "reconnecting", and fleet snapshots and deltas still
  apply after a reconnect while a re-sent record is dropped, a run that lists a
  card's answer in `result.skippedResume` leaves it skipped, not approved or rejected,
  every run input declares `protocolVersion: "1.0"`, with two interrupts open one
  resume covering both is sent only after both are decided, on the interrupted run's
  thread, and a collecting run sent while a decision is held starts a fresh thread and
  keeps only the decisions its new interrupt list still names.
  Run it alone with
  `node examples/ag-ui/cao-dojo/tools/e2e-local.mjs page`;

- **approve**: the card appears, Approve posts the resume, the run ends `RUN_FINISHED`
  (success) and the file is written;
- **reject**: a fresh agent, Reject, `RUN_FINISHED` (success), no file;
- **none**: nothing pending, the run ends `RUN_FINISHED` (success) with no pending card,
  and a resume for an unknown interrupt is skipped, so that run also ends
  `RUN_FINISHED` (success) and its `result.skippedResume` lists the id;
- **skipped**: no agent. An `approval_card` for an id CAO never raised is sent through
  `POST /agui/v1/emit_ui` and arrives over the stream. Approve resumes that unknown id:
  the run ends `RUN_FINISHED` (success), `result.skippedResume` lists the id, and the
  card shows "answer not used — this prompt was no longer pending" in the `skipped`
  state, never approved or rejected. It needs only a `cao-server` with `[agui]`, so it
  can run alone: `PAGE_URL=… CAO_URL=… node examples/ag-ui/cao-dojo/tools/e2e-local.mjs skipped`.

```bash
(cd examples/ag-ui/cao-dojo/tools && npm install && npx playwright install chromium)
examples/ag-ui/cao-dojo/tools/e2e-local.sh
```

It needs `cao`, `cao-server` (with #1107 and `[agui]`) and a logged-in `claude`
on `PATH`, so it is not wired into CI. It refuses port 9889 and any port already in use.
It is isolated by default: unless you set `CAO_HOME_DIR` or `TMUX_TMPDIR`, it uses a
fresh CAO home (so the profile install never reaches your own store) and a private tmux
server, and removes both on exit. `E2E_SHOT_DIR`, `E2E_VIDEO_DIR`, `E2E_CAPTION` and
`E2E_HOLD_MS` record screenshots and video.

## Recorded trace and drift checks

`tools/record_trace.py` records what this page sees on the wire for three
captures, each produced by construction against its own isolated `cao-server`
(a fresh `CAO_HOME_DIR`, a private tmux server, loopback only):

| Capture | What happens | Run 1 | Run 2 |
|---|---|---|---|
| `none` | nothing is pending | `RUN_FINISHED` success | none |
| `approve` | an agent raises a permission prompt and the card appears | `RUN_FINISHED` interrupt | resume `approved: true`, `RUN_FINISHED` success |
| `reject` | the same, in a fresh session | `RUN_FINISHED` interrupt | resume `approved: false`, `RUN_FINISHED` success |

The recording is normalised by `tools/trace_scrub.py` (ids become `id-N`,
`terminal-N`, `session-N`, `profile-N` and `window-N`; timestamps come from a
synthetic clock), then checked by the typed leak rule in `tools/trace_leaks.py`
before anything is written. Run 2 must end clean: a `result.skippedResume`
fails the recording as the approval bridge's expiry race, never as drift.

| Mode | Agent | Credentials | Where it runs |
|---|---|---|---|
| `--live` | Claude Code, `dojo-approver.md` | a logged-in `claude` | local only; writes the published trace |
| `--mock` | `mock_cli`, `tools/dojo-trace-mock.md`, answering the scripted `__mock_approval__` prompt | none | CI (`AG-UI stock-client live (AC3)`), and locally |
| `--offline` | none: CAO's own producers, in-process, no server and no tmux | none | the unit tests |

| Gate | Where | Command |
|---|---|---|
| Leak rule (L1-L17: a mutation case and a near miss for each) | pre-commit, and the Unit Tests job | `uv run pytest test/examples/test_cao_dojo_trace_leaks.py` |
| Replay, tier 1 (SDK models, closed shape, lifecycle, cross-plane, upstream controls) | pre-push, and the Unit Tests job | `uv run pytest test/examples/test_cao_dojo_trace_replay.py test/examples/test_cao_dojo_trace_recorder.py` |
| Replay, tier 1 with upstream `enforceEvents` and `verifyEvents` | CI | `node examples/ag-ui/ag-ui-stock-client-live/verify-trace.mjs <trace.ndjson>...` |
| Keyless approve/reject/none e2e and tier 2 drift | CI | `uv run python examples/ag-ui/cao-dojo/tools/record_trace.py --mock --check` |
| Re-record debt | warns on PRs; blocks the release path | `make check-agui-trace-release` |

`make check-agui-trace` runs the offline gates in one command. Tier 2 compares
a recording with `trace/cao-dojo-trace.shape.json`, which carries no
provider-specific value, so a contributor without Claude Code credentials can
clear a deliberate frame change by regenerating it:

```bash
uv run python examples/ag-ui/cao-dojo/tools/record_trace.py --mock --write-shape
```

The published trace records the hash of the shape it was recorded against
(`_meta.shape_sha256`). When the shape moves on, the published trace is stale:
PRs get a warning and the release path is blocked until someone with Claude Code
runs `make record-agui-trace`. See [`trace/README.md`](trace/README.md) for the
current state of the published trace.

The recorder refuses port 9889 and any busy port; pass `--port` (default 19899).
It removes its isolated directories on success and keeps them, with the server
log, on failure. It runs tmux only against its own server, named with `-S`, and
never passes on `TMUX` or `TMUX_PANE`: inside tmux, `TMUX` overrides
`TMUX_TMPDIR`, so a bare `tmux kill-server` would end the caller's own tmux
server.

These tools live beside the page they record rather than in `scripts/`. The
release path depends on one of them, `record_trace.py --check-debt`, which needs
only the standard library and two committed files. The tests in `test/examples/`
exercise the tools; the mypy ratchet covers `src/` only, so it does not
type-check them.

## Not in this page yet

- A replay mode from a recorded trace, and a docs-site build.
- A browser test of either page in CI. Neither this page nor the
  [raw event inspector](#raw-event-inspector-inspectorhtml) has one: CI checks the trace the
  page receives (see the gates above), not the pages themselves. The inspector's browser test
  runs locally and wherever Playwright is installed.
- The "edit" option. CAO offers it, but this page sends only approve and reject.

## Raw event inspector (`inspector.html`)

A second single-file page, [`inspector.html`](inspector.html), lists every record of a
recorded `/agui/v1` trace in file order: capture, plane, direction, sequence, type, and
the frame `timestamp` (epoch milliseconds and UTC) with the record's JSON one click away.
It reads the committed [`trace/cao-dojo-trace.ndjson`](trace/cao-dojo-trace.ndjson) by
default, or a trace you pick from disk.

It is a reader of a file, not a client of CAO: no `cao-server`, no `CAO_AGUI_ENABLED`, no
route, no stream, no token, nothing written. Serve this directory and open it:

```bash
cd examples/ag-ui/cao-dojo
python3 -m http.server 8790 --bind 127.0.0.1
# http://localhost:8790/inspector.html?trace=trace/cao-dojo-trace.ndjson
```

Only the `CUSTOM` approval cards carry a frame `timestamp` in the published trace; every
other record shows `—` rather than an invented time. A request record (the
`RunAgentInput` the recorder posted) has no frame and shows the type `REQUEST`.

The `?trace=` path is resolved against the page and must stay on the page's origin and
under its directory: a backslash, any whitespace or control character, a scheme, a
protocol-relative `//host` or a `..` that climbs out is refused with a message and nothing
is fetched. The browser test tries eight such paths, including `\\evil.example/x` and
`ht<TAB>tp://evil.example/x`, and fails on any request to another origin.

`pytest test/examples/test_cao_dojo_inspector.py` checks the page statically (no
dependency, `textContent` only, no write path) and, with Playwright and a Chromium installed, opens it on
the published trace and compares every row with the `.ndjson`. Without Playwright the
browser test skips and the static check still runs.
