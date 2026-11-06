import { describe, expect, it } from "vitest";
import { decodeChunk, readFrames } from "../sse.ts";
import { assertAllowed, ROUTES, urlFor } from "../routes.ts";
import { fixtureText } from "./helpers.ts";

describe("decodeChunk", () => {
  it("decodes data lines, ignores ids and comments, and keeps the unfinished tail", () => {
    const text = 'id: a\ndata: {"type":"RUN_STARTED"}\n\n:keep-alive\n\ndata: {"type":"RUN_FIN';
    const { frames, rest } = decodeChunk(text);
    expect(frames).toEqual([{ type: "RUN_STARTED" }]);
    expect(rest).toBe('data: {"type":"RUN_FIN');
  });

  it("accepts CRLF and joins multi-line data", () => {
    const { frames } = decodeChunk('data: {"type":\r\ndata: "X"}\r\n\r\n');
    expect(frames).toEqual([{ type: "X" }]);
  });

  it("drops malformed JSON and JSON that is not a frame", () => {
    const { frames } = decodeChunk("data: {nope\n\ndata: [1,2]\n\ndata: {\"no\":\"type\"}\n\ndata: null\n\n");
    expect(frames).toEqual([]);
  });

  it("decodes every frame of a captured run", () => {
    const { frames } = decodeChunk(fixtureText("one-interrupt.sse"));
    expect(frames.map((f) => f.type)).toEqual(["RUN_STARTED", "STATE_SNAPSHOT", "RUN_FINISHED"]);
  });
});

describe("readFrames", () => {
  it("yields frames split across chunks, including a final frame with no blank line", async () => {
    const encoder = new TextEncoder();
    const parts = ['data: {"type":"A"}\n', '\ndata: {"ty', 'pe":"B"}'];
    const body = new ReadableStream<Uint8Array>({
      start(c) {
        for (const p of parts) c.enqueue(encoder.encode(p));
        c.close();
      },
    });
    const seen: string[] = [];
    for await (const f of readFrames(body)) seen.push(f.type);
    expect(seen).toEqual(["A", "B"]);
  });
});

describe("routes", () => {
  it("builds the three allowed URLs with or without a trailing slash", () => {
    expect(urlFor("http://127.0.0.1:9889", "run")).toBe("http://127.0.0.1:9889/agui/v1/run");
    expect(urlFor("http://127.0.0.1:9889/", "capabilities")).toBe("http://127.0.0.1:9889/agui/v1/capabilities");
    expect(urlFor("http://h:1", "stream", { threadId: "t 1" })).toBe("http://h:1/agui/v1/stream?threadId=t+1");
  });

  it("allows only the listed paths", () => {
    expect(Object.values(ROUTES).sort()).toEqual(["/agui/v1/capabilities", "/agui/v1/run", "/agui/v1/stream"]);
    for (const path of ["/agui/v1/interrupts/x/resume", "/agui/v1/emit_ui", "/terminals/abc/output", "/sessions"]) {
      expect(() => assertAllowed(`http://h${path}`)).toThrow(/route not allowed/);
    }
    expect(() => assertAllowed("http://h/agui/v1/run?x=1")).not.toThrow();
  });
});
