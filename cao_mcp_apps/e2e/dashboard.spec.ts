// E2E tier — dashboard cells.
//
// Drives the built ui://cao/dashboard bundle inside the MCP host harness and
// asserts on the live iframe content.
//
//   19.3 launch agent       -> a new card appears in the grid,
//   19.6 stop agent         -> the agent status badge transitions to "stopped",
//   19.7 30s auto-refresh   -> the dashboard re-polls its refresh tool,
//   19.8 iframe teardown    -> after teardown the View ignores host updates.
//
// NOTE (authored-but-not-executed locally): the sandbox network mode
// (COMMON_DEPENDENCIES) blocks the Playwright browser download, so these specs
// were authored and CI-wired but not run locally. They are structured to pass
// in CI, where `npx playwright install --with-deps chromium` succeeds.

import { expect, test } from "@playwright/test";

const frame = (page: import("@playwright/test").Page) =>
  page.frameLocator('iframe[data-view="dashboard"]');

test.describe("dashboard E2E", () => {
  test("19.3 — launching an agent adds a new card", async ({ page }) => {
    await page.goto("/host.html?view=dashboard");
    await page.evaluate(() => (window as any).__host.ready());

    const f = frame(page);
    await expect(f.getByTestId("agent-card")).toHaveCount(1);

    await page.evaluate(() => (window as any).__host.launch("t2", "reviewer"));

    await expect(f.getByTestId("agent-card")).toHaveCount(2);
    await expect(f.locator('[data-terminal-id="t2"]')).toBeVisible();
  });

  test("19.6 — stopping an agent transitions its status to stopped", async ({
    page,
  }) => {
    await page.goto("/host.html?view=dashboard");
    await page.evaluate(() => (window as any).__host.ready());

    const f = frame(page);
    const badge = f.getByTestId("status-badge").first();
    await expect(badge).toHaveText("processing");

    await page.evaluate(() => (window as any).__host.stop("t1"));

    await expect(badge).toHaveText("stopped");
  });

  test("19.7 — the dashboard auto-refreshes on the 30s cycle", async ({
    page,
  }) => {
    // Install a controllable clock before navigation so the 30s polling
    // interval is deterministic.
    await page.clock.install();
    await page.goto("/host.html?view=dashboard");
    await page.evaluate(() => (window as any).__host.ready());

    // The initial poll has fired at least once after connect.
    await expect
      .poll(() =>
        page.evaluate(() =>
          (window as any).__host.toolCallCount("render_dashboard"),
        ),
      )
      .toBeGreaterThanOrEqual(1);
    const before = await page.evaluate(() =>
      (window as any).__host.toolCallCount("render_dashboard"),
    );

    // Advance one full 30s refresh cycle.
    await page.clock.runFor(30_000);

    await expect
      .poll(() =>
        page.evaluate(() =>
          (window as any).__host.toolCallCount("render_dashboard"),
        ),
      )
      .toBeGreaterThan(before);
  });

  test("19.8 — host teardown releases the View's listeners", async ({
    page,
  }) => {
    await page.goto("/host.html?view=dashboard");
    await page.evaluate(() => (window as any).__host.ready());
    const f = frame(page);
    await expect(f.getByTestId("agent-card")).toHaveCount(1);

    // Tear the iframe down, then attempt a host-pushed update.
    await page.evaluate(() => (window as any).__host.teardown());
    await page.evaluate(() => (window as any).__host.launch("t9", "ghost"));

    // The View released its message listener on teardown, so the post-teardown
    // launch must NOT add a card.
    await page.waitForTimeout(250);
    await expect(f.getByTestId("agent-card")).toHaveCount(1);
  });
});
