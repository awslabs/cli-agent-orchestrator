import { describe, expect, it } from "vitest";
import {
  callText,
  emptyTimeline,
  isFinished,
  markReconnect,
  MAX_FIELD_CHARS,
  MAX_TIMELINE_ENTRIES,
  MAX_TIMELINE_SEEN,
  reduceTimeline,
  stateLabel,
  timeText,
  type Timeline,
} from "../timeline.ts";
import type { Frame } from "../types.ts";
import { fixtureFrames } from "./helpers.ts";

const fold = (frames: Frame[], from: Timeline = emptyTimeline()): Timeline => frames.reduce(reduceTimeline, from);

const start = (id: string, extra: Record<string, unknown> = {}, cao: Record<string, unknown> = {}): Frame => ({
  type: "TOOL_CALL_START",
  timestamp: 1_000,
  metadata: { cao: { eventId: id, sender: "sup", receiver: "wrk", orchestrationType: "assign", ...cao } },
  subagentRunId: "terminal:sup",
  toolCallId: id,
  toolCallName: "OrchestrationType.ASSIGN",
  ...extra,
});

const end = (id: string, extra: Record<string, unknown> = {}): Frame => ({
  type: "TOOL_CALL_END",
  timestamp: 2_000,
  metadata: { cao: { closedBy: "completion" } },
  subagentRunId: "terminal:sup",
  toolCallId: id,
  ...extra,
});

describe("the captured session (real cao-server frames)", () => {
  const timeline = fold(fixtureFrames("session-timeline.sse"));

  it("pairs the assign with its completion and leaves the handoff open", () => {
    expect(timeline.entries.map((e) => [e.orchestrationType, e.state])).toEqual([
      ["assign", "completed"],
      ["handoff", "open"],
    ]);
    const [assign, handoff] = timeline.entries;
    expect(assign.endedAt).toBeGreaterThan(assign.startedAt!);
    expect(handoff.endedAt).toBeUndefined();
  });

  it("the stream's own closing end (closedBy stream_closed, last frames of the capture) does not complete the open handoff", () => {
    const frames = fixtureFrames("session-timeline.sse");
    const closer = frames.find((f) => f.type === "TOOL_CALL_END" && (f.metadata as { cao: { closedBy: string } }).cao.closedBy === "stream_closed");
    expect(closer, "the capture holds the stream-closing end").toBeDefined();
    expect(closer!.toolCallId).toBe(timeline.entries[1].toolCallId);
  });

  it("keeps who called whom, and the subagent the wire attributes the call to", () => {
    for (const entry of timeline.entries) {
      expect(entry.sender).toBe("1dd0761a");
      expect(entry.owner).toEqual({ subagentRunId: "terminal:1dd0761a", sender: "1dd0761a" });
      expect(entry.sessionName).toBe("cao-timeline");
    }
    expect(timeline.entries.map((e) => e.receiver)).toEqual(["5ac6264b", "be17ba90"]);
  });

  it("is the same when the whole capture is replayed (each call once)", () => {
    const frames = fixtureFrames("session-timeline.sse");
    expect(fold([...frames, ...frames]).entries).toEqual(timeline.entries);
  });
});

describe("the captured superseded sequence (real cao-server frames)", () => {
  const timeline = fold(fixtureFrames("session-superseded.sse"));

  it("a second assign to the same worker ends the first as superseded; the second completes", () => {
    expect(timeline.entries.map((e) => [e.orchestrationType, e.receiver, e.state])).toEqual([
      ["assign", "c0156f46", "superseded"],
      ["assign", "c0156f46", "completed"],
    ]);
  });

  it("stays the same across a reconnect, and when the capture is replayed", () => {
    expect(markReconnect(timeline)).toBe(timeline);
    const frames = fixtureFrames("session-superseded.sse");
    expect(fold([...frames, ...frames]).entries).toEqual(timeline.entries);
  });
});

describe("pairing", () => {
  it("opens on a start and closes on its end", () => {
    const open = fold([start("a")]);
    expect(open.entries).toMatchObject([{ toolCallId: "a", state: "open", startedAt: 1_000 }]);
    const done = fold([end("a")], open);
    expect(done.entries).toMatchObject([{ toolCallId: "a", state: "completed", endedAt: 2_000 }]);
  });

  it("closes only the call the end names", () => {
    const t = fold([start("a"), start("b"), end("b")]);
    expect(t.entries.map((e) => [e.toolCallId, e.state])).toEqual([["a", "open"], ["b", "completed"]]);
  });

  it("an end for an unknown call is ignored and not remembered", () => {
    const t = fold([end("ghost"), start("ghost")]);
    expect(t.entries).toMatchObject([{ toolCallId: "ghost", state: "open" }]);
  });

  it("an out-of-order end before its start leaves the call open", () => {
    expect(fold([end("a"), start("a")]).entries[0].state).toBe("open");
  });

  it("an end that only closes a stream leaves the call open", () => {
    const open = fold([start("a")]);
    expect(fold([end("a", { metadata: { cao: { closedBy: "stream_closed" } } })], open)).toBe(open);
  });

  it("superseded: a newer call to the same receiver ends the old one, which is neither open nor completed", () => {
    // The server's order (_open_call): END a (superseded), then START b, for receiver wrk.
    const t = fold([start("a"), end("a", { metadata: { cao: { closedBy: "superseded" } } }), start("b")]);
    expect(t.entries.map((e) => [e.toolCallId, e.state])).toEqual([["a", "superseded"], ["b", "open"]]);
    expect(t.entries[0].endedAt).toBe(2_000);
    expect(isFinished(t.entries[0])).toBe(true);
    expect(stateLabel(t.entries[0])).toBe("ended: superseded by a newer call to the same receiver");
  });

  it("any other closedBy, or none, ends the call for good, with the value kept as capped text", () => {
    const open = fold([start("a")]);
    const cases: Array<[unknown, string]> = [["something_new", "something_new"], [3, "unknown"], [undefined, "unknown"], ["x".repeat(500), `${"x".repeat(MAX_FIELD_CHARS)}…`]];
    for (const [closedBy, kept] of cases) {
      const e = fold([end("a", { metadata: { cao: { closedBy } } })], open).entries[0];
      expect([e.state, e.closedBy]).toEqual(["ended", kept]);
      expect(stateLabel(e)).toBe(`ended: ${kept}`);
    }
    expect(fold([end("a", { metadata: undefined })], open).entries[0].state).toBe("ended");
  });

  it("a finished call takes no second end: superseded stays superseded, ended stays ended", () => {
    const sup = fold([start("a"), end("a", { metadata: { cao: { closedBy: "superseded" } } })]);
    expect(fold([end("a")], sup)).toBe(sup);
    const odd = fold([start("a"), end("a", { metadata: { cao: { closedBy: "odd" } } })]);
    expect(fold([end("a")], odd)).toBe(odd);
  });

  it("a second end changes nothing", () => {
    const once = fold([start("a"), end("a")]);
    expect(fold([end("a", { timestamp: 9_999 })], once)).toBe(once);
  });

  it("a duplicate start is a no-op", () => {
    const once = fold([start("a")]);
    expect(fold([start("a")], once)).toBe(once);
  });

  it("a replayed start does not reopen a call that is already closed", () => {
    const done = fold([start("a"), end("a")]);
    expect(fold([start("a")], done)).toBe(done);
  });

  it("the same metadata.cao.eventId on a different toolCallId is dropped", () => {
    const t = fold([start("a"), start("b", {}, { eventId: "a" })]);
    expect(t.entries.map((e) => e.toolCallId)).toEqual(["a"]);
  });

  it("toolCallName is never a key: the same start with another name is one entry, labelled by orchestrationType", () => {
    // The wire sends toolCallName "OrchestrationType.ASSIGN", not "assign" (part of #1292).
    const t = fold([start("a"), start("a", { toolCallName: "assign" })]);
    expect(t.entries).toHaveLength(1);
    expect(callText(t.entries[0])).toBe("assign: sup → wrk");
  });

  it("the same toolCallId with a different eventId is dropped", () => {
    const t = fold([start("a"), start("a", {}, { eventId: "other" })]);
    expect(t.entries).toHaveLength(1);
  });

  it("a start with no eventId is keyed by its toolCallId", () => {
    const frame = start("a");
    delete (frame.metadata as { cao: Record<string, unknown> }).cao.eventId;
    const once = fold([frame]);
    expect([...once.seen]).toEqual(["a"]);
    expect(fold([frame], once)).toBe(once);
  });

  it("frames with no usable toolCallId, and other frame types, change nothing", () => {
    const empty = emptyTimeline();
    for (const frame of [
      { type: "TOOL_CALL_START" },
      { type: "TOOL_CALL_START", toolCallId: "" },
      { type: "TOOL_CALL_END", toolCallId: 7 },
      { type: "TOOL_CALL_ARGS", toolCallId: "a" },
      { type: "CUSTOM", name: "cao.generative_ui", toolCallId: "a" },
      { type: "RUN_ERROR", code: "cao.stream_overflow" },
    ]) {
      expect(reduceTimeline(empty, frame as Frame)).toBe(empty);
    }
  });

  it("does not mutate the timeline it was given", () => {
    const before = fold([start("a")]);
    const snapshot = JSON.stringify([before.entries, [...before.seen]]);
    fold([end("a"), start("b")], before);
    expect(JSON.stringify([before.entries, [...before.seen]])).toBe(snapshot);
  });
});

describe("reconnects", () => {
  it("a call still open at a reconnect becomes unconfirmed; completed calls are unchanged", () => {
    const t = markReconnect(fold([start("a"), start("b"), end("b")]));
    expect(t.entries.map((e) => [e.toolCallId, e.state])).toEqual([["a", "unconfirmed"], ["b", "completed"]]);
  });

  it("an unconfirmed call completes when its end arrives later", () => {
    const t = fold([end("a")], markReconnect(fold([start("a")])));
    expect(t.entries[0]).toMatchObject({ state: "completed", endedAt: 2_000 });
  });

  it("a replayed start does not confirm the call; a start first seen after the reconnect is open", () => {
    const t = fold([start("a"), start("c")], markReconnect(fold([start("a")])));
    expect(t.entries.map((e) => [e.toolCallId, e.state])).toEqual([["a", "unconfirmed"], ["c", "open"]]);
  });

  it("a stream-closing end does not complete an unconfirmed call", () => {
    const t = markReconnect(fold([start("a")]));
    expect(fold([end("a", { metadata: { cao: { closedBy: "stream_closed" } } })], t)).toBe(t);
  });

  it("a superseded or ended call stays ended across a reconnect, it is not turned unconfirmed", () => {
    const t = fold([start("a"), end("a", { metadata: { cao: { closedBy: "superseded" } } }), start("c"), end("c", { metadata: { cao: { closedBy: "odd" } } }), start("b")]);
    expect(markReconnect(t).entries.map((e) => [e.toolCallId, e.state])).toEqual([["a", "superseded"], ["c", "ended"], ["b", "unconfirmed"]]);
  });

  it("an unconfirmed call that is then superseded ends as superseded", () => {
    const t = fold([end("a", { metadata: { cao: { closedBy: "superseded" } } })], markReconnect(fold([start("a")])));
    expect(t.entries[0].state).toBe("superseded");
  });

  it("returns the same timeline when nothing is open, and stays unconfirmed across a second reconnect", () => {
    const done = fold([start("a"), end("a")]);
    expect(markReconnect(done)).toBe(done);
    const empty = emptyTimeline();
    expect(markReconnect(empty)).toBe(empty);
    const once = markReconnect(fold([start("a")]));
    expect(markReconnect(once)).toBe(once);
  });

  it("stateLabel says what each state means", () => {
    const [a] = fold([start("a")]).entries;
    expect(stateLabel(a)).toBe("open");
    expect(stateLabel({ ...a, state: "completed" })).toBe("completed");
    expect(stateLabel({ ...a, state: "unconfirmed" })).toBe("open; its end was not seen across a reconnect");
  });
});

describe("what is kept from the wire", () => {
  it("keeps only metadata: sender, receiver, type, name, session and times", () => {
    const frame = start("a", { message: "secret body", delta: "secret delta" }, { prompt: "secret prompt" });
    const entry = fold([frame]).entries[0];
    expect(Object.keys(entry).sort()).toEqual(
      ["name", "orchestrationType", "owner", "receiver", "sender", "startedAt", "state", "toolCallId"],
    );
    expect(JSON.stringify(entry)).not.toContain("secret");
  });

  it("an absent field stays absent; a non-string or empty one is dropped", () => {
    const frame: Frame = { type: "TOOL_CALL_START", toolCallId: "a", metadata: { cao: { sender: 5, receiver: "" } }, subagentRunId: 9 };
    expect(fold([frame]).entries[0]).toEqual({ toolCallId: "a", state: "open", owner: {} });
  });

  it("a frame with no metadata at all still opens a call", () => {
    expect(fold([{ type: "TOOL_CALL_START", toolCallId: "a" }]).entries[0]).toEqual({ toolCallId: "a", state: "open", owner: {} });
  });

  it("a sender that is not a subagent is kept as the owner's sender only", () => {
    const frame = start("a");
    delete frame.subagentRunId;
    expect(fold([frame]).entries[0].owner).toEqual({ sender: "sup" });
  });

  it("hostile strings are kept as data, and cut at the cap", () => {
    const hostile = `<img src=x onerror=alert(1)>${"x".repeat(MAX_FIELD_CHARS * 2)}`;
    const entry = fold([start("a", { toolCallName: hostile }, { sender: "<script>1</script>", receiver: hostile })]).entries[0];
    expect(entry.sender).toBe("<script>1</script>");
    expect(entry.name!.length).toBe(MAX_FIELD_CHARS + 1);
    expect(entry.name!.endsWith("…")).toBe(true);
    expect(entry.receiver!.startsWith("<img src=x onerror=alert(1)>")).toBe(true);
  });

  it("an end with no timestamp completes the call without an end time", () => {
    const t = fold([start("a"), end("a", { timestamp: undefined })]);
    expect(t.entries[0]).toMatchObject({ state: "completed" });
    expect(t.entries[0].endedAt).toBeUndefined();
  });

  it("a start with no timestamp has no startedAt", () => {
    expect(fold([start("a", { timestamp: "soon" })]).entries[0].startedAt).toBeUndefined();
  });
});

describe("bounds", () => {
  it("keeps the newest MAX_TIMELINE_ENTRIES calls", () => {
    const frames = Array.from({ length: MAX_TIMELINE_ENTRIES + 5 }, (_, i) => start(`c${i}`));
    const t = fold(frames);
    expect(t.entries).toHaveLength(MAX_TIMELINE_ENTRIES);
    expect(t.entries[0].toolCallId).toBe("c5");
  });

  it("remembers at most MAX_TIMELINE_SEEN keys, newest kept, so a replay of a dropped call is not resurrected soon after", () => {
    const frames = Array.from({ length: MAX_TIMELINE_SEEN + 3 }, (_, i) => start(`c${i}`));
    const t = fold(frames);
    expect(t.seen.size).toBe(MAX_TIMELINE_SEEN);
    expect(t.seen.has("c0")).toBe(false);
    expect(t.seen.has(`c${MAX_TIMELINE_SEEN + 2}`)).toBe(true);
    // c10 left the entries (200 newest) but is still remembered, so its replay is dropped.
    expect(MAX_TIMELINE_SEEN).toBeGreaterThan(MAX_TIMELINE_ENTRIES);
    expect(fold([start("c10")], t)).toBe(t);
  });

  it("when it must drop, it drops finished calls before live ones, oldest first", () => {
    // 2 open, then 199 finished (completed, superseded or ended), then one more start: 202 > 200.
    const frames: Frame[] = [start("live0"), start("live1")];
    const kinds = ["completion", "superseded", "odd"];
    for (let i = 0; i < 199; i += 1) frames.push(start(`f${i}`), end(`f${i}`, { metadata: { cao: { closedBy: kinds[i % 3] } } }));
    frames.push(start("new"));
    const t = fold(frames);
    expect(t.entries).toHaveLength(MAX_TIMELINE_ENTRIES);
    const ids = t.entries.map((e) => e.toolCallId);
    expect(ids.slice(0, 2)).toEqual(["live0", "live1"]); // the live calls stay
    expect(ids).not.toContain("f0"); // the oldest finished one went
    expect(ids).not.toContain("f1");
    expect(ids).toContain("f2");
    expect(ids.at(-1)).toBe("new");
  });

  it("an unconfirmed call is kept like an open one", () => {
    const frames: Frame[] = [start("held")];
    for (let i = 0; i < 199; i += 1) frames.push(start(`f${i}`), end(`f${i}`));
    const t = fold([start("new")], markReconnect(fold(frames)));
    expect(t.entries.map((e) => e.toolCallId)).toContain("held");
    expect(t.entries.find((e) => e.toolCallId === "held")!.state).toBe("unconfirmed");
  });

  it("with only live calls, the oldest live one goes", () => {
    const t = fold(Array.from({ length: MAX_TIMELINE_ENTRIES + 2 }, (_, i) => start(`c${i}`)));
    expect(t.entries.map((e) => e.toolCallId).slice(0, 1)).toEqual(["c2"]);
  });

  it("an end for a call that left the bounded entries is ignored", () => {
    const t = fold(Array.from({ length: MAX_TIMELINE_ENTRIES + 1 }, (_, i) => start(`c${i}`)));
    expect(fold([end("c0")], t)).toBe(t);
  });
});

describe("text helpers", () => {
  it("callText names the kind and both sides, with ? for a missing side", () => {
    expect(callText(fold([start("a")]).entries[0])).toBe("assign: sup → wrk");
    expect(callText({ toolCallId: "a", state: "open", owner: {}, name: "N" })).toBe("N: ? → ?");
    expect(callText({ toolCallId: "a", state: "open", owner: {} })).toBe("call: ? → ?");
  });

  it("timeText gives ISO text, and 'unknown' for anything that is not a time", () => {
    expect(timeText(1_794_358_170_090)).toBe("2026-11-11T00:49:30.090Z");
    expect(timeText(undefined)).toBe("unknown");
    expect(timeText(Number.NaN)).toBe("unknown");
    expect(timeText(1e20)).toBe("unknown");
  });
});
