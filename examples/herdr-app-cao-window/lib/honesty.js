// Honesty labels (design D23): every surface whose content is a guess, a heuristic, inferred, the
// agent's own words, proposed or missing in CAO carries one fixed label. HONESTY is the table of label
// texts; HONESTY_SOURCES names, for each surface, the function whose view model must contain that label
// and a fixture to call it with; HONESTY_PENDING lists the surfaces whose section is not built yet.
export const HONESTY = Object.freeze({
  bestGuess: "best guess",
  heuristic: "heuristic",
  handOff: "held while you were busy",
  knownGap: "known gap: a done agent asking in prose is not counted",
  recap: "the agent's own words",
  flowInferred: "inferred from the session name",
  needsCaoChange: "needs a CAO change",
  serverChecksSkipped: "server checks skipped",
  proposed: "proposed",
  aiField: "AI",
  declaredTools: "declared by the profile; the server resolves the final set",
  notEnforced: "not enforced by this provider",
  inputsUndescribed: "inputs not described by the spec",
});

const HONESTY_PROFILE = { name: "dev", role: "developer", allowedTools: ["fs_read"] };

// surface -> { label: key of HONESTY, module, fn, args(mod) -> arguments for fn }. The signatures the
// fixtures call are fixed by tasks 7.1 to 12.1; when one changes there, change its row here in the same commit.
export const HONESTY_SOURCES = {
  bestGuess: { label: "bestGuess", module: "./logic.js", fn: "classify",
    args: () => [[{ id: "w1", profile: "reviewer", caller: "c1", createdMs: 2 }, { id: "w2", profile: "reviewer", caller: "c1", createdMs: 3 }], {}, []] },
  stuckHint: { label: "heuristic", module: "./logic.js", fn: "quietHint",
    args: () => [{ workers: [{ id: "w1", state: "working" }] }, { subscribedAt: 0, at: {} }, 10, 11 * 60_000] },
  contextPercent: { label: "heuristic", module: "./logic.js", fn: "contextPercent",
    args: () => ["ctx 42% used", "ctx (\\d+)%"] },
  releasedToast: { label: "handOff", module: "./logic.js", fn: "toastStep",
    args: (mod) => [
      { ...mod.initialToast({ holdMaxSeconds: 120 }),
        queue: [{ key: "t9:asking", kind: "asking", session: "cao-b", terminalId: "t9", heldAt: 0, releaseAt: 120_000, later: 0,
          notification: { title: "b needs you", body: "cao-b / w", action: { name: "agent.focus", args: { terminal: "cao/t9" } } } }],
        focus: { terminal: "cao/t1", state: "idle", since: 0 } },
      { type: "focus", terminal: "cao/t1", agentState: "working", busy: true }, 10_000] },
  knownGap: { label: "knownGap", module: "./logic.js", fn: "knownGapNote", args: () => [] },
  recap: { label: "recap", module: "./logic.js", fn: "parseRecap", args: () => ["※ recap: Tests pass; waiting for review."] },
  startedByFlow: { label: "flowInferred", module: "./logic.js", fn: "flowOfSession", args: () => ["cao-flow-daily", ["daily"]] },
  flowRoutesMissing: { label: "needsCaoChange", module: "./flows.js", fn: "routesState", args: () => [404] },
  flowRuns: { label: "needsCaoChange", module: "./flows.js", fn: "flowRunsNote", args: () => [] },
  createChecksSkipped: { label: "serverChecksSkipped", module: "./flows.js", fn: "routesState", args: () => [404] },
  draftBox: { label: "proposed", module: "./draft.js", fn: "draftBox", args: () => [{ phase: "idle" }] },
  draftField: { label: "aiField", module: "./draft.js", fn: "applyDraft",
    args: () => [{ name: "", schedule: "" }, { kind: "flow", fields: { name: "x", schedule: "0 9 * * *" } }] },
  declaredTools: { label: "declaredTools", module: "./launch.js", fn: "askFirstDialog", args: () => [HONESTY_PROFILE, "claude_code", null] },
  notEnforced: { label: "notEnforced", module: "./launch.js", fn: "askFirstDialog", args: () => [HONESTY_PROFILE, "codex", null] },
  inputsUndescribed: { label: "inputsUndescribed", module: "./workflows.js", fn: "inputsForm", args: () => [{}] },
};

// surface -> the section that builds it. Each section deletes its rows in the task that writes the
// function; task 12.4 asserts this object is empty.
export const HONESTY_PENDING = {};
