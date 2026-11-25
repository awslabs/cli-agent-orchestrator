// Event-stream view (ui://cao/event-stream).
//
// Hydrates the governance ticker via `cao_fetch_history`, then subscribes to the
// live SSE feed at the descriptor returned by `subscribe_events`. Re-mounting
// re-fetches history (Req: re-mount idempotence). SSE gaps are backfilled by the
// history replay, so a dropped event is never permanently lost.

import React, { useEffect, useRef, useState } from "react";
import { EventStream } from "../shared/EventStream";
import { HeaderBar } from "../shared/HeaderBar";
import { McpApp } from "../shared/mcpApp";
import type { CaoEvent } from "../shared/types";

const HISTORY_LIMIT = 500;

export interface EventStreamViewProps {
  app?: McpApp;
  initialEvents?: CaoEvent[];
  /**
   * Base URL for the Backplane SSE endpoint. Optional: the host supplies it as
   * `base_url` in the `subscribe_events` descriptor. There is no built-in
   * default, so a view that is given no address streams nothing rather than
   * connecting to whatever listens on a well-known port.
   */
  backplaneBaseUrl?: string;
  /** Injectable EventSource for tests (defaults to the global). */
  eventSourceFactory?: (url: string) => EventSourceLike;
}

/** Minimal EventSource surface we depend on (keeps tests simple). */
export interface EventSourceLike {
  addEventListener(
    type: "message",
    listener: (ev: { data: string }) => void,
  ): void;
  close(): void;
}

/**
 * Where the live feed is, as told by the host. `sse_url` may be absolute; a
 * relative one is resolved against the descriptor's `base_url`, then the
 * `fallbackBase` prop. Returns undefined when neither gives an address.
 */
export function resolveStreamUrl(
  desc: { sse_url?: string; base_url?: string } | undefined,
  fallbackBase?: string,
): string | undefined {
  const path = desc?.sse_url ?? "/events";
  try {
    return new URL(path, desc?.base_url ?? fallbackBase).toString();
  } catch {
    return undefined;
  }
}

export function EventStreamView({
  app,
  initialEvents,
  backplaneBaseUrl,
  eventSourceFactory,
}: EventStreamViewProps): React.JSX.Element {
  const [events, setEvents] = useState<CaoEvent[]>(initialEvents ?? []);
  const seen = useRef<Set<string>>(
    new Set((initialEvents ?? []).map((e) => e.id)),
  );

  function ingest(incoming: CaoEvent[]): void {
    const fresh = incoming.filter((e) => e && e.id && !seen.current.has(e.id));
    if (fresh.length === 0) return;
    for (const e of fresh) seen.current.add(e.id);
    setEvents((prev) => [...prev, ...fresh].slice(-HISTORY_LIMIT));
  }

  useEffect(() => {
    if (!app) return;
    let source: EventSourceLike | undefined;
    let cancelled = false;

    app.connect().then(async () => {
      // 1) Hydrate from history (re-mount safe).
      const history = await app.fetchHistory(HISTORY_LIMIT);
      if (!cancelled) ingest(history);

      // 2) Subscribe to the live SSE feed via the descriptor.
      try {
        const desc = await app.callServerTool("subscribe_events");
        const sseUrl = resolveStreamUrl(desc, backplaneBaseUrl);
        if (!sseUrl) return; // the host gave no address: history only
        const factory =
          eventSourceFactory ??
          ((url: string) => new EventSource(url) as unknown as EventSourceLike);
        source = factory(sseUrl);
        source.addEventListener("message", (ev) => {
          try {
            const parsed = JSON.parse(ev.data) as CaoEvent;
            if (!cancelled) ingest([parsed]);
          } catch {
            // ignore malformed SSE frames
          }
        });
      } catch {
        // SSE optional; history already hydrated the ticker.
      }
    });

    return () => {
      cancelled = true;
      if (source) source.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [app]);

  return (
    <div className="cao-root">
      <HeaderBar title="Governance Stream" />
      <EventStream events={events} emptyLabel="No fleet events yet" />
    </div>
  );
}
