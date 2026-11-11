/**
 * Keyless end-to-end check: drives core/ headless against a real cao-server
 * with mock_cli terminals. Started by e2e/run.sh, which owns the server and
 * its private tmux socket. See e2e/README.md.
 *
 * SIMULATED: the agent showing a prompt. The harness types the marker
 * `APPROVAL_REQUIRED: <text>` into a mock_cli pane WITHOUT Enter, standing in
 * for a CLI that prints a permission prompt and waits. It also calls emit_ui as
 * an agent would. COVERED FOR REAL: the approval bridge, classification, the
 * run plane, resume, and key delivery to the pane. Everything the operator does
 * goes through createController(), the same code the view uses.
 */

import { execFileSync } from "node:child_process";
import assert from "node:assert/strict";
import { realpathSync } from "node:fs";
import { dirname, sep } from "node:path";
import { createController, emptyPanel, openAmbient, reduceFrame, ROUTES, ALLOWED_COMPONENTS, type Frame } from "../core/index.ts";

const BASE = process.env.CAO_E2E_BASE ?? "";
const SOCKET = process.env.CAO_E2E_TMUX_SOCKET ?? "";
const WORKDIR = process.env.CAO_E2E_WORKDIR ?? "";
assert.ok(BASE && SOCKET && WORKDIR, "run this through e2e/run.sh");
assert.notEqual(new URL(BASE).port, "9889", "refusing the default port 9889");

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const log = (msg: string) => console.log(`[e2e] ${msg}`);

// Every request the app makes goes through this fetch, so the route allow-list is checked on real traffic.
const appRequests: string[] = [];
const appFetch: typeof fetch = (input, init) => {
  appRequests.push(new URL(String(input)).pathname);
  return fetch(input, init);
};

// ── harness helpers (the "agent" side; never used by the app) ──────────────

/**
 * The only way this file runs tmux. It always names the private server's socket
 * with -S, runs with TMUX and TMUX_PANE removed, and refuses unless that socket
 * lives under this job's own TMUX_TMPDIR. When run through run.sh, which sets
 * TMUX_TMPDIR to its fresh scratch dir, that means it cannot reach any other
 * server; the check trusts TMUX_TMPDIR, so run directly it is only as good as
 * the environment it is given.
 */
function privateTmux(...args: string[]): string {
  const tmpdir = process.env.TMUX_TMPDIR ?? "";
  assert.ok(tmpdir, "TMUX_TMPDIR is not set; refusing to run tmux");
  const root = realpathSync(tmpdir);
  assert.notEqual(root, realpathSync("/tmp"), "TMUX_TMPDIR is the shared /tmp; refusing to run tmux");
  const socketDir = realpathSync(dirname(SOCKET));
  assert.ok(socketDir.startsWith(root + sep), `socket ${SOCKET} is not under TMUX_TMPDIR; refusing to run tmux`);
  const env = { ...process.env };
  delete env.TMUX;
  delete env.TMUX_PANE;
  return execFileSync("tmux", ["-S", SOCKET, ...args], { env, encoding: "utf8" });
}

async function newTerminal(session: string): Promise<{ id: string; session: string }> {
  const q = new URLSearchParams({ agent_profile: "developer", provider: "mock_cli", session_name: session, working_directory: WORKDIR });
  const res = await fetch(`${BASE}/sessions?${q}`, { method: "POST" });
  const text = await res.text();
  assert.equal(res.status, 201, `create session: ${res.status} ${text}`);
  const body = JSON.parse(text) as { id: string; session_name: string };
  await waitStatus(body.id, ["idle", "completed"]);
  return { id: body.id, session: body.session_name };
}

async function status(id: string): Promise<string> {
  const res = await fetch(`${BASE}/terminals/${id}`);
  return ((await res.json()) as { status: string }).status;
}

async function waitStatus(id: string, want: string[], timeoutMs = 15_000): Promise<string> {
  const end = Date.now() + timeoutMs;
  for (;;) {
    const s = await status(id);
    if (want.includes(s)) return s;
    if (Date.now() > end) throw new Error(`terminal ${id} is ${s}, wanted ${want.join("/")}`);
    await sleep(200);
  }
}

async function raisePrompt(t: { id: string; session: string }, text: string) {
  // No Enter: the marker stays on the prompt line, as a waiting CLI's prompt does.
  privateTmux("send-keys", "-t", t.session, "-l", `APPROVAL_REQUIRED: ${text}`);
  await waitStatus(t.id, ["waiting_user_answer"]);
}

function paneTail(t: { session: string }): string[] {
  // capture-pane's own -S is the start line (30 lines back), not a socket.
  return privateTmux("capture-pane", "-p", "-S", "-30", "-t", t.session)
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean);
}

async function emitUi(component: string, terminalId: string, props: object): Promise<{ status: number; body: unknown }> {
  const res = await fetch(`${BASE}/agui/v1/emit_ui`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ component, terminal_id: terminalId, props }),
  });
  return { status: res.status, body: await res.json() };
}

/** A worker the supervisor `caller` calls: an `assign` or `handoff` the ambient stream turns into a TOOL_CALL pair. */
async function callWorker(session: string, caller: string, kind: "assign" | "handoff"): Promise<string> {
  const q = new URLSearchParams({ agent_profile: "developer", provider: "mock_cli", caller_id: caller, defer_init: "true" });
  const res = await fetch(`${BASE}/sessions/${session}/terminals?${q}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ initial_message: "Do the assigned task.", initial_message_orchestration_type: kind }),
  });
  const text = await res.text();
  assert.equal(res.status, 201, `create worker: ${res.status} ${text}`);
  return (JSON.parse(text) as { id: string }).id;
}

/** mock_cli never finishes by itself: closing the worker ends its call, as the receiver's work ending does. */
async function closeWorker(id: string) {
  const res = await fetch(`${BASE}/terminals/${id}`, { method: "DELETE" });
  assert.equal(res.status, 200, `delete worker ${id}: ${res.status}`);
}

// ── the app ─────────────────────────────────────────────────────────────────
const ctl = createController({ base: BASE, threadId: "operator-dashboard-e2e", fetchImpl: appFetch, timeoutMs: 15_000 });

async function refreshUntil(count: number, timeoutMs = 10_000) {
  const end = Date.now() + timeoutMs;
  for (;;) {
    await ctl.refresh();
    const open = ctl.board.cards.filter((c) => c.state.s === "pending");
    if (open.length >= count) return open;
    if (Date.now() > end) throw new Error(`wanted ${count} open approvals, have ${open.length}`);
    await sleep(250);
  }
}

const runsSent = () => ctl.sent.length;

async function main() {
  // 1. Empty run.
  const empty = await ctl.refresh();
  assert.equal(empty?.kind, "SUCCESS");
  assert.equal(ctl.board.cards.length, 0);
  log("PASS empty run: RUN_FINISHED(success), no cards");

  const A = await newTerminal("od-e2e-a");
  log(`terminal A ${A.id}`);

  // 2. Approve.
  await raisePrompt(A, "Allow file write?");
  let [card] = await refreshUntil(1);
  assert.deepEqual(card.controls, ["approve", "deny"], "mock_cli offers approve/deny only");
  await ctl.decide(card.id, { kind: "approve" });
  assert.equal(ctl.board.cards.find((c) => c.id === card.id)?.state.s, "delivered");
  await waitStatus(A.id, ["completed", "idle"]);
  assert.ok(paneTail(A).some((l) => l.endsWith("MOCK: y")), "A received y");
  log(`PASS approve: ${card.id} delivered, pane received "y"`);

  // 3. Deny.
  await raisePrompt(A, "Delete the build cache?");
  [card] = await refreshUntil(1);
  await ctl.decide(card.id, { kind: "deny" });
  assert.equal(ctl.board.cards.find((c) => c.id === card.id)?.state.s, "delivered");
  await waitStatus(A.id, ["completed", "idle"]);
  assert.ok(paneTail(A).some((l) => l.endsWith("MOCK: n")), "A received n");
  log(`PASS deny: ${card.id} delivered, pane received "n"`);

  // 4. Edit is refused in the client where the prompt does not offer it; nothing is sent.
  await raisePrompt(A, "Allow network?");
  [card] = await refreshUntil(1);
  const before = runsSent();
  await ctl.decide(card.id, { kind: "edit", text: "   " });
  assert.equal(runsSent(), before, "no run was sent");
  assert.deepEqual(ctl.board.cards.find((c) => c.id === card.id)?.state, { s: "refused", reason: "this prompt does not accept edited text" });
  assert.equal(await status(A.id), "waiting_user_answer", "the agent is still waiting");
  await ctl.decide(card.id, { kind: "deny" });
  await waitStatus(A.id, ["completed", "idle"]);
  log("PASS edit refused in the client: no run sent, agent still waiting until denied");

  // 5. Two interrupts, batched.
  const B = await newTerminal("od-e2e-b");
  log(`terminal B ${B.id}`);
  await raisePrompt(A, "Run the migration?");
  await raisePrompt(B, "Overwrite config?");
  const open = await refreshUntil(2);
  const cardA = open.find((c) => c.terminalId === A.id)!;
  const cardB = open.find((c) => c.terminalId === B.id)!;
  const sentBefore = runsSent();
  await ctl.decide(cardA.id, { kind: "approve" });
  assert.equal(runsSent(), sentBefore, "held: nothing sent with one of two decided");
  assert.equal(ctl.board.cards.find((c) => c.id === cardA.id)?.state.s, "held");
  await ctl.decide(cardB.id, { kind: "deny" });
  assert.equal(runsSent(), sentBefore + 1, "one run for both");
  assert.deepEqual([...ctl.sent.at(-1)!].sort(), [cardA.id, cardB.id].sort());
  await waitStatus(A.id, ["completed", "idle"]);
  await waitStatus(B.id, ["completed", "idle"]);
  assert.ok(paneTail(A).some((l) => l.endsWith("MOCK: y")) && paneTail(B).some((l) => l.endsWith("MOCK: n")));
  log("PASS two interrupts: held until both decided, one resume[] with both, A got y and B got n");

  // 6. The six components from real emit_ui calls, and the off-list call's 400, read on both ambient
  //    transports at once: EventSource on GET (no token), and fetch on POST with a token.
  const esFrames: Frame[] = [];
  const esStatus: string[] = [];
  const ambientEs = openAmbient({
    base: BASE,
    threadId: "operator-dashboard-e2e-ambient",
    onFrame: (f) => {
      esFrames.push(f);
      void ctl.ambient(f);
    },
    onStatus: (st) => esStatus.push(st),
  });
  let tokenPanel = emptyPanel();
  let tokenFrames = 0;
  const tokenStatus: string[] = [];
  const ambientToken = openAmbient({
    base: BASE,
    threadId: "operator-dashboard-e2e-ambient-token",
    // Auth is off on this server, so any token is accepted; what matters is the POST transport.
    token: "e2e-token",
    fetchImpl: appFetch,
    onFrame: (f) => {
      tokenFrames += 1;
      tokenPanel = reduceFrame(tokenPanel, f);
    },
    onStatus: (st) => tokenStatus.push(st),
  });
  const until = async (ok: () => boolean, what: string, ms = 10_000) => {
    const stop = Date.now() + ms;
    while (!ok()) {
      if (Date.now() > stop) throw new Error(`timed out waiting for ${what}`);
      await sleep(100);
    }
  };
  await until(() => esStatus.includes("open") && tokenStatus.includes("open"), "both streams to open");
  for (const component of ALLOWED_COMPONENTS) {
    const r = await emitUi(component, A.id, { label: `e2e ${component}`, note: "<b>not markup</b>" });
    assert.equal(r.status, 200, `${component}: ${JSON.stringify(r.body)}`);
  }
  const offList = await emitUi("iframe_embed", A.id, { src: "x" });
  assert.equal(offList.status, 400);
  assert.match((offList.body as { detail: string }).detail, /^Unknown UI component 'iframe_embed'/);
  const mineIn = (entries: typeof ctl.panel.entries) =>
    entries.filter((e) => e.kind === "component" && e.owner.sender === A.id && String(e.props.label).startsWith("e2e "));
  await until(() => mineIn(ctl.panel.entries).length >= 6 && mineIn(tokenPanel.entries).length >= 6, "six components on both streams");
  // Settle: give a refused call time to arrive, were it ever to reach the stream, before asserting it did not.
  await sleep(2000);
  ambientEs.close();
  ambientToken.close();
  await ambientToken.done;
  for (const [name, entries] of [["EventSource", ctl.panel.entries], ["token POST", tokenPanel.entries]] as const) {
    const seen = mineIn(entries).map((e) => (e.kind === "component" ? e.component : "")).sort();
    assert.deepEqual(seen, [...ALLOWED_COMPONENTS], `${name}: ${seen.join(",")}`);
    assert.ok(mineIn(entries).every((e) => e.owner.subagentRunId === `terminal:${A.id}`), `${name}: attribution`);
    assert.ok(!entries.some((e) => e.component === "iframe_embed"), `${name}: the refused call reached the stream`);
  }
  assert.deepEqual(tokenStatus, ["connecting", "open", "closed"]);
  log(`PASS six components from real emit_ui calls on the ambient stream, EventSource (${esFrames.length} frames)`);
  log(`PASS the same six on the token POST stream (${tokenFrames} frames; status ${tokenStatus.join(" → ")})`);
  log('PASS off-list emit_ui: HTTP 400 "Unknown UI component \'iframe_embed\'", and nothing on either stream after a 2s settle');

  // 6b. A token stream that drops replays what it missed (#1248 review, P3-2). The first request
  //     asks the server to end the stream after 2 s (forwardedProps.cao.maxSeconds); the client
  //     then waits 4 s before reconnecting. A component emitted in that gap must still arrive,
  //     through forwardedProps.cao.since on the reconnect, and the reopen must be reported.
  const replayBodies: Array<Record<string, unknown>> = [];
  const droppingFetch: typeof fetch = (input, init) => {
    const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
    if (replayBodies.length === 0) body.forwardedProps = { cao: { maxSeconds: 2 } };
    replayBodies.push(body);
    return appFetch(input, { ...init, body: JSON.stringify(body) });
  };
  let replayPanel = emptyPanel();
  let reopened = 0;
  const replayStatus: string[] = [];
  const ambientReplay = openAmbient({
    base: BASE,
    threadId: "operator-dashboard-e2e-ambient-replay",
    token: "e2e-token",
    fetchImpl: droppingFetch,
    retryMs: 4000,
    onFrame: (f) => {
      replayPanel = reduceFrame(replayPanel, f);
    },
    onReopen: () => {
      reopened += 1;
    },
    onStatus: (st) => replayStatus.push(st),
  });
  await until(() => replayStatus.includes("open"), "the replay stream to open");
  // A frame with a timestamp before the drop, so the reconnect has a cursor to send.
  assert.equal((await emitUi("progress", A.id, { label: "e2e before the drop" })).status, 200);
  await until(() => replayStatus.includes("reconnecting"), "the server to end the first stream", 10_000);
  assert.equal((await emitUi("metric", A.id, { label: "e2e during the drop" })).status, 200);
  // Structural: the component was emitted while no stream was open, so it can only arrive by replay.
  assert.equal(replayBodies.length, 1, "the reconnect had already started when the component was emitted");
  const missed = () => replayPanel.entries.some((e) => e.kind === "component" && e.props.label === "e2e during the drop");
  await until(missed, "the component emitted during the drop to arrive after the reconnect", 15_000);
  ambientReplay.close();
  await ambientReplay.done;
  const since = (replayBodies[1]?.forwardedProps as { cao?: { since?: string } } | undefined)?.cao?.since;
  assert.ok(since && !Number.isNaN(Date.parse(since)), `the reconnect body carried no forwardedProps.cao.since: ${JSON.stringify(replayBodies[1])}`);
  assert.equal(reopened, 1);
  log(`PASS the token stream replays across a drop: the reconnect sent forwardedProps.cao.since=${since}, the component emitted during the drop arrived, and the reopen was reported once`);

  // 6c. A token stream that stops reading overflows the server's per-subscriber queue (256
  //     events; sse_bus.SSE_MAX_QUEUE_SIZE) and is ended with RUN_ERROR cao.stream_overflow. The
  //     client reconnects with forwardedProps.cao.since. The client "stops reading" because the
  //     fetch it is given holds the first response body back: nothing is pulled from the socket
  //     until the gate opens, so the server's writes back up. How much the socket buffers absorb
  //     before the queue fills depends on the OS (about 340 events of 7 KB on macOS; Linux
  //     loopback buffers hold several MB), so FILL is sized to overflow on both.
  //     After the fill, and so after the overflow: the second call (its start is dropped), the
  //     first worker's end, and MARK markers. The replay ring holds the newest 500 records
  //     (event_log_service.RING_CAPACITY), so fillers older than that are not replayed; the
  //     markers and both calls are among the newest and must be. Asserted: the overflow, the
  //     dropped start recovered, every marker once, no filler or call twice, and the call held
  //     across the gap listed once as completed or unconfirmed.
  const FILL = 3000;
  const MARK = 20;
  let releaseGate: () => void = () => {};
  let gate: Promise<void> = Promise.resolve();
  const overflowBodies: Array<Record<string, unknown>> = [];
  const gatedFetch: typeof fetch = async (input, init) => {
    overflowBodies.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
    const res = await appFetch(input, init);
    if (overflowBodies.length > 1 || !res.body) return res; // only the first stream is held back
    const reader = res.body.getReader();
    const held = new ReadableStream<Uint8Array>(
      {
        async pull(controller) {
          await gate;
          const chunk = await reader.read();
          if (chunk.done) controller.close();
          else controller.enqueue(chunk.value);
        },
        cancel: (reason) => reader.cancel(reason),
      },
      { highWaterMark: 0 },
    );
    return new Response(held, { status: res.status, headers: res.headers });
  };
  const flood = createController({ base: BASE, threadId: "operator-dashboard-e2e-overflow", fetchImpl: appFetch });
  const floodFrames: Frame[] = [];
  const floodStatus: string[] = [];
  let floodReopened = 0;
  const ambientFlood = openAmbient({
    base: BASE,
    threadId: "operator-dashboard-e2e-overflow",
    token: "e2e-token",
    fetchImpl: gatedFetch,
    retryMs: 500,
    onFrame: (f) => {
      floodFrames.push(f);
      void flood.ambient(f);
    },
    // Wired as view/Root.tsx wires it.
    onReopen: () => {
      floodReopened += 1;
      flood.reconnected();
    },
    onStatus: (st) => floodStatus.push(st),
  });
  await until(() => floodStatus.includes("open"), "the overflow stream to open");
  const workerOne = await callWorker(A.session, A.id, "assign");
  await until(() => flood.timeline.entries.some((e) => e.receiver === workerOne && e.state === "open"), "the first call to reach the client");
  gate = new Promise<void>((resolve) => (releaseGate = resolve)); // from here the client reads nothing
  const floodPad = "x".repeat(7500); // emit_ui refuses props over 8 KB
  for (let i = 0; i < FILL; i += 1) {
    const r = await emitUi("progress", A.id, { label: `e2e fill ${i}`, note: floodPad });
    assert.equal(r.status, 200, `fill ${i}: ${JSON.stringify(r.body)}`);
  }
  // After the queue is full: a call whose start the held stream cannot carry, the end of call 1,
  // and the markers.
  const workerTwo = await callWorker(A.session, A.id, "handoff");
  await closeWorker(workerOne);
  for (let i = 0; i < MARK; i += 1) {
    const r = await emitUi("metric", A.id, { label: `e2e mark ${i}` });
    assert.equal(r.status, 200, `mark ${i}: ${JSON.stringify(r.body)}`);
  }
  assert.ok(!floodFrames.some((f) => f.type === "RUN_ERROR"), "nothing has been read yet");
  releaseGate();
  const labelsWith = (prefix: string) =>
    floodFrames.flatMap((f) => {
      const value = f.type === "CUSTOM" ? (f.value as { props?: { label?: unknown } } | undefined) : undefined;
      const label = value?.props?.label;
      return typeof label === "string" && label.startsWith(prefix) ? [label] : [];
    });
  const marksSeen = () => new Set(labelsWith("e2e mark ")).size;
  const floodDone = () => floodReopened > 0 && marksSeen() >= MARK && flood.timeline.entries.some((e) => e.receiver === workerTwo);
  try {
    await until(floodDone, "the resync to deliver the dropped start and every marker", 60_000);
  } catch (error) {
    const detail = {
      status: floodStatus,
      frames: floodFrames.length,
      runError: floodFrames.find((f) => f.type === "RUN_ERROR"),
      requests: overflowBodies.length,
      fill: new Set(labelsWith("e2e fill ")).size,
      marks: `${marksSeen()}/${MARK}`,
      timeline: flood.timeline.entries.map((e) => [e.receiver, e.orchestrationType, e.state]),
    };
    throw new Error(`${error instanceof Error ? error.message : error}: ${JSON.stringify(detail)}`);
  }
  await sleep(1000); // settle: anything else the replay or the live stream sends arrives first
  ambientFlood.close();
  await ambientFlood.done;
  const overflowError = floodFrames.find((f) => f.type === "RUN_ERROR");
  assert.equal(overflowError?.code, "cao.stream_overflow", `the held stream was not ended by an overflow: ${JSON.stringify(overflowError)}`);
  const floodSince = (overflowBodies[1]?.forwardedProps as { cao?: { since?: string } } | undefined)?.cao?.since;
  assert.ok(floodSince && !Number.isNaN(Date.parse(floodSince)), `the reconnect carried no since: ${JSON.stringify(overflowBodies[1])}`);
  assert.equal(floodReopened, 1, "the reopen is reported once");
  const fills = labelsWith("e2e fill ");
  assert.equal(new Set(fills).size, fills.length, "a filler arrived twice");
  const marks = labelsWith("e2e mark ");
  assert.equal(marks.length, MARK, `every marker once: ${marks.length}`);
  assert.equal(new Set(marks).size, MARK, "a marker arrived twice");
  const ids = flood.timeline.entries.map((e) => e.toolCallId);
  assert.equal(new Set(ids).size, ids.length, `a call is listed twice: ${ids.join(",")}`);
  const one = flood.timeline.entries.filter((e) => e.receiver === workerOne);
  const two = flood.timeline.entries.filter((e) => e.receiver === workerTwo);
  assert.equal(one.length, 1, "the call held across the gap is listed once");
  assert.ok(["completed", "unconfirmed"].includes(one[0].state), `the held call is ${one[0].state}`);
  assert.equal(two.length, 1, "the call whose start was dropped is recovered once");
  assert.equal(two[0].orchestrationType, "handoff");
  const startFrames = floodFrames.filter((f) => f.type === "TOOL_CALL_START").length;
  log(
    `PASS overflow then resync: RUN_ERROR cao.stream_overflow after the held stream was read, the reconnect sent since=${floodSince}, ` +
      `${fills.length}/${FILL} fillers (none twice) and all ${MARK} markers once (counted in frames), the dropped start was recovered (call 2: ${two[0].state}), ` +
      `the held call is listed once as ${one[0].state}, and ${startFrames} TOOL_CALL_START frames gave 2 entries`,
  );

  // 7. Routes.
  const allowed = new Set<string>(Object.values(ROUTES));
  const outside = appRequests.filter((p) => !allowed.has(p));
  assert.deepEqual(outside, [], `the app requested ${outside.join(", ")}`);
  log(`PASS the app requested only ${[...new Set(appRequests)].sort().join(", ")} (${appRequests.length} requests), plus the EventSource stream`);
  log("ALL PASS");
}

main().catch((error) => {
  console.error(`[e2e] FAIL: ${error instanceof Error ? error.stack : error}`);
  process.exit(1);
});
