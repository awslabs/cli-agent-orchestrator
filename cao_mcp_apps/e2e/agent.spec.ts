// E2E tier — agent-detail cells.
//
//   19.5  open agent detail -> the terminal-log view (ui://cao/agent) renders
//         its output region. In MCP Apps, opening an agent's detail is
//         host-mediated navigation to the ui://cao/agent resource; this spec
//         asserts that resource's terminal-output region renders.
//   19.12 oversized payload -> the View rejects the transaction with a warning
//         and never calls submit_command.
//
// NOTE (authored-but-not-executed locally): the sandbox network mode
// (COMMON_DEPENDENCIES) blocks the Playwright browser download. Authored +
// CI-wired; structured to pass in CI where chromium installs successfully.

import { expect, test } from "@playwright/test";

const frame = (page: import("@playwright/test").Page) =>
  page.frameLocator('iframe[data-view="agent"]');

test.describe("agent E2E", () => {
  test("19.5 — opening an agent detail renders the terminal-log region", async ({
    page,
  }) => {
    await page.goto("/host.html?view=agent");
    await page.evaluate(() => (window as any).__host.ready());

    const f = frame(page);
    // The agent (terminal-log) resource hydrated from the opening tool result.
    await expect(f.getByTestId("agent-detail")).toBeVisible();
    await expect(f.getByTestId("agent-output")).toContainText("terminal t1");
    await expect(f.getByTestId("agent-status")).toBeVisible();
  });

  test("19.12 — an oversized task payload is rejected before submit", async ({
    page,
  }) => {
    await page.goto("/host.html?view=agent");
    await page.evaluate(() => (window as any).__host.ready());

    const f = frame(page);
    await expect(f.getByTestId("task-input")).toBeVisible();

    // 5000 chars exceeds the View's 4000-char cap.
    const oversized = "x".repeat(5000);
    await f.getByTestId("task-input").fill(oversized);
    await f.getByTestId("btn-send_message").click();

    // The View surfaces a size warning …
    await expect(f.getByTestId("task-error")).toBeVisible();
    await expect(f.getByTestId("task-error")).toContainText(
      /characters or fewer/,
    );

    // … and the choke point was never reached.
    const submits = await page.evaluate(() =>
      (window as any).__host.submitCount(),
    );
    expect(submits).toBe(0);
  });
});
