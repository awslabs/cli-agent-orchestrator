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

const region = () => screen.getByRole("region", { name: "Fleet" });

const term = (id: string, extra: Record<string, unknown> = {}) => ({
  id,
  session_name: "main",
  provider: "kiro_cli",
  agent_profile: "dev",
  window: "w",
  status: "idle",
  last_active: null,
  ...extra,
});

const synthetic = (): Frame => ({
  type: "STATE_SNAPSHOT",
  snapshot: {
    sessions: [{ id: "main", name: "main", status: "active" }],
    terminals: [term("t-local"), term("t-remote", { runner_id: "rn-a", status: "waiting_user_answer" })],
    counts: { sessions: 1, terminals: 2 },
    scopes: [],
    profiles: [{ name: "dev", source: "kiro", role: "developer", loadable: true }],
  },
});

describe("the fleet panel", () => {
  it("says so when no fleet has arrived", () => {
    mount();
    expect(region().textContent).toContain("No terminals yet.");
  });

  it("groups the captured local fleet under `local`", async () => {
    const store = mount();
    await feed(store, fixtureFrames("ambient.sse"));
    const headings = within(region()).getAllByRole("heading", { level: 3 });
    expect(headings.map((h) => h.textContent)).toEqual(["local"]);
  });

  it("groups synthetic runners, local first", async () => {
    const store = mount();
    await feed(store, [synthetic()]);
    expect(within(region()).getAllByRole("heading", { level: 3 }).map((h) => h.textContent)).toEqual(["local", "rn-a"]);
    expect(region().textContent).toContain("t-remote");
    expect(region().textContent).toContain("waiting_user_answer");
  });

  it("shows the profile behind a terminal as an agent_card from the catalog", async () => {
    const store = mount();
    await feed(store, [synthetic()]);
    const cards = within(region()).getAllByRole("article", { name: "agent_card" });
    expect(cards).toHaveLength(2);
    expect(cards[0].textContent).toContain("developer");
    expect(cards[0].textContent).toContain("kiro");
    expect(region().textContent).toContain("from the CAO profile catalog");
  });

  it("renders hostile strings as text", async () => {
    const store = mount();
    await feed(store, [
      {
        type: "STATE_SNAPSHOT",
        snapshot: {
          sessions: [],
          terminals: [term("x", { runner_id: "<script>alert(1)</script>", agent_profile: "<img src=x onerror=alert(1)>" })],
          counts: { sessions: 0, terminals: 1 },
          scopes: [],
          profiles: [],
        },
      },
    ]);
    expect(region().querySelector("script, img")).toBeNull();
    expect(region().textContent).toContain("<script>alert(1)</script>");
  });

  it("has no controls", async () => {
    const store = mount();
    await feed(store, [synthetic()]);
    expect(within(region()).queryAllByRole("button")).toHaveLength(0);
    expect(region().querySelector("form, input, select, textarea, button")).toBeNull();
    expect(region().textContent).toContain("This panel has no controls");
  });

  it("says when the stream's deltas could not be applied", async () => {
    const store = mount();
    await feed(store, [synthetic(), { type: "STATE_DELTA", delta: [{ op: "frobnicate", path: "/x" }] }]);
    expect(within(region()).getByRole("status").textContent).toContain("out of sync");
  });
});

describe("Host resources strip", () => {
  const strip = () => screen.getByRole("region", { name: "Host resources" });
  const snap = (host: unknown): Frame => ({
    type: "STATE_SNAPSHOT",
    snapshot: { sessions: [], terminals: [term("a")], counts: {}, scopes: [], host },
  });

  it("shows the local numbers, and no control", async () => {
    const store = mount();
    await feed(store, [
      snap([
        { runner_id: null, sampled_at: "2026-11-12T00:00:00+00:00", cpu_percent: 7, load_per_core: 0.4, memory_used_percent: 55, memory_pressure: "normal" },
      ]),
    ]);
    const text = strip().textContent ?? "";
    expect(text).toContain("7%");
    expect(text).toContain("0.4");
    expect(text).toContain("55%");
    expect(text).toContain("normal");
    expect(within(strip()).queryAllByRole("button")).toHaveLength(0);
    expect(strip().querySelector("form,input,select,textarea")).toBeNull();
  });

  it("a missing field is a dash, never 0", async () => {
    const store = mount();
    await feed(store, [snap([{ runner_id: null, sampled_at: "2026-11-12T00:00:00+00:00", load_per_core: 0.4 }])]);
    const text = strip().textContent ?? "";
    expect(text).toContain("CPU—");
    expect(text).toContain("Memory used—");
    expect(text).toContain("Memory pressure—");
    expect(text).not.toMatch(/CPU0/);
  });

  it("an empty or missing host says not reported", async () => {
    const store = mount();
    await feed(store, [snap([])]);
    expect(strip().textContent).toContain("not reported");
  });
});
