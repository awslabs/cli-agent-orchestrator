// Component tier cells (happy-dom).
//
// Closes the matrix component cells:
//   - zero-agents dashboard shows ONLY a placeholder and renders NO cards,
//   - active-agents render cards with the correct status badges,
//   - escaped-markup input/metadata renders as an escaped string (no XSS),
//   - scope-gated button rendering: controls whose required scope is absent are
//     hidden (and the default-off full-scope case shows everything).

import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Dashboard } from "../dashboard/Dashboard";
import { AgentStatus } from "../shared/AgentStatus";
import { EventStream } from "../shared/EventStream";
import { TaskControl } from "../shared/TaskControl";
import type {
  CaoEvent,
  DashboardSnapshot,
  SubmitCommandResult,
  TerminalView,
} from "../shared/types";

function terminal(overrides: Partial<TerminalView> = {}): TerminalView {
  return {
    id: "term-1",
    session_name: "cao-x",
    provider: "kiro_cli",
    agent_profile: "dev",
    window: "w0",
    status: "idle",
    last_active: null,
    ...overrides,
  };
}

function snapshot(terminals: TerminalView[]): DashboardSnapshot {
  return {
    sessions: [{ id: "cao-x", name: "cao-x", status: "active" }],
    terminals,
    counts: { sessions: 1, terminals: terminals.length },
    scopes: ["cao:read", "cao:write", "cao:admin"],
  };
}

describe("19.1 — zero-agents placeholder", () => {
  it("shows only the placeholder and renders no agent cards", () => {
    render(<Dashboard initialSnapshot={snapshot([])} />);
    expect(screen.getByTestId("empty-placeholder")).toBeTruthy();
    expect(screen.queryByTestId("agent-grid")).toBeNull();
    expect(screen.queryAllByTestId("agent-card")).toHaveLength(0);
  });
});

describe("19.2 — active-agents cards with status badges", () => {
  it("renders one card per terminal with the correct status badge text/class", () => {
    const terminals = [
      terminal({ id: "t1", status: "processing", agent_profile: "alpha" }),
      terminal({ id: "t2", status: "stopped", agent_profile: "beta" }),
      terminal({ id: "t3", status: "error", agent_profile: "gamma" }),
    ];
    render(<Dashboard initialSnapshot={snapshot(terminals)} />);

    const cards = screen.getAllByTestId("agent-card");
    expect(cards).toHaveLength(3);

    const badges = screen.getAllByTestId("status-badge");
    expect(badges.map((b) => b.textContent)).toEqual([
      "processing",
      "stopped",
      "error",
    ]);
    // Each badge carries the normalized status modifier class.
    expect(badges[0].className).toContain("cao-status-processing");
    expect(badges[1].className).toContain("cao-status-stopped");
    expect(badges[2].className).toContain("cao-status-error");
  });

  it("falls back to the 'unknown' badge for an unrecognized status", () => {
    render(<AgentStatus terminal={terminal({ status: "weird-state" })} />);
    const badge = screen.getByTestId("status-badge");
    // Unknown statuses keep their text but get the safe 'unknown' modifier class.
    expect(badge.className).toContain("cao-status-unknown");
  });
});

describe("19.11 — escaped markup (no XSS)", () => {
  const XSS = '<img src=x onerror="window.__pwned=1"><script>alert(1)</script>';

  it("renders malicious event metadata as an escaped string, injecting no nodes", () => {
    const event: CaoEvent = {
      id: "e1",
      kind: "handoff",
      terminal_id: XSS,
      session_name: null,
      timestamp: "2026-01-01T00:00:00Z",
      detail: {},
    };
    const { container } = render(<EventStream events={[event]} />);
    // No live <img>/<script> node was created from the payload …
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    // … and the raw markup survives verbatim as text content.
    expect(screen.getByTestId("event-row").textContent).toContain(XSS);
  });

  it("renders a malicious agent profile/provider as text, not HTML", () => {
    const { container } = render(
      <AgentStatus
        terminal={terminal({ agent_profile: XSS, provider: XSS })}
      />,
    );
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(
      within(screen.getByTestId("agent-card")).getAllByText(XSS).length,
    ).toBeGreaterThan(0);
  });

  it("keeps a markup message in the textarea value (controlled, never parsed as HTML)", () => {
    const onSubmit = vi.fn(async (): Promise<SubmitCommandResult> => ({
      success: true,
    }));
    const { container } = render(
      <TaskControl onSubmit={onSubmit} target="t1" />,
    );
    const input = screen.getByTestId("task-input") as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: XSS } });
    expect(input.value).toBe(XSS);
    expect(container.querySelector("script")).toBeNull();
  });
});

describe("scope-gated button rendering", () => {
  function renderControls(scopes?: string[]) {
    const onSubmit = vi.fn(async (): Promise<SubmitCommandResult> => ({
      success: true,
    }));
    render(<TaskControl onSubmit={onSubmit} target="t1" scopes={scopes} />);
  }

  it("hides every control when only cao:read is granted", () => {
    renderControls(["cao:read"]);
    expect(screen.queryByTestId("btn-send_message")).toBeNull();
    expect(screen.queryByTestId("btn-assign")).toBeNull();
    expect(screen.queryByTestId("btn-shutdown_session")).toBeNull();
  });

  it("hides the admin-only destructive control when only cao:write is granted", () => {
    renderControls(["cao:write"]);
    expect(screen.getByTestId("btn-send_message")).toBeTruthy();
    expect(screen.getByTestId("btn-assign")).toBeTruthy();
    expect(screen.getByTestId("btn-interrupt")).toBeTruthy();
    // Destructive shutdown requires cao:admin — absent, so it is not rendered.
    expect(screen.queryByTestId("btn-shutdown_session")).toBeNull();
  });

  it("shows the destructive control with cao:admin granted", () => {
    renderControls(["cao:write", "cao:admin"]);
    expect(screen.getByTestId("btn-shutdown_session")).toBeTruthy();
  });

  it("default-off (no scopes) shows every control", () => {
    renderControls(undefined);
    expect(screen.getByTestId("btn-send_message")).toBeTruthy();
    expect(screen.getByTestId("btn-shutdown_session")).toBeTruthy();
  });
});

describe("supervisor distinction", () => {
  it("badges the supervisor and orders it first in the fleet grid", () => {
    const terminals = [
      terminal({ id: "w1", status: "idle", agent_profile: "developer" }),
      terminal({
        id: "sup",
        status: "processing",
        agent_profile: "code_supervisor",
      }),
      terminal({ id: "w2", status: "completed", agent_profile: "reviewer" }),
    ];
    render(<Dashboard initialSnapshot={snapshot(terminals)} />);

    // Exactly one supervisor badge.
    const badges = screen.getAllByTestId("role-badge");
    expect(badges).toHaveLength(1);
    expect(badges[0].textContent).toBe("supervisor");

    // The supervisor card sorts first and carries the accent class.
    const cards = screen.getAllByTestId("agent-card");
    expect(cards[0].getAttribute("data-terminal-id")).toBe("sup");
    expect(cards[0].className).toContain("cao-card-supervisor");
    // Worker cards do not get the supervisor accent.
    expect(cards[1].className).not.toContain("cao-card-supervisor");
  });
});
