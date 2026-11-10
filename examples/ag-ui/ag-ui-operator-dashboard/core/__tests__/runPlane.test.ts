import { describe, expect, it } from "vitest";
import { detailOf, httpLabel, outcomeFromFrames, runBody, runOnce } from "../runPlane.ts";
import { fakeFetch, fixtureFrames, fixtureJson, fixtureText, sse } from "./helpers.ts";

const BASE = "http://127.0.0.1:9894";
const req = { base: BASE, threadId: "t", runId: "r" };

describe("runBody", () => {
  it("omits resume when there is nothing to answer", () => {
    expect(JSON.parse(runBody({ threadId: "t", runId: "r" }))).toEqual({
      threadId: "t",
      runId: "r",
      messages: [],
      protocolVersion: "1.0",
    });
    expect(JSON.parse(runBody({ threadId: "t", runId: "r", resume: [] }))).not.toHaveProperty("resume");
  });

  it("sends status: resolved on every entry it is given", () => {
    const resume = [{ interruptId: "i", status: "resolved" as const, payload: { approved: true } }];
    expect(JSON.parse(runBody({ threadId: "t", runId: "r", resume })).resume).toEqual(resume);
  });
});

describe("outcomeFromFrames on captured runs", () => {
  it("empty run: success", () => {
    expect(outcomeFromFrames(fixtureFrames("empty-run.sse"))).toEqual({ kind: "SUCCESS", skipped: [] });
  });

  it("one waiting terminal: one interrupt with its schema and terminal", () => {
    const outcome = outcomeFromFrames(fixtureFrames("one-interrupt.sse"));
    expect(outcome.kind).toBe("INTERRUPT");
    if (outcome.kind !== "INTERRUPT") return;
    expect(outcome.interrupts).toHaveLength(1);
    expect(outcome.interrupts[0].metadata?.options).toEqual(["approve", "deny"]);
    expect(outcome.interrupts[0].subagentRunId).toMatch(/^terminal:/);
  });

  it("an edit on an approve/deny-only prompt is a RUN_ERROR with the server's message", () => {
    const outcome = outcomeFromFrames(fixtureFrames("resume-edit-refused.sse"));
    expect(outcome.kind).toBe("RUN_ERROR");
    if (outcome.kind === "RUN_ERROR") expect(outcome.message).toMatch(/Decision 'edit' not supported/);
  });

  it("skippedResume is read from result", () => {
    const outcome = outcomeFromFrames(fixtureFrames("resume-skipped.sse"));
    expect(outcome).toEqual({
      kind: "SUCCESS",
      skipped: [{ interruptId: "00000000-0000-0000-0000-000000000000", reason: "unknown" }],
    });
  });

  it("no terminal frame: NO_END; an interrupt outcome without a list is empty", () => {
    expect(outcomeFromFrames([{ type: "RUN_STARTED" }])).toEqual({ kind: "NO_END" });
    expect(outcomeFromFrames([{ type: "RUN_FINISHED", outcome: { type: "interrupt" } }])).toEqual({
      kind: "INTERRUPT",
      interrupts: [],
      skipped: [],
    });
    expect(outcomeFromFrames([{ type: "RUN_ERROR" }])).toEqual({ kind: "RUN_ERROR", message: "" });
  });

  it("a RUN_FINISHED that is neither success nor interrupt is OTHER_END, never success", () => {
    expect(outcomeFromFrames([{ type: "RUN_FINISHED", outcome: { type: "cancelled" } }])).toEqual({
      kind: "OTHER_END",
      outcomeType: "cancelled",
    });
    expect(outcomeFromFrames([{ type: "RUN_FINISHED" }])).toEqual({ kind: "OTHER_END", outcomeType: "none" });
    expect(outcomeFromFrames([{ type: "RUN_FINISHED", outcome: { type: 7 } }])).toEqual({ kind: "OTHER_END", outcomeType: "none" });
  });
});

describe("runOnce", () => {
  it("posts JSON for SSE to /agui/v1/run, with a bearer token when given", async () => {
    const { fetch, calls } = fakeFetch([{ sse: fixtureText("empty-run.sse"), chunks: 5 }]);
    expect(await runOnce({ ...req, token: "tok", fetchImpl: fetch })).toEqual({ kind: "SUCCESS", skipped: [] });
    expect(calls[0].url).toBe(`${BASE}/agui/v1/run`);
    expect(calls[0].method).toBe("POST");
    expect(calls[0].headers.authorization).toBe("Bearer tok");
    expect(calls[0].headers.accept).toBe("text/event-stream");
  });

  it("sends no authorization header without a token", async () => {
    const { fetch, calls } = fakeFetch([{ sse: fixtureText("empty-run.sse") }]);
    await runOnce({ ...req, fetchImpl: fetch });
    expect(calls[0].headers).not.toHaveProperty("authorization");
  });

  it("422 from the captured missing-status resume", async () => {
    const captured = fixtureJson<{ status: number; body: unknown }>("resume-no-status.json");
    const { fetch } = fakeFetch([{ status: captured.status, json: captured.body }]);
    const outcome = await runOnce({ ...req, fetchImpl: fetch });
    expect(outcome.kind).toBe("HTTP_ERROR");
    if (outcome.kind !== "HTTP_ERROR") return;
    expect(outcome.status).toBe(422);
    expect(outcome.label).toBe("the server refused the request body");
    expect(outcome.detail).toMatch(/"status"/);
  });

  it.each([
    [403, { detail: "cao:write required when resume[] is non-empty" }, "read-only access: answering an approval needs cao:write"],
    [404, { detail: "AG-UI surface disabled" }, "AG-UI is off"],
    [404, { detail: "Approval bridge not initialized" }, "no approval bridge"],
    [501, { detail: "ag-ui-protocol is not installed." }, "the server lacks the [agui] extra"],
    [500, { error: "x" }, "HTTP 500"],
  ])("HTTP %i is labelled", async (status, json, label) => {
    const { fetch } = fakeFetch([{ status, json }]);
    const outcome = await runOnce({ ...req, fetchImpl: fetch });
    expect(outcome).toMatchObject({ kind: "HTTP_ERROR", status, label });
  });

  it("a stream with no terminal frame, or no body, is NO_END", async () => {
    const { fetch } = fakeFetch([{ sse: sse({ type: "RUN_STARTED" }) }, { noBody: true }]);
    expect(await runOnce({ ...req, fetchImpl: fetch })).toEqual({ kind: "NO_END" });
    expect(await runOnce({ ...req, fetchImpl: fetch })).toEqual({ kind: "NO_END" });
  });

  it("a thrown fetch is NETWORK_ERROR, whatever was thrown", async () => {
    const { fetch } = fakeFetch([{ throws: new TypeError("Failed to fetch") }]);
    expect(await runOnce({ ...req, fetchImpl: fetch })).toEqual({ kind: "NETWORK_ERROR", message: "Failed to fetch" });
    const odd = (async () => {
      throw "boom";
    }) as unknown as typeof fetch;
    expect(await runOnce({ ...req, fetchImpl: odd })).toEqual({ kind: "NETWORK_ERROR", message: "boom" });
  });

  it("a run that does not answer in time is TIMEOUT", async () => {
    const { fetch } = fakeFetch([{ hang: true }]);
    expect(await runOnce({ ...req, timeoutMs: 5, fetchImpl: fetch })).toEqual({ kind: "TIMEOUT" });
  });

  it("uses the global fetch when none is given", async () => {
    const { fetch } = fakeFetch([{ sse: fixtureText("empty-run.sse") }]);
    const saved = globalThis.fetch;
    globalThis.fetch = fetch;
    try {
      expect((await runOnce(req)).kind).toBe("SUCCESS");
    } finally {
      globalThis.fetch = saved;
    }
  });
});

describe("detailOf and httpLabel", () => {
  it("reads detail as text, falls back to the raw body", () => {
    expect(detailOf('{"detail":"x"}')).toBe("x");
    expect(detailOf('{"detail":[{"msg":"m"}]}')).toBe('[{"msg":"m"}]');
    expect(detailOf('{"other":1}')).toBe('{"other":1}');
    expect(detailOf("plain")).toBe("plain");
    expect(httpLabel(418, "")).toBe("HTTP 418");
  });
});
