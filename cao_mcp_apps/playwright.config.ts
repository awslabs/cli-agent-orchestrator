import { defineConfig, devices } from "@playwright/test";

// Playwright config for the CAO MCP App E2E tier. The harness server
// (e2e/server.mjs) serves the built single-file
// bundles, the host harness page, and an SSE feed on 127.0.0.1:9889 — the same
// origin the event-stream bundle defaults to — so the Views run exactly as a
// host would load them.
//
// Per-tier budget: E2E < 60s per test.

export default defineConfig({
  testDir: "./e2e",
  testMatch: /.*\.spec\.ts/,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["github"], ["list"]] : "list",
  use: {
    baseURL: `http://127.0.0.1:${process.env.E2E_PORT ?? 9889}`,
    trace: "on-first-retry",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    // Build the bundles first, then serve them + the harness. `reuseExistingServer`
    // keeps local re-runs fast.
    command: "npm run build:all && node e2e/server.mjs",
    url: `http://127.0.0.1:${process.env.E2E_PORT ?? 9889}/host.html?view=dashboard`,
    timeout: 120_000,
    reuseExistingServer: !process.env.CI,
  },
});
