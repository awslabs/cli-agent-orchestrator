/**
 * Glue between the pure reducers and the run plane. The view and the
 * headless e2e both drive the app through this object, so they exercise the
 * same code.
 *
 * One run at a time. Every run ends by checking whether the held decisions now
 * cover every open interrupt and, if so, sending them, so a decision never
 * waits in `held` because it landed while another run was in flight. A
 * refresh asked for while a run is in flight is not dropped: requests coalesce
 * into one follow-up refresh.
 */

import { runOnce } from "./runPlane.ts";
import type { Decision } from "./controls.ts";
import type { Frame, ResumeEntry, RunOutcome } from "./types.ts";
import {
  applyAmbientHint,
  applyListing,
  applyResumeOutcome,
  batch,
  decide,
  describeFailure,
  emptyBoard,
  markSubmitting,
  undecide,
  type Board,
} from "./interrupts.ts";
import { approvalHintProps, emptyPanel, reduceFrame, type Panel } from "./genui.ts";
import { emptyTimeline, markReconnect, reduceTimeline, type Timeline } from "./timeline.ts";

export interface ControllerOptions {
  base: string;
  threadId: string;
  token?: string;
  timeoutMs?: number;
  fetchImpl?: typeof fetch;
  onChange?: (board: Board, panel: Panel, timeline: Timeline) => void;
  newRunId?: () => string;
}

export interface Controller {
  readonly board: Board;
  readonly panel: Panel;
  /** The session timeline built from `TOOL_CALL_*` frames. */
  readonly timeline: Timeline;
  /** True while a run is in flight. */
  readonly busy: boolean;
  /**
   * A run with no `resume[]`: lists the open interrupts. After a failed run
   * that carried decisions, this is also the retry for the reconcile.
   * Returns `null` when a run is in flight; the request is then coalesced into
   * one refresh after that run.
   */
  refresh(): Promise<RunOutcome | null>;
  /** Hold a decision; once every open interrupt is held, send one batched `resume[]`. */
  decide(id: string, decision: Decision): Promise<RunOutcome | null>;
  undecide(id: string): void;
  /** Send the held decisions now if they cover every open interrupt. */
  send(): Promise<RunOutcome | null>;
  /**
   * The ambient stream opened again after a drop (its `onReopen`). Calls still open
   * in the timeline become "unconfirmed" (see `markReconnect`).
   */
  reconnected(): void;
  /** Feed one ambient-stream frame. Never changes a card's state. */
  ambient(frame: Frame): Promise<void>;
  /** The interrupt ids of each `resume[]` sent so far, for tests and the e2e. */
  readonly sent: ReadonlyArray<ReadonlyArray<string>>;
}

export function createController(opts: ControllerOptions): Controller {
  let board = emptyBoard();
  let panel = emptyPanel();
  let timeline = emptyTimeline();
  let busy = false;
  let refreshWanted = false;
  let counter = 0;
  const sent: string[][] = [];
  const newRunId = opts.newRunId ?? (() => `run-${Date.now()}-${++counter}`);
  const changed = () => opts.onChange?.(board, panel, timeline);

  const run = (resume?: ResumeEntry[]) =>
    runOnce({
      base: opts.base,
      threadId: opts.threadId,
      runId: newRunId(),
      resume,
      token: opts.token,
      timeoutMs: opts.timeoutMs,
      fetchImpl: opts.fetchImpl,
    });

  /** One listing run. The caller holds `busy`. */
  async function listOnce(): Promise<RunOutcome> {
    const outcome = await run();
    if (outcome.kind === "SUCCESS" || outcome.kind === "INTERRUPT") {
      board = applyListing(board, outcome.kind === "INTERRUPT" ? outcome.interrupts : []);
      board = { ...board, lastError: undefined };
    } else {
      board = { ...board, lastError: describeFailure(outcome) };
    }
    changed();
    return outcome;
  }

  /**
   * The failure of the last resume run, kept visible across the listing runs
   * that follow it: its reconcile, a refresh coalesced while it was in flight,
   * and later refreshes the fleet triggers (ambient frames). It is cleared only
   * by the next send or by a refresh the operator asks for.
   */
  let sendFailure: string | undefined;

  /** A listing run that keeps `sendFailure` visible, adding the listing's own failure. */
  async function checkAfterSend(): Promise<RunOutcome> {
    const check = await listOnce();
    if (sendFailure !== undefined) {
      const ok = check.kind === "SUCCESS" || check.kind === "INTERRUPT";
      board = {
        ...board,
        lastError: ok
          ? sendFailure
          : `${sendFailure}. The check that followed also failed: ${describeFailure(check)}. Use "Check for approvals" to retry.`,
      };
      changed();
    }
    return check;
  }

  /** One resume run for the current batch, then its reconcile if it failed. The caller holds `busy`. */
  async function sendOnce(): Promise<RunOutcome> {
    const b = batch(board);
    const ids = b.resume.map((e) => e.interruptId);
    board = markSubmitting(board, ids);
    sent.push(ids);
    changed();
    sendFailure = undefined;
    const outcome = await run(b.resume);
    board = applyResumeOutcome(board, ids, outcome);
    sendFailure = board.lastError;
    changed();
    if (board.needsReconcile) await checkAfterSend();
    return outcome;
  }

  /**
   * Run `first` unless a run is in flight, then any coalesced refresh, then
   * send the batch if it has become ready. `null` means it was queued.
   *
   * A coalesced refresh waits while a batch is ready, so frames that keep
   * asking for refreshes (forged ones included) cannot hold back a send; the
   * send's own cycle runs that refresh afterwards.
   */
  async function cycle(first: () => Promise<RunOutcome>): Promise<RunOutcome | null> {
    if (busy) return null;
    busy = true;
    let outcome: RunOutcome;
    try {
      outcome = await first();
      while (refreshWanted && !batch(board).ready) {
        refreshWanted = false;
        await checkAfterSend();
      }
    } finally {
      busy = false;
    }
    if (batch(board).ready) await cycle(sendOnce);
    return outcome;
  }

  /** A listing run; `byOperator` clears a kept resume failure first. */
  async function refreshRun(byOperator: boolean): Promise<RunOutcome | null> {
    if (busy) {
      refreshWanted = true;
      return null;
    }
    if (byOperator) sendFailure = undefined;
    return cycle(checkAfterSend);
  }

  const refresh = () => refreshRun(true);

  async function send(): Promise<RunOutcome | null> {
    return batch(board).ready ? cycle(sendOnce) : null;
  }

  return {
    get board() {
      return board;
    },
    get panel() {
      return panel;
    },
    get timeline() {
      return timeline;
    },
    get busy() {
      return busy;
    },
    get sent() {
      return sent;
    },
    refresh,
    send,
    async decide(id, decision) {
      board = decide(board, id, decision);
      changed();
      // While a run is in flight, that run's end sends the batch.
      return send();
    },
    undecide(id) {
      board = undecide(board, id);
      changed();
    },
    reconnected() {
      timeline = markReconnect(timeline);
      changed();
    },
    async ambient(frame) {
      panel = reduceFrame(panel, frame);
      timeline = reduceTimeline(timeline, frame);
      const props = approvalHintProps(frame);
      let wantRefresh = false;
      if (props) {
        const hinted = applyAmbientHint(board, props);
        board = hinted.board;
        wantRefresh = hinted.refresh;
      }
      changed();
      if (wantRefresh) await refreshRun(false);
    },
  };
}
