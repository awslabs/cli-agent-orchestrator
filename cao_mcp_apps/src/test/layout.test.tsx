// Component tests for the container-query dashboard layout.
//
// happy-dom has no layout engine, so we cannot read computed grid tracks. We
// instead verify the structural + stylesheet contract that drives the layout:
//   - the grid container and all agent cards render (no truncation at 350px),
//   - the stylesheet declares the <=350px single-column and >=1280px
//     multi-column container queries,
//   - the rendered grid is stable (snapshot) at both widths.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Dashboard } from "../dashboard/Dashboard";
import type { DashboardSnapshot } from "../shared/types";

// vitest runs from the package root; read the stylesheet relative to cwd.
const STYLES = readFileSync(
  resolve(process.cwd(), "src/shared/styles.css"),
  "utf8",
);

function snapshotWith(n: number): DashboardSnapshot {
  return {
    sessions: [{ id: "cao-x", name: "cao-x", status: "active" }],
    terminals: Array.from({ length: n }, (_, i) => ({
      id: `term${i}`,
      session_name: "cao-x",
      provider: "kiro_cli",
      agent_profile: `agent-${i}-with-a-very-long-name-that-must-wrap-not-truncate`,
      window: "w",
      status: "idle",
      last_active: null,
    })),
    counts: { sessions: 1, terminals: n },
    scopes: ["cao:read", "cao:write", "cao:admin"],
  };
}

function setRootWidth(px: number): void {
  const root = document.querySelector(".cao-root") as HTMLElement | null;
  if (root) root.style.width = `${px}px`;
}

describe("dashboard container-query layout", () => {
  it("renders the grid and all cards with no truncation at 350px", () => {
    render(<Dashboard initialSnapshot={snapshotWith(3)} />);
    setRootWidth(350);
    const grid = screen.getByTestId("agent-grid");
    expect(grid).toBeTruthy();
    // No truncation == every agent card is present in the DOM.
    expect(screen.getAllByTestId("agent-card")).toHaveLength(3);
    expect(grid.innerHTML).toMatchSnapshot("dashboard-grid-350");
  });

  it("renders the same grid structure at 1280px", () => {
    render(<Dashboard initialSnapshot={snapshotWith(3)} />);
    setRootWidth(1280);
    const grid = screen.getByTestId("agent-grid");
    expect(screen.getAllByTestId("agent-card")).toHaveLength(3);
    expect(grid.innerHTML).toMatchSnapshot("dashboard-grid-1280");
  });

  it("shows the empty placeholder and no cards with zero agents", () => {
    render(<Dashboard initialSnapshot={snapshotWith(0)} />);
    expect(screen.getByTestId("empty-placeholder")).toBeTruthy();
    expect(screen.queryByTestId("agent-card")).toBeNull();
  });

  it("stylesheet declares the 350px single-column and 1280px multi-column queries", () => {
    expect(STYLES).toContain("container-type: inline-size");
    expect(STYLES).toMatch(/@container cao \(max-width: 350px\)/);
    expect(STYLES).toMatch(/@container cao \(min-width: 1280px\)/);
    // 1280px rule uses a multi-column grid.
    const wide = STYLES.slice(STYLES.indexOf("min-width: 1280px"));
    expect(wide).toMatch(/grid-template-columns:\s*repeat\(3/);
  });
});
