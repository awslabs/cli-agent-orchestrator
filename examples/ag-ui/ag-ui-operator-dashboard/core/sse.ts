/**
 * A minimal SSE decoder for `fetch` bodies. Only `data:` lines are used:
 * CAO's 1.0 framing sends one JSON frame per `data:` line, with optional
 * `id:` lines and `:keep-alive` comments.
 */

import type { Frame } from "./types.ts";

/** Decode complete SSE events from `text`, returning the frames and the unparsed tail. */
export function decodeChunk(text: string): { frames: Frame[]; rest: string } {
  const frames: Frame[] = [];
  const normalized = text.replace(/\r\n/g, "\n");
  const blocks = normalized.split("\n\n");
  // split() always returns at least one element: the unfinished tail.
  const rest = blocks.pop() as string;
  for (const block of blocks) {
    const data = block
      .split("\n")
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).replace(/^ /, ""))
      .join("\n");
    if (!data) continue;
    const parsed = parseFrame(data);
    if (parsed) frames.push(parsed);
  }
  return { frames, rest };
}

function parseFrame(data: string): Frame | null {
  try {
    const value: unknown = JSON.parse(data);
    if (value && typeof value === "object" && typeof (value as Frame).type === "string") {
      return value as Frame;
    }
  } catch {
    // A malformed frame is dropped, never interpreted.
  }
  return null;
}

/** Yield frames from a streamed response body as they complete. */
export async function* readFrames(body: ReadableStream<Uint8Array>): AsyncGenerator<Frame> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { done, value } = await reader.read();
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      const { frames, rest } = decodeChunk(done ? `${buffer}\n\n` : buffer);
      buffer = rest;
      yield* frames;
      if (done) return;
    }
  } finally {
    reader.releaseLock();
  }
}
