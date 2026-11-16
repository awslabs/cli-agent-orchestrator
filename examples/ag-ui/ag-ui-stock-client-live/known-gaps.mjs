// Known-gap accounting for the node lane of the CAO Dojo drift check (#1116).
//
// No dependencies, so the rule can be tested without @ag-ui/client installed.
// verify-trace.mjs feeds every trace's enforcement warnings into one GapLedger,
// then asks it once, after all traces, which known gaps no trace observed. That
// is the same union the Python lane's test_every_known_gap_is_still_observed
// takes across its sources: a gap the fresh recording dropped but the stale
// published trace still carries is still observed, and so is the reverse.

// The enforcement warning text a known_gaps.json entry allows, mapped to its gap id.
export function allowedWarnings(gaps) {
  const allowed = new Map();
  for (const [gapId, gap] of Object.entries(gaps)) {
    for (const [type, keys] of Object.entries(gap)) {
      for (const key of keys) {
        allowed.set(`[ag-ui][enforce] Removed unrecognised material at '/${key}' on ${type}.`, gapId);
      }
    }
  }
  return allowed;
}

export class GapLedger {
  constructor(allowed) {
    this.allowed = allowed;
    this.seen = new Set();
  }

  // Record one run's warnings. Returns the ones no known gap allows.
  record(warnings) {
    const unlisted = [];
    for (const w of warnings) {
      const text = [...this.allowed.keys()].find((prefix) => w.startsWith(prefix));
      if (text === undefined) unlisted.push(w);
      else this.seen.add(text);
    }
    return unlisted;
  }

  // The known gaps that no recorded trace observed, as [text, gapId] pairs.
  unobserved() {
    return [...this.allowed].filter(([text]) => !this.seen.has(text));
  }
}
