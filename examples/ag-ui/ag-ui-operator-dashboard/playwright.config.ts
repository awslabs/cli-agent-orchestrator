import { defineConfig, devices } from "@playwright/test";

// Browser evidence for the operator dashboard's e2e (e2e/timeline.spec.ts). It runs
// after e2e/e2e.ts, against the private cao-server that e2e/run.sh started, and never
// runs on its own. The dashboard view is served by vite on 5173, one of the origins
// cao-server allows by default, so the page can read the server it is pointed at.
//
// Settings mirror what the operator dashboard keeps for its evidence: a video and a
// screenshot only when a test fails. The timeline screenshot is taken on every run and
// saved to e2e/artifacts/ by the spec itself.
//
// The browser is the one `npm run playwright:install` put in place (chromium). Set
// CAO_E2E_CHROME to a local Chromium to reuse a browser that is already installed.
export default defineConfig({
  testDir: "./e2e",
  testMatch: /.*\.spec\.ts/,
  outputDir: "./e2e/test-results",
  timeout: 60_000,
  expect: { timeout: 20_000 },
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: 0,
  reporter: process.env.CI ? [["github"], ["list"]] : "list",
  use: {
    baseURL: "http://127.0.0.1:5173",
    video: "retain-on-failure",
    screenshot: "only-on-failure",
    trace: "off",
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        launchOptions: process.env.CAO_E2E_CHROME ? { executablePath: process.env.CAO_E2E_CHROME } : {},
      },
    },
  ],
  webServer: {
    command: "npx vite --port 5173 --strictPort",
    url: "http://127.0.0.1:5173/",
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
  },
});
