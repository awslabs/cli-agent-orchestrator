// Component tests for the ui://cao/graph view.
//
// Sigma.js requires a real WebGL2 context, which happy-dom does not provide,
// so `sigma` is mocked here: the fake records the graphology graph it was
// constructed with and lets tests simulate a `clickNode` event. Assertions
// target the graphology node/edge attributes GraphView sets (hub size, orphan
// color, contradiction edge color) — the same attrs the mocked Sigma instance
// would read to render.
//
// The FakeSigma mock never runs Sigma's real refresh()/node-position and
// edge-program validation, so it can't catch a missing layout or an
// unregistered edge `type`. The "Sigma mount contract" describe block below
// asserts directly on the graphology graph buildGraph() produces — every
// node has finite x/y (Sigma v3 throws at construction otherwise) and no
// edge carries a `type` outside the set Sigma v3 core actually registers.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { McpApp } from "../shared/mcpApp";
import { MockHost, type MockHostOptions } from "../test/mockHost";
import type { GraphViewData } from "./types";

type ClickNodeHandler = (payload: { node: string }) => void;

interface FakeSigmaInstance {
  graph: unknown;
  container: HTMLElement;
  handlers: Record<string, ClickNodeHandler[]>;
  killed: boolean;
  on(event: string, handler: ClickNodeHandler): void;
  emit(event: string, payload: { node: string }): void;
  kill(): void;
}

// vi.mock is hoisted above this file's imports, so the fake class must be
// constructed inside vi.hoisted rather than referenced from a later
// module-scope declaration.
const { FakeSigma, getLastMockSigma, resetLastMockSigma } = vi.hoisted(() => {
  let last: FakeSigmaInstance | undefined;

  class FakeSigmaImpl implements FakeSigmaInstance {
    graph: unknown;
    container: HTMLElement;
    handlers: Record<string, ((payload: { node: string }) => void)[]> = {};
    killed = false;

    constructor(graph: unknown, container: HTMLElement) {
      this.graph = graph;
      this.container = container;
      last = this;
    }

    on(event: string, handler: (payload: { node: string }) => void): void {
      const list = this.handlers[event] ?? [];
      list.push(handler);
      this.handlers[event] = list;
    }

    emit(event: string, payload: { node: string }): void {
      for (const handler of this.handlers[event] ?? []) handler(payload);
    }

    kill(): void {
      this.killed = true;
    }
  }

  return {
    FakeSigma: FakeSigmaImpl,
    getLastMockSigma: () => last,
    resetLastMockSigma: () => {
      last = undefined;
    },
  };
});

vi.mock("sigma", () => ({
  default: FakeSigma,
}));

// eslint-disable-next-line import/first
import { GraphView } from "./GraphView";

function lastMockSigma(): FakeSigmaInstance | undefined {
  return getLastMockSigma();
}

afterEach(() => {
  cleanup();
  resetLastMockSigma();
});

function makeApp(host: MockHost): McpApp {
  return new McpApp({
    scope: host.appWindow as unknown as Window,
    target: host.appTarget as unknown as Window,
  });
}

function buildHost(opts: MockHostOptions = {}): MockHost {
  return new MockHost(opts);
}

function snapshot(overrides: Partial<GraphViewData> = {}): GraphViewData {
  return {
    nodes: [
      {
        id: "hub1",
        kind: "topic",
        label: "Hub",
        status: "active",
        attrs: { is_hub: true },
      },
      {
        id: "orphan1",
        kind: "topic",
        label: "Orphan",
        status: "active",
        attrs: { is_orphan: true },
      },
      { id: "n3", kind: "topic", label: "Plain", status: "active", attrs: {} },
    ],
    edges: [
      { source: "hub1", target: "orphan1", type: "contradiction", attrs: {} },
      { source: "hub1", target: "n3", type: "relates_to", attrs: {} },
    ],
    meta: {},
    ...overrides,
  };
}

describe("GraphView — happy path visual markers", () => {
  it("sets hub size, orphan color, and contradiction edge styling on the graphology graph", async () => {
    render(<GraphView initialSnapshot={snapshot()} />);

    await waitFor(() => expect(lastMockSigma()).toBeDefined());
    const graph = lastMockSigma()!.graph as import("graphology").default;

    const hubAttrs = graph.getNodeAttributes("hub1");
    expect(hubAttrs.size).toBe(12);

    const orphanAttrs = graph.getNodeAttributes("orphan1");
    expect(orphanAttrs.color).toBe("#9ca3af");

    const plainAttrs = graph.getNodeAttributes("n3");
    expect(plainAttrs.size).toBe(6);
    expect(plainAttrs.color).toBe("#2563eb");

    const contradictionEdge = graph.getEdgeAttributes(
      graph.edge("hub1", "orphan1"),
    );
    expect(contradictionEdge.color).toBe("#dc2626");

    const relatesEdge = graph.getEdgeAttributes(graph.edge("hub1", "n3"));
    expect(relatesEdge.color).toBe("#94a3b8");
  });

  it("mounts the canvas container", async () => {
    render(<GraphView initialSnapshot={snapshot()} />);
    expect(screen.getByTestId("graph-canvas")).toBeTruthy();
  });
});

// FakeSigma never calls Sigma's real refresh()/edge-program resolution, so it
// can't catch either bug that slipped past review: (1) nodes with no x/y
// position — Sigma v3's refresh() throws "could not find a valid position"
// for any node missing one; (2) an edge `type` that isn't a program Sigma v3
// core actually registers ("line", "arrow") — Sigma throws "could not find a
// suitable program for edge type '<type>'". These assertions run directly
// against the graphology graph buildGraph() produces, which is the same
// object Sigma itself would validate at construction — no WebGL required.
describe("GraphView — Sigma mount contract", () => {
  const SIGMA_CORE_EDGE_TYPES = new Set([undefined, "line", "arrow"]);

  it("assigns a finite x/y position to every node before mounting", async () => {
    render(<GraphView initialSnapshot={snapshot()} />);

    await waitFor(() => expect(lastMockSigma()).toBeDefined());
    const graph = lastMockSigma()!.graph as import("graphology").default;

    expect(graph.order).toBeGreaterThan(0);
    graph.forEachNode((node) => {
      const x = graph.getNodeAttribute(node, "x");
      const y = graph.getNodeAttribute(node, "y");
      expect(Number.isFinite(x)).toBe(true);
      expect(Number.isFinite(y)).toBe(true);
    });
  });

  it("never assigns an edge type outside Sigma v3's registered programs", async () => {
    render(<GraphView initialSnapshot={snapshot()} />);

    await waitFor(() => expect(lastMockSigma()).toBeDefined());
    const graph = lastMockSigma()!.graph as import("graphology").default;

    expect(graph.size).toBeGreaterThan(0);
    graph.forEachEdge((edge) => {
      const type = graph.getEdgeAttribute(edge, "type");
      expect(SIGMA_CORE_EDGE_TYPES.has(type)).toBe(true);
    });
  });

  it("still gives contradiction edges a distinct visual signal via color alone", async () => {
    render(<GraphView initialSnapshot={snapshot()} />);

    await waitFor(() => expect(lastMockSigma()).toBeDefined());
    const graph = lastMockSigma()!.graph as import("graphology").default;

    const contradictionEdge = graph.getEdgeAttributes(
      graph.edge("hub1", "orphan1"),
    );
    const relatesEdge = graph.getEdgeAttributes(graph.edge("hub1", "n3"));
    expect(contradictionEdge.color).toBe("#dc2626");
    expect(relatesEdge.color).toBe("#94a3b8");
    expect(contradictionEdge.color).not.toBe(relatesEdge.color);
  });
});

describe("GraphView — duplicate / dangling edge guards", () => {
  it("renders without throwing when a topic pair carries BOTH a relates_to and a contradiction edge", async () => {
    // graphology's simple Graph throws on a second edge between the same pair.
    // The memory provider can emit relates_to + contradiction for one pair;
    // buildGraph must keep the first and skip the duplicate rather than crash.
    const dup = snapshot({
      edges: [
        { source: "hub1", target: "orphan1", type: "relates_to", attrs: {} },
        { source: "hub1", target: "orphan1", type: "contradiction", attrs: {} },
      ],
    });

    render(<GraphView initialSnapshot={dup} />);

    // A throw in buildGraph would prevent the canvas from ever mounting.
    await waitFor(() => expect(lastMockSigma()).toBeDefined());
    const graph = lastMockSigma()!.graph as import("graphology").default;
    // Exactly one edge survives for the duplicated pair.
    expect(graph.size).toBe(1);
    expect(graph.hasEdge("hub1", "orphan1")).toBe(true);
  });

  it("skips an edge that references an unknown node instead of throwing", async () => {
    const dangling = snapshot({
      edges: [
        { source: "hub1", target: "n3", type: "relates_to", attrs: {} },
        { source: "hub1", target: "ghost", type: "relates_to", attrs: {} },
      ],
    });

    render(<GraphView initialSnapshot={dangling} />);

    await waitFor(() => expect(lastMockSigma()).toBeDefined());
    const graph = lastMockSigma()!.graph as import("graphology").default;
    expect(graph.size).toBe(1);
    expect(graph.hasEdge("hub1", "n3")).toBe(true);
    expect(graph.hasNode("ghost")).toBe(false);
  });
});

describe("GraphView — click-through delegates navigation (FR-19 AC2)", () => {
  it("invokes onOpenTopic with the clicked node id and its attrs", async () => {
    const onOpenTopic = vi.fn();
    render(
      <GraphView initialSnapshot={snapshot()} onOpenTopic={onOpenTopic} />,
    );

    await waitFor(() => expect(lastMockSigma()).toBeDefined());
    lastMockSigma()!.emit("clickNode", { node: "hub1" });

    expect(onOpenTopic).toHaveBeenCalledOnce();
    expect(onOpenTopic.mock.calls[0][0]).toBe("hub1");
    expect(onOpenTopic.mock.calls[0][1]).toMatchObject({ size: 12 });
  });
});

describe("GraphView — empty state", () => {
  it("renders the empty placeholder and mounts no Sigma canvas", () => {
    render(<GraphView initialSnapshot={{ nodes: [], edges: [], meta: {} }} />);
    expect(screen.getByTestId("empty-placeholder")).toBeTruthy();
    expect(screen.queryByTestId("graph-canvas")).toBeNull();
    expect(lastMockSigma()).toBeUndefined();
  });
});

describe("GraphView — loading state", () => {
  it("renders the loading placeholder when no snapshot is available yet", () => {
    render(<GraphView />);
    expect(screen.getByTestId("graph-loading")).toBeTruthy();
    expect(screen.queryByTestId("graph-canvas")).toBeNull();
  });
});

describe("GraphView — error state", () => {
  it("shows the retry banner on a failed poll and clears it on retry", async () => {
    const host = buildHost({
      tools: {
        render_graph_view: () => {
          throw new Error("backplane down");
        },
      },
    });
    const app = makeApp(host);
    render(<GraphView app={app} initialSnapshot={snapshot()} />);

    await waitFor(() =>
      expect(screen.getByTestId("retry-banner")).toBeTruthy(),
    );
    // No unhandled throw escaped the render.
    expect(screen.queryByTestId("empty-placeholder")).toBeNull();

    app.disconnect();
  });
});

describe("GraphView — lifecycle invariant", () => {
  it("registers onToolResult before connect() resolves", async () => {
    const host = buildHost({ tools: {} });
    const app = makeApp(host);
    const onSpy = vi.spyOn(app, "on");
    const connectSpy = vi.spyOn(app, "connect");

    render(<GraphView app={app} initialSnapshot={snapshot()} />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    expect(onSpy).toHaveBeenCalledWith(
      "ui/notifications/tool-result",
      expect.any(Function),
    );
    // The registration call must have happened before connect() was invoked.
    const onCallOrder = onSpy.mock.invocationCallOrder[0];
    const connectCallOrder = connectSpy.mock.invocationCallOrder[0];
    expect(onCallOrder).toBeLessThan(connectCallOrder);

    app.disconnect();
  });
});

describe("GraphView — no direct fetch (FR-19 AC3)", () => {
  it("contains no fetch/XMLHttpRequest/axios token in source", () => {
    const path = resolve(__dirname, "GraphView.tsx");
    const source = readFileSync(path, "utf8");
    expect(source).not.toMatch(/\bfetch\s*\(/);
    expect(source).not.toMatch(/XMLHttpRequest/);
    expect(source).not.toMatch(/\baxios\b/);
  });
});
