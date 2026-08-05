import type { Page, Route } from "@playwright/test";
import { tryParseNotation, renderPreview } from "../src/lib/macroNotation";
import { projectedTerminal } from "../src/test/projectedTerminal";
import type { AnnotationsResponse } from "../src/api";

/**
 * The §10.5 stubbed server: Playwright route mocks for every HTTP endpoint
 * the dashboard touches, plus an injected FakeWebSocket so the terminal
 * channel is observable (frames the app sends) and drivable (bytes the
 * "server" writes). No real CAO server, tmux, or provider ever runs.
 *
 * The stub is pinned to the §3.5 NEW-server shapes — the Lane A responses
 * Lane B integrates against until Lane A merges: full 16-key set,
 * streaming, provider_controls (kimi), command_controls, schema v4, and the
 * per-terminal control_input.provider_controls block on the identity route.
 */

const FULL_KEYS = [
  "Backspace", "C-c", "C-s", "Delete", "Down", "End", "Enter", "Escape",
  "Home", "Insert", "Left", "PageDown", "PageUp", "Right", "Tab", "Up",
];

const KIMI_COMPACT_EVENTS = [
  { type: "text", text: "/compact" },
  { type: "key", key: "Enter" },
];
const KIMI_STOP_EVENTS = [{ type: "key", key: "Escape" }];

/**
 * The stubbed fleet, as `terminal_projection.project_row` actually shapes it.
 *
 * This array used to be seven hand-written keys against the projection's
 * twenty-five, including a `created_at` the server has never sent, so the e2e
 * suite was rendering a shape production cannot produce. It now comes from the
 * one shared fixture, so widening the projection cannot leave this stub behind.
 *
 * `t-native` keeps its id and generation: every other spec resolves the
 * terminal by that id, and the annotation generation fence keys on the
 * generation.
 */
export const T_NATIVE_GENERATION = "generation-1";

export const stubTerminals = [
  projectedTerminal({
    id: "t-native",
    tmux_window: "0",
    provider: "kimi_cli",
    agent_profile: "spec-writer-k3",
    generation: T_NATIVE_GENERATION,
    callback_target_generation: T_NATIVE_GENERATION,
    last_active: "2026-07-28T10:00:00Z",
    status: "not_fifo_monitored",
  }),
];

/** The §9.5 "no conductor installed" answer: empty, typed, never an error. */
export function emptyAnnotations(): AnnotationsResponse {
  return {
    annotation_schema: "cao-annotations-v1",
    coverage: "unavailable",
    sources_read: 0,
    sources_failed: 0,
    items_dropped: 0,
    items_omitted: 0,
    reasons: [],
    annotations: [],
  };
}

export interface StubMacro {
  id: string;
  name: string;
  description: string | null;
  scope: Record<string, unknown>;
  events: Array<Record<string, unknown>>;
  favorite: boolean;
  origin: "builtin" | "user";
  mutable: boolean;
  builtin_kind?: "compact" | "stop";
  created_at: string | null;
  updated_at: string | null;
}

function builtin(kind: "compact" | "stop"): StubMacro {
  return {
    id: `builtin:kimi_cli:${kind}`,
    name: kind === "compact" ? "Compact" : "Stop",
    description: kind === "compact" ? "Provider-native /compact" : "Interrupt the current turn (Escape)",
    scope: { kind: "provider", provider: "kimi_cli" },
    events: kind === "compact" ? KIMI_COMPACT_EVENTS : KIMI_STOP_EVENTS,
    favorite: true,
    origin: "builtin",
    mutable: false,
    builtin_kind: kind,
    created_at: null,
    updated_at: null,
  };
}

function userMacro(overrides: Partial<StubMacro>): StubMacro {
  return {
    id: "user-1",
    name: "Model K2.7",
    description: null,
    scope: { kind: "provider", provider: "kimi_cli" },
    events: [
      { type: "text", text: "/model" },
      { type: "key", key: "Enter" },
      { type: "key", key: "Up" },
      { type: "key", key: "Up" },
      { type: "key", key: "Up" },
      { type: "key", key: "Enter" },
    ],
    favorite: true,
    origin: "user",
    mutable: true,
    created_at: "2026-07-28T00:00:00Z",
    updated_at: "2026-07-28T00:00:00Z",
    ...overrides,
  };
}

export interface ControlInputPost {
  control_id: string;
  events?: Array<Record<string, unknown>>;
  text?: string;
  enter?: boolean;
  payload_class?: string;
  expected_identity?: Record<string, unknown>;
}

export interface OperatorMessagePost {
  operation_id: string;
  text: string;
  attachments: string[];
  token_map: Record<string, string>;
  expected_identity?: Record<string, unknown>;
}

export interface StubAttachment {
  attachment_id: string;
  terminal_id: string;
  state: string;
  format: string;
  width: number;
  height: number;
  size_bytes: number;
  display_filename: string;
  bound_operation_id: string | null;
  error: null;
  created_at: string;
  updated_at: string;
}

export interface StubHarness {
  /** Every POST body the app sent to /terminals/{id}/control-input. */
  controlInputPosts: ControlInputPost[];
  /** Every POST body the app sent to /terminals/{id}/operator-message (§8.3). */
  operatorMessagePosts: OperatorMessagePost[];
  /** Every attachment upload the app POSTed (multipart bodies; §8.4). */
  attachmentUploads: Array<{ size: number }>;
  /** The in-memory attachment records behind the §8.4 routes. */
  attachments: StubAttachment[];
  /** In-memory macro library behind /macros (mutations work). */
  macros: StubMacro[];
}

const FAKE_WS_INIT = `
  class FakeWebSocket {
    static OPEN = 1;
    constructor(url) {
      this.url = url;
      this.readyState = 1;
      this.binaryType = "arraybuffer";
      window.__fakeWebSockets.push(this);
      setTimeout(() => this.onopen && this.onopen(), 0);
    }
    send(data) { window.__wsSent.push(String(data)); }
    close() {}
  }
  window.__fakeWebSockets = [];
  window.__wsSent = [];
  window.WebSocket = FakeWebSocket;
`;

function json(route: Route, body: unknown, status = 200): Promise<void> {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

export interface StubSession {
  id: string;
  name: string;
  status: string;
}

export async function stubBackend(page: Page, options?: {
  macros?: StubMacro[];
  /** §8.6: omit the operator_message/image blocks (old-server degradation). */
  laneC?: boolean;
  /** §6.7 (r15): advertise interactive_streaming in the per-terminal,
   * build-exact identity block. Default false — the honest old-server
   * shape, under which armed batches never declare. */
  interactiveStreaming?: boolean;
  /** Canned control-input POST responses by ordinal (1-based); later POSTs
   * fall back to the default accepted answer. */
  controlInputResponses?: Array<Record<string, unknown>>;
  /** The §9.5 annotation payload. Omit for the no-annotations control. */
  annotations?: AnnotationsResponse;
  /**
   * The fleet, when a spec needs more than one row.
   *
   * ADDITIVE, AND DEFAULTED TO THE SHARED ARRAY ON PURPOSE. Five other specs
   * resolve the fleet by `t-native` and would change meaning if the shared
   * array simply grew a second entry, so a spec that wants a fleet asks for
   * one and every other spec keeps the single row it was written against.
   */
  terminals?: typeof stubTerminals;
  /**
   * Several sessions, when a spec needs them.
   *
   * SAME RULE AS `terminals`: defaulted to the one `cao-fleet` session every
   * pre-existing spec was written against, so a spec that wants a multi-
   * session fleet says so and nobody else's page changes underneath them.
   * `terminals` fills `cao-fleet` when `terminalsBySession` is not given.
   */
  sessions?: StubSession[];
  /** Per-session rows, keyed by session name. Unknown sessions 404, as the
   * real server does for a name tmux does not have. */
  terminalsBySession?: Record<string, typeof stubTerminals>;
}): Promise<StubHarness> {
  const sessions = options?.sessions ?? [{ id: "s-1", name: "cao-fleet", status: "active" }];
  const terminalsBySession = options?.terminalsBySession ?? {
    "cao-fleet": options?.terminals ?? stubTerminals,
  };
  // Everything below serves THIS list. `/terminals/{id}` is resolved by
  // LOOKUP rather than by index: `stubTerminals[0]` happens to be `t-native`
  // today, and an index is a silent lie the moment the list has two entries.
  const terminals = Object.values(terminalsBySession).flat();
  const laneC = options?.laneC ?? true;
  const interactiveStreaming = options?.interactiveStreaming ?? false;
  let controlInputPostNumber = 0;
  const harness: StubHarness = {
    controlInputPosts: [],
    operatorMessagePosts: [],
    attachmentUploads: [],
    attachments: [],
    macros: options?.macros ?? [
      builtin("compact"),
      builtin("stop"),
      userMacro({}),
      userMacro({
        id: "user-2",
        name: "Max effort",
        scope: { kind: "profile", profile: "spec-writer-k3" },
        favorite: true,
        events: [
          { type: "text", text: "/effort max" },
          { type: "key", key: "Enter" },
        ],
      }),
      userMacro({
        id: "user-3",
        name: "Scrollback",
        scope: { kind: "global" },
        favorite: false,
        events: [{ type: "key", key: "PageUp" }],
      }),
    ],
  }

  await page.addInitScript(FAKE_WS_INIT);

  await page.route("**/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();

    if (path === "/health") return json(route, { status: "ok" });
    if (path === "/settings/memory") return json(route, { enabled: false });
    if (path === "/agents/profiles") return json(route, []);
    if (path === "/sessions" && method === "GET") {
      return json(route, sessions);
    }
    const sessionMatch = /^\/sessions\/([^/]+)$/.exec(path);
    if (sessionMatch && method === "GET" && sessionMatch[1] in terminalsBySession) {
      const name = sessionMatch[1];
      return json(route, {
        session: sessions.find((s) => s.name === name),
        terminals: terminalsBySession[name],
      });
    }
    if (path === "/annotations" && method === "GET") {
      // §9.5: absent conductor state = an empty, typed answer, never a 404 and
      // never an error. The default is exactly that, so every pre-existing spec
      // runs against the no-annotations control.
      return json(route, options?.annotations ?? emptyAnnotations());
    }
    // The same projected row the listing serves, found BY ID. It used to
    // answer `status: "running"`, which is not a value `project_row` can
    // produce — the dashboard uppercased it into a bucket STATUS_ORDER has
    // never held and drew every stubbed worker as "Unknown". A managed
    // native-TUI worker reports `not_fifo_monitored` ("Managed Live"), which is
    // what nearly all of the real fleet reports.
    //
    // The pattern matches exactly ONE path segment, so every
    // `/terminals/t-native/<action>` route below is untouched by it.
    const terminalMatch = /^\/terminals\/([^/]+)$/.exec(path);
    if (terminalMatch && method === "GET") {
      const found = terminals.find((t) => t.id === terminalMatch[1]);
      if (found) return json(route, found);
    }
    if (path === "/terminals/t-native/managed-control") {
      return json(route, {
        managed: true,
        generation: "generation-1",
        provider: "kimi_cli",
        execution_mode: "native_tui",
      });
    }
    if (path === "/control-input/capabilities") {
      return json(route, {
        protocol: "cao-control-input-v1",
        execution_modes: ["native_tui"],
        literal_write: true,
        bracketed_paste: false,
        enter_required: true,
        request_schema_versions: [1, 2, 3, 4],
        sequence: {
          event_types: ["chord", "key", "text"],
          keys: FULL_KEYS,
          max_events: 32,
          max_text_bytes: 512,
        },
        streaming: { supported: true, max_in_flight: 1, coalesce_window_ms: 200 },
        provider_controls: {
          kimi_cli: {
            compact: { events: KIMI_COMPACT_EVENTS },
            stop: { events: KIMI_STOP_EVENTS },
            steer_chords: ["C-s"],
            dispatch_grace_ms: 5000,
            // §8.6 additive Lane C blocks (absent on old servers).
            ...(laneC
              ? {
                  operator_message: {
                    supported: true,
                    max_text_bytes: 8192,
                    multiline: true,
                    max_attachments: 4,
                  },
                  image: {
                    supported: true,
                    formats: ["png"],
                    max_bytes: 5242880,
                    max_width: 8000,
                    max_height: 8000,
                    mechanism: "staged-path-text",
                    reference_template:
                      "Use the ReadMediaFile tool to read the image file at {path} and analyze it in the context of this message.",
                  },
                }
              : {}),
          },
        },
        command_controls: { composer_nonempty_guard: true },
      });
    }
    if (path === "/terminals/t-native/control-identity") {
      return json(route, {
        terminal_id: "t-native",
        terminal_incarnation: "incarnation-1",
        terminal_generation: "generation-1",
        pane_birth_id: "%7",
        provider_process_id: "42@start",
        provider: "kimi_cli",
        native_session_id: "session-1",
        execution_mode: "native_tui",
        session_name: "cao-fleet",
        control_input: {
          schema_versions: [1, 2, 3, 4],
          sequence: { keys: FULL_KEYS, max_events: 32, max_text_bytes: 512 },
          provider_controls: {
            kimi_cli: {
              steer_chords: ["C-s"],
              dispatch_grace_ms: 5000,
              ...(interactiveStreaming ? { interactive_streaming: { supported: true } } : {}),
              ...(laneC
                ? {
                    operator_message: {
                      supported: true,
                      max_text_bytes: 8192,
                      multiline: true,
                      max_attachments: 4,
                    },
                    image: {
                      supported: true,
                      formats: ["png"],
                      max_bytes: 5242880,
                      max_width: 8000,
                      max_height: 8000,
                      mechanism: "staged-path-text",
                    },
                  }
                : {}),
            },
          },
        },
      });
    }
    // Lane C routes (§8.3/§8.4). On an old server they 404 — the dashboard
    // must degrade by hiding the affordances, never by retrying.
    if (!laneC && (path.endsWith("/attachments") || path.includes("/attachments/")
      || path.endsWith("/operator-message") || path.startsWith("/operator-message/"))) {
      return json(route, { detail: "not found" }, 404);
    }
    if (path === "/terminals/t-native/attachments" && method === "POST") {
      const raw = request.postDataBuffer() ?? Buffer.alloc(0);
      harness.attachmentUploads.push({ size: raw.length });
      const record: StubAttachment = {
        attachment_id: `att-${harness.attachments.length + 1}`,
        terminal_id: "t-native",
        state: "ready",
        format: "png",
        width: 120,
        height: 80,
        size_bytes: 213,
        display_filename: "shot.png",
        bound_operation_id: null,
        error: null,
        created_at: "2026-07-29T00:00:00Z",
        updated_at: "2026-07-29T00:00:00Z",
      };
      harness.attachments.push(record);
      return json(route, { attachment: record }, 201);
    }
    if (path === "/terminals/t-native/attachments" && method === "GET") {
      return json(route, {
        attachments: harness.attachments.filter((a) => a.state !== "removed"),
      });
    }
    const attachmentMatch = /^\/terminals\/t-native\/attachments\/(.+)$/.exec(path);
    if (attachmentMatch && method === "DELETE") {
      const record = harness.attachments.find((a) => a.attachment_id === attachmentMatch[1]);
      if (!record) return json(route, { detail: "no image attachment" }, 404);
      record.state = "removed";
      return json(route, { deleted: true, attachment: record });
    }
    if (path === "/terminals/t-native/operator-message" && method === "POST") {
      const body = request.postDataJSON() as OperatorMessagePost;
      harness.operatorMessagePosts.push(body);
      for (const attachment of harness.attachments) {
        if (body.attachments?.includes(attachment.attachment_id)) {
          attachment.state = "submitted";
          attachment.bound_operation_id = body.operation_id;
        }
      }
      return json(route, {
        operation_id: body.operation_id,
        outcome: "accepted",
        reason_code: "delivered",
        record_state: "posted",
        replayed: false,
      });
    }
    if (path.startsWith("/operator-message/")) {
      return json(route, {
        outcome: "ambiguous",
        reason_code: "response-lost",
        detail: "the operation was journaled but no transport record exists",
      });
    }
    if (path === "/terminals/t-native/control-input" && method === "POST") {
      const body = request.postDataJSON() as ControlInputPost;
      harness.controlInputPosts.push(body);
      controlInputPostNumber += 1;
      const canned = options?.controlInputResponses?.[controlInputPostNumber - 1];
      if (canned) {
        return json(route, { control_id: body.control_id, events: body.events, ...canned });
      }
      return json(route, {
        control_id: body.control_id,
        outcome: "accepted",
        events: body.events,
      });
    }
    if (path.startsWith("/control-input/")) {
      return json(route, { outcome: "ambiguous", reason_code: "response-lost" });
    }
    if (path === "/macros" && method === "GET") {
      return json(route, { macros: harness.macros });
    }
    if (path === "/macros" && method === "POST") {
      const body = request.postDataJSON() as Record<string, unknown>;
      const parsed = tryParseNotation(String(body.notation ?? ""));
      if (!parsed.ok) return json(route, { errors: parsed.errors }, 422);
      const record: StubMacro = {
        id: `user-${harness.macros.length + 1}-${Date.now()}`,
        name: String(body.name ?? ""),
        description: (body.description as string) ?? null,
        scope: body.scope as Record<string, unknown>,
        events: parsed.events as unknown as Array<Record<string, unknown>>,
        favorite: Boolean(body.favorite),
        origin: "user",
        mutable: true,
        created_at: "2026-07-28T12:00:00Z",
        updated_at: "2026-07-28T12:00:00Z",
      };
      harness.macros.push(record);
      return json(route, record, 201);
    }
    if (path === "/macros/parse-notation" && method === "POST") {
      const body = request.postDataJSON() as { notation?: string };
      const parsed = tryParseNotation(String(body.notation ?? ""));
      if (!parsed.ok) return json(route, { errors: parsed.errors }, 422);
      return json(route, {
        events: parsed.events,
        preview: renderPreview(parsed.events),
      });
    }
    const duplicateMatch = /^\/macros\/(.+)\/duplicate$/.exec(path);
    if (duplicateMatch && method === "POST") {
      const source = harness.macros.find((m) => m.id === duplicateMatch[1]);
      if (!source) return json(route, { detail: "no macro" }, 404);
      const copy: StubMacro = {
        ...source,
        id: `user-${Date.now()}`,
        origin: "user",
        mutable: true,
      };
      delete copy.builtin_kind;
      harness.macros.push(copy);
      return json(route, copy, 201);
    }
    const macroMatch = /^\/macros\/(.+)$/.exec(path);
    if (macroMatch && method === "DELETE") {
      const index = harness.macros.findIndex((m) => m.id === macroMatch[1]);
      if (index === -1) return json(route, { detail: "no macro" }, 404);
      const [removed] = harness.macros.splice(index, 1);
      return json(route, { deleted: removed.id });
    }
    if (macroMatch && method === "PUT") {
      const body = request.postDataJSON() as Record<string, unknown>;
      const record = harness.macros.find((m) => m.id === macroMatch[1]);
      if (!record) return json(route, { detail: "no macro" }, 404);
      const parsed = tryParseNotation(String(body.notation ?? ""));
      if (!parsed.ok) return json(route, { errors: parsed.errors }, 422);
      Object.assign(record, {
        name: String(body.name ?? record.name),
        scope: (body.scope as Record<string, unknown>) ?? record.scope,
        events: parsed.events as unknown as Array<Record<string, unknown>>,
        favorite: Boolean(body.favorite),
        updated_at: "2026-07-28T12:30:00Z",
      });
      return json(route, record);
    }

    // The app itself (document, scripts, styles) passes through to vite preview.
    return route.continue();
  });

  return harness;
}

/** Frames the app sent on the terminal websocket (all of them, any type). */
export async function wsSentFrames(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return (window as any).__wsSent as string[];
  });
}

/** Feed the terminal bytes as if the server wrote them (enables mouse mode). */
export async function pushTerminalBytes(page: Page, bytes: string): Promise<void> {
  await page.evaluate((payload) => {
    // Plain JS inside evaluate: Playwright serializes this function's source.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const sockets = (window as any).__fakeWebSockets as any[];
    const ws = sockets[sockets.length - 1];
    if (!ws?.onmessage) throw new Error("terminal websocket not mounted");
    const data = new TextEncoder().encode(payload);
    ws.onmessage({ data: data.buffer });
  }, bytes);
}

/** Open the dashboard and enter the native terminal view. */
export async function openNativeTerminal(page: Page): Promise<void> {
  await page.goto("/");
  // The session auto-expands on first load; the terminal row is direct.
  await page.getByRole("button", { name: "Terminal" }).click();
  await page.getByText("Managed native TUI · identity-bound controls").waitFor();
}
