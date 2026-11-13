import { describe, expect, it } from "vitest";
import {
  agentCardFor,
  createController,
  emptyFleet,
  groupByRunner,
  LOCAL_LABEL,
  reduceFleet,
  type Fleet,
} from "../index.ts";
import type { Frame } from "../types.ts";
import { fixtureFrames } from "./helpers.ts";

const fold = (frames: Frame[]): Fleet => frames.reduce(reduceFleet, emptyFleet());

const snapshot = (snap: Record<string, unknown>): Frame => ({ type: "STATE_SNAPSHOT", snapshot: snap });
const delta = (...delta: Array<Record<string, unknown>>): Frame => ({ type: "STATE_DELTA", delta });

const term = (id: string, extra: Record<string, unknown> = {}) => ({
  id,
  session_name: "alpha",
  provider: "kiro_cli",
  agent_profile: "dev",
  window: "w",
  status: "idle",
  last_active: null,
  ...extra,
});

describe("the captured ambient stream (made before runner_id existed)", () => {
  const fleet = fold(fixtureFrames("ambient.sse"));

  it("folds the snapshot and its deltas into the live terminals", () => {
    expect(fleet.hydrated).toBe(true);
    expect(fleet.terminals.length).toBeGreaterThan(0);
    expect(fleet.terminals[0]).toMatchObject({ provider: "mock_cli", agentProfile: "developer" });
  });

  it("a row without a runner_id key is local (null), never undefined", () => {
    expect(fleet.terminals.every((t) => t.runnerId === null)).toBe(true);
    const groups = groupByRunner(fleet);
    expect(groups).toHaveLength(1);
    expect(groups[0].runnerId).toBeNull();
    expect(groups[0].label).toBe(LOCAL_LABEL);
  });
});

describe("grouping by runner", () => {
  const fleet = fold([
    snapshot({
      sessions: [{ id: "main", name: "main", status: "active" }],
      terminals: [
        term("a", { session_name: "main", runner_id: "rn-b" }),
        term("b", { session_name: "main", runner_id: null }),
        term("c", { session_name: "main", runner_id: "rn-a" }),
        term("d", { session_name: "main", runner_id: "rn-a", status: "waiting_user_answer" }),
      ],
      counts: { sessions: 1, terminals: 4 },
      scopes: [],
    }),
  ]);

  it("puts local first, then the runners by id", () => {
    expect(groupByRunner(fleet).map((g) => [g.runnerId, g.label])).toEqual([
      [null, "local"],
      ["rn-a", "rn-a"],
      ["rn-b", "rn-b"],
    ]);
  });

  it("counts waiting terminals per group", () => {
    const rtA = groupByRunner(fleet).find((g) => g.runnerId === "rn-a")!;
    expect(rtA.terminals.map((t) => t.id)).toEqual(["c", "d"]);
    expect(rtA.waiting).toBe(1);
  });

  it("keys sessions by (runner, session name): two runners may both own `main`", () => {
    const groups = groupByRunner(fleet);
    expect(groups.every((g) => g.sessions.length === 1 && g.sessions[0].name === "main")).toBe(true);
    expect(groups.map((g) => g.sessions[0].terminals.length)).toEqual([1, 2, 1]);
  });

  it("an empty fleet has no groups", () => {
    expect(groupByRunner(emptyFleet())).toEqual([]);
  });
});

describe("STATE_DELTA", () => {
  const base = snapshot({
    sessions: [],
    terminals: [term("a")],
    counts: { sessions: 0, terminals: 1 },
    scopes: ["cao:read"],
  });

  it("replaces whole keys and per-key counts", () => {
    const f = fold([
      base,
      delta(
        { op: "replace", path: "/terminals", value: [term("a", { runner_id: "rn-z" }), term("b")] },
        { op: "replace", path: "/counts/terminals", value: 2 },
      ),
    ]);
    expect(f.terminals.map((t) => [t.id, t.runnerId])).toEqual([["a", "rn-z"], ["b", null]]);
    expect(f.counts.terminals).toBe(2);
    expect(f.stale).toBe(false);
  });

  it("add and remove on a counts key", () => {
    const f = fold([base, delta({ op: "add", path: "/counts/extra", value: 3 })]);
    expect(f.counts).toMatchObject({ extra: 3 });
    const g = fold([base, delta({ op: "add", path: "/counts/extra", value: 3 }), delta({ op: "remove", path: "/counts/extra" })]);
    expect("extra" in g.counts).toBe(false);
  });

  it("a delta before any snapshot changes nothing", () => {
    const f = reduceFleet(emptyFleet(), delta({ op: "replace", path: "/terminals", value: [] }));
    expect(f).toEqual(emptyFleet());
  });

  it("ignores frames that are not fleet state", () => {
    expect(reduceFleet(emptyFleet(), { type: "STATE_DELTA" })).toEqual(emptyFleet());
    expect(reduceFleet(emptyFleet(), { type: "RAW", event: {} })).toEqual(emptyFleet());
    expect(reduceFleet(emptyFleet(), { type: "STATE_SNAPSHOT" })).toEqual(emptyFleet());
  });

  // The unknown-op rule, mirroring genui.test.ts "ignores frames that are not generative UI":
  // the applier never throws and never half-applies. It keeps what it had, marks itself
  // stale, and the next STATE_SNAPSHOT (a reconnect) clears that.
  it.each([
    ["move", { op: "move", from: "/terminals", path: "/sessions" }],
    ["copy", { op: "copy", from: "/terminals", path: "/sessions" }],
    ["test", { op: "test", path: "/terminals", value: [] }],
    ["an op that is not RFC 6902", { op: "frobnicate", path: "/terminals" }],
    ["an unknown path", { op: "replace", path: "/host/cpu", value: 1 }],
    ["a path with no leading slash", { op: "replace", path: "terminals", value: [] }],
    ["no path", { op: "replace", value: [] }],
  ])("%s is ignored, not thrown, and marks the fleet stale", (_name, op) => {
    const before = fold([base]);
    const after = reduceFleet(before, delta({ op: "replace", path: "/counts/terminals", value: 9 }, op));
    expect(after.terminals).toEqual(before.terminals);
    // All-or-nothing: the good op in the same delta is not applied alone.
    expect(after.counts).toEqual(before.counts);
    expect(after.stale).toBe(true);
  });

  it("the next snapshot clears stale", () => {
    const stale = reduceFleet(fold([base]), delta({ op: "frobnicate", path: "/x" }));
    expect(stale.stale).toBe(true);
    expect(reduceFleet(stale, base).stale).toBe(false);
  });

  it("a non-array delta is ignored and stale", () => {
    const f = reduceFleet(fold([base]), { type: "STATE_DELTA", delta: "nope" });
    expect(f.stale).toBe(true);
  });

  it("malformed rows are dropped, not rendered", () => {
    const f = fold([snapshot({ terminals: [null, 3, "x", {}, term("ok")], sessions: "no", counts: [] })]);
    expect(f.terminals.map((t) => t.id)).toEqual(["ok"]);
    expect(f.sessions).toEqual([]);
  });
});

describe("the profile catalog and the agent_card", () => {
  const f = fold([
    snapshot({
      sessions: [],
      terminals: [term("a", { agent_profile: "dev" }), term("b", { agent_profile: "ghost" }), term("c", { agent_profile: null })],
      counts: { sessions: 0, terminals: 3 },
      scopes: [],
      profiles: [
        { name: "dev", source: "kiro", role: "developer", loadable: true },
        { name: "evil", source: "<img src=x onerror=alert(1)>", role: null, loadable: false, description: "must not be read" },
      ],
    }),
  ]);

  it("holds only the four catalog fields", () => {
    expect(f.profiles[1]).toEqual({ name: "evil", source: "<img src=x onerror=alert(1)>", role: null, loadable: false });
  });

  it("composes the card client-side from terminal and catalog", () => {
    expect(agentCardFor(f.terminals[0], f.profiles)).toEqual({
      name: "dev",
      provider: "kiro_cli",
      status: "idle",
      profile: "dev",
      role: "developer",
      source: "kiro",
      loadable: "yes",
    });
  });

  it("a profile that is not in the catalog still gets a card, without catalog fields", () => {
    expect(agentCardFor(f.terminals[1], f.profiles)).toEqual({ name: "ghost", provider: "kiro_cli", status: "idle" });
  });

  it("a terminal with no profile is named by its id", () => {
    expect(agentCardFor(f.terminals[2], f.profiles)).toMatchObject({ name: "c" });
  });

  it("a catalog change arrives as a whole-key replace", () => {
    const g = reduceFleet(f, delta({ op: "replace", path: "/profiles", value: [{ name: "new", source: "local", role: null, loadable: true }] }));
    expect(g.profiles.map((p) => p.name)).toEqual(["new"]);
  });
});

describe("the controller feeds the fleet", () => {
  it("folds ambient frames and reports them through onChange", async () => {
    const seen: Fleet[] = [];
    const controller = createController({
      base: "http://127.0.0.1:9894",
      threadId: "t",
      onChange: (_b, _p, _t, fleet) => {
        seen.push(fleet);
      },
    });
    expect(controller.fleet).toEqual(emptyFleet());
    for (const frame of fixtureFrames("ambient.sse")) await controller.ambient(frame);
    expect(controller.fleet.hydrated).toBe(true);
    expect(seen.at(-1)).toBe(controller.fleet);
  });
});

describe("every shape the server may send, and the wrong ones", () => {
  const snap = (extra: Record<string, unknown> = {}) =>
    snapshot({ sessions: [], terminals: [term("a")], counts: { sessions: 0, terminals: 1 }, scopes: [], profiles: [], ...extra });

  it("fills in what a session row leaves out, and drops one with no id", () => {
    const f = fold([snap({ sessions: [{ id: "s1" }, { id: 7 }, { id: "s2", name: "named", status: "active" }, null] })]);
    expect(f.sessions).toEqual([
      { id: "s1", name: "s1", status: "unknown" },
      { id: "s2", name: "named", status: "active" },
    ]);
  });

  it("drops a terminal whose id is not text and a profile with no name", () => {
    const f = fold([snap({ terminals: [{ id: 5 }, term("ok")], profiles: [{ source: "x" }, null, { name: "p" }] })]);
    expect(f.terminals.map((t) => t.id)).toEqual(["ok"]);
    expect(f.profiles).toEqual([{ name: "p", source: "", role: null, loadable: false }]);
  });

  it("keeps only finite numbers in counts", () => {
    const f = fold([snap({ counts: { sessions: 1, bad: "x", inf: Infinity } })]);
    expect(f.counts).toEqual({ sessions: 1 });
  });

  it.each([
    ["terminals", "/terminals", [term("z")], (f: Fleet) => f.terminals.map((t) => t.id), ["z"], []],
    ["sessions", "/sessions", [{ id: "q", name: "q", status: "active" }], (f: Fleet) => f.sessions.map((s) => s.id), ["q"], []],
    ["profiles", "/profiles", [{ name: "p", source: "s", role: null, loadable: true }], (f: Fleet) => f.profiles.map((p) => p.name), ["p"], []],
  ])("%s: replace sets it and remove empties it", (_n, path, value, read, set, empty) => {
    const base = fold([snap({ sessions: [{ id: "old", name: "old", status: "x" }], profiles: [{ name: "old", source: "", role: null, loadable: false }] })]);
    expect(read(reduceFleet(base, delta({ op: "replace", path, value })))).toEqual(set);
    expect(read(reduceFleet(base, delta({ op: "remove", path })))).toEqual(empty);
  });

  it("counts: whole-object replace and remove, and `scopes` is accepted and ignored", () => {
    const base = fold([snap()]);
    expect(reduceFleet(base, delta({ op: "replace", path: "/counts", value: { sessions: 9 } })).counts).toEqual({ sessions: 9 });
    expect(reduceFleet(base, delta({ op: "remove", path: "/counts" })).counts).toEqual({});
    const scoped = reduceFleet(base, delta({ op: "replace", path: "/scopes", value: ["cao:read"] }));
    expect(scoped.stale).toBe(false);
    expect(scoped.terminals).toEqual(base.terminals);
  });

  it("a counts key needs a finite number, and an escaped key is unescaped", () => {
    const base = fold([snap()]);
    expect(reduceFleet(base, delta({ op: "replace", path: "/counts/terminals", value: "3" })).stale).toBe(true);
    expect(reduceFleet(base, delta({ op: "replace", path: "/counts/terminals", value: Infinity })).stale).toBe(true);
    expect(reduceFleet(base, delta({ op: "add", path: "/counts/a~1b~0c", value: 4 })).counts["a/b~c"]).toBe(4);
  });

  it("a top-level key the server does not send is not understood", () => {
    const base = fold([snap()]);
    expect(reduceFleet(base, delta({ op: "add", path: "/not-a-fleet-key", value: {} })).stale).toBe(true);
  });

  it("a path deeper than the server sends is not understood", () => {
    const base = fold([snap()]);
    expect(reduceFleet(base, delta({ op: "replace", path: "/terminals/0/status", value: "x" })).stale).toBe(true);
  });
});

describe("ordering of runners", () => {
  it("sorts ids against each other, in either arrival order", () => {
    const order = (ids: Array<string | null>) =>
      groupByRunner(fold([snapshot({ terminals: ids.map((id, i) => term(`t${i}`, { runner_id: id })), sessions: [] })])).map((g) => g.runnerId);
    expect(order(["rn-b", "rn-a", null])).toEqual([null, "rn-a", "rn-b"]);
    expect(order(["rn-a", "rn-b", null])).toEqual([null, "rn-a", "rn-b"]);
    expect(order([null, "rn-b"])).toEqual([null, "rn-b"]);
  });
});

describe("the card for a catalog entry that says less", () => {
  it("leaves out an unset role and says `no` for an unloadable profile", () => {
    const f = fold([
      snapshot({
        sessions: [],
        terminals: [term("a", { agent_profile: "p" })],
        profiles: [{ name: "p", source: "local", role: null, loadable: false }],
      }),
    ]);
    expect(agentCardFor(f.terminals[0], f.profiles)).toEqual({
      name: "p",
      provider: "kiro_cli",
      status: "idle",
      profile: "p",
      source: "local",
      loadable: "no",
    });
  });
});

describe("host resources", () => {
  const host = (extra: Record<string, unknown> = {}) => ({
    runner_id: null,
    sampled_at: "2026-11-12T00:00:00+00:00",
    cpu_percent: 12,
    load_per_core: 0.5,
    memory_used_percent: 40,
    memory_pressure: "warn",
    ...extra,
  });

  it("folds the host list from a snapshot; a field the server omitted is null, never 0", () => {
    const fleet = fold([snapshot({ terminals: [], host: [host({ cpu_percent: undefined })] })]);
    expect(fleet.host).toEqual([
      {
        runnerId: null,
        sampledAt: "2026-11-12T00:00:00+00:00",
        cpuPercent: null,
        loadPerCore: 0.5,
        memoryUsedPercent: 40,
        memoryPressure: "warn",
      },
    ]);
  });

  it("a snapshot with no host key (older server, or not reported) is an empty list", () => {
    expect(fold([snapshot({ terminals: [] })]).host).toEqual([]);
    expect(fold([snapshot({ terminals: [], host: "x" })]).host).toEqual([]);
  });

  it("applies a whole-key replace of /host and a remove, and keeps the rest of the fleet", () => {
    const base = snapshot({ terminals: [term("a")], host: [host()] });
    const replaced = fold([base, delta({ op: "replace", path: "/host", value: [host({ cpu_percent: 90 })] })]);
    expect(replaced.host[0].cpuPercent).toBe(90);
    expect(replaced.terminals).toHaveLength(1);
    expect(replaced.stale).toBe(false);
    expect(fold([base, delta({ op: "remove", path: "/host" })]).host).toEqual([]);
  });

  it("drops junk entries and an unknown pressure level", () => {
    const fleet = fold([snapshot({ terminals: [], host: [7, null, host({ memory_pressure: "SENTINEL" })] })]);
    expect(fleet.host).toHaveLength(1);
    expect(fleet.host[0].memoryPressure).toBeNull();
  });
});
