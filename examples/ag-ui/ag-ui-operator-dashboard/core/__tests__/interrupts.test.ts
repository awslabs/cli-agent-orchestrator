import { describe, expect, it } from "vitest";
import {
  applyAmbientHint,
  applyListing,
  applyResumeOutcome,
  batch,
  decide,
  DELIVERED_TEXT,
  describeFailure,
  emptyBoard,
  GONE_REASON,
  markSubmitting,
  stateText,
  undecide,
  UNCONFIRMED_TEXT,
  type Board,
} from "../interrupts.ts";
import { outcomeFromFrames } from "../runPlane.ts";
import type { RunOutcome, WireInterrupt } from "../types.ts";
import { fixtureFrames, interruptsOf } from "./helpers.ts";

const two = interruptsOf("two-interrupts.sse");
const [A, B] = two;
const edit = interruptsOf("edit-offered.generated.sse")[0];

function boardWith(interrupts: WireInterrupt[]): Board {
  return applyListing(emptyBoard(), interrupts);
}
const states = (b: Board) => b.cards.map((c) => c.state.s);

describe("cards come from the run plane", () => {
  it("one card per interrupt, with controls from responseSchema", () => {
    const board = boardWith([...two, edit]);
    expect(board.cards.map((c) => c.id)).toEqual([A.id, B.id, edit.id]);
    expect(board.cards.map((c) => c.controls)).toEqual([
      ["approve", "deny"],
      ["approve", "deny"],
      ["approve", "deny", "edit"],
    ]);
    expect(board.cards[0]).toMatchObject({ provider: "mock_cli", terminalId: A.metadata?.terminalId });
    expect(board.cards[0].subagentRunId).toBe(A.subagentRunId);
  });

  it("missing optional fields become empty text", () => {
    const board = boardWith([{ id: "bare" }]);
    expect(board.cards[0]).toMatchObject({ reason: "", message: "", provider: "", terminalId: "", subagentRunId: "" });
  });
});

describe("decisions are held until every open interrupt is covered", () => {
  it("one decided of two is not ready, and names the one still waiting", () => {
    const board = decide(boardWith(two), A.id, { kind: "approve" });
    expect(batch(board)).toEqual({ ready: false, resume: [expect.objectContaining({ interruptId: A.id })], waitingOn: [B.id] });
    expect(stateText(board.cards[0])).toMatch(/^held: approve/);
  });

  it("both decided: one resume[] with both entries, each status: resolved", () => {
    let board = decide(boardWith(two), A.id, { kind: "approve" });
    board = decide(board, B.id, { kind: "deny" });
    const b = batch(board);
    expect(b.ready).toBe(true);
    expect(b.resume).toEqual([
      { interruptId: A.id, status: "resolved", payload: { approved: true } },
      { interruptId: B.id, status: "resolved", payload: { approved: false } },
    ]);
  });

  it("nothing open is never ready; a decision can be withdrawn or changed", () => {
    expect(batch(emptyBoard()).ready).toBe(false);
    let board = decide(boardWith([A]), A.id, { kind: "approve" });
    board = undecide(board, A.id);
    expect(states(board)).toEqual(["pending"]);
    expect(undecide(board, A.id)).toEqual(board);
    board = decide(decide(board, A.id, { kind: "approve" }), A.id, { kind: "deny" });
    expect(batch(board).resume[0].payload).toEqual({ approved: false });
  });

  it("not ready while a run is in flight or a reconcile is owed", () => {
    let board = decide(boardWith([A, B]), A.id, { kind: "approve" });
    board = markSubmitting(board, [A.id]);
    board = decide(board, B.id, { kind: "deny" });
    expect(batch(board).ready).toBe(false);
    const owed = { ...decide(boardWith([A]), A.id, { kind: "approve" }), needsReconcile: true };
    expect(batch(owed).ready).toBe(false);
  });
});

describe("client refusals", () => {
  it("blank edit is refused, nothing is held, and the card stays decidable", () => {
    let board = decide(boardWith([edit]), edit.id, { kind: "edit", text: "   " });
    expect(board.cards[0].state).toEqual({ s: "refused", reason: "edited text is empty; nothing was sent" });
    expect(batch(board)).toMatchObject({ ready: false, resume: [], waitingOn: [edit.id] });
    expect(stateText(board.cards[0])).toBe("refused: edited text is empty; nothing was sent");
    board = decide(board, edit.id, { kind: "edit", text: "use a temp dir" });
    expect(batch(board).resume).toEqual([
      { interruptId: edit.id, status: "resolved", payload: { approved: true, editedArgs: "use a temp dir" } },
    ]);
  });

  it("a card that is not open ignores new decisions", () => {
    const board = markSubmitting(decide(boardWith([A]), A.id, { kind: "approve" }), [A.id]);
    expect(decide(board, A.id, { kind: "deny" })).toEqual(board);
    expect(markSubmitting(boardWith([A]), [A.id]).cards[0].state.s).toBe("pending");
  });
});

describe("outcome decided per id", () => {
  function sent(interrupts: WireInterrupt[], ids: string[]): Board {
    let board = boardWith(interrupts);
    for (const id of ids) board = decide(board, id, { kind: "approve" });
    return markSubmitting(board, ids);
  }

  it("captured batched resume: both delivered", () => {
    const board = applyResumeOutcome(sent(two, [A.id, B.id]), [A.id, B.id], outcomeFromFrames(fixtureFrames("resume-batched.sse")));
    expect(states(board)).toEqual(["delivered", "delivered"]);
    expect(stateText(board.cards[0])).toBe(DELIVERED_TEXT);
  });

  it("skippedResume → skipped with the server's reason; listed again → still pending", () => {
    const outcome: RunOutcome = {
      kind: "INTERRUPT",
      interrupts: [B],
      skipped: [{ interruptId: A.id, reason: "expired" }],
    };
    const board = applyResumeOutcome(sent(two, [A.id, B.id]), [A.id, B.id], outcome);
    expect(board.cards.map((c) => c.state)).toEqual([
      { s: "skipped", reason: "expired" },
      { s: "pending", note: "still open after the answer was sent" },
    ]);
  });

  it("outcome: interrupt because others are pending does not undo a delivery", () => {
    const C: WireInterrupt = { id: "new-one" };
    const outcome: RunOutcome = { kind: "INTERRUPT", interrupts: [C], skipped: [] };
    const board = applyResumeOutcome(sent([A], [A.id]), [A.id], outcome);
    expect(board.cards.map((c) => [c.id, c.state.s])).toEqual([
      [A.id, "delivered"],
      ["new-one", "pending"],
    ]);
  });

  it("a card that was not sent and has disappeared is shown as no longer pending", () => {
    let board = boardWith(two);
    board = decide(board, A.id, { kind: "approve" });
    board = markSubmitting(board, [A.id]);
    board = applyResumeOutcome(board, [A.id], { kind: "SUCCESS", skipped: [] });
    expect(board.cards[1].state).toEqual({ s: "skipped", reason: GONE_REASON });
  });

  it.each<[RunOutcome, string]>([
    [outcomeFromFrames(fixtureFrames("resume-edit-refused.sse")), "RUN_ERROR: Resume failed"],
    [{ kind: "HTTP_ERROR", status: 403, detail: "d", label: "l" }, "l (HTTP 403): d"],
    [{ kind: "HTTP_ERROR", status: 500, detail: "d", label: "HTTP 500" }, "HTTP 500: d"],
    [{ kind: "NETWORK_ERROR", message: "Failed to fetch" }, "network error"],
    [{ kind: "TIMEOUT" }, "the run timed out"],
    [{ kind: "NO_END" }, "the run ended with no RUN_FINISHED or RUN_ERROR"],
    [{ kind: "OTHER_END", outcomeType: "cancelled" }, 'the run finished with outcome "cancelled", which reports no delivery'],
  ])("a failed run marks every sent card unknown and owes a reconcile (%j)", (outcome, text) => {
    let board = sent(two, [A.id, B.id]);
    board = applyResumeOutcome(board, [A.id, B.id], outcome);
    expect(states(board)).toEqual(["unknown", "unknown"]);
    expect(board.needsReconcile).toBe(true);
    expect(board.lastError).toContain(text);
    expect(stateText(board.cards[0])).toMatch(/^unknown, reconciling: /);
  });

  it("the reconcile run: ids still listed are pending again, the rest delivered (unconfirmed)", () => {
    let board = sent(two, [A.id, B.id]);
    board = applyResumeOutcome(board, [A.id, B.id], { kind: "RUN_ERROR", message: "x" });
    board = applyListing(board, [B]);
    expect(board.cards.map((c) => c.state)).toEqual([
      { s: "delivered", unconfirmed: true },
      { s: "pending", note: "still open after the failed run" },
    ]);
    expect(board.needsReconcile).toBe(false);
    expect(stateText(board.cards[0])).toBe(UNCONFIRMED_TEXT);
    expect(stateText(board.cards[1])).toBe("pending (still open after the failed run)");
  });

  it("captured edit refusal then reconcile: the interrupt is still pending", () => {
    const [E] = interruptsOf("reconcile-before-edit.sse");
    let board = boardWith([E]);
    board = decide(board, E.id, { kind: "approve" });
    board = markSubmitting(board, [E.id]);
    board = applyResumeOutcome(board, [E.id], outcomeFromFrames(fixtureFrames("resume-edit-refused.sse")));
    board = applyListing(board, interruptsOf("reconcile-after-edit.sse"));
    expect(board.cards[0].state.s).toBe("pending");
  });

  it("a submitting card left by a listing run is treated like unknown", () => {
    let board = markSubmitting(decide(boardWith([A]), A.id, { kind: "approve" }), [A.id]);
    expect(applyListing(board, []).cards[0].state).toEqual({ s: "delivered", unconfirmed: true });
    expect(applyListing(board, [A]).cards[0].state.s).toBe("pending");
    board = applyResumeOutcome(board, [A.id], { kind: "SUCCESS", skipped: [] });
    expect(applyListing(board, [A]).cards[0].state).toEqual({ s: "pending", note: "listed as open again" });
    expect(applyListing(board, []).cards[0].state.s).toBe("delivered");
  });

  it("a failed run leaves unsent cards alone", () => {
    let board = decide(boardWith(two), A.id, { kind: "approve" });
    board = markSubmitting(board, [A.id]);
    board = applyResumeOutcome(board, [A.id], { kind: "TIMEOUT" });
    expect(states(board)).toEqual(["unknown", "pending"]);
  });

  it("an unsent card that is not open stays as it is", () => {
    let board = decide(boardWith(two), A.id, { kind: "approve" });
    board = markSubmitting(board, [A.id]);
    board = applyResumeOutcome(board, [A.id], { kind: "INTERRUPT", interrupts: [B], skipped: [] });
    board = decide(board, B.id, { kind: "deny" });
    board = markSubmitting(board, [B.id]);
    board = applyResumeOutcome(board, [B.id], { kind: "SUCCESS", skipped: [] });
    expect(states(board)).toEqual(["delivered", "delivered"]);
  });
});

describe("ambient approval_card frames never change a card", () => {
  it("attaches a hint and asks for a fresh run when resolved or expired", () => {
    const board = boardWith([A]);
    const resolved = applyAmbientHint(board, { interrupt_id: A.id, resolved: true, outcome: "approve" });
    expect(resolved.refresh).toBe(true);
    expect(resolved.board.cards[0].state).toEqual(board.cards[0].state);
    expect(resolved.board.cards[0].hint).toEqual({ resolved: true, outcome: "approve" });
    expect(applyAmbientHint(board, { interrupt_id: A.id, outcome: "expired" }).refresh).toBe(true);
    const open = applyAmbientHint(board, { interrupt_id: A.id, outcome: 3 });
    expect(open).toMatchObject({ refresh: false });
    expect(open.board.cards[0].hint).toEqual({ resolved: false, outcome: undefined });
  });

  it("a resolved or expired claim about an unknown id, or a card with no id, changes nothing", () => {
    const board = boardWith([A]);
    expect(applyAmbientHint(board, { interrupt_id: "forged", resolved: true })).toEqual({ board, refresh: false });
    expect(applyAmbientHint(board, { interrupt_id: "forged", outcome: "expired" })).toEqual({ board, refresh: false });
    expect(applyAmbientHint(board, { resolved: true })).toEqual({ board, refresh: false });
  });

  it("an open card for an unknown id asks for a read-only refresh and changes no card", () => {
    const board = boardWith([A]);
    expect(applyAmbientHint(board, { interrupt_id: "new-id", options: ["approve", "deny"] })).toEqual({ board, refresh: true });
  });
});

describe("text", () => {
  it("every state has a status line", () => {
    const card = boardWith([A]).cards[0];
    expect(stateText(card)).toBe("pending");
    expect(stateText({ ...card, state: { s: "submitting", decision: { kind: "deny" } } })).toBe("sending: deny");
    expect(stateText({ ...card, state: { s: "skipped", reason: "unknown" } })).toBe("skipped: unknown");
    expect(describeFailure({ kind: "SUCCESS", skipped: [] })).toBe("");
  });
});
