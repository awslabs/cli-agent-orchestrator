// MCP App lifecycle bridge for the CAO views.
//
// DEVIATION NOTE (per the stable SEP-1865 spec, 2026-01-26): the
// `@modelcontextprotocol/ext-apps` SDK exists (npm
// v1.7.4), but the spec explicitly states you "don't need an SDK to 'talk MCP'
// with the host." To keep the single-file bundles dependency-free and trivially
// JIT-free, this bridge implements the spec's native `postMessage` JSON-RPC
// pattern directly rather than importing the SDK. The public interface
// (connect / submitCommand / fetchHistory / startPolling / updateModelContext /
// silentlyNoteToModel / openLink / requestDisplayMode) is preserved so the SDK
// can be dropped in later.
//
// Lifecycle invariant: notification handlers are registered BEFORE
// `connect()` sends `ui/initialize`, because the host MUST NOT send
// `ui/notifications/*` to the View before it observes `ui/notifications/initialized`.

import type { CaoEvent, SubmitCommandKind, SubmitCommandResult } from "./types";

type JsonRpcId = number;
type NotificationHandler = (params: any) => void;

interface PendingRequest {
  resolve: (value: any) => void;
  reject: (reason: Error) => void;
}

// The MCP Apps protocol version announced in the `ui/initialize` handshake. The
// stable SEP-1865 spec (2026-01-26) uses this exact value in both its
// `ui/initialize` request example and `McpUiInitializeResult`. This is the MCP
// Apps extension version negotiated View<->Host — distinct from the base MCP
// client<->server protocol version used on the server's `initialize` handshake.
const PROTOCOL_VERSION = "2026-01-26";

export interface McpAppOptions {
  /** Window to post messages to (defaults to window.parent). */
  target?: Window;
  /** Override the global object for tests (defaults to window). */
  scope?: Window;
}

/**
 * Thin MCP App bridge: a JSON-RPC client over postMessage to the host, plus the
 * CAO-specific tool calls the views use.
 */
export class McpApp {
  private target: Window;
  private scope: Window;
  private nextId: JsonRpcId = 1;
  private pending = new Map<JsonRpcId, PendingRequest>();
  private notificationHandlers = new Map<string, NotificationHandler[]>();
  private listener?: (event: MessageEvent) => void;
  private connected = false;
  /** Host context (theme, container dimensions, etc.) from initialize. */
  hostContext: Record<string, unknown> = {};
  /**
   * Host capabilities from the `ui/initialize` result (e.g. `openLinks`,
   * `serverTools`). The View checks these before requesting host-delegated
   * actions (per SEP-1865 HostCapabilities).
   */
  hostCapabilities: Record<string, unknown> = {};
  /** The expected host origin once known; messages from elsewhere are ignored. */
  private hostOrigin: string | null = null;

  constructor(options: McpAppOptions = {}) {
    // In an iframe the host is the parent; tests can inject a stub.
    this.scope = options.scope ?? (globalThis as unknown as Window);
    this.target = options.target ?? this.scope.parent ?? this.scope;
  }

  // ---- handler registration (call BEFORE connect) ------------------------

  /** Register a notification handler. MUST be called before `connect()`. */
  on(method: string, handler: NotificationHandler): void {
    const list = this.notificationHandlers.get(method) ?? [];
    list.push(handler);
    this.notificationHandlers.set(method, list);
  }

  /** Convenience: the tool result that instantiated/refreshed the View. */
  onToolResult(handler: (result: any) => void): void {
    this.on("ui/notifications/tool-result", handler);
  }

  /** Convenience: the tool input (arguments) for the current tool call. */
  onToolInput(handler: (args: any) => void): void {
    this.on("ui/notifications/tool-input", (params) =>
      handler(params?.arguments),
    );
  }

  /** Convenience: host context changes (theme, display mode, size, ...). */
  onHostContextChanged(handler: (ctx: Record<string, unknown>) => void): void {
    this.on("ui/notifications/host-context-changed", (params) => {
      this.hostContext = { ...this.hostContext, ...(params ?? {}) };
      handler(this.hostContext);
    });
  }

  /** Convenience: host teardown notice (release listeners before unmount). */
  onTeardown(handler: (reason: string) => void): void {
    this.on("ui/resource-teardown", (params) => handler(params?.reason ?? ""));
  }

  // ---- lifecycle ---------------------------------------------------------

  /**
   * Begin listening and perform the `ui/initialize` handshake.
   *
   * Handlers registered via `on*` before this call are guaranteed to be in
   * place before the host can deliver any notification.
   */
  async connect(appCapabilities: Record<string, unknown> = {}): Promise<void> {
    if (this.connected) return;
    this.listener = (event: MessageEvent) => this.handleMessage(event);
    this.scope.addEventListener("message", this.listener);
    this.connected = true;

    // Release event listeners for GC when the host tears down the iframe.
    // Registered last so any user-supplied teardown handler runs
    // first; `disconnect()` then removes the message listener.
    this.on("ui/resource-teardown", () => this.disconnect());
    // Also disconnect on page hide/unload so a hard iframe removal cleans up.
    const onHide = () => this.disconnect();
    const maybeAdd = (
      this.scope as unknown as {
        addEventListener?: (t: string, fn: () => void) => void;
      }
    ).addEventListener;
    if (typeof maybeAdd === "function") {
      maybeAdd.call(this.scope, "pagehide", onHide);
    }

    const result = await this.request("ui/initialize", {
      protocolVersion: PROTOCOL_VERSION,
      appCapabilities: {
        availableDisplayModes: ["inline", "fullscreen"],
        ...appCapabilities,
      },
      clientInfo: { name: "cao-mcp-app", version: "0.1.0" },
    });
    this.hostContext = (result?.hostContext as Record<string, unknown>) ?? {};
    this.hostCapabilities =
      (result?.hostCapabilities as Record<string, unknown>) ?? {};
    this.notify("ui/notifications/initialized", {});
  }

  /** Stop listening and reject any in-flight requests (idempotent). */
  disconnect(): void {
    if (this.listener) {
      this.scope.removeEventListener("message", this.listener);
      this.listener = undefined;
    }
    this.connected = false;
    for (const { reject } of this.pending.values()) {
      reject(new Error("disconnected"));
    }
    this.pending.clear();
    this.notificationHandlers.clear();
  }

  // ---- CAO tool calls ----------------------------------------------------

  /** Call a server tool through the host (`tools/call`). */
  async callServerTool(
    name: string,
    args: Record<string, unknown> = {},
  ): Promise<any> {
    const result = await this.request("tools/call", { name, arguments: args });
    // Unwrap structuredContent when present (UI-optimized payload), else content.
    if (result && typeof result === "object" && "structuredContent" in result) {
      return (result as any).structuredContent;
    }
    return result;
  }

  /** The single mutation choke point: every state change flows through here. */
  async submitCommand(
    kind: SubmitCommandKind,
    payload: Record<string, unknown> = {},
  ): Promise<SubmitCommandResult> {
    return (await this.callServerTool("submit_command", {
      kind,
      payload,
    })) as SubmitCommandResult;
  }

  /** Replay recent fleet events from the ring buffer. */
  async fetchHistory(limit = 500, kinds?: string[]): Promise<CaoEvent[]> {
    const result = await this.callServerTool("cao_fetch_history", {
      limit,
      kinds,
    });
    return (result?.events ?? []) as CaoEvent[];
  }

  /**
   * Poll a read tool on an interval, invoking `onResult` with each snapshot.
   * Returns a stop function. Failures are swallowed (the iframe keeps polling);
   * an optional `onError` is notified on each failed tick so the view can
   * surface an unreachable-Backplane state.
   */
  startPolling(
    toolName: string,
    intervalMs: number,
    onResult: (snapshot: any) => void,
    args: Record<string, unknown> = {},
    onError?: (err: unknown) => void,
  ): () => void {
    let stopped = false;
    const tick = async () => {
      if (stopped) return;
      try {
        const snapshot = await this.callServerTool(toolName, args);
        if (!stopped) onResult(snapshot);
      } catch (err) {
        // Best-effort: a transient Backplane error must not kill the loop, but
        // the view may want to surface a retry affordance.
        if (!stopped && onError) onError(err);
      }
    };
    void tick();
    const handle = this.scope.setInterval(tick, intervalMs);
    return () => {
      stopped = true;
      this.scope.clearInterval(handle);
    };
  }

  // ---- model-context loop --------------------------------------

  /**
   * Update the host's model context for FUTURE turns (spec
   * `ui/update-model-context`). Silent: does not trigger an immediate inference
   * cycle, and each call overwrites the previous context.
   */
  async updateModelContext(
    content?: Array<{ type: "text"; text: string }>,
    structuredContent?: Record<string, unknown>,
  ): Promise<void> {
    await this.request("ui/update-model-context", {
      content,
      structuredContent,
    });
  }

  /**
   * Post a single token-efficient summary note to the model, silently and
   * failure-tolerantly. Excludes message bodies (privacy boundary).
   * Never throws — a failed note must not block the iframe.
   */
  async silentlyNoteToModel(summary: string): Promise<void> {
    try {
      await this.updateModelContext([{ type: "text", text: summary }]);
    } catch {
      // Swallow: model-context notes are best-effort.
    }
  }

  // ---- host-delegated actions (SEP-1865 ui/* requests) -------------------

  /** Whether the host advertised support for opening external links. */
  canOpenLinks(): boolean {
    return Boolean(this.hostCapabilities.openLinks);
  }

  /**
   * Ask the host to open an external URL (spec `ui/open-link`). This delegates
   * to the host's capability rather than calling `window.open` (the sandbox
   * forbids it) — e.g. opening CAO's bundled Web UI at http://127.0.0.1:9889
   * from inside the chat host. Resolves on success; rejects if the host denies
   * or fails. Callers SHOULD gate on `canOpenLinks()` first.
   */
  async openLink(url: string): Promise<void> {
    await this.request("ui/open-link", { url });
  }

  /**
   * Request a display-mode change (spec `ui/request-display-mode`). The host
   * returns the *actual* resulting mode, which MAY differ from the request
   * (e.g. the host declined). Returns the resulting mode string.
   */
  async requestDisplayMode(
    mode: "inline" | "fullscreen" | "pip",
  ): Promise<string> {
    const result = await this.request("ui/request-display-mode", { mode });
    return (result?.mode as string) ?? mode;
  }

  // ---- transport internals ----------------------------------------------

  private request(method: string, params: unknown): Promise<any> {
    const id = this.nextId;
    this.nextId += 1;
    return new Promise<any>((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.post({ jsonrpc: "2.0", id, method, params });
    });
  }

  private notify(method: string, params: unknown): void {
    this.post({ jsonrpc: "2.0", method, params });
  }

  private post(message: unknown): void {
    // Target origin "*" is acceptable for the sandbox proxy transport; inbound
    // messages are origin-checked in handleMessage once the host origin is known.
    this.target.postMessage(message, "*");
  }

  private handleMessage(event: MessageEvent): void {
    // Untrusted-origin guard: once we learn the host origin from the
    // first correlated reply, ignore anything from a different origin.
    const data: any = event.data;
    if (!data || data.jsonrpc !== "2.0") return;

    if (
      data.id !== undefined &&
      data.id !== null &&
      this.pending.has(data.id)
    ) {
      // Pin the host origin on the first reply we accept.
      if (this.hostOrigin === null && event.origin)
        this.hostOrigin = event.origin;
      const pending = this.pending.get(data.id)!;
      this.pending.delete(data.id);
      if (data.error)
        pending.reject(new Error(data.error?.message ?? "RPC error"));
      else pending.resolve(data.result);
      return;
    }

    // Notification (no id, or id we don't recognize): dispatch by method.
    if (typeof data.method === "string") {
      if (
        this.hostOrigin !== null &&
        event.origin &&
        event.origin !== this.hostOrigin
      ) {
        return; // drop notifications from an unexpected origin
      }
      const handlers = this.notificationHandlers.get(data.method);
      if (handlers) {
        for (const handler of handlers) handler(data.params);
      }
    }
  }
}

/** Map a completed gesture to a token-efficient, body-free model-context note. */
export function describeGesture(
  kind: SubmitCommandKind,
  target?: string,
): string {
  const subject = target ? ` ${target}` : "";
  switch (kind) {
    case "send_message":
      return `Operator sent a message to${subject} (handoff).`;
    case "assign":
      return `Operator assigned a task to${subject} (handoff).`;
    case "create_session":
      return `Operator launched a session${subject}.`;
    case "interrupt":
      return `Operator interrupted${subject}.`;
    case "pause":
      return `Operator paused${subject}.`;
    case "resume":
      return `Operator resumed${subject}.`;
    case "shutdown_session":
      return `Operator shut down session${subject} (completion).`;
    default:
      return `Operator performed ${kind}${subject}.`;
  }
}

/**
 * Build the payload shape `submit_command` expects for a gesture, mapping the
 * gesture's `target` to the field the server route reads (verified against
 * `mcp_server/app_tools._route_command`):
 *
 * - `shutdown_session` -> `{ session_name }`
 * - `create_session`   -> caller-supplied `extras` (e.g. `{ agent_profile }`)
 * - everything else (`send_message`/`assign`/`interrupt`/`pause`/`resume`)
 *   -> `{ terminal_id }`
 *
 * `extras` (e.g. `message`, `sender_id`) is merged last so callers can add the
 * gesture-specific fields.
 */
export function buildGesturePayload(
  kind: SubmitCommandKind,
  target?: string,
  extras: Record<string, unknown> = {},
): Record<string, unknown> {
  switch (kind) {
    case "shutdown_session":
      return { session_name: target, ...extras };
    case "create_session":
      return { ...extras };
    case "send_message":
    case "assign":
    case "interrupt":
    case "pause":
    case "resume":
    default:
      return { terminal_id: target, ...extras };
  }
}

/**
 * The single Command_Kind a drag-and-drop reassignment gesture maps to.
 * Dragging a task card onto an agent reassigns it -> `assign`.
 */
export const DRAG_REASSIGN_KIND: SubmitCommandKind = "assign";
