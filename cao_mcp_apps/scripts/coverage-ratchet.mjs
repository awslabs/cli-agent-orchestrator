#!/usr/bin/env node
// Coverage ratchet: fail CI if measured line coverage regresses below the
// floor recorded in .coverage-baseline.json.
//
// Reads:
//   - Python:   `coverage.json` produced by `pytest --cov --cov-report=json`
//   - Frontend: `coverage/coverage-summary.json` produced by `vitest --coverage`
//
// A `null` floor means "not yet established" — the measured value is reported
// but never fails the gate. Phase V raises the floors to lock gains.
//
// Usage:  node scripts/coverage-ratchet.mjs [repoRoot]

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const REPO_ROOT = resolve(process.argv[2] ?? "..");
const BASELINE_PATH = resolve(REPO_ROOT, ".coverage-baseline.json");

function readJson(path) {
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch (err) {
    if (err.code === "ENOENT") return null;
    throw err;
  }
}

function pythonLinePct(report) {
  // coverage.py json schema: { "totals": { "percent_covered": <float> } }
  const pct = report?.totals?.percent_covered;
  return typeof pct === "number" ? pct : null;
}

function frontendLinePct(summary) {
  // istanbul json-summary schema: { "total": { "lines": { "pct": <float> } } }
  const pct = summary?.total?.lines?.pct;
  return typeof pct === "number" ? pct : null;
}

const baseline = readJson(BASELINE_PATH);
if (!baseline) {
  console.error(`coverage-ratchet: no baseline at ${BASELINE_PATH}`);
  process.exit(1);
}

let failed = 0;

function evaluate(label, floor, measured) {
  if (measured === null) {
    console.warn(
      `coverage-ratchet: ${label}: no coverage report found — skipping`,
    );
    return;
  }
  if (floor === null || floor === undefined) {
    console.log(
      `coverage-ratchet: ${label}: ${measured.toFixed(2)}% (floor not yet established)`,
    );
    return;
  }
  const ok = measured + 1e-9 >= floor;
  console.log(
    `${ok ? "OK" : "FAIL"}: ${label}: ${measured.toFixed(2)}% (floor ${floor.toFixed(2)}%)`,
  );
  if (!ok) failed += 1;
}

const py = baseline.python ?? {};
evaluate(
  "python",
  py.floor_pct ?? null,
  pythonLinePct(
    readJson(resolve(REPO_ROOT, py.coverage_json ?? "coverage.json")),
  ),
);

const fe = baseline.frontend ?? {};
evaluate(
  "frontend",
  fe.floor_pct ?? null,
  frontendLinePct(
    readJson(
      resolve(
        REPO_ROOT,
        fe.coverage_summary_json ??
          "cao_mcp_apps/coverage/coverage-summary.json",
      ),
    ),
  ),
);

process.exit(failed > 0 ? 1 : 0);
