// Lifecycle / transport + live-feed cells.
//
// These close the previously-untested but reachable paths the matrix tiers
// above don't exercise directly:
//   - the McpApp convenience notification handlers (tool-input, host-context
//     changed, teardown) that views may subscribe to,
//   - the EventStreamView live SSE subscription path (descriptor -> EventSource
//     -> message frame -> ingest), including malformed-frame tolerance,
//   - AgentView's interval poll applying a refreshed snapshot,
//   - an AgentStatus card click invoking its onOpen affordance,
//   - TaskControl drag-and-drop error branches (oversize payload + failed
//     submit).
//
// They assert real behaviour (not coverage for its own sake): every case here
// maps to a documented part of the bridge/view contract.

import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AgentView } from "../agent/AgentView";
import {
  EventStreamView,
  type EventSourceLike,
} from "../event-stream/EventStreamView";
import { AgentStatus } from "../shared/AgentStatus";
import { McpApp } from "../shared/mcpApp";
import { TaskControl } from "../shared/TaskControl";
import type {
  AgentDetailSnapshot,
  CaoEvent,
  SubmitCommandResult,
  TerminalView,
} from "../shared/types";
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

describe("McpApp convenience notification handlers", () => {
  it("delivers tool-input arguments, host-context changes, and teardown reason", async () => {
    const host = buildHost({ tools: {} });
    const app = makeApp(host);

    const onInput = vi.fn();
    const onCtx = vi.fn();
    const onTeardown = vi.fn();

    // Registered BEFORE connect (lifecycle invariant).
    app.onToolInput(onInput);
    app.onHostContextChanged(onCtx);
    app.onTeardown(onTeardown);

    await app.connect();

    host.pushNotification("ui/notifications/tool-input", {
      arguments: { terminal_id: "t9" },
    });
    expect(onInput).toHaveBeenCalledWith({ terminal_id: "t9" });

    host.pushNotification("ui/notifications/host-context-changed", {
      theme: "dark",
    });
    expect(onCtx).toHaveBeenCalledOnce();
    // The bridge merges the change into hostContext and hands back the merged map.
    expect(onCtx.mock.calls[0][0]).toMatchObject({ theme: "dark" });
    expect(app.hostContext).toMatchObject({ theme: "dark" });

    // Teardown notifies our handler (reason) and then releases listeners.
    host.pushNotification("ui/resource-teardown", { reason: "host-unmount" });
    expect(onTeardown).toHaveBeenCalledWith("host-unmount");

    // After teardown the message listener is gone: a later notification is dropped.
    host.pushNotification("ui/notifications/tool-input", {
      arguments: { terminal_id: "ignored" },
    });
    expect(onInput).toHaveBeenCalledOnce();
  });
});

describe("EventStreamView — live SSE subscription", () => {
  it("subscribes via the descriptor and ingests a live frame, tolerating a malformed one", async () => {
    const liveEvent: CaoEvent = {
      id: "live-1",
      kind: "completion",
      terminal_id: "t1",
      session_name: "cao-x",
      timestamp: "2026-01-01T00:00:09Z",
      detail: {},
    };

    // Capture the message listener the view registers so the test can push frames.
    let push: ((ev: { data: string }) => void) | undefined;
    let closed = false;
    const factory = (url: string): EventSourceLike => {
      // The view builds the SSE URL from base + descriptor.sse_url.
      expect(url).toBe("http://127.0.0.1:9889/events");
      return {
        addEventListener: (_type, listener) => {
          push = listener;
        },
        close: () => {
          closed = true;
        },
      };
    };

    const host = buildHost({
      tools: {
        cao_fetch_history: () => ({ events: [] }),
        subscribe_events: () => ({ sse_url: "/events" }),
      },
    });
    const app = makeApp(host);
    const view = render(
      <EventStreamView app={app} eventSourceFactory={factory} />,
    );

    // Empty ticker until a frame arrives.
    await screen.findByText("No fleet events yet");
    await waitFor(() => expect(push).toBeTypeOf("function"));

    // A malformed frame is swallowed (no crash, no row); a valid one is rendered.
    push!({ data: "{not json" });
    push!({ data: JSON.stringify(liveEvent) });

    await waitFor(() =>
      expect(screen.getAllByTestId("event-row")).toHaveLength(1),
    );

    app.disconnect();
    view.unmount();
    // Unmount closes the SSE source (listener release).
    expect(closed).toBe(true);
  });
});

describe("AgentView — interval poll refreshes the snapshot", () => {
  it("applies a freshly polled render_agent_view snapshot over the initial one", async () => {
    const initial: AgentDetailSnapshot = {
      terminal_id: "t1",
      session_name: "cao-main",
      provider: "kiro_cli",
      agent_profile: "builder",
      status: "idle",
      last_active: null,
      output_tail: "boot",
      scopes: [],
    };
    const polled: AgentDetailSnapshot = { ...initial, status: "processing" };

    const host = buildHost({
      tools: { render_agent_view: () => polled },
    });
    const app = makeApp(host);
    // initialSnapshot seeds the terminal id so connect() arms the poll loop,
    // whose immediate first tick fetches render_agent_view.
    render(<AgentView app={app} initialSnapshot={initial} />);

    await waitFor(() =>
      expect(screen.getByTestId("agent-status").textContent).toBe("processing"),
    );
    expect(host.toolCalls.some((c) => c.name === "render_agent_view")).toBe(
      true,
    );
    app.disconnect();
  });
});

describe("AgentStatus — open affordance", () => {
  it("invokes onOpen with the terminal id when the card is activated", () => {
    const terminal: TerminalView = {
      id: "term-7",
      session_name: "cao-x",
      provider: "kiro_cli",
      agent_profile: "dev",
      window: "w0",
      status: "idle",
      last_active: null,
    };
    const onOpen = vi.fn();
    render(<AgentStatus terminal={terminal} onOpen={onOpen} />);
    fireEvent.click(screen.getByTestId("agent-card"));
    expect(onOpen).toHaveBeenCalledWith("term-7");
  });
});

describe("TaskControl — drag-and-drop error branches", () => {
  function renderControl(onSubmitImpl: () => Promise<SubmitCommandResult>) {
    const onSubmit = vi.fn(onSubmitImpl);
    render(<TaskControl onSubmit={onSubmit} target="term-1" />);
    return onSubmit;
  }

  it("rejects an oversize dropped payload without submitting", async () => {
    const onSubmit = renderControl(async () => ({ success: true }));
    const huge = "x".repeat(4001);
    fireEvent.drop(screen.getByTestId("task-control"), {
      dataTransfer: { getData: () => huge },
    });
    await Promise.resolve();
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByTestId("task-error").textContent).toContain(
      "4000 characters or fewer",
    );
  });

  it("surfaces the error when a dropped reassignment is rejected by the choke point", async () => {
    const onSubmit = renderControl(async () => ({
      success: false,
      error: "scope denied",
    }));
    fireEvent.drop(screen.getByTestId("task-control"), {
      dataTransfer: { getData: () => "reassign me" },
    });
    await waitFor(() =>
      expect(screen.getByTestId("task-error").textContent).toContain(
        "scope denied",
      ),
    );
    expect(onSubmit).toHaveBeenCalledOnce();
  });
});
