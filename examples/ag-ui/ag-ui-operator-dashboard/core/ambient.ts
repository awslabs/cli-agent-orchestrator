/**
 * The ambient stream (`/agui/v1/stream`).
 *
 * Without a token: a browser `EventSource` on `GET /agui/v1/stream`.
 * With a token: fetch-based SSE on `POST /agui/v1/stream`, because an
 * `EventSource` cannot send `Authorization` and a token in the URL would land
 * in logs. Frames are handed to `onFrame`; dedupe happens in the panel.
 *
 * Status: "connecting", then "open" only once the server has accepted the
 * stream. A dropped stream is "reconnecting" and is retried. A stream the
 * server refuses (an HTTP error, or an EventSource the browser gives up on) is
 * "refused" and is not retried. `close()` ends it as "closed".
 */

import { readFrames } from "./sse.ts";
import { urlFor } from "./routes.ts";
import type { Frame } from "./types.ts";

/** `EventSource.CLOSED`: the browser will not reconnect. */
const EVENTSOURCE_CLOSED = 2;

export interface EventSourceLike {
  readyState: number;
  onopen: ((event: unknown) => void) | null;
  onmessage: ((event: { data: string }) => void) | null;
  onerror: ((event: unknown) => void) | null;
  close(): void;
}

export type AmbientStatus = "connecting" | "open" | "reconnecting" | "refused" | "closed";

export interface AmbientOptions {
  base: string;
  threadId: string;
  token?: string;
  onFrame: (frame: Frame) => void;
  onStatus?: (status: AmbientStatus, detail?: string) => void;
  fetchImpl?: typeof fetch;
  eventSourceImpl?: new (url: string) => EventSourceLike;
  /** Delay before reconnecting the token stream, in ms (default 2000). */
  retryMs?: number;
}

export interface AmbientHandle {
  close(): void;
  /** Resolves when a fetch-based stream stops for good; resolves at once for EventSource. */
  done: Promise<void>;
}

/** Parse one `data:` payload, dropping anything that is not a frame. */
export function parseData(data: string): Frame | null {
  try {
    const value: unknown = JSON.parse(data);
    if (value && typeof value === "object" && typeof (value as Frame).type === "string") return value as Frame;
  } catch {
    // dropped
  }
  return null;
}

export function openAmbient(opts: AmbientOptions): AmbientHandle {
  const status = opts.onStatus ?? (() => {});
  status("connecting");
  return opts.token ? openPost(opts, opts.token, status) : openEventSource(opts, status);
}

function openEventSource(opts: AmbientOptions, status: NonNullable<AmbientOptions["onStatus"]>): AmbientHandle {
  const Impl = opts.eventSourceImpl ?? (globalThis as { EventSource?: AmbientOptions["eventSourceImpl"] }).EventSource;
  if (!Impl) throw new Error("EventSource is not available; pass a token to use the POST stream");
  const source = new Impl(urlFor(opts.base, "stream", { threadId: opts.threadId }));
  source.onopen = () => status("open");
  source.onmessage = (event) => {
    const frame = parseData(event.data);
    if (frame) opts.onFrame(frame);
  };
  source.onerror = () => {
    if (source.readyState === EVENTSOURCE_CLOSED) {
      status("refused", "the server refused the stream (or this origin is not allowed)");
    } else {
      status("reconnecting");
    }
  };
  return {
    close: () => {
      source.close();
      status("closed");
    },
    done: Promise.resolve(),
  };
}

function openPost(opts: AmbientOptions, token: string, status: NonNullable<AmbientOptions["onStatus"]>): AmbientHandle {
  const controller = new AbortController();
  const fetchImpl = opts.fetchImpl ?? fetch;
  const retryMs = opts.retryMs ?? 2000;
  let attempt = 0;
  // Wait between reconnects, ending early on close(). The abort listener is
  // removed when the timer fires, so listeners do not pile up across retries.
  const wait = (ms: number) =>
    new Promise<void>((resolve) => {
      const onAbort = () => {
        clearTimeout(timer);
        resolve();
      };
      const timer = setTimeout(() => {
        controller.signal.removeEventListener("abort", onAbort);
        resolve();
      }, ms);
      controller.signal.addEventListener("abort", onAbort, { once: true });
    });

  const done = (async () => {
    while (!controller.signal.aborted) {
      attempt += 1;
      try {
        const response = await fetchImpl(urlFor(opts.base, "stream"), {
          method: "POST",
          headers: {
            "content-type": "application/json",
            accept: "text/event-stream",
            authorization: `Bearer ${token}`,
          },
          body: JSON.stringify({
            threadId: opts.threadId,
            runId: `ambient-${Date.now()}-${attempt}`,
            messages: [],
            protocolVersion: "1.0",
          }),
          signal: controller.signal,
        });
        if (!response.ok || !response.body) {
          status("refused", `HTTP ${response.status}`);
          return;
        }
        status("open");
        for await (const frame of readFrames(response.body)) {
          opts.onFrame(frame);
          if (controller.signal.aborted) break;
        }
        if (controller.signal.aborted) break;
        status("reconnecting", "the stream ended");
      } catch (error) {
        if (controller.signal.aborted) break;
        status("reconnecting", error instanceof Error ? error.message : String(error));
      }
      await wait(retryMs);
    }
    status("closed");
  })();
  return { close: () => controller.abort(), done };
}
