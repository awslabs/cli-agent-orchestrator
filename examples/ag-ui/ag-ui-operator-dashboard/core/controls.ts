/**
 * What an operator may answer, and the exact `resume[]` entry each answer
 * becomes. Pure: no I/O, no DOM.
 */

import type { ResponseSchema, ResumeEntry } from "./types.ts";

export type Control = "approve" | "deny" | "edit";

export type Decision =
  | { kind: "approve" }
  | { kind: "deny" }
  | { kind: "edit"; text: string };

/**
 * The longest edited text CAO accepts, counted as Python's `len()` counts it:
 * in code points. Longer text is not cut: `handoff_approval.py` refuses it, and
 * the run plane answers that entry with `RUN_ERROR`, which stops the batch there.
 */
export const EDIT_MAX_CHARS = 4000;

/** Length in code points, as Python's `len()` counts a `str` (an emoji is 1, not 2). */
export function codePoints(text: string): number {
  return [...text].length;
}

/**
 * Text the server would treat as blank. It tests `editedArgs.strip()`, and
 * Python's whitespace includes U+001C..U+001F and U+0085, which JavaScript's
 * `\s` does not.
 */
const BLANK = /^[\s\x1c-\x1f\x85]*$/;

/** True when the server would see `text` as empty and apply an approve. */
export function isBlankForServer(text: string): boolean {
  return BLANK.test(text);
}

/** Approve and deny always; edit only when the schema carries `editedArgs`. */
export function controlsFor(schema: ResponseSchema | undefined): Control[] {
  const offersEdit = Boolean(schema?.properties && "editedArgs" in schema.properties);
  return offersEdit ? ["approve", "deny", "edit"] : ["approve", "deny"];
}

export type PayloadResult = { ok: true; entry: ResumeEntry } | { ok: false; reason: string };

/**
 * Build one `resume[]` entry, or refuse with a reason the UI shows.
 *
 * - Edit is refused when the interrupt does not offer it.
 * - Blank or whitespace-only edit text is refused: the server would apply it
 *   as an approve.
 * - Edit text must be one line: the server would cut it without notice,
 *   dropping carriage returns and everything after the first line feed.
 * - Edit text must be at most {@link EDIT_MAX_CHARS} code points: the server
 *   refuses longer text with `RUN_ERROR`, and that stops the whole batch at
 *   that entry.
 * - Edit always sends `approved: true`; `approved: false` with text is a deny.
 */
export function payloadFor(
  interruptId: string,
  decision: Decision,
  schema: ResponseSchema | undefined,
): PayloadResult {
  if (decision.kind === "approve") {
    return { ok: true, entry: { interruptId, status: "resolved", payload: { approved: true } } };
  }
  if (decision.kind === "deny") {
    return { ok: true, entry: { interruptId, status: "resolved", payload: { approved: false } } };
  }
  if (!controlsFor(schema).includes("edit")) {
    return { ok: false, reason: "this prompt does not accept edited text" };
  }
  const { text } = decision;
  if (isBlankForServer(text)) {
    return { ok: false, reason: "edited text is empty; nothing was sent" };
  }
  if (/[\r\n]/.test(text)) {
    return { ok: false, reason: "edited text must be a single line" };
  }
  if (codePoints(text) > EDIT_MAX_CHARS) {
    return { ok: false, reason: `edited text is longer than ${EDIT_MAX_CHARS} characters` };
  }
  return {
    ok: true,
    entry: { interruptId, status: "resolved", payload: { approved: true, editedArgs: text } },
  };
}
