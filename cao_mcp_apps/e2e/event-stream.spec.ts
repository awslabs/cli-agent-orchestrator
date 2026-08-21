// E2E tier — event-stream cell.
//
//   19.4 send a task to an agent -> the event stream updates immediately.
//
// Uses the "combo" harness page (agent + event-stream iframes on one origin).
// The send gesture in the agent view flows through submit_command -> the host
// peer -> POST /emit, which pushes a governance event onto the same-origin SSE
// feed (/events). The event-stream bundle is subscribed there and renders a new
// row live — exactly the real path: choke point -> Backplane -> EventLog -> SSE.
//
// NOTE (authored-but-not-executed locally): the sandbox network mode
// (COMMON_DEPENDENCIES) blocks the Playwright browser download. Authored +
// CI-wired; structured to pass in CI where chromium installs successfully.

import { expect, test } from "@playwright/test";

test.describe("event-stream E2E", () => {
  test("19.4 — sending a task updates the event stream live", async ({
    page,
  }) => {
    await page.goto("/host.html?view=combo");
    await page.evaluate(() => (window as any).__host.ready());

    const agent = page.frameLocator('iframe[data-view="agent"]');
    const stream = page.frameLocator('iframe[data-view="event-stream"]');

    // The ticker hydrated with the single seed event.
    await expect(stream.getByTestId("event-row")).toHaveCount(1);

    // Send a task to the agent through the choke point.
    await agent.getByTestId("task-input").fill("run the build");
    await agent.getByTestId("btn-send_message").click();

    // The new governance event is fanned out over SSE and rendered live.
    await expect(stream.getByTestId("event-row")).toHaveCount(2);
  });
});
