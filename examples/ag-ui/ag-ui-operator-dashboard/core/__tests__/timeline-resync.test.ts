import { describe, expect, it } from "vitest";
import { openAmbient, type AmbientStatus, type EventSourceLike } from "../ambient.ts";
import { createController } from "../controller.ts";
import type { Frame } from "../types.ts";

const BASE = "http://127.0.0.1:9894";
const sse = (...frames: object[]) => frames.map((f) => `data: ${JSON.stringify(f)}\n\n`).join("");

const start = (id: string, ts: number): Frame => ({
  type: "TOOL_CALL_START",
  timestamp: ts,
  metadata: { cao: { eventId: id, sender: "sup", receiver: `w-${id}`, orchestrationType: "assign" } },
  subagentRunId: "terminal:sup",
  toolCallId: id,
  toolCallName: "OrchestrationType.ASSIGN",
});
const end = (id: string, ts: number): Frame => ({
  type: "TOOL_CALL_END",
  timestamp: ts,
  metadata: { cao: { closedBy: "completion" } },
  subagentRunId: "terminal:sup",
  toolCallId: id,
});
// The server's RUN_ERROR carries no timestamp (RunErrorEvent(message, code) in ambient_run.py).
const OVERFLOW: Frame = { type: "RUN_ERROR", code: "cao.stream_overflow", message: "event buffer overflowed; reconnect to replay" };

/**
 * A scripted cao-server for the ambient token stream, shaped like the measured one.
 * Stream 1 delivers `first`, then the overflow RUN_ERROR, and ends. Stream 2 (with
 * `forwardedProps.cao.since`) delivers `replay` and then `later`, and stays open. As on
 * the real server, the replay does NOT close a call whose start came before the cursor:
 * the end that fell in the gap is simply absent from it.
 */
function server(first: Frame[], replay: Frame[], later: Frame[] = []) {
  const bodies: Array<Record<string, unknown>> = [];
  const fetchImpl = (async (_url: string | URL | Request, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
    bodies.push(body);
    const encoder = new TextEncoder();
    if (bodies.length === 1) {
      return new Response(new Blob([encoder.encode(sse({ type: "RUN_STARTED" }, ...first, OVERFLOW))]).stream(), { status: 200 });
    }
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode(sse({ type: "RUN_STARTED" }, ...replay)));
        if (later.length) setTimeout(() => controller.enqueue(encoder.encode(sse(...later))), 20);
        init?.signal?.addEventListener("abort", () => controller.error(new DOMException("aborted", "AbortError")));
      },
    });
    return new Response(stream, { status: 200 });
  }) as typeof fetch;
  return { fetchImpl, bodies };
}

type Ctl = ReturnType<typeof createController>;

async function untilTrue(ok: () => boolean, what: () => string) {
  const stop = Date.now() + 5_000;
  while (!ok()) {
    if (Date.now() > stop) throw new Error(`timed out: ${what()}`);
    await new Promise((r) => setTimeout(r, 2));
  }
}

async function runPost(first: Frame[], replay: Frame[], later: Frame[], done: (ctl: Ctl) => boolean) {
  const { fetchImpl, bodies } = server(first, replay, later);
  const ctl = createController({ base: BASE, threadId: "t", fetchImpl });
  const frames: Frame[] = [];
  const statuses: AmbientStatus[] = [];
  // Wired the way view/Root.tsx wires it.
  const handle = openAmbient({
    base: BASE,
    threadId: "t",
    token: "k",
    fetchImpl,
    retryMs: 1,
    onFrame: (f) => {
      frames.push(f);
      void ctl.ambient(f);
    },
    onReopen: () => ctl.reconnected(),
    onStatus: (s) => statuses.push(s),
  });
  await untilTrue(() => done(ctl), () => JSON.stringify(ctl.timeline.entries.map((e) => [e.toolCallId, e.state])));
  handle.close();
  await handle.done;
  return { ctl, frames, statuses, bodies };
}

const states = (ctl: Ctl) => ctl.timeline.entries.map((e) => [e.toolCallId, e.state]);

describe("POST transport: overflow, then a since resync that cannot close a call held across the gap", () => {
  // A and B reach the client. In the gap: A's end and C's start. The replay (since = B) repeats B
  // and adds C, but carries no end for A: its start predates the cursor.
  const first = [start("A", 1_000), start("B", 2_000)];
  const replay = [start("B", 2_000), start("C", 3_000)];

  it("lists each call once, marks the held calls unconfirmed, and the missed start as open", async () => {
    const { ctl, frames, statuses, bodies } = await runPost(first, replay, [], (c) => c.timeline.entries.length >= 3);
    expect(frames.some((f) => f.type === "RUN_ERROR" && f.code === "cao.stream_overflow")).toBe(true);
    expect(statuses).toEqual(["connecting", "open", "reconnecting", "open", "closed"]);
    expect(bodies.map((b) => (b.forwardedProps as { cao?: { since?: string } } | undefined)?.cao?.since)).toEqual([
      undefined,
      new Date(2_000).toISOString(),
    ]);
    // B arrived twice on the wire and is listed once.
    expect(frames.filter((f) => f.type === "TOOL_CALL_START").map((f) => f.toolCallId)).toEqual(["A", "B", "B", "C"]);
    expect(states(ctl)).toEqual([["A", "unconfirmed"], ["B", "unconfirmed"], ["C", "open"]]);
    const ids = ctl.timeline.entries.map((e) => e.toolCallId);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("a later end closes an unconfirmed call", async () => {
    const { ctl } = await runPost(first, replay, [end("A", 4_000)], (c) => c.timeline.entries[0]?.state === "completed");
    expect(states(ctl)).toEqual([["A", "completed"], ["B", "unconfirmed"], ["C", "open"]]);
    expect(ctl.timeline.entries[0].endedAt).toBe(4_000);
  });

  it("a call completed before the drop stays completed", async () => {
    const { ctl } = await runPost([start("A", 1_000), end("A", 1_500)], [], [start("D", 5_000)], (c) => c.timeline.entries.length >= 2);
    expect(states(ctl)).toEqual([["A", "completed"], ["D", "open"]]);
  });
});

describe("EventSource transport: the browser reconnects, onopen runs again, and onReopen marks the held calls", () => {
  class FakeEventSource implements EventSourceLike {
    static last: FakeEventSource;
    readyState = 0;
    onopen: ((event: unknown) => void) | null = null;
    onmessage: ((event: { data: string }) => void) | null = null;
    onerror: ((event: unknown) => void) | null = null;
    constructor(_url: string) {
      FakeEventSource.last = this;
    }
    close() {
      this.readyState = 2;
    }
  }
  const send = (es: FakeEventSource, frame: Frame) => es.onmessage!({ data: JSON.stringify(frame) });

  it("the same states as on POST, across a drop that keeps the browser reconnecting", async () => {
    const ctl = createController({ base: BASE, threadId: "t", fetchImpl: (async () => new Response("")) as typeof fetch });
    const statuses: AmbientStatus[] = [];
    const handle = openAmbient({
      base: BASE,
      threadId: "t",
      eventSourceImpl: FakeEventSource,
      onFrame: (f) => void ctl.ambient(f),
      onReopen: () => ctl.reconnected(),
      onStatus: (s) => statuses.push(s),
    });
    const es = FakeEventSource.last;
    es.readyState = 1;
    es.onopen!({});
    send(es, start("A", 1_000));
    send(es, OVERFLOW);
    // The browser keeps the source and reconnects (readyState CONNECTING), then opens again.
    es.readyState = 0;
    es.onerror!({});
    es.readyState = 1;
    es.onopen!({});
    send(es, start("A", 1_000)); // a Last-Event-ID replay may repeat the start
    send(es, start("C", 3_000));
    await Promise.resolve();
    expect(statuses).toEqual(["connecting", "open", "reconnecting", "open"]);
    expect(states(ctl)).toEqual([["A", "unconfirmed"], ["C", "open"]]);
    send(es, end("A", 4_000));
    await Promise.resolve();
    expect(states(ctl)).toEqual([["A", "completed"], ["C", "open"]]);
    handle.close();
  });

  it("the first open is not a reconnect: nothing is marked", async () => {
    const ctl = createController({ base: BASE, threadId: "t", fetchImpl: (async () => new Response("")) as typeof fetch });
    openAmbient({ base: BASE, threadId: "t", eventSourceImpl: FakeEventSource, onFrame: (f) => void ctl.ambient(f), onReopen: () => ctl.reconnected() });
    const es = FakeEventSource.last;
    send(es, start("A", 1_000));
    es.readyState = 0;
    es.onerror!({}); // fails while connecting, before any open
    es.readyState = 1;
    es.onopen!({});
    await Promise.resolve();
    expect(states(ctl)).toEqual([["A", "open"]]);
  });
});
