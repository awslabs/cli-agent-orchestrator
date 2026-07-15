/* eslint-disable no-undef */
// In-browser MCP host JSON-RPC peer for the E2E harness.
//
// Embeds the built View bundle(s) and answers their postMessage requests
// (ui/initialize, tools/call, ui/update-model-context) from canned, mutable
// fleet state. Playwright drives the harness through `window.__host`:
//
//   window.__host.ready()                    -> Promise resolved once every
//                                                embedded iframe has sent
//                                                ui/notifications/initialized.
//   window.__host.launch(id, profile)        -> add a terminal + push a fresh
//                                                dashboard snapshot.
//   window.__host.stop(id)                    -> mark a terminal stopped + push
//                                                updated snapshots.
//   window.__host.teardown()                  -> send ui/resource-teardown to
//                                                every iframe.
//   window.__host.toolCallCount(name)         -> how many times a tool was
//                                                called (auto-refresh).
//   window.__host.submitCount()               -> submit_command call count
//                                                (must stay 0).
//
// The host peer routes replies back to the requesting iframe via
// event.source.postMessage, and tracks each iframe's contentWindow so it can
// push notifications (the "tool result that opened the view", host-driven
// snapshot refreshes, and teardown).

(function () {
  const params = new URLSearchParams(location.search);
  const view = params.get("view") || "dashboard";
  // `fleet=rich` selects the multi-agent demo fleet (used by the demo recorder).
  // The default single-terminal fleet keeps the deterministic state the e2e
  // specs assert against.
  const fleet = params.get("fleet") || "default";
  const useRich = fleet === "rich";

  // --- canned, mutable fleet state ----------------------------------------
  // A robust multi-agent fleet: one supervisor coordinating five workers across
  // two sessions, with varied live statuses (processing / idle /
  // waiting_user_answer / completed / error) so the dashboard reads as a real,
  // first-class orchestration console. The supervisor is listed first.
  const nowIso = () => new Date().toISOString();
  const RICH = {
    terminals: [
      {
        id: "sup-1",
        session_name: "cao-feature-build",
        provider: "kiro_cli",
        agent_profile: "code_supervisor",
        status: "processing",
        window: "w0",
        last_active: nowIso(),
      },
      {
        id: "dev-1",
        session_name: "cao-feature-build",
        provider: "kiro_cli",
        agent_profile: "developer",
        status: "processing",
        window: "w1",
        last_active: nowIso(),
      },
      {
        id: "dev-2",
        session_name: "cao-feature-build",
        provider: "claude_code",
        agent_profile: "developer",
        status: "idle",
        window: "w2",
        last_active: nowIso(),
      },
      {
        id: "test-1",
        session_name: "cao-feature-build",
        provider: "codex",
        agent_profile: "test-engineer",
        status: "completed",
        window: "w3",
        last_active: nowIso(),
      },
      {
        id: "rev-1",
        session_name: "cao-review",
        provider: "kiro_cli",
        agent_profile: "reviewer",
        status: "waiting_user_answer",
        window: "w0",
        last_active: nowIso(),
      },
      {
        id: "doc-1",
        session_name: "cao-review",
        provider: "gemini_cli",
        agent_profile: "documentation-curator",
        status: "error",
        window: "w1",
        last_active: nowIso(),
      },
    ],
    events: [
      {
        id: "s1",
        kind: "launch",
        terminal_id: "sup-1",
        session_name: "cao-feature-build",
        timestamp: nowIso(),
        detail: { agent_name: "code_supervisor" },
      },
      {
        id: "s2",
        kind: "a2a_delegation",
        terminal_id: "sup-1",
        session_name: "cao-feature-build",
        timestamp: nowIso(),
        detail: { to: "developer" },
      },
      {
        id: "s3",
        kind: "launch",
        terminal_id: "dev-1",
        session_name: "cao-feature-build",
        timestamp: nowIso(),
        detail: { agent_name: "developer" },
      },
      {
        id: "s4",
        kind: "launch",
        terminal_id: "dev-2",
        session_name: "cao-feature-build",
        timestamp: nowIso(),
        detail: { agent_name: "developer" },
      },
      {
        id: "s5",
        kind: "handoff",
        terminal_id: "sup-1",
        session_name: "cao-feature-build",
        timestamp: nowIso(),
        detail: { to: "test-engineer" },
      },
      {
        id: "s6",
        kind: "file_mod",
        terminal_id: "dev-1",
        session_name: "cao-feature-build",
        timestamp: nowIso(),
        detail: { path: "ext_apps/apps.py" },
      },
      {
        id: "s7",
        kind: "launch",
        terminal_id: "test-1",
        session_name: "cao-feature-build",
        timestamp: nowIso(),
        detail: { agent_name: "test-engineer" },
      },
      {
        id: "s8",
        kind: "completion",
        terminal_id: "test-1",
        session_name: "cao-feature-build",
        timestamp: nowIso(),
        detail: { result: "tests green" },
      },
      {
        id: "s9",
        kind: "a2a_delegation",
        terminal_id: "sup-1",
        session_name: "cao-review",
        timestamp: nowIso(),
        detail: { to: "reviewer" },
      },
      {
        id: "s10",
        kind: "file_mod",
        terminal_id: "dev-2",
        session_name: "cao-feature-build",
        timestamp: nowIso(),
        detail: { path: "shared/mcpApp.ts" },
      },
      {
        id: "s11",
        kind: "error",
        terminal_id: "doc-1",
        session_name: "cao-review",
        timestamp: nowIso(),
        detail: { error: "link check failed" },
      },
    ],
    toolCalls: {},
    submits: 0,
    modelNotes: [],
  };

  // Default deterministic fleet — the state the e2e specs assert against.
  const DEFAULT = {
    terminals: [
      {
        id: "t1",
        session_name: "cao-main",
        provider: "kiro_cli",
        agent_profile: "builder",
        status: "processing",
        window: "w0",
        last_active: null,
      },
    ],
    events: [
      {
        id: "seed-1",
        kind: "launch",
        terminal_id: "t1",
        session_name: "cao-main",
        timestamp: nowIso(),
        detail: {},
      },
    ],
  };

  const chosen = useRich ? RICH : DEFAULT;
  const state = {
    terminals: chosen.terminals.map((t) => ({ ...t })),
    events: chosen.events.map((e) => ({ ...e })),
    toolCalls: {},
    submits: 0,
    modelNotes: [],
  };
  // Which terminal the agent-detail view hydrates with on open.
  const AGENT_VIEW_ID = useRich ? "sup-1" : "t1";

  // Per-role output tails so the agent-detail view reads like a real terminal.
  const OUTPUT_TAILS = {
    "sup-1":
      "--- code_supervisor (cao-feature-build) ---\n" +
      "[plan] MCP Apps fleet UI — 4 tracks\n" +
      "→ assign developer: implement ui_meta permissions  (dev-1)\n" +
      "→ assign developer: host-delegated open-link        (dev-2)\n" +
      "→ handoff test-engineer: coverage + JIT gates        (test-1)\n" +
      "→ assign reviewer: canonical-source audit            (rev-1)\n" +
      "waiting on rev-1 (review) and doc-1 (docs)…",
    "dev-1":
      "--- developer (dev-1) ---\n" +
      "edit ext_apps/apps.py: add _meta.ui.permissions (object)\n" +
      "run: pytest test/ext_apps -q … 13 passed\n" +
      "processing: reconcile preferredFrameSize comment",
    "dev-2":
      "--- developer (dev-2 · claude_code) ---\n" +
      "edit shared/mcpApp.ts: openLink() → ui/open-link\n" +
      "idle: awaiting supervisor review",
    "test-1":
      "--- test-engineer (test-1 · codex) ---\n" +
      "vitest run … 63 passed\ncoverage 92.16% ≥ 90% floor\ncompleted ✓",
    "rev-1":
      "--- reviewer (rev-1) ---\n" +
      "audit: references → canonical sources\n" +
      "WAITING: approve protocolVersion 2026-01-26 change? [y/N]",
    "doc-1":
      "--- documentation-curator (doc-1 · gemini_cli) ---\n" +
      "ERROR: link check failed for draft/apps.mdx (stale)\nretrying with 2026-01-26…",
  };

  function dashboardSnapshot() {
    const sessionNames = Array.from(
      new Set(state.terminals.map((t) => t.session_name)),
    );
    return {
      sessions: sessionNames.map((n) => ({ id: n, name: n, status: "active" })),
      terminals: state.terminals.map((t) => ({ ...t })),
      counts: {
        sessions: sessionNames.length,
        terminals: state.terminals.length,
      },
      scopes: ["cao:read", "cao:write", "cao:admin"],
    };
  }

  function agentSnapshot(terminalId) {
    const t =
      state.terminals.find((x) => x.id === terminalId) || state.terminals[0];
    return {
      terminal_id: t.id,
      session_name: t.session_name,
      provider: t.provider,
      agent_profile: t.agent_profile,
      status: t.status,
      last_active: t.last_active,
      output_tail: OUTPUT_TAILS[t.id] || `--- terminal ${t.id} ---\nready.`,
      scopes: ["cao:read", "cao:write", "cao:admin"],
    };
  }

  // Canned knowledge-graph snapshot for the ui://cao/graph view. Mirrors
  // GraphView.to_dict()'s wire shape (nodes id/kind/label/status/attrs; edges
  // source/target/type/attrs; meta). Deliberately exercises all three FR-19
  // visual signals so the render proof is meaningful: `auth` is a hub
  // (attrs.is_hub + several outgoing edges), `orphan-note` is dimmed
  // (attrs.is_orphan), and login↔session is a red `contradiction` edge. Every
  // edge endpoint resolves to a declared node id — the GraphView model rejects
  // dangling edges.
  function graphSnapshot() {
    const now = nowIso();
    const nodes = [
      {
        id: "auth",
        kind: "topic",
        label: "auth",
        status: "active",
        attrs: { is_hub: true },
      },
      {
        id: "login",
        kind: "topic",
        label: "login",
        status: "active",
        attrs: {},
      },
      {
        id: "session",
        kind: "topic",
        label: "session",
        status: "active",
        attrs: {},
      },
      {
        id: "tokens",
        kind: "topic",
        label: "tokens",
        status: "active",
        attrs: {},
      },
      {
        id: "orphan-note",
        kind: "topic",
        label: "orphan-note",
        status: "active",
        attrs: { is_orphan: true },
      },
    ];
    const edges = [
      {
        source: "auth",
        target: "login",
        type: "relates_to",
        attrs: { source: "related_keys" },
      },
      {
        source: "auth",
        target: "session",
        type: "relates_to",
        attrs: { source: "related_keys" },
      },
      {
        source: "auth",
        target: "tokens",
        type: "relates_to",
        attrs: { source: "related_keys" },
      },
      {
        source: "login",
        target: "session",
        type: "contradiction",
        attrs: {
          source: "wiki_lint",
          summary: "login vs session lifetime disagree",
        },
      },
    ];
    return {
      nodes,
      edges,
      meta: {
        provider: "memory",
        generated_at: now,
        node_count: nodes.length,
        edge_count: edges.length,
        filters: {},
        live: false,
        as_of: now,
      },
    };
  }

  // --- iframe registry + JSON-RPC plumbing --------------------------------
  /** view name -> { win: contentWindow, initialized: boolean, resolve } */
  const frames = new Map();
  const HOST_ORIGIN = location.origin;

  function replyTo(winInfo, id, result) {
    winInfo.win.postMessage({ jsonrpc: "2.0", id, result }, "*");
  }
  function pushTo(winInfo, method, params) {
    winInfo.win.postMessage({ jsonrpc: "2.0", method, params }, "*");
  }

  function bump(name) {
    state.toolCalls[name] = (state.toolCalls[name] || 0) + 1;
  }

  function handleToolCall(winInfo, id, name, args) {
    bump(name);
    if (name === "render_dashboard")
      return replyResult(winInfo, id, dashboardSnapshot());
    if (name === "render_agent_view")
      return replyResult(winInfo, id, agentSnapshot(args.terminal_id));
    if (name === "render_graph_view")
      return replyResult(winInfo, id, graphSnapshot());
    if (name === "cao_fetch_history")
      return replyResult(winInfo, id, {
        events: state.events.map((e) => ({ ...e })),
      });
    if (name === "subscribe_events")
      return replyResult(winInfo, id, {
        sse_url: "/events",
        history_tool: "cao_fetch_history",
        ring_capacity: 500,
      });
    if (name === "submit_command") {
      state.submits += 1;
      return replyResult(winInfo, id, applySubmit(args));
    }
    winInfo.win.postMessage(
      {
        jsonrpc: "2.0",
        id,
        error: { code: -32601, message: `unknown tool ${name}` },
      },
      "*",
    );
  }

  // A real host echoes a CallToolResult with a plain-text content block and a
  // structuredContent payload; the View prefers structuredContent.
  function replyResult(winInfo, id, structured) {
    replyTo(winInfo, id, {
      content: [{ type: "text", text: JSON.stringify(structured) }],
      structuredContent: structured,
    });
  }

  function applySubmit(args) {
    const kind = args.kind;
    const payload = args.payload || {};
    if (kind === "send_message" || kind === "assign") {
      const ev = {
        id: `ev-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
        kind: kind === "assign" ? "handoff" : "handoff",
        terminal_id: payload.terminal_id || null,
        session_name: null,
        timestamp: new Date().toISOString(),
        detail: {},
      };
      state.events.push(ev);
      // Relay into the live SSE feed so the event-stream view updates.
      void fetch("/emit", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ event: ev }),
      });
    }
    return { success: true, kind };
  }

  function onMessage(event) {
    const data = event.data;
    if (!data || data.jsonrpc !== "2.0") return;
    // Find which registered iframe sent this.
    let winInfo = null;
    for (const info of frames.values()) {
      if (info.win === event.source) {
        winInfo = info;
        break;
      }
    }
    if (!winInfo) return;

    const { id, method, params } = data;
    if (method === "ui/initialize") {
      replyTo(winInfo, id, {
        hostContext: { theme: "light", uiSurface: true },
        // Advertise host-delegated capabilities so the views surface them
        // (e.g. the dashboard's "Open full Web UI" → ui/open-link).
        hostCapabilities: { openLinks: {} },
      });
      return;
    }
    if (method === "ui/notifications/initialized") {
      winInfo.initialized = true;
      // Deliver the "tool result that opened the view" so views needing an
      // initial payload (the agent view needs a terminal_id) hydrate.
      if (winInfo.view === "agent") {
        pushTo(winInfo, "ui/notifications/tool-result", {
          structuredContent: agentSnapshot(AGENT_VIEW_ID),
        });
      }
      if (winInfo.view === "graph") {
        pushTo(winInfo, "ui/notifications/tool-result", {
          structuredContent: graphSnapshot(),
        });
      }
      if (winInfo.resolve) winInfo.resolve();
      return;
    }
    if (method === "ui/update-model-context") {
      state.modelNotes.push(params);
      replyTo(winInfo, id, {});
      return;
    }
    if (method === "tools/call") {
      handleToolCall(winInfo, id, params.name, params.arguments || {});
      return;
    }
    if (id !== undefined && id !== null) {
      winInfo.win.postMessage(
        {
          jsonrpc: "2.0",
          id,
          error: { code: -32601, message: `unknown ${method}` },
        },
        "*",
      );
    }
  }

  window.addEventListener("message", onMessage);

  // --- embed the requested bundle(s) --------------------------------------
  const container = document.getElementById("frames");
  const viewsToEmbed = view === "combo" ? ["agent", "event-stream"] : [view];

  for (const v of viewsToEmbed) {
    const iframe = document.createElement("iframe");
    iframe.dataset.view = v;
    iframe.src = `/bundles/${v}.html`;
    const info = { view: v, win: null, initialized: false, resolve: null };
    info.ready = new Promise((res) => (info.resolve = res));
    iframe.addEventListener("load", () => {
      info.win = iframe.contentWindow;
    });
    // contentWindow is available synchronously after append in most engines,
    // but we also set it on load above to be safe.
    container.appendChild(iframe);
    info.win = iframe.contentWindow;
    info.el = iframe;
    frames.set(v, info);
  }

  // --- test control surface (driven by Playwright) ------------------------
  window.__host = {
    view,
    ready() {
      return Promise.all(Array.from(frames.values()).map((f) => f.ready));
    },
    launch(id, profile) {
      state.terminals.push({
        id,
        session_name: "cao-main",
        provider: "kiro_cli",
        agent_profile: profile || id,
        status: "idle",
        window: "w1",
        last_active: null,
      });
      const dash = frames.get("dashboard");
      if (dash)
        pushTo(dash, "ui/notifications/tool-result", {
          structuredContent: dashboardSnapshot(),
        });
    },
    stop(id) {
      const t = state.terminals.find((x) => x.id === id);
      if (t) t.status = "stopped";
      const dash = frames.get("dashboard");
      if (dash)
        pushTo(dash, "ui/notifications/tool-result", {
          structuredContent: dashboardSnapshot(),
        });
      const agent = frames.get("agent");
      if (agent)
        pushTo(agent, "ui/notifications/tool-result", {
          structuredContent: agentSnapshot(id),
        });
    },
    teardown() {
      for (const info of frames.values())
        pushTo(info, "ui/resource-teardown", { reason: "host-unmount" });
    },
    toolCallCount(name) {
      return state.toolCalls[name] || 0;
    },
    submitCount() {
      return state.submits;
    },
    modelNoteCount() {
      return state.modelNotes.length;
    },
  };
})();
