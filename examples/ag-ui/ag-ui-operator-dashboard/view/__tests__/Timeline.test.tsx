// @vitest-environment jsdom
import { act, cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { App } from "../App.tsx";
import { createStore } from "../store.ts";
import { fakeFetch, fixtureFrames } from "../../core/__tests__/helpers.ts";
import type { Frame } from "../../core/index.ts";

afterEach(cleanup);

const BASE = "http://127.0.0.1:9894";

function mount() {
  const { fetch } = fakeFetch([]);
  const store = createStore({ base: BASE, threadId: "view", fetchImpl: fetch });
  render(<App store={store} server={BASE} ambientStatus="open" />);
  return store;
}

const feed = async (store: ReturnType<typeof mount>, frames: Frame[]) => {
  await act(async () => {
    for (const f of frames) await store.controller.ambient(f);
  });
};

const region = () => screen.getByRole("region", { name: "Session timeline" });

describe("the session timeline panel", () => {
  it("says so when there are no calls", () => {
    mount();
    expect(region().textContent).toContain("No calls yet.");
    expect(within(region()).queryAllByRole("article")).toHaveLength(0);
  });

  it("lists the captured assign as completed and the handoff as open, with who called whom", async () => {
    const store = mount();
    await feed(store, fixtureFrames("session-timeline.sse"));
    const rows = within(region()).getAllByRole("article");
    expect(rows).toHaveLength(2);
    expect(within(rows[0]).getByRole("heading").textContent).toBe("assign: 1dd0761a → 5ac6264b");
    expect(within(rows[0]).getByTestId("call-state").textContent).toBe("completed");
    expect(rows[0].textContent).toContain("Completed");
    expect(rows[0].textContent).toContain("terminal:1dd0761a");
    expect(within(rows[1]).getByRole("heading").textContent).toBe("handoff: 1dd0761a → be17ba90");
    expect(within(rows[1]).getByTestId("call-state").textContent).toBe("open");
    // The capture ends with the stream closing its open call. That is not a completion.
    expect(rows[1].textContent).not.toContain("Completed");
  });

  it("updates in place when the end of a call arrives later", async () => {
    const store = mount();
    const frames = fixtureFrames("session-timeline.sse").filter((f) => f.type === "TOOL_CALL_START" || (f.type === "TOOL_CALL_END" && f.metadata));
    await feed(store, frames.slice(0, 2));
    expect(within(region()).getAllByTestId("call-state").map((n) => n.textContent)).toEqual(["open", "open"]);
    await feed(store, frames.slice(2, 3));
    expect(within(region()).getAllByTestId("call-state").map((n) => n.textContent)).toEqual(["completed", "open"]);
  });

  it("after a reconnect a held call reads as unconfirmed, and an end later completes it", async () => {
    const store = mount();
    const frames = fixtureFrames("session-timeline.sse").filter((f) => f.type === "TOOL_CALL_START");
    await feed(store, frames);
    await act(async () => store.controller.reconnected());
    expect(within(region()).getAllByTestId("call-state").map((n) => n.textContent)).toEqual([
      "open; its end was not seen across a reconnect",
      "open; its end was not seen across a reconnect",
    ]);
    await feed(store, fixtureFrames("session-timeline.sse").filter((f) => f.type === "TOOL_CALL_END"));
    expect(within(region()).getAllByTestId("call-state").map((n) => n.textContent)).toEqual([
      "completed",
      "open; its end was not seen across a reconnect",
    ]);
  });

  it("shows the captured superseded call as ended, not open, and the newer call as completed", async () => {
    const store = mount();
    await feed(store, fixtureFrames("session-superseded.sse"));
    const rows = within(region()).getAllByRole("article");
    expect(rows.map((r) => within(r).getByTestId("call-state").textContent)).toEqual([
      "ended: superseded by a newer call to the same receiver",
      "completed",
    ]);
    expect(rows[0].textContent).toContain("Ended");
    expect(rows[0].textContent).not.toContain("Completed");
    expect(rows[1].textContent).toContain("Completed");
    await act(async () => store.controller.reconnected());
    expect(within(region()).getAllByTestId("call-state")[0].textContent).toBe("ended: superseded by a newer call to the same receiver");
  });

  it("shows hostile names as text, shows no message text, and has no controls", async () => {
    const store = mount();
    await feed(store, [
      {
        type: "TOOL_CALL_START",
        toolCallId: "x1",
        timestamp: 1_794_358_170_090,
        toolCallName: "<img src=x onerror=alert(1)>",
        metadata: { cao: { eventId: "x1", sender: "<script>alert(1)</script>", receiver: "r", orchestrationType: "assign", message: "secret body" } },
        message: "secret body",
      },
    ]);
    expect(region().textContent).toContain("assign: <script>alert(1)</script> → r");
    expect(region().textContent).toContain("2026-11-11T00:49:30.090Z");
    expect(region().textContent).toContain("sender <script>alert(1)</script> (not a subagent)");
    expect(region().textContent).not.toContain("secret body");
    expect(region().querySelector("img, script")).toBeNull();
    expect(within(region()).queryAllByRole("button")).toHaveLength(0);
  });
});
