import { defineConfig, devices } from "@playwright/test";

// Lane B browser + mobile evidence suite (§10.5): desktop Chromium
// 1280×800 and mobile Chromium 390×844 (device-scale, touch) against a
// stubbed server — Playwright route mocks for HTTP, an injected
// FakeWebSocket for the terminal channel. The built app is served by
// `vite preview`; no real CAO server ever runs here.

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
    baseURL: `http://127.0.0.1:${process.env.E2E_PORT ?? 4173}`,
    trace: "on-first-retry",
  },
  projects: [
    {
      name: "desktop-chromium",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1280, height: 800 },
      },
    },
    {
      name: "mobile-chromium",
      use: {
        ...devices["Pixel 7"],
        // §10.5 pins exactly 390×844 with device scale + touch.
        viewport: { width: 390, height: 844 },
        hasTouch: true,
        isMobile: true,
      },
    },
  ],
  webServer: {
    command:
      "npm run build && npm run preview -- --host 127.0.0.1 --port " +
      (process.env.E2E_PORT ?? 4173) +
      " --strictPort",
    url: `http://127.0.0.1:${process.env.E2E_PORT ?? 4173}/`,
    timeout: 180_000,
    reuseExistingServer: !process.env.CI,
  },
});
