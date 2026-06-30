// Host-delegated action tier — SEP-1865 `ui/open-link` and
// `ui/request-display-mode`, exercised through the Mock Host peer.
//
// Covers:
//   - the bridge `openLink` / `requestDisplayMode` methods and the
//     `canOpenLinks()` capability gate,
//   - host denial of `ui/open-link` surfaces as a rejection,
//   - the dashboard "Open full Web UI" affordance: shown only when the host
//     advertises `openLinks`, and clicking it delegates the bundled Web UI URL
//     to the host.

import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { Dashboard } from "../dashboard/Dashboard";
import { McpApp } from "../shared/mcpApp";
import type { DashboardSnapshot } from "../shared/types";
import { MockHost, type MockHostOptions } from "./mockHost";

afterEach(() => cleanup());

function makeApp(host: MockHost): McpApp {
  return new McpApp({
    scope: host.appWindow as unknown as Window,
    target: host.appTarget as unknown as Window,
  });
}

function emptySnapshot(): DashboardSnapshot {
  return {
    sessions: [],
    terminals: [],
    counts: { sessions: 0, terminals: 0 },
    scopes: [],
  };
}

describe("ui/open-link bridge", () => {
  it("delegates the URL to the host when supported", async () => {
    const host = new MockHost({ hostCapabilities: { openLinks: {} } });
    const app = makeApp(host);
    await app.connect();

    expect(app.canOpenLinks()).toBe(true);
    await app.openLink("http://127.0.0.1:9889");
    expect(host.openedLinks).toEqual(["http://127.0.0.1:9889"]);
  });

  it("reports no open-link support when the host omits the capability", async () => {
    const host = new MockHost({ hostCapabilities: {} });
    const app = makeApp(host);
    await app.connect();
    expect(app.canOpenLinks()).toBe(false);
  });

  it("rejects when the host denies the open-link request", async () => {
    const host = new MockHost({
      hostCapabilities: { openLinks: {} },
      denyOpenLink: true,
    });
    const app = makeApp(host);
    await app.connect();
    await expect(app.openLink("http://127.0.0.1:9889")).rejects.toThrow(
      /denied/i,
    );
    expect(host.openedLinks).toEqual([]);
  });
});

describe("ui/request-display-mode bridge", () => {
  it("returns the host's resulting mode and records the request", async () => {
    const host = new MockHost({ hostCapabilities: { openLinks: {} } });
    const app = makeApp(host);
    await app.connect();
    const mode = await app.requestDisplayMode("fullscreen");
    expect(mode).toBe("fullscreen");
    expect(host.displayModeRequests).toEqual(["fullscreen"]);
  });
});

describe("dashboard Open full Web UI affordance", () => {
  function buildHost(opts: MockHostOptions = {}): MockHost {
    return new MockHost({
      tools: { render_dashboard: () => emptySnapshot() },
      ...opts,
    });
  }

  it("is hidden when the host cannot open links", async () => {
    const host = buildHost({ hostCapabilities: {} });
    const app = makeApp(host);
    render(<Dashboard app={app} />);
    // Allow connect() to resolve.
    await waitFor(() => expect(host.initialized).toBe(true));
    expect(screen.queryByTestId("open-webui")).toBeNull();
  });

  it("is shown and delegates the Web UI URL when the host can open links", async () => {
    const host = buildHost({ hostCapabilities: { openLinks: {} } });
    const app = makeApp(host);
    render(<Dashboard app={app} />);

    const btn = await screen.findByTestId("open-webui");
    fireEvent.click(btn);
    await waitFor(() =>
      expect(host.openedLinks).toEqual(["http://127.0.0.1:9889"]),
    );
  });
});
