// Mock Host harness for the Integration tier.
//
// The real MCP App host is a postMessage JSON-RPC peer: the View (our McpApp)
// is a JSON-RPC client over `postMessage`, and the host answers `ui/initialize`,
// `tools/call`, and `ui/update-model-context`, and may push notifications
// (`ui/notifications/tool-result`, etc.). This harness implements that peer in
// happy-dom so the views can be exercised end-to-end at the message layer
// without a browser.
//
// Two `FakeWindow` event buses model the iframe/host split so the View never
// receives its own outbound frames:
//   - appWindow  — the View listens here (its `scope`),
//   - hostWindow — the host listens here; the View's `target` posts here.
// Each delivered message carries the *sender's* origin, so the untrusted-origin
// guard can be exercised by delivering a frame from another origin.

export type ToolImpl = (
  args: Record<string, unknown>,
) => unknown | Promise<unknown>;

type Listener = (event: { data: unknown; origin: string }) => void;

/** Minimal Window stand-in: message listeners + interval timers. */
export class FakeWindow {
  private listeners = new Set<Listener>();

  addEventListener(type: string, fn: Listener): void {
    if (type === "message") this.listeners.add(fn);
  }

  removeEventListener(type: string, fn: Listener): void {
    if (type === "message") this.listeners.delete(fn);
  }

  /** Deliver a frame to this window's listeners, stamped with `origin`. */
  deliver(origin: string, data: unknown): void {
    for (const fn of Array.from(this.listeners)) fn({ data, origin });
  }

  setInterval(fn: () => void, ms: number): ReturnType<typeof setInterval> {
    return setInterval(fn, ms);
  }

  clearInterval(handle: ReturnType<typeof setInterval>): void {
    clearInterval(handle);
  }
}

export interface MockHostOptions {
  /** Tool name -> implementation. Throwing/rejecting models an unreachable Backplane. */
  tools?: Record<string, ToolImpl>;
  /** Host origin stamped on every host->View frame. */
  origin?: string;
  /** hostContext returned from `ui/initialize` (model a no-UI-surface host here). */
  hostContext?: Record<string, unknown>;
  /** hostCapabilities returned from `ui/initialize` (e.g. `{ openLinks: {} }`). */
  hostCapabilities?: Record<string, unknown>;
  /** When true, the host denies `ui/open-link` requests (models user/policy denial). */
  denyOpenLink?: boolean;
  /** Wrap tool results as a CallToolResult with a plain-text content block too. */
  includePlainText?: boolean;
}

interface ToolCallRecord {
  name: string;
  arguments: Record<string, unknown>;
}

/**
 * A JSON-RPC host peer for the View. Construct, then build an `McpApp` with
 * `{ scope: host.appWindow, target: host.appTarget }`.
 */
export class MockHost {
  readonly appWindow = new FakeWindow();
  readonly hostWindow = new FakeWindow();
  readonly origin: string;
  readonly modelNotes: Array<{
    content?: unknown;
    structuredContent?: unknown;
  }> = [];
  readonly toolCalls: ToolCallRecord[] = [];
  /** URLs the View asked the host to open via `ui/open-link`. */
  readonly openedLinks: string[] = [];
  /** Display modes the View requested via `ui/request-display-mode`. */
  readonly displayModeRequests: string[] = [];
  initialized = false;

  private tools: Record<string, ToolImpl>;
  private hostContext: Record<string, unknown>;
  private hostCapabilities: Record<string, unknown>;
  private denyOpenLink: boolean;
  private includePlainText: boolean;

  constructor(options: MockHostOptions = {}) {
    this.tools = options.tools ?? {};
    this.origin = options.origin ?? "https://host.example";
    this.hostContext = options.hostContext ?? {};
    this.hostCapabilities = options.hostCapabilities ?? {};
    this.denyOpenLink = options.denyOpenLink ?? false;
    this.includePlainText = options.includePlainText ?? true;
    this.hostWindow.addEventListener("message", (event) =>
      this.handle(event.data),
    );
  }

  /** The `target` the View posts to: routes frames into the host with the View origin. */
  get appTarget(): {
    postMessage: (data: unknown, targetOrigin?: string) => void;
  } {
    return {
      postMessage: (data: unknown) =>
        // Frames from the View carry the iframe origin.
        this.hostWindow.deliver("https://view.example", data),
    };
  }

  /** Push a notification to the View from the host origin (e.g. tool-result replay). */
  pushNotification(method: string, params: unknown): void {
    this.appWindow.deliver(this.origin, { jsonrpc: "2.0", method, params });
  }

  /**
   * Deliver a frame to the View from an arbitrary (untrusted) origin. Used to
   * verify the View ignores frames whose origin is not the pinned host origin.
   */
  deliverFromOrigin(origin: string, data: unknown): void {
    this.appWindow.deliver(origin, data);
  }

  private reply(id: unknown, result: unknown): void {
    this.appWindow.deliver(this.origin, { jsonrpc: "2.0", id, result });
  }

  private replyError(id: unknown, message: string): void {
    this.appWindow.deliver(this.origin, {
      jsonrpc: "2.0",
      id,
      error: { code: -32000, message },
    });
  }

  private async handle(frame: any): Promise<void> {
    if (!frame || frame.jsonrpc !== "2.0") return;
    const { id, method, params } = frame;

    if (method === "ui/initialize") {
      this.reply(id, {
        hostContext: this.hostContext,
        hostCapabilities: this.hostCapabilities,
      });
      return;
    }
    if (method === "ui/notifications/initialized") {
      this.initialized = true;
      return;
    }
    if (method === "ui/open-link") {
      const url = params?.url as string;
      if (this.denyOpenLink) {
        this.replyError(id, "Link opening denied by user");
        return;
      }
      this.openedLinks.push(url);
      this.reply(id, {});
      return;
    }
    if (method === "ui/request-display-mode") {
      const mode = params?.mode as string;
      this.displayModeRequests.push(mode);
      // Echo the requested mode as the resulting mode (host accepted it).
      this.reply(id, { mode });
      return;
    }
    if (method === "ui/update-model-context") {
      this.modelNotes.push({
        content: params?.content,
        structuredContent: params?.structuredContent,
      });
      this.reply(id, {});
      return;
    }
    if (method === "tools/call") {
      const name = params?.name as string;
      const args = (params?.arguments ?? {}) as Record<string, unknown>;
      this.toolCalls.push({ name, arguments: args });
      const impl = this.tools[name];
      if (!impl) {
        this.replyError(id, `unknown tool: ${name}`);
        return;
      }
      try {
        const structured = await impl(args);
        this.reply(id, this.wrapToolResult(structured));
      } catch (err) {
        this.replyError(id, err instanceof Error ? err.message : "tool failed");
      }
      return;
    }
    // Unknown request: error so the View doesn't hang.
    if (id !== undefined && id !== null)
      this.replyError(id, `unknown method: ${method}`);
  }

  /**
   * Wrap a structured tool return as the host delivers it. A real host echoes a
   * `CallToolResult` with a plain-text `content` block (the serialized result)
   * and, for UI-capable tools, a `structuredContent` payload. A no-UI-surface
   * host still returns structured plain-text results — modeled by
   * `includePlainText` with `structuredContent` omitted.
   */
  private wrapToolResult(structured: unknown): unknown {
    const text = JSON.stringify(structured);
    if (this.includePlainText && this.hostContext.uiSurface === false) {
      // No UI surface: plain-text content only (still structured/serializable).
      return { content: [{ type: "text", text }] };
    }
    return {
      content: [{ type: "text", text }],
      structuredContent: structured,
    };
  }
}
