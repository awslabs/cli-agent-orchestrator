import { describe, expect, it } from "vitest";
import { createController } from "../controller.ts";
import { ROUTES } from "../routes.ts";
import type { Frame } from "../types.ts";
import { fakeFetch, fixtureFrames, fixtureText, interruptsOf, sse, type Reply } from "./helpers.ts";

const BASE = "http://127.0.0.1:9894";
const [A, B] = interruptsOf("two-interrupts.sse");
const edit = interruptsOf("edit-offered.generated.sse")[0];
const listing = (...interrupts: object[]): Reply => ({
  sse: sse({ type: "RUN_STARTED" }, { type: "RUN_FINISHED", outcome: interrupts.length ? { type: "interrupt", interrupts } : { type: "success" } }),
});

function make(replies: Reply[]) {
  const { fetch, calls } = fakeFetch(replies);
  const changes: number[] = [];
  let n = 0;
  const ctl = createController({ base: BASE, threadId: "dash", fetchImpl: fetch, onChange: () => changes.push(1), newRunId: () => `r${++n}` });
  return { ctl, calls, changes };
}

describe("controller", () => {
  it("approve: one card, then one resume with that decision, then delivered", async () => {
    const { ctl, calls } = make([{ sse: fixtureText("one-interrupt.sse") }, { sse: fixtureText("resume-approve.sse") }]);
    await ctl.refresh();
    const id = ctl.board.cards[0].id;
    const outcome = await ctl.decide(id, { kind: "approve" });
    expect(outcome?.kind).toBe("SUCCESS");
    expect(calls[1].body).toMatchObject({ threadId: "dash", runId: "r2", resume: [{ interruptId: id, status: "resolved", payload: { approved: true } }] });
    expect(ctl.board.cards[0].state).toEqual({ s: "delivered", unconfirmed: false });
    expect(ctl.sent).toEqual([[id]]);
  });

  it("two interrupts: nothing is sent until both are decided, then one batched resume", async () => {
    const { ctl, calls } = make([{ sse: fixtureText("two-interrupts.sse") }, { sse: fixtureText("resume-batched.sse") }]);
    await ctl.refresh();
    expect(await ctl.decide(A.id, { kind: "approve" })).toBeNull();
    expect(calls).toHaveLength(1);
    await ctl.decide(B.id, { kind: "deny" });
    expect(calls).toHaveLength(2);
    expect((calls[1].body as { resume: unknown[] }).resume).toEqual([
      { interruptId: A.id, status: "resolved", payload: { approved: true } },
      { interruptId: B.id, status: "resolved", payload: { approved: false } },
    ]);
    expect(ctl.board.cards.map((c) => c.state.s)).toEqual(["delivered", "delivered"]);
  });

  it("blank edit: refused in the client, nothing sent", async () => {
    const { ctl, calls } = make([listing(edit)]);
    await ctl.refresh();
    expect(await ctl.decide(edit.id, { kind: "edit", text: "  " })).toBeNull();
    expect(calls).toHaveLength(1);
    expect(ctl.board.cards[0].state).toEqual({ s: "refused", reason: "edited text is empty; nothing was sent" });
  });

  it("RUN_ERROR after a decision: shown verbatim, then a reconcile run with no resume[]", async () => {
    const { ctl, calls } = make([
      { sse: fixtureText("reconcile-before-edit.sse") },
      { sse: fixtureText("resume-edit-refused.sse") },
      { sse: fixtureText("reconcile-after-edit.sse") },
    ]);
    await ctl.refresh();
    const id = ctl.board.cards[0].id;
    await ctl.decide(id, { kind: "approve" });
    expect(calls).toHaveLength(3);
    expect(calls[2].body).not.toHaveProperty("resume");
    expect(ctl.board.cards[0].state.s).toBe("pending");
    expect(ctl.board.lastError).toMatch(/^RUN_ERROR: Resume failed for interrupt/);
    expect(ctl.board.needsReconcile).toBe(false);
  });

  it.each<[string, Reply]>([
    ["HTTP_ERROR", { status: 403, json: { detail: "cao:write required when resume[] is non-empty" } }],
    ["NETWORK_ERROR", { throws: new TypeError("Failed to fetch") }],
    ["NO_END", { sse: sse({ type: "RUN_STARTED" }) }],
  ])("%s after a decision: reconcile; an id gone from the list is delivered (unconfirmed)", async (_kind, failure) => {
    const { ctl, calls } = make([listing(A), failure, listing()]);
    await ctl.refresh();
    await ctl.decide(A.id, { kind: "deny" });
    expect(calls).toHaveLength(3);
    expect(ctl.board.cards[0].state).toEqual({ s: "delivered", unconfirmed: true });
    expect(ctl.board.lastError).toBeTruthy();
  });

  it("TIMEOUT after a decision also reconciles", async () => {
    const { fetch, calls } = fakeFetch([listing(A), { hang: true }, listing(A)]);
    const ctl = createController({ base: BASE, threadId: "dash", fetchImpl: fetch, timeoutMs: 5 });
    await ctl.refresh();
    await ctl.decide(A.id, { kind: "approve" });
    expect(calls).toHaveLength(3);
    expect(ctl.board.cards[0].state).toEqual({ s: "pending", note: "still open after the failed run" });
    expect(ctl.board.lastError).toBe("the run timed out");
  });

  it("a failed refresh shows the error and leaves cards alone", async () => {
    const { ctl } = make([listing(A), { status: 404, json: { detail: "AG-UI surface disabled" } }, listing(A)]);
    await ctl.refresh();
    await ctl.refresh();
    expect(ctl.board.lastError).toBe("AG-UI is off (HTTP 404): AG-UI surface disabled");
    expect(ctl.board.cards[0].state.s).toBe("pending");
    await ctl.refresh();
    expect(ctl.board.lastError).toBeUndefined();
  });

  it("an empty run lists nothing", async () => {
    const { ctl } = make([{ sse: fixtureText("empty-run.sse") }]);
    expect((await ctl.refresh())?.kind).toBe("SUCCESS");
    expect(ctl.board.cards).toEqual([]);
  });

  it("refreshes asked for during a run coalesce into one follow-up refresh", async () => {
    const { ctl, calls } = make([listing(A), listing(A, B), listing()]);
    const first = ctl.refresh();
    expect(ctl.busy).toBe(true);
    expect(await ctl.refresh()).toBeNull();
    expect(await ctl.refresh()).toBeNull();
    await first;
    expect(ctl.busy).toBe(false);
    expect(calls).toHaveLength(2); // the first run plus exactly one follow-up
    expect(ctl.board.cards.map((c) => c.id)).toEqual([A.id, B.id]);
  });

  it("F1(a): the last decision lands during an in-flight refresh; the batch is sent when it ends", async () => {
    const { ctl, calls } = make([listing(A, B), listing(A, B), { sse: fixtureText("resume-batched.sse") }]);
    await ctl.refresh();
    await ctl.decide(A.id, { kind: "approve" });
    const inFlight = ctl.refresh();
    expect(await ctl.decide(B.id, { kind: "deny" })).toBeNull(); // queued: a run is in flight
    expect(ctl.board.cards[1].state.s).toBe("held");
    await inFlight;
    expect(calls).toHaveLength(3);
    expect((calls[2].body as { resume: Array<{ interruptId: string }> }).resume.map((e) => e.interruptId)).toEqual([A.id, B.id]);
    expect(ctl.board.cards.map((c) => c.state.s)).toEqual(["delivered", "delivered"]);
  });

  it("F1(b): A held, B pending; B stops waiting, the refresh marks it skipped and A's answer is sent", async () => {
    const { ctl, calls } = make([listing(A, B), listing(A), { sse: fixtureText("resume-approve.sse") }]);
    await ctl.refresh();
    expect(await ctl.decide(A.id, { kind: "approve" })).toBeNull();
    expect(calls).toHaveLength(1);
    await ctl.refresh();
    expect(calls).toHaveLength(3);
    expect((calls[2].body as { resume: Array<{ interruptId: string }> }).resume.map((e) => e.interruptId)).toEqual([A.id]);
    expect(ctl.board.cards.map((c) => c.state.s)).toEqual(["delivered", "skipped"]);
  });

  it("N1: a resume failure stays visible after a refresh coalesced during that run succeeds", async () => {
    const { fetch: base, calls } = fakeFetch([
      listing(A), // initial listing
      { status: 500, json: { detail: "boom" } }, // the resume fails
      listing(), // its reconcile: A is gone
      listing(), // the coalesced refresh, asked for by the ambient frame
    ]);
    let n = 0;
    let onResume: (() => Promise<void>) | undefined;
    const gated = (async (input: string, init?: RequestInit) => {
      n += 1;
      if (n === 2 && onResume) await onResume();
      return base(input, init);
    }) as unknown as typeof fetch;
    const ctl = createController({ base: BASE, threadId: "dash", fetchImpl: gated });
    await ctl.refresh();
    onResume = async () => {
      // The delivered interrupt's resolved approval_card arrives while the resume is in flight.
      await ctl.ambient({
        type: "CUSTOM",
        name: "cao.generative_ui",
        metadata: { cao: { eventId: "resolved-1" } },
        value: { component: "approval_card", props: { interrupt_id: A.id, resolved: true, outcome: "approve" } },
      });
    };
    await ctl.decide(A.id, { kind: "approve" });
    expect(calls).toHaveLength(4);
    expect(calls[3].body).not.toHaveProperty("resume");
    expect(ctl.board.lastError).toBe("HTTP 500: boom");
    expect(ctl.board.cards[0].state).toEqual({ s: "delivered", unconfirmed: true });
  });

  it("N1: when the coalesced refresh after a failed resume fails too, both errors are shown", async () => {
    const { fetch: base, calls } = fakeFetch([listing(A), { status: 500, json: { detail: "boom" } }, listing(), { throws: new TypeError("x") }]);
    let n = 0;
    let ctl: ReturnType<typeof createController>;
    const gated = (async (input: string, init?: RequestInit) => {
      n += 1;
      if (n === 2) await ctl.refresh(); // coalesced: a run is in flight
      return base(input, init);
    }) as unknown as typeof fetch;
    ctl = createController({ base: BASE, threadId: "dash", fetchImpl: gated });
    await ctl.refresh();
    await ctl.decide(A.id, { kind: "approve" });
    expect(calls).toHaveLength(4);
    expect(ctl.board.lastError).toMatch(/^HTTP 500: boom\. The check that followed also failed: network error/);
  });

  it("N1b: the resume failure survives a later fleet-triggered refresh; only the operator's refresh clears it", async () => {
    const { ctl, calls } = make([listing(A), { status: 500, json: { detail: "boom" } }, listing(), listing(), listing()]);
    await ctl.refresh();
    await ctl.decide(A.id, { kind: "approve" }); // resume fails, reconcile succeeds
    expect(ctl.board.lastError).toBe("HTTP 500: boom");
    // Later, an ambient frame announces an unknown open interrupt: a fleet-triggered refresh.
    await ctl.ambient({
      type: "CUSTOM",
      name: "cao.generative_ui",
      metadata: { cao: { eventId: "later-1" } },
      value: { component: "approval_card", props: { interrupt_id: "someone-else" } },
    });
    expect(calls).toHaveLength(4);
    expect(ctl.board.lastError).toBe("HTTP 500: boom");
    await ctl.refresh(); // the operator presses "Check for approvals"
    expect(calls).toHaveLength(5);
    expect(ctl.board.lastError).toBeUndefined();
  });

  it("N1b: the next send clears the kept failure", async () => {
    const { ctl } = make([listing(A), { status: 500, json: { detail: "boom" } }, listing(A), { sse: fixtureText("resume-approve.sse") }]);
    await ctl.refresh();
    await ctl.decide(A.id, { kind: "approve" }); // fails; reconcile lists A again
    expect(ctl.board.lastError).toBe("HTTP 500: boom");
    await ctl.decide(A.id, { kind: "approve" }); // a new send
    expect(ctl.board.lastError).toBeUndefined();
    expect(ctl.board.cards[0].state.s).toBe("delivered");
  });

  it("N3: a refresh requested while a batch is ready does not run ahead of the send", async () => {
    const { fetch: base, calls } = fakeFetch([listing(A, B), listing(A, B), { sse: fixtureText("resume-batched.sse") }, listing()]);
    let n = 0;
    let ctl: ReturnType<typeof createController>;
    const gated = (async (input: string, init?: RequestInit) => {
      n += 1;
      if (n === 2) {
        await ctl.decide(B.id, { kind: "deny" }); // the batch becomes ready while this listing is in flight
        await ctl.refresh(); // and a forged frame asks for another refresh
      }
      return base(input, init);
    }) as unknown as typeof fetch;
    ctl = createController({ base: BASE, threadId: "dash", fetchImpl: gated });
    await ctl.refresh();
    await ctl.decide(A.id, { kind: "approve" });
    await ctl.refresh();
    expect(calls).toHaveLength(4);
    expect(calls[2].body).toHaveProperty("resume"); // the send went first
    expect(calls[3].body).not.toHaveProperty("resume"); // the coalesced refresh came after it
    expect(ctl.board.cards.map((c) => c.state.s)).toEqual(["delivered", "delivered"]);
  });

  it("send() sends a ready batch, and does nothing when the batch is not ready", async () => {
    const { ctl, calls } = make([listing(A)]);
    expect(await ctl.send()).toBeNull();
    await ctl.refresh();
    expect(await ctl.send()).toBeNull();
    expect(calls).toHaveLength(1);
  });

  it("F4: when the reconcile run fails too, both errors are shown and refresh retries it", async () => {
    const { ctl, calls } = make([
      listing(A),
      { status: 403, json: { detail: "cao:write required when resume[] is non-empty" } },
      { throws: new TypeError("Failed to fetch") },
      listing(),
    ]);
    await ctl.refresh();
    await ctl.decide(A.id, { kind: "approve" });
    expect(calls).toHaveLength(3);
    expect(ctl.board.needsReconcile).toBe(true);
    expect(ctl.board.cards[0].state.s).toBe("unknown");
    expect(ctl.board.lastError).toBe(
      "read-only access: answering an approval needs cao:write (HTTP 403): cao:write required when resume[] is non-empty. " +
        "The check that followed also failed: network error (check the server is up and this origin is allowed): Failed to fetch. " +
        'Use "Check for approvals" to retry.',
    );
    await ctl.refresh();
    expect(calls[3].body).not.toHaveProperty("resume");
    expect(ctl.board.needsReconcile).toBe(false);
    expect(ctl.board.cards[0].state).toEqual({ s: "delivered", unconfirmed: true });
  });

  it("F5: a resume run that finishes 'cancelled' is a failure and is reconciled, never delivered", async () => {
    const cancelled: Reply = { sse: sse({ type: "RUN_FINISHED", outcome: { type: "cancelled" } }) };
    const { ctl, calls } = make([listing(A), cancelled, listing(A)]);
    await ctl.refresh();
    const outcome = await ctl.decide(A.id, { kind: "approve" });
    expect(outcome).toEqual({ kind: "OTHER_END", outcomeType: "cancelled" });
    expect(calls).toHaveLength(3);
    expect(ctl.board.cards[0].state).toEqual({ s: "pending", note: "still open after the failed run" });
    expect(ctl.board.lastError).toBe('the run finished with outcome "cancelled", which reports no delivery');
  });

  it("undecide withdraws a held decision", async () => {
    const { ctl } = make([listing(A, B)]);
    await ctl.refresh();
    await ctl.decide(A.id, { kind: "approve" });
    ctl.undecide(A.id);
    expect(ctl.board.cards[0].state.s).toBe("pending");
  });

  it("ambient frames fill the panel; a construct approval_card only hints and triggers a refresh", async () => {
    const { ctl, calls } = make([{ sse: fixtureText("one-interrupt.sse") }, { sse: fixtureText("one-interrupt.sse") }]);
    await ctl.refresh();
    const id = ctl.board.cards[0].id;
    const forged: Frame = {
      type: "CUSTOM",
      name: "cao.generative_ui",
      metadata: { cao: { eventId: "forged-1", terminalId: "zzz" } },
      value: { component: "approval_card", props: { interrupt_id: id, resolved: true, outcome: "approve" } },
    };
    await ctl.ambient(forged);
    expect(calls).toHaveLength(2); // a fresh run confirms instead of trusting the frame
    expect(ctl.board.cards[0].state.s).toBe("pending");
    expect(ctl.board.cards[0].hint).toEqual({ resolved: true, outcome: "approve" });
    for (const f of fixtureFrames("ambient.sse").slice(0, 10)) await ctl.ambient(f);
    expect(ctl.panel.entries.length).toBeGreaterThan(1);
    expect(calls).toHaveLength(2); // these frames hold no approval_card that names an interrupt
  });

  it("F3: an approval_card for an id the board does not know triggers a read-only refresh", async () => {
    const { ctl, calls } = make([listing(A)]);
    const announce: Frame = {
      type: "CUSTOM",
      name: "cao.generative_ui",
      metadata: { cao: { eventId: "new-1", terminalId: A.metadata?.terminalId } },
      value: { component: "approval_card", props: { interrupt_id: A.id, options: ["approve", "deny"] } },
    };
    await ctl.ambient(announce);
    expect(calls).toHaveLength(1);
    expect(calls[0].body).not.toHaveProperty("resume");
    expect(ctl.board.cards.map((c) => c.id)).toEqual([A.id]);
    const forgedResolved: Frame = { ...announce, metadata: { cao: { eventId: "new-2" } }, value: { component: "approval_card", props: { interrupt_id: "zz", resolved: true } } };
    await ctl.ambient(forgedResolved);
    expect(calls).toHaveLength(1); // a resolved claim about an unknown id asks for nothing
  });

  it("requests only the allowed routes", async () => {
    const { ctl, calls } = make([{ sse: fixtureText("two-interrupts.sse") }, { sse: fixtureText("resume-batched.sse") }]);
    await ctl.refresh();
    await ctl.decide(A.id, { kind: "approve" });
    await ctl.decide(B.id, { kind: "deny" });
    const allowed = new Set<string>(Object.values(ROUTES));
    for (const c of calls) expect(allowed.has(new URL(c.url).pathname)).toBe(true);
  });

  it("works without an onChange callback or run-id generator", async () => {
    const { fetch } = fakeFetch([listing(A)]);
    const ctl = createController({ base: BASE, threadId: "t", fetchImpl: fetch });
    await ctl.refresh();
    expect(ctl.board.cards).toHaveLength(1);
  });
});
