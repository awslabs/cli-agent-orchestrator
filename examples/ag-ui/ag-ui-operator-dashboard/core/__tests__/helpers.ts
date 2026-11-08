import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { decodeChunk } from "../sse.ts";
import type { Frame, WireInterrupt } from "../types.ts";

// vitest runs from the app root (vite.config.ts), in node and jsdom alike.
const FIXTURES = resolve(process.cwd(), "fixtures") + "/";

export function fixtureText(name: string): string {
  return readFileSync(FIXTURES + name, "utf8");
}

export function fixtureFrames(name: string): Frame[] {
  return decodeChunk(`${fixtureText(name)}\n\n`).frames;
}

export function fixtureJson<T = unknown>(name: string): T {
  return JSON.parse(fixtureText(name)) as T;
}

/** The interrupts of a fixture's terminal `RUN_FINISHED(interrupt)`. */
export function interruptsOf(name: string): WireInterrupt[] {
  const last = fixtureFrames(name).find((f) => f.type === "RUN_FINISHED");
  return ((last?.outcome as { interrupts?: WireInterrupt[] })?.interrupts ?? []) as WireInterrupt[];
}

export interface Recorded {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: unknown;
}

export type Reply =
  | { sse: string; chunks?: number }
  | { status: number; json?: unknown; text?: string }
  | { throws: Error }
  | { hang: true }
  | { noBody: true };

/** A fake `fetch` that answers from a queue of replies and records every request. */
export function fakeFetch(replies: Reply[]): { fetch: typeof fetch; calls: Recorded[] } {
  const calls: Recorded[] = [];
  const impl = async (input: string | URL | Request, init?: RequestInit): Promise<Response> => {
    const headers = Object.fromEntries(Object.entries((init?.headers ?? {}) as Record<string, string>));
    calls.push({
      url: String(input),
      method: init?.method ?? "GET",
      headers,
      body: typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
    });
    const reply = replies.shift();
    if (!reply) throw new Error("no reply queued");
    if ("throws" in reply) throw reply.throws;
    if ("hang" in reply) {
      return new Promise((_, reject) => {
        init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
      });
    }
    if ("noBody" in reply) return new Response(null, { status: 200 });
    if ("sse" in reply) {
      const text = reply.sse;
      const parts = reply.chunks ?? 1;
      const size = Math.ceil(text.length / parts);
      const encoder = new TextEncoder();
      const stream = new ReadableStream<Uint8Array>({
        start(controller) {
          for (let i = 0; i < text.length; i += size) controller.enqueue(encoder.encode(text.slice(i, i + size)));
          controller.close();
        },
      });
      return new Response(stream, { status: 200, headers: { "content-type": "text/event-stream" } });
    }
    const body = reply.json !== undefined ? JSON.stringify(reply.json) : (reply.text ?? "");
    return new Response(body, { status: reply.status });
  };
  return { fetch: impl as typeof fetch, calls };
}

export function sse(...frames: object[]): string {
  return frames.map((f) => `data: ${JSON.stringify(f)}\n\n`).join("");
}
