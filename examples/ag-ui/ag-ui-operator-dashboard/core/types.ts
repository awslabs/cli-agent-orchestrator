/**
 * Wire types for the two AG-UI surfaces this app reads: the run plane
 * (`POST /agui/v1/run`) and the ambient stream (`/agui/v1/stream`).
 *
 * Only the fields the app uses are typed. Everything else is kept as
 * `unknown` and never interpreted.
 */

export interface ResponseSchema {
  type?: string;
  properties?: Record<string, unknown>;
  required?: string[];
}

/** One entry of a `RUN_FINISHED(interrupt)` outcome's `interrupts[]`. */
export interface WireInterrupt {
  id: string;
  reason?: string;
  message?: string;
  subagentRunId?: string;
  responseSchema?: ResponseSchema;
  expiresAt?: string;
  metadata?: {
    provider?: string | null;
    terminalId?: string | null;
    sessionName?: string | null;
    options?: string[];
  };
}

export interface SkippedResume {
  interruptId: string;
  reason: string;
}

/** A frame as decoded from one SSE `data:` line. */
export type Frame = { type: string } & Record<string, unknown>;

/** One `resume[]` entry. `status` is required by CAO (422 without it). */
export interface ResumeEntry {
  interruptId: string;
  status: "resolved";
  payload: { approved: boolean; editedArgs?: string };
}

/**
 * How one run on the run plane ended. Exactly one of these per run.
 * `OTHER_END` is a `RUN_FINISHED` whose outcome is neither "success" nor
 * "interrupt" (for example "cancelled", or none at all): it is a failure, never
 * a delivery.
 */
export type RunOutcome =
  | { kind: "SUCCESS"; skipped: SkippedResume[] }
  | { kind: "INTERRUPT"; interrupts: WireInterrupt[]; skipped: SkippedResume[] }
  | { kind: "RUN_ERROR"; message: string }
  | { kind: "OTHER_END"; outcomeType: string }
  | { kind: "NO_END" }
  | { kind: "HTTP_ERROR"; status: number; detail: string; label: string }
  | { kind: "NETWORK_ERROR"; message: string }
  | { kind: "TIMEOUT" };

/** Kinds after which a run that carried decisions must be reconciled. */
export const RECONCILE_KINDS: ReadonlySet<RunOutcome["kind"]> = new Set([
  "RUN_ERROR",
  "OTHER_END",
  "HTTP_ERROR",
  "NETWORK_ERROR",
  "TIMEOUT",
  "NO_END",
]);
