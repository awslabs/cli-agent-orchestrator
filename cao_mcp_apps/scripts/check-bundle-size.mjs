#!/usr/bin/env node
// Bundle-size budget enforcement for the MCP App single-file artifacts.
//
// Budgets (gzipped wire cost the host pays on first resource read):
//   dashboard.html     <= 250 KB gz
//   agent.html         <= 250 KB gz
//   event-stream.html  <= 150 KB gz
//   graph.html         <= 120 KB gz (measured ~86.3 KB gz with sigma+graphology+graphology-layout; ~28% headroom)
//
// Each artifact is a single inlined HTML file (vite-plugin-singlefile). Exits
// non-zero if any built bundle exceeds its budget.
//
// Usage:  node scripts/check-bundle-size.mjs [artifactsDir]

import { gzipSync } from "node:zlib";
import { readFileSync, statSync } from "node:fs";
import { resolve } from "node:path";

const ROOT = resolve(
  process.argv[2] ?? "../src/cli_agent_orchestrator/ext_apps/apps_static",
);

const BUDGETS = [
  { file: "dashboard.html", limitKb: 250 },
  { file: "agent.html", limitKb: 250 },
  { file: "event-stream.html", limitKb: 150 },
  { file: "graph.html", limitKb: 120 },
];

let failed = 0;
let checked = 0;
for (const { file, limitKb } of BUDGETS) {
  const path = `${ROOT}/${file}`;
  try {
    statSync(path);
  } catch {
    // Not built yet — skip (the gate becomes active once Phase II emits bundles).
    console.warn(`check-bundle-size: ${file} not built yet — skipping`);
    continue;
  }
  checked += 1;
  const gz = gzipSync(readFileSync(path));
  const kb = gz.length / 1024;
  const ok = kb <= limitKb;
  console.log(
    `${ok ? "OK" : "FAIL"}: ${file} -> ${kb.toFixed(1)} KB gz (limit ${limitKb} KB)`,
  );
  if (!ok) failed += 1;
}

if (checked === 0) {
  console.warn(
    "check-bundle-size: no artifacts found (build the frontend first?)",
  );
}

process.exit(failed > 0 ? 1 : 0);
