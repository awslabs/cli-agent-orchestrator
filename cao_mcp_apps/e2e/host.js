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

  // --- canned, mutable fleet state ----------------------------------------
  const state = {
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
        timestamp: new Date().toISOString(),
        detail: {},
      },
    ],
    toolCalls: {},
    submits: 0,
    modelNotes: [],
  };

  function dashboardSnapshot() {
    return {
      sessions: [{ id: "cao-main", name: "cao-main", status: "active" }],
      terminals: state.terminals.map((t) => ({ ...t })),
      counts: { sessions: 1, terminals: state.terminals.length },
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
      output_tail: `--- terminal ${t.id} ---\nready.`,
      scopes: ["cao:read", "cao:write", "cao:admin"],
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
      });
      return;
    }
    if (method === "ui/notifications/initialized") {
      winInfo.initialized = true;
      // Deliver the "tool result that opened the view" so views needing an
      // initial payload (the agent view needs a terminal_id) hydrate.
      if (winInfo.view === "agent") {
        pushTo(winInfo, "ui/notifications/tool-result", {
          structuredContent: agentSnapshot("t1"),
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
