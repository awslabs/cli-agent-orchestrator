#!/usr/bin/env node
// JIT-free deny-list scan for the MCP App single-file bundles.
//
// The MCP App iframes run under `allowUnsafeEval: false`, so strict-CSP hosts
// reject any bundle that uses eval, new Function, the Function(string)
// constructor, or a WASM JIT entry point. This scan catches those tokens in
// the built artifacts before they ship and exits non-zero on any hit
// (enforces the JIT-free bundle guarantee).
//
// Usage:  node scripts/scan-jit.mjs [artifactsDir]
// Default artifactsDir: ../src/cli_agent_orchestrator/ext_apps/apps_static
// (the package-shipped location built by `npm run build:all`).

import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, resolve } from "node:path";

const ROOT = resolve(
  process.argv[2] ?? "../src/cli_agent_orchestrator/ext_apps/apps_static",
);

// Conservative deny-list. False positives are tolerable; false negatives are
// not — a missed token means a bundle that a strict-CSP host silently refuses.
const PATTERNS = [
  { name: "direct-eval", re: /\beval\s*\(/g },
  { name: "new-Function", re: /\bnew\s+Function\s*\(/g },
  { name: "Function-constructor", re: /\bFunction\s*\(\s*["'`]/g },
  { name: "indirect-eval", re: /\b\(\s*0\s*,\s*eval\s*\)\s*\(/g },
  {
    name: "wasm-jit",
    re: /WebAssembly\.(compileStreaming|compile|instantiateStreaming)\b/g,
  },
];

function* walk(dir) {
  let entries;
  try {
    entries = readdirSync(dir);
  } catch (err) {
    if (err.code === "ENOENT") return;
    throw err;
  }
  for (const name of entries) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) yield* walk(path);
    else if (/\.(html|js|mjs|cjs)$/.test(name)) yield path;
  }
}

let failed = 0;
let files = 0;
for (const file of walk(ROOT)) {
  files += 1;
  const src = readFileSync(file, "utf8");
  for (const { name, re } of PATTERNS) {
    const matches = src.match(re);
    if (matches && matches.length > 0) {
      console.error(
        `FAIL: ${file}: pattern '${name}' matched ${matches.length} time(s)`,
      );
      failed += matches.length;
    }
  }
}

if (files === 0) {
  // No artifacts yet (frontend not built). The gate is a no-op until Phase II
  // produces bundles; CI builds first, so a real run always has files.
  console.warn(
    `scan-jit: no files scanned under ${ROOT} (build the frontend first?)`,
  );
  process.exit(0);
}

if (failed > 0) {
  console.error(
    `scan-jit: ${failed} disallowed JIT token(s) across ${files} file(s)`,
  );
  process.exit(1);
}

console.log(`scan-jit: clean — ${files} file(s) scanned, no JIT tokens.`);
