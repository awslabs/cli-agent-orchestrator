/**
 * One run on the run plane: `POST /agui/v1/run` read as fetch-based SSE.
 *
 * Every run ends in exactly one {@link RunOutcome}. A resume is a new run on
 * the same thread that carries `resume[]`.
 */

import { readFrames } from "./sse.ts";
import { urlFor } from "./routes.ts";
import type { Frame, ResumeEntry, RunOutcome, SkippedResume, WireInterrupt } from "./types.ts";

export interface RunRequest {
  base: string;
  threadId: string;
  runId: string;
  resume?: ResumeEntry[];
  token?: string;
  timeoutMs?: number;
  fetchImpl?: typeof fetch;
}

/** The body sent for one run. `resume` is omitted when there is nothing to answer. */
export function runBody(req: Pick<RunRequest, "threadId" | "runId" | "resume">): string {
  const body: Record<string, unknown> = {
    threadId: req.threadId,
    runId: req.runId,
    messages: [],
    protocolVersion: "1.0",
  };
  if (req.resume && req.resume.length > 0) body.resume = req.resume;
  return JSON.stringify(body);
}

/** Human label for an HTTP failure on the run plane. */
export function httpLabel(status: number, detail: string): string {
  switch (status) {
    case 403:
      return "read-only access: answering an approval needs cao:write";
    case 404:
      return detail === "AG-UI surface disabled" ? "AG-UI is off" : "no approval bridge";
    case 422:
      return "the server refused the request body";
    case 501:
      return "the server lacks the [agui] extra";
    default:
      return `HTTP ${status}`;
  }
}

/** Pull FastAPI's `detail` out of an error body as plain text. */
export function detailOf(text: string): string {
  try {
    const parsed: unknown = JSON.parse(text);
    if (parsed && typeof parsed === "object" && "detail" in parsed) {
      const detail = (parsed as { detail: unknown }).detail;
      return typeof detail === "string" ? detail : JSON.stringify(detail);
    }
  } catch {
    // Not JSON: fall through to the raw text.
  }
  return text;
}

/** Fold one run's frames into its outcome. */
export function outcomeFromFrames(frames: Iterable<Frame>): RunOutcome {
  for (const frame of frames) {
    const terminal = terminalOutcome(frame);
    if (terminal) return terminal;
  }
  return { kind: "NO_END" };
}

function terminalOutcome(frame: Frame): RunOutcome | null {
  if (frame.type === "RUN_ERROR") {
    return { kind: "RUN_ERROR", message: String(frame.message ?? "") };
  }
  if (frame.type !== "RUN_FINISHED") return null;
  const result = frame.result as { skippedResume?: SkippedResume[] } | undefined;
  const skipped = Array.isArray(result?.skippedResume) ? result.skippedResume : [];
  const outcome = frame.outcome as { type?: unknown; interrupts?: WireInterrupt[] } | undefined;
  if (outcome?.type === "interrupt") {
    return { kind: "INTERRUPT", interrupts: outcome.interrupts ?? [], skipped };
  }
  if (outcome?.type === "success") return { kind: "SUCCESS", skipped };
  return { kind: "OTHER_END", outcomeType: typeof outcome?.type === "string" ? outcome.type : "none" };
}

/** Run once and report how it ended. Never throws. */
export async function runOnce(req: RunRequest): Promise<RunOutcome> {
  const fetchImpl = req.fetchImpl ?? fetch;
  const controller = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, req.timeoutMs ?? 30_000);
  const headers: Record<string, string> = {
    "content-type": "application/json",
    accept: "text/event-stream",
  };
  if (req.token) headers.authorization = `Bearer ${req.token}`;
  try {
    const response = await fetchImpl(urlFor(req.base, "run"), {
      method: "POST",
      headers,
      body: runBody(req),
      signal: controller.signal,
    });
    if (!response.ok) {
      const detail = detailOf(await response.text());
      return { kind: "HTTP_ERROR", status: response.status, detail, label: httpLabel(response.status, detail) };
    }
    if (!response.body) return { kind: "NO_END" };
    for await (const frame of readFrames(response.body)) {
      const terminal = terminalOutcome(frame);
      if (terminal) return terminal;
    }
    return { kind: "NO_END" };
  } catch (error) {
    if (timedOut) return { kind: "TIMEOUT" };
    return { kind: "NETWORK_ERROR", message: error instanceof Error ? error.message : String(error) };
  } finally {
    clearTimeout(timer);
  }
}
