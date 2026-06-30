// Integration tier cells — driven through the Mock Host peer.
//
// Closes the matrix integration cells at the postMessage/JSON-RPC layer:
//   - a frame from an untrusted origin is ignored; view state unchanged,
//   - a no-UI-surface host still returns structured plain-text results,
//   - an unreachable Backplane surfaces a retry control that recovers,
//   - re-mount idempotence: `oninitialized` replay + re-mount
//     hydration both reproduce the same governance timeline.

import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Dashboard } from "../dashboard/Dashboard";
import { EventStreamView } from "../event-stream/EventStreamView";
import { AgentView } from "../agent/AgentView";
import { McpApp } from "../shared/mcpApp";
import type { CaoEvent, DashboardSnapshot } from "../shared/types";
import { MockHost, type MockHostOptions } from "./mockHost";

afterEach(() => cleanup());

function makeApp(host: MockHost): McpApp {
  return new McpApp({
    scope: host.appWindow as unknown as Window,
    target: host.appTarget as unknown as Window,
  });
}

function buildHost(opts: MockHostOptions = {}): MockHost {
  return new MockHost(opts);
}

const SAMPLE_EVENTS: CaoEvent[] = [
  {
    id: "e1",
    kind: "launch",
    terminal_id: "t1",
    session_name: "cao-x",
    timestamp: "2026-01-01T00:00:01Z",
    detail: {},
  },
  {
    id: "e2",
    kind: "handoff",
    terminal_id: "t1",
    session_name: "cao-x",
    timestamp: "2026-01-01T00:00:02Z",
    detail: {},
  },
];

const noopEventSource = () => ({
  addEventListener() {},
  close() {},
});

function snapshot(n: number): DashboardSnapshot {
  return {
    sessions: [{ id: "cao-x", name: "cao-x", status: "active" }],
    terminals: Array.from({ length: n }, (_, i) => ({
      id: `t${i}`,
      session_name: "cao-x",
      provider: "kiro_cli",
      agent_profile: `agent-${i}`,
      window: "w",
      status: "idle",
      last_active: null,
    })),
    counts: { sessions: 1, terminals: n },
    scopes: [],
  };
}

describe("19.13 — untrusted-origin frames are ignored", () => {
  it("drops a notification delivered from an unexpected origin", async () => {
    const host = buildHost({ tools: {} });
    const app = makeApp(host);
    const onToolResult = vi.fn();
    app.onToolResult(onToolResult);

    await app.connect(); // pins the host origin on the first reply

    // A forged tool-result from a different origin must be ignored.
    host.deliverFromOrigin("https://evil.example", {
      jsonrpc: "2.0",
      method: "ui/notifications/tool-result",
      params: { structuredContent: snapshot(3) },
    });
    expect(onToolResult).not.toHaveBeenCalled();

    // The same notification from the genuine host origin is delivered.
    host.pushNotification("ui/notifications/tool-result", {
      structuredContent: snapshot(3),
    });
    expect(onToolResult).toHaveBeenCalledOnce();
    app.disconnect();
  });

  it("leaves the dashboard grid unchanged when an evil-origin snapshot arrives", async () => {
    const host = buildHost({ tools: { render_dashboard: () => snapshot(0) } });
    const app = makeApp(host);
    render(<Dashboard app={app} />);

    // Empty placeholder after the initial (zero-agent) hydration.
    await screen.findByTestId("empty-placeholder");

    host.deliverFromOrigin("https://evil.example", {
      jsonrpc: "2.0",
      method: "ui/notifications/tool-result",
      params: { structuredContent: snapshot(5) },
    });

    // State unchanged: still the placeholder, no cards injected by the attacker.
    await Promise.resolve();
    expect(screen.queryByTestId("agent-card")).toBeNull();
    expect(screen.getByTestId("empty-placeholder")).toBeTruthy();
    app.disconnect();
  });
});

describe("19.9 — no-UI-surface host returns structured plain-text results", () => {
  it("delivers a plain-text content block whose payload is the structured result", async () => {
    const snap = snapshot(2);
    const host = buildHost({
      hostContext: { uiSurface: false },
      tools: { render_dashboard: () => snap },
    });
    const app = makeApp(host);
    await app.connect();

    const result = (await app.callServerTool("render_dashboard")) as {
      content: Array<{ type: string; text: string }>;
    };
    // No structuredContent (no UI surface) -> the raw CallToolResult is returned.
    expect(result.content[0].type).toBe("text");
    // The plain text is the serialized structured result and round-trips.
    expect(JSON.parse(result.content[0].text)).toEqual(snap);
    app.disconnect();
  });

  it("unwraps structuredContent for a UI-capable host", async () => {
    const snap = snapshot(1);
    const host = buildHost({
      hostContext: { uiSurface: true },
      tools: { render_dashboard: () => snap },
    });
    const app = makeApp(host);
    await app.connect();
    const result = await app.callServerTool("render_dashboard");
    expect(result).toEqual(snap);
    app.disconnect();
  });
});

describe("19.14 — unreachable Backplane surfaces a recoverable retry control", () => {
  it("shows the retry control on failure and recovers once the Backplane returns", async () => {
    let reachable = false;
    const host = buildHost({
      tools: {
        render_dashboard: () => {
          if (!reachable) throw new Error("ECONNREFUSED 127.0.0.1:9889");
          return snapshot(2);
        },
      },
    });
    const app = makeApp(host);
    render(<Dashboard app={app} />);

    // The failed initial poll surfaces the retry banner + button.
    await screen.findByTestId("retry-banner");
    expect(screen.getByTestId("retry-button")).toBeTruthy();

    // Backplane recovers; clicking Retry clears the banner and renders cards.
    reachable = true;
    fireEvent.click(screen.getByTestId("retry-button"));
    await waitFor(() =>
      expect(screen.queryByTestId("retry-banner")).toBeNull(),
    );
    expect(screen.getAllByTestId("agent-card")).toHaveLength(2);
    app.disconnect();
  });
});

describe("agent view — host-mediated hydration + choke point", () => {
  it("hydrates from the opening tool-result and routes a send through submit_command", async () => {
    const agentDetail = {
      terminal_id: "t1",
      session_name: "cao-main",
      provider: "kiro_cli",
      agent_profile: "builder",
      status: "processing",
      last_active: null,
      output_tail: "--- terminal t1 ---",
      scopes: ["cao:read", "cao:write", "cao:admin"],
    };
    const host = buildHost({
      tools: {
        render_agent_view: () => agentDetail,
        submit_command: () => ({ success: true, kind: "send_message" }),
      },
    });
    const app = makeApp(host);
    render(<AgentView app={app} />);

    // The agent view needs a terminal_id; the host delivers it as the opening
    // tool-result once initialized (host-mediated hydration).
    await waitFor(() => expect(host.initialized).toBe(true));
    host.pushNotification("ui/notifications/tool-result", {
      structuredContent: agentDetail,
    });

    await screen.findByTestId("agent-detail");
    expect(screen.getByTestId("agent-output").textContent).toContain(
      "terminal t1",
    );

    // Send a task through the choke point; a body-free model-context note is posted.
    fireEvent.change(screen.getByTestId("task-input"), {
      target: { value: "run the build" },
    });
    fireEvent.click(screen.getByTestId("btn-send_message"));

    await waitFor(() =>
      expect(host.toolCalls.some((c) => c.name === "submit_command")).toBe(
        true,
      ),
    );
    await waitFor(() => expect(host.modelNotes.length).toBeGreaterThan(0));
    app.disconnect();
  });

  it("shows the loading placeholder until a snapshot arrives", () => {
    render(<AgentView app={undefined} />);
    expect(screen.getByTestId("agent-loading")).toBeTruthy();
  });
});

describe("19.8 — iframe teardown releases listeners", () => {
  it("disconnects on ui/resource-teardown so later notifications are ignored", async () => {
    const host = buildHost({ tools: {} });
    const app = makeApp(host);
    const onToolResult = vi.fn();
    app.onToolResult(onToolResult);
    await app.connect();

    // Host tears the iframe down.
    host.pushNotification("ui/resource-teardown", { reason: "host-unmount" });

    // A subsequent (genuine-origin) notification must NOT reach a handler,
    // because the message listener was released for GC.
    host.pushNotification("ui/notifications/tool-result", {
      structuredContent: snapshot(2),
    });
    expect(onToolResult).not.toHaveBeenCalled();
  });
});

describe("re-mount idempotence", () => {
  it("oninitialized replay hydrates the dashboard from a pushed tool-result", async () => {
    const host = buildHost({ tools: { render_dashboard: () => snapshot(0) } });
    const app = makeApp(host);
    render(<Dashboard app={app} />);
    await screen.findByTestId("empty-placeholder");

    // After initialized, the host replays the current fleet as a tool-result.
    expect(host.initialized).toBe(true);
    host.pushNotification("ui/notifications/tool-result", {
      structuredContent: snapshot(3),
    });
    await waitFor(() =>
      expect(screen.getAllByTestId("agent-card")).toHaveLength(3),
    );
    app.disconnect();
  });

  it("re-fetches the same history on re-mount (idempotent timeline)", async () => {
    const tools = { cao_fetch_history: () => ({ events: SAMPLE_EVENTS }) };

    // First mount.
    const host1 = buildHost({ tools });
    const app1 = makeApp(host1);
    const first = render(
      <EventStreamView app={app1} eventSourceFactory={noopEventSource} />,
    );
    await waitFor(() =>
      expect(screen.getAllByTestId("event-row")).toHaveLength(2),
    );
    const firstHtml = screen.getByTestId("event-stream").innerHTML;
    app1.disconnect();
    first.unmount();
    cleanup();

    // Re-mount: the same history is replayed, producing an identical timeline.
    const host2 = buildHost({ tools });
    const app2 = makeApp(host2);
    render(<EventStreamView app={app2} eventSourceFactory={noopEventSource} />);
    await waitFor(() =>
      expect(screen.getAllByTestId("event-row")).toHaveLength(2),
    );
    expect(screen.getByTestId("event-stream").innerHTML).toBe(firstHtml);
    app2.disconnect();
  });
});
