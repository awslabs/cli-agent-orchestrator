import { describe, expect, it, vi } from "vitest";
import { openAmbient, parseData, SINCE_MARGIN_MS, type EventSourceLike } from "../ambient.ts";
import type { Frame } from "../types.ts";
import { fakeFetch, fixtureText, sse } from "./helpers.ts";

const BASE = "http://127.0.0.1:9894";

class FakeEventSource implements EventSourceLike {
  static last: FakeEventSource;
  readyState = 0;
  onopen: ((event: unknown) => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  closed = false;
  url: string;
  constructor(url: string) {
    this.url = url;
    FakeEventSource.last = this;
  }
  close() {
    this.closed = true;
    this.readyState = 2;
  }
}

describe("parseData", () => {
  it("keeps frames, drops the rest", () => {
    expect(parseData('{"type":"X"}')).toEqual({ type: "X" });
    expect(parseData("nope")).toBeNull();
    expect(parseData("1")).toBeNull();
  });
});

describe("openAmbient without a token: EventSource on GET", () => {
  it("is 'open' only on the real open event, then forwards frames", async () => {
    const frames: Frame[] = [];
    const status: string[] = [];
    const handle = openAmbient({
      base: BASE,
      threadId: "dash",
      onFrame: (f) => frames.push(f),
      onStatus: (s) => status.push(s),
      eventSourceImpl: FakeEventSource,
    });
    const es = FakeEventSource.last;
    expect(es.url).toBe(`${BASE}/agui/v1/stream?threadId=dash`);
    expect(status).toEqual(["connecting"]);
    es.readyState = 1;
    es.onopen!({});
    es.onmessage!({ data: '{"type":"RUN_STARTED"}' });
    es.onmessage!({ data: "garbage" });
    es.readyState = 0;
    es.onerror!({});
    handle.close();
    await handle.done;
    expect(frames).toEqual([{ type: "RUN_STARTED" }]);
    expect(status).toEqual(["connecting", "open", "reconnecting", "closed"]);
    expect(es.closed).toBe(true);
  });

  it("P3-2: an EventSource opened again after an accepted stream dropped calls onReopen; the first open does not", () => {
    let reopened = 0;
    const handle = openAmbient({
      base: BASE,
      threadId: "dash",
      onFrame: () => {},
      onReopen: () => (reopened += 1),
      eventSourceImpl: FakeEventSource,
    });
    const es = FakeEventSource.last;
    es.onopen!({});
    expect(reopened).toBe(0);
    es.readyState = 0;
    es.onerror!({});
    es.onopen!({});
    expect(reopened).toBe(1);
    es.onopen!({}); // a second open without a drop in between is not a reopen
    expect(reopened).toBe(1);
    handle.close();
  });

  it("P3-2: an EventSource that fails while connecting and then opens for the first time is not a reopen", () => {
    let reopened = 0;
    const handle = openAmbient({
      base: BASE,
      threadId: "dash",
      onFrame: () => {},
      onReopen: () => (reopened += 1),
      eventSourceImpl: FakeEventSource,
    });
    const es = FakeEventSource.last;
    es.readyState = 0; // still CONNECTING: the browser retries on its own
    es.onerror!({});
    es.onopen!({});
    expect(reopened).toBe(0);
    handle.close();
  });

  it("a stream the browser gives up on is 'refused', not endless reconnecting", () => {
    const seen: Array<[string, string | undefined]> = [];
    openAmbient({ base: BASE, threadId: "t", onFrame: () => {}, onStatus: (s, d) => seen.push([s, d]), eventSourceImpl: FakeEventSource });
    const es = FakeEventSource.last;
    es.readyState = 2;
    es.onerror!({});
    expect(seen).toEqual([
      ["connecting", undefined],
      ["refused", "the server refused the stream (or this origin is not allowed)"],
    ]);
  });

  it("uses the global EventSource, and says so when there is none", () => {
    const g = globalThis as { EventSource?: unknown };
    const saved = g.EventSource;
    try {
      g.EventSource = FakeEventSource;
      openAmbient({ base: BASE, threadId: "t", onFrame: () => {} }).close();
      expect(FakeEventSource.last.url).toContain("/agui/v1/stream");
      delete g.EventSource;
      expect(() => openAmbient({ base: BASE, threadId: "t", onFrame: () => {} })).toThrow(/EventSource is not available/);
    } finally {
      g.EventSource = saved;
    }
  });
});

describe("openAmbient with a token: fetch on POST", () => {
  it("sends Authorization in a header, never in the URL, and reads every frame", async () => {
    const { fetch, calls } = fakeFetch([{ sse: fixtureText("ambient.sse"), chunks: 7 }, { status: 404, json: {} }]);
    const frames: Frame[] = [];
    const status: string[] = [];
    const handle = openAmbient({ base: BASE, threadId: "dash", token: "tok", onFrame: (f) => frames.push(f), onStatus: (s) => status.push(s), fetchImpl: fetch, retryMs: 1 });
    await handle.done;
    expect(calls[0].url).toBe(`${BASE}/agui/v1/stream`);
    expect(calls[0].url).not.toContain("tok");
    expect(calls[0].headers.authorization).toBe("Bearer tok");
    expect(calls[0].body).toMatchObject({ threadId: "dash", messages: [], protocolVersion: "1.0" });
    expect(frames[0].type).toBe("RUN_STARTED");
    expect(frames.at(-1)!.type).toBe("RUN_FINISHED");
    expect(status).toEqual(["connecting", "open", "reconnecting", "refused"]);
  });

  it("reconnects after the stream ends and after a network error, with a new run id each time", async () => {
    const { fetch, calls } = fakeFetch([
      { sse: sse({ type: "RUN_STARTED" }) },
      { throws: new TypeError("Failed to fetch") },
      { sse: sse({ type: "CUSTOM", name: "x" }) },
      { hang: true },
    ]);
    const frames: string[] = [];
    const seen: Array<[string, string | undefined]> = [];
    const handle = openAmbient({
      base: BASE,
      threadId: "t",
      token: "k",
      onFrame: (f) => frames.push(f.type),
      onStatus: (s, d) => seen.push([s, d]),
      fetchImpl: fetch,
      retryMs: 1,
    });
    while (calls.length < 4) await new Promise((r) => setTimeout(r, 2));
    handle.close();
    await handle.done;
    expect(frames).toEqual(["RUN_STARTED", "CUSTOM"]);
    expect(seen).toEqual([
      ["connecting", undefined],
      ["open", undefined],
      ["reconnecting", "the stream ended"],
      ["reconnecting", "Failed to fetch"],
      ["open", undefined],
      ["reconnecting", "the stream ended"],
      ["closed", undefined],
    ]);
    const runIds = calls.map((c) => (c.body as { runId: string }).runId);
    expect(new Set(runIds).size).toBe(4);
  });

  it("P3-2: each reconnect replays from the last frame seen, and a reopen lists interrupts again", async () => {
    const { fetch, calls } = fakeFetch([
      { sse: sse({ type: "RUN_STARTED" }, { type: "CUSTOM", name: "x", timestamp: 1_793_994_274_723 }) },
      { throws: new TypeError("Failed to fetch") },
      { sse: sse({ type: "RUN_STARTED" }, { type: "CUSTOM", name: "y", timestamp: 1_793_994_280_001 }) },
      { hang: true },
    ]);
    let reopened = 0;
    const handle = openAmbient({
      base: BASE,
      threadId: "t",
      token: "k",
      onFrame: () => {},
      onReopen: () => (reopened += 1),
      fetchImpl: fetch,
      retryMs: 1,
    });
    while (calls.length < 4) await new Promise((r) => setTimeout(r, 2));
    handle.close();
    await handle.done;
    const since = calls.map((c) => (c.body as { forwardedProps?: { cao?: { since?: string } } }).forwardedProps?.cao?.since);
    expect(since).toEqual([
      undefined, // the first stream asks for live frames only
      "2026-11-06T19:44:34.723Z", // the last frame seen, after the stream ended
      "2026-11-06T19:44:34.723Z", // unchanged across a failed attempt
      "2026-11-06T19:44:40.001Z",
    ]);
    // The first open is not a reopen; the second accepted stream is.
    expect(reopened).toBe(1);
  });

  it("P3-2: before any frame carries a timestamp, a reconnect replays from just before the first open", async () => {
    vi.spyOn(Date, "now").mockReturnValue(1_793_994_300_000);
    try {
      const { fetch, calls } = fakeFetch([{ sse: sse({ type: "RUN_STARTED" }) }, { hang: true }]);
      const handle = openAmbient({ base: BASE, threadId: "t", token: "k", onFrame: () => {}, fetchImpl: fetch, retryMs: 1 });
      while (calls.length < 2) await new Promise((r) => setTimeout(r, 2));
      handle.close();
      await handle.done;
      const body = calls[1].body as { forwardedProps: { cao: { since: string } } };
      expect(body.forwardedProps.cao.since).toBe(new Date(1_793_994_300_000 - SINCE_MARGIN_MS).toISOString());
    } finally {
      vi.restoreAllMocks();
    }
  });

  it("an HTTP refusal is 'refused' and is not retried; a non-Error throw is reported as text", async () => {
    const { fetch, calls } = fakeFetch([{ status: 403, json: { detail: "x" } }]);
    const seen: Array<[string, string | undefined]> = [];
    await openAmbient({ base: BASE, threadId: "t", token: "k", onFrame: () => {}, onStatus: (s, d) => seen.push([s, d]), fetchImpl: fetch }).done;
    expect(calls).toHaveLength(1);
    expect(seen).toEqual([
      ["connecting", undefined],
      ["refused", "HTTP 403"],
    ]);
    const odd = fakeFetch([{ throws: "odd" as unknown as Error }, { noBody: true }]);
    const seen2: string[] = [];
    await openAmbient({ base: BASE, threadId: "t", token: "k", onFrame: () => {}, onStatus: (s, d) => seen2.push(`${s}:${d}`), fetchImpl: odd.fetch, retryMs: 1 }).done;
    expect(seen2).toEqual(["connecting:undefined", "reconnecting:odd", "refused:HTTP 200"]);
  });

  it("N5b: each reconnect wait removes its abort listener when its timer fires", async () => {
    const add = vi.spyOn(AbortSignal.prototype, "addEventListener");
    const remove = vi.spyOn(AbortSignal.prototype, "removeEventListener");
    try {
      const { fetch, calls } = fakeFetch([
        { sse: sse({ type: "A" }) },
        { sse: sse({ type: "A" }) },
        { sse: sse({ type: "A" }) },
        { status: 403, json: {} },
      ]);
      await openAmbient({ base: BASE, threadId: "t", token: "k", onFrame: () => {}, fetchImpl: fetch, retryMs: 1 }).done;
      expect(calls).toHaveLength(4);
      const abortAdds = add.mock.calls.filter(([type]) => type === "abort").length;
      const abortRemoves = remove.mock.calls.filter(([type]) => type === "abort").length;
      expect(abortAdds).toBe(3); // one per wait, three waits
      expect(abortRemoves).toBe(3); // each removed when its timer fired
    } finally {
      add.mockRestore();
      remove.mockRestore();
    }
  });

  it("close() during the wait or the request ends it as closed", async () => {
    const waiting = fakeFetch([{ sse: sse({ type: "A" }) }]);
    const seen: string[] = [];
    const h1 = openAmbient({ base: BASE, threadId: "t", token: "k", onFrame: () => {}, onStatus: (s) => seen.push(s), fetchImpl: waiting.fetch, retryMs: 60_000 });
    while (!seen.includes("reconnecting")) await new Promise((r) => setTimeout(r, 1));
    h1.close();
    await h1.done;
    expect(seen.at(-1)).toBe("closed");
    const hanging = fakeFetch([{ hang: true }]);
    const seen2: string[] = [];
    const h2 = openAmbient({ base: BASE, threadId: "t", token: "k", onFrame: () => {}, onStatus: (s) => seen2.push(s), fetchImpl: hanging.fetch });
    h2.close();
    await h2.done;
    expect(seen2).toEqual(["connecting", "closed"]);
  });

  it("close() while frames are still arriving stops without reconnecting", async () => {
    const encoder = new TextEncoder();
    let push: ((s: string) => void) | undefined;
    const body = new ReadableStream<Uint8Array>({
      start(c) {
        push = (s) => c.enqueue(encoder.encode(s));
      },
    });
    const fetchImpl = (async () => new Response(body, { status: 200 })) as unknown as typeof fetch;
    const seen: string[] = [];
    let handle: ReturnType<typeof openAmbient> | undefined;
    handle = openAmbient({
      base: BASE,
      threadId: "t",
      token: "k",
      fetchImpl,
      onStatus: (s) => seen.push(s),
      onFrame: () => handle!.close(),
    });
    while (!push) await new Promise((r) => setTimeout(r, 1));
    push(sse({ type: "A" }));
    await handle.done;
    expect(seen).toEqual(["connecting", "open", "closed"]);
  });

  it("uses the global fetch when none is given", async () => {
    const { fetch } = fakeFetch([{ status: 401, json: {} }]);
    const saved = globalThis.fetch;
    globalThis.fetch = fetch;
    const seen: string[] = [];
    try {
      await openAmbient({ base: BASE, threadId: "t", token: "k", onFrame: () => {}, onStatus: (s) => seen.push(s) }).done;
    } finally {
      globalThis.fetch = saved;
    }
    expect(seen).toEqual(["connecting", "refused"]);
  });

  it("works without an onStatus callback", async () => {
    const { fetch } = fakeFetch([{ status: 401, json: {} }]);
    await openAmbient({ base: BASE, threadId: "t", token: "k", onFrame: () => {}, fetchImpl: fetch }).done;
  });
});
