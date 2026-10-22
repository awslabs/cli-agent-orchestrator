#!/usr/bin/env node
// Coverage ratchet: fail CI if measured coverage regresses below the floor
// recorded in .coverage-baseline.json, for each metric that has one.
//
// Metrics:
//   - python:        line coverage, computed as covered_lines / num_statements
//                     from `coverage.json` (pytest --cov --cov-report=json).
//   - python-branch: branch coverage from the same report (#1005). Collected
//                     and reported whenever `--cov-branch` ran, but seeded
//                     with a null floor -- record-only until a full-suite run
//                     establishes a real baseline.
//   - frontend:       line coverage from `coverage/coverage-summary.json`
//                     (vitest --coverage).
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
  // coverage.py json schema: { "totals": { "covered_lines", "num_statements" } }.
  //
  // Computed, NOT read from `totals.percent_covered` (#1005): with `--cov-branch`
  // on, coverage.py redefines that field as the blended
  // (covered_lines + covered_branches) / (num_statements + num_branches). On a
  // measured subset it moved 20.19% -> 16.52% purely from the flag. Reading it
  // would silently re-point the 87.0 line floor at a different metric.
  const totals = report?.totals;
  const covered = totals?.covered_lines;
  const total = totals?.num_statements;
  if (typeof covered !== "number" || typeof total !== "number") return null;
  return total === 0 ? 100 : (covered / total) * 100;
}

function pythonBranchPct(report) {
  // Present only when pytest ran with `--cov-branch`; null means "not measured",
  // which `evaluate` reports and skips rather than failing on.
  const totals = report?.totals;
  const covered = totals?.covered_branches;
  const total = totals?.num_branches;
  if (typeof covered !== "number" || typeof total !== "number") return null;
  return total === 0 ? 100 : (covered / total) * 100;
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
const pyReport = readJson(resolve(REPO_ROOT, py.coverage_json ?? "coverage.json"));
evaluate("python", py.floor_pct ?? null, pythonLinePct(pyReport));

// Branch coverage is a SECOND, separately-floored metric (#1005): a line floor
// cannot see a condition only ever taken one way, which is how a declared
// capability's other branch stays unreachable. Seeded with a null floor
// (record-only) because the full-suite figure is unknown until CI measures it.
const pyBranch = baseline.python_branch ?? {};
evaluate(
  "python-branch",
  pyBranch.floor_pct ?? null,
  pythonBranchPct(
    pyBranch.coverage_json
      ? readJson(resolve(REPO_ROOT, pyBranch.coverage_json))
      : pyReport,
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
