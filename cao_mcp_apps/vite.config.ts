// Shared Vite config factory for the CAO MCP App single-file builds.
//
// Each view is built independently into one inlined HTML file (no external JS/CSS
// assets) via `vite-plugin-singlefile`, emitted to the package-shipped location
// `src/cli_agent_orchestrator/ext_apps/apps_static/` (the same dir the Phase 0
// `scan-jit` / `check-bundle-size` gates scan, and the dir `ext_apps.apps.py`
// serves from at runtime).
//
// JIT-free guarantee ("allowUnsafeEval: false" intent): there is no Vite option
// literally named `allowUnsafeEval` — that is an MCP-UI host-renderer prop, not a
// build setting. We instead target ES2021
// and ship no code that needs `eval`/`new Function`; the `npm run scan:jit` gate
// fails the build if any JIT token slips into the output, which is what lets the
// bundle run under the spec's no-`unsafe-eval` CSP.

import { resolve } from "node:path";
import react from "@vitejs/plugin-react";
import { defineConfig, type UserConfig } from "vite";
import { viteSingleFile } from "vite-plugin-singlefile";

// Absolute path to the package-shipped artifact directory.
export const APPS_STATIC_DIR = resolve(
  __dirname,
  "../src/cli_agent_orchestrator/ext_apps/apps_static",
);

/**
 * Build a single-file Vite config for one view.
 *
 * @param view     subdirectory under `src/` (e.g. "dashboard")
 * @param htmlName the entry HTML file in that dir (e.g. "dashboard.html")
 */
export function makeViewConfig(view: string, htmlName: string): UserConfig {
  return defineConfig({
    root: resolve(__dirname, "src", view),
    plugins: [react(), viteSingleFile()],
    build: {
      target: "es2021",
      outDir: APPS_STATIC_DIR,
      // Each view build appends to the shared apps_static dir.
      emptyOutDir: false,
      cssCodeSplit: false,
      assetsInlineLimit: 100_000_000,
      reportCompressedSize: false,
      rollupOptions: {
        input: resolve(__dirname, "src", view, htmlName),
        output: { inlineDynamicImports: true },
      },
    },
  }) as UserConfig;
}

// Default export builds the dashboard (so a bare `vite build` does something
// useful); `build:all` invokes each per-view config explicitly.
export default makeViewConfig("dashboard", "dashboard.html");
