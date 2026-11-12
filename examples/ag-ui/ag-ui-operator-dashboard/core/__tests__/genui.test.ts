import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  ALLOWED_COMPONENTS,
  approvalHintProps,
  emptyPanel,
  entryFor,
  MAX_PANEL_ENTRIES,
  MAX_REFUSED_ENTRIES,
  MAX_SEEN_EVENT_IDS,
  ownerText,
  propRows,
  reduceFrame,
  type Panel,
} from "../genui.ts";
import type { Frame } from "../types.ts";
import { fixtureFrames, fixtureJson } from "./helpers.ts";

const ambient = fixtureFrames("ambient.sse");
const fold = (frames: Frame[]): Panel => frames.reduce(reduceFrame, emptyPanel());

describe("allow-list", () => {
  it("equals GENERATIVE_UI_COMPONENTS in the server", () => {
    const source = readFileSync(
      fileURLToPath(new URL("../../../../../src/cli_agent_orchestrator/services/agui_stream.py", import.meta.url)),
      "utf8",
    );
    const block = /GENERATIVE_UI_COMPONENTS[^=]*=\s*frozenset\(\s*\{([^}]*)\}/s.exec(source);
    expect(block, "GENERATIVE_UI_COMPONENTS not found in agui_stream.py").not.toBeNull();
    const names = [...block![1].matchAll(/"([a-z_]+)"/g)].map((m) => m[1]).sort();
    expect(names).toEqual([...ALLOWED_COMPONENTS]);
  });
});

describe("captured ambient stream", () => {
  const panel = fold(ambient);
  const components = panel.entries.filter((e) => e.kind === "component");

  it("renders each of the six components from a real emit_ui call", () => {
    const first = components.slice(0, 6).map((e) => (e.kind === "component" ? e.component : ""));
    expect([...first].sort()).toEqual([...ALLOWED_COMPONENTS]);
    expect(components[0].owner).toEqual({ subagentRunId: expect.stringMatching(/^terminal:/), sender: expect.any(String) });
  });

  it("a sender that is not a terminal has no subagent (the #1243 shape)", () => {
    const operator = components.find((e) => e.owner.sender === "operator");
    expect(operator?.owner.subagentRunId).toBeUndefined();
    expect(ownerText(operator!.owner)).toBe("sender operator (not a subagent)");
  });

  it("hostile props stay strings", () => {
    const hostile = components.find((e) => e.kind === "component" && String(e.props.label).includes("<img"));
    expect(hostile && hostile.kind === "component" && propRows(hostile.props)).toEqual([
      ["label", "<img src=x onerror=alert(1)>"],
      ["value", "<script>alert(1)</script>"],
    ]);
  });

  it("dedupes a replayed record by eventId", () => {
    const replayed = fold([...ambient, ...ambient]);
    expect(replayed.entries).toHaveLength(panel.entries.length);
  });

  it("the construct's approval_card frames give hint props only", () => {
    const hints = ambient.map(approvalHintProps).filter(Boolean);
    expect(hints.length).toBeGreaterThan(0);
    expect(hints.some((h) => h!.resolved === true)).toBe(true);
    const first = ambient.find((f) => f.type === "CUSTOM")!;
    expect(approvalHintProps(first)).toBeNull(); // the agent's own approval_card names no interrupt
  });
});

describe("refusals", () => {
  it("RAW with event.rejected_component (captured from a real off-list emit_ui, #1158) is a refusal without props", () => {
    const raw = fixtureJson<Frame>("raw-offlist.json");
    expect(raw.event).not.toHaveProperty("props");
    expect(entryFor(raw)).toEqual({
      kind: "refused",
      eventId: "5a5f7064-1e75-4317-bf63-3103e5e2fd2c",
      component: "iframe_embed.8688ac89",
      owner: { subagentRunId: "terminal:34a10464", sender: "34a10464" },
    });
  });

  it("a CUSTOM frame with an off-list component is refused by the client too", () => {
    const frame: Frame = { type: "CUSTOM", name: "cao.generative_ui", value: { event_id: "e1", component: "iframe_embed", props: { src: "x" } } };
    expect(entryFor(frame)).toEqual({ kind: "refused", eventId: "e1", component: "iframe_embed", owner: {} });
    expect(ownerText({})).toBe("unattributed");
    expect(ownerText({ subagentRunId: "terminal:a", sender: "a" })).toBe("terminal:a");
  });

  it("the ambient capture, made before #1158, lists the six components and no refusal", () => {
    const panel = fold(ambient);
    expect(panel.entries.some((e) => e.component === "iframe_embed")).toBe(false);
    expect(panel.entries.filter((e) => e.kind === "refused")).toEqual([]);
    const components = new Set(panel.entries.map((e) => e.component));
    for (const c of ALLOWED_COMPONENTS) expect(components.has(c)).toBe(true);
  });
});

describe("edge shapes", () => {
  it("ignores frames that are not generative UI", () => {
    expect(entryFor({ type: "STATE_DELTA" })).toBeNull();
    expect(entryFor({ type: "RAW", event: { kind: "other" } })).toBeNull();
    expect(entryFor({ type: "RAW" })).toBeNull();
    expect(entryFor({ type: "CUSTOM", name: "other" })).toBeNull();
    expect(reduceFrame(emptyPanel(), { type: "STATE_DELTA" })).toEqual(emptyPanel());
  });

  it("missing value, props or ids", () => {
    expect(entryFor({ type: "CUSTOM", name: "cao.generative_ui" })).toEqual({ kind: "refused", eventId: "", component: "", owner: {} });
    const noProps = entryFor({ type: "CUSTOM", name: "cao.generative_ui", value: { component: "metric", props: "x" } });
    expect(noProps).toMatchObject({ kind: "component", props: {}, eventId: "" });
    // No id at all: nothing to dedupe on, so both are kept.
    const f: Frame = { type: "CUSTOM", name: "cao.generative_ui", value: { component: "metric", props: {} } };
    expect(fold([f, f]).entries).toHaveLength(2);
    expect(approvalHintProps({ type: "CUSTOM", name: "cao.generative_ui", value: { component: "approval_card", props: {} } })).toBeNull();
  });

  it("propRows: JSON text for non-strings, truncated past the cap", () => {
    expect(propRows({ n: 1, o: { a: [1] }, u: undefined, s: "abcdef" }, 3)).toEqual([
      ["n", "1"],
      ["o", '{"a…'],
      ["u", "und…"],
      ["s", "abc…"],
    ]);
  });
});

describe("bounded panel (Note 3)", () => {
  const metric = (n: number): Frame => ({
    type: "CUSTOM",
    name: "cao.generative_ui",
    value: { component: "metric", props: { n } },
    metadata: { cao: { eventId: `e${n}` } },
  });

  it(`keeps only the newest ${MAX_PANEL_ENTRIES} entries`, () => {
    let panel = emptyPanel();
    for (let n = 1; n <= MAX_PANEL_ENTRIES + 5; n += 1) panel = reduceFrame(panel, metric(n));
    expect(panel.entries).toHaveLength(MAX_PANEL_ENTRIES);
    expect(panel.entries[0].eventId).toBe("e6");
    expect(panel.entries.at(-1)!.eventId).toBe(`e${MAX_PANEL_ENTRIES + 5}`);
  });

  it(`remembers the newest ${MAX_SEEN_EVENT_IDS} event ids, so a replay of a dropped entry stays dropped`, () => {
    let panel = emptyPanel();
    for (let n = 1; n <= MAX_SEEN_EVENT_IDS + 1; n += 1) panel = reduceFrame(panel, metric(n));
    expect(panel.seen.size).toBe(MAX_SEEN_EVENT_IDS);
    expect(panel.seen.has("e1")).toBe(false);
    expect(panel.seen.has("e2")).toBe(true);
    // e300 left the panel long ago but is still remembered: a replay is not re-added.
    expect(panel.entries.some((e) => e.eventId === "e300")).toBe(false);
    expect(reduceFrame(panel, metric(300))).toBe(panel);
  });
});

describe("refused entries are capped apart (#1158)", () => {
  const metric = (n: number): Frame => ({
    type: "CUSTOM",
    name: "cao.generative_ui",
    value: { component: "metric", props: { n } },
    metadata: { cao: { eventId: `m${n}` } },
  });
  const refusal = (n: number, more?: unknown): Frame => ({
    type: "RAW",
    metadata: { cao: { eventId: `r${n}` } },
    event: { rejected_component: `x${n}.0123abcd`, ...(more === undefined ? {} : { more_refused: more }) },
  });
  const kinds = (panel: Panel) => panel.entries.map((e) => e.kind);

  it(`keeps at most ${MAX_REFUSED_ENTRIES} refused entries, the newest`, () => {
    let panel = emptyPanel();
    for (let n = 1; n <= 50; n += 1) panel = reduceFrame(panel, refusal(n));
    expect(panel.entries).toHaveLength(MAX_REFUSED_ENTRIES);
    expect(panel.entries[0].eventId).toBe(`r${50 - MAX_REFUSED_ENTRIES + 1}`);
  });

  it("500 refusals, then 200 real components: every real one is there", () => {
    let panel = emptyPanel();
    for (let n = 1; n <= 500; n += 1) panel = reduceFrame(panel, refusal(n));
    for (let n = 1; n <= MAX_PANEL_ENTRIES; n += 1) panel = reduceFrame(panel, metric(n));
    const real = panel.entries.filter((e) => e.kind === "component").map((e) => e.eventId);
    expect(real).toEqual(Array.from({ length: MAX_PANEL_ENTRIES }, (_, i) => `m${i + 1}`));
    expect(panel.entries).toHaveLength(MAX_PANEL_ENTRIES);
  });

  it("real components, then 500 refusals: the real ones survive", () => {
    let panel = emptyPanel();
    for (let n = 1; n <= 100; n += 1) panel = reduceFrame(panel, metric(n));
    for (let n = 1; n <= 500; n += 1) panel = reduceFrame(panel, refusal(n));
    expect(kinds(panel).filter((k) => k === "component")).toHaveLength(100);
    expect(kinds(panel).filter((k) => k === "refused")).toHaveLength(MAX_REFUSED_ENTRIES);
    expect(panel.entries.at(-1)!.eventId).toBe("r500"); // arrival order is kept
  });

  it("a full panel of real components drops a refusal before any real one", () => {
    let panel = emptyPanel();
    for (let n = 1; n <= 5; n += 1) panel = reduceFrame(panel, refusal(n));
    for (let n = 1; n <= MAX_PANEL_ENTRIES; n += 1) panel = reduceFrame(panel, metric(n));
    expect(kinds(panel).filter((k) => k === "component")).toHaveLength(MAX_PANEL_ENTRIES);
    expect(panel.entries[0].eventId).toBe("m1");
  });

  it("the server's fold count is kept only as a positive integer", () => {
    expect(entryFor(refusal(1, 4))).toMatchObject({ kind: "refused", moreRefused: 4 });
    for (const bad of [0, -1, 1.5, "4", true, null, { n: 1 }]) {
      expect(entryFor(refusal(1, bad))).not.toHaveProperty("moreRefused");
    }
  });
});

