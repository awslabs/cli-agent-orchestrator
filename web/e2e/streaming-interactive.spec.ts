import { test, expect } from "@playwright/test";
import { stubBackend, openNativeTerminal } from "./stub";

/**
 * §6.7 (r15) interactive-streaming status evidence, desktop 1280×800 and
 * mobile 390×844 against the stubbed backend: the armed surface declares
 * `payload_class: "interactive"` only when the per-terminal block
 * advertises it; an accepted batch traces normally (queued, never a blind
 * disarm); a turn-gate pane-busy pauses with its notice; genuine lease
 * contention disarms with the truthful reason; an old server arms without
 * any declaration and says why.
 */

const CAPTURE_NAME = /Streaming keystroke capture/;

test.describe("r15 interactive streaming status (§6.7)", () => {
  test("armed with the block: batches declare interactive and an accepted batch traces", async ({ page }, testInfo) => {
    const harness = await stubBackend(page, { interactiveStreaming: true });
    await openNativeTerminal(page);

    await page.getByRole("button", { name: "Streaming" }).click();
    const capture = page.getByRole("textbox", { name: CAPTURE_NAME });
    await expect(capture).toBeVisible();
    await expect(page.getByText(/STREAMING TO kimi_cli/)).toBeVisible();

    await capture.pressSequentially("hi");
    await capture.press("Enter");
    await expect.poll(() => harness.controlInputPosts.length).toBe(1);
    expect(harness.controlInputPosts[0].payload_class).toBe("interactive");
    expect(harness.controlInputPosts[0].events).toEqual([
      { type: "text", text: "hi" },
      { type: "key", key: "Enter" },
    ]);
    // An explainable accepted batch is a trace entry, never a disarm.
    await expect(page.getByText("accepted").first()).toBeVisible();
    await expect(page.getByText(/Streaming disarmed/)).toHaveCount(0);
    await page.screenshot({
      path: `e2e/screenshots/${testInfo.project.name}-interactive-armed-accepted.png`,
    });
  });

  test("a turn-gate pane-busy pauses with the notice and recovers (detail normalization)", async ({ page }, testInfo) => {
    const harness = await stubBackend(page, {
      interactiveStreaming: true,
      controlInputResponses: [
        {
          outcome: "refused",
          reason_code: "pane-busy",
          detail:
            "the receiver is processing, not idle; a composer-class sequence is " +
            "readiness-gated and nothing was written",
        },
        { outcome: "accepted" },
      ],
    });
    await openNativeTerminal(page);

    await page.getByRole("button", { name: "Streaming" }).click();
    const capture = page.getByRole("textbox", { name: CAPTURE_NAME });
    await capture.pressSequentially("hi");
    await capture.press("Enter");

    // The wire `detail` field (never `reason_detail`) drives the pause
    // classification: the trace names the pause, no disarm fires, and the
    // single retry is accepted.
    await expect(page.getByText(/provider busy/)).toBeVisible();
    await expect.poll(() => harness.controlInputPosts.length, { timeout: 5000 }).toBe(2);
    await expect(page.getByText("accepted").first()).toBeVisible();
    await expect(page.getByText(/Streaming disarmed/)).toHaveCount(0);
    await page.screenshot({
      path: `e2e/screenshots/${testInfo.project.name}-interactive-pause-recovered.png`,
    });
  });

  test("genuine lease contention disarms with the truthful reason", async ({ page }, testInfo) => {
    await stubBackend(page, {
      interactiveStreaming: true,
      controlInputResponses: [
        {
          outcome: "refused",
          reason_code: "pane-busy",
          detail:
            "another writer holds pane %7: input lease is held by another " +
            "process/thread: operator-message:op-1",
        },
      ],
    });
    await openNativeTerminal(page);

    await page.getByRole("button", { name: "Streaming" }).click();
    const capture = page.getByRole("textbox", { name: CAPTURE_NAME });
    await capture.pressSequentially("hi");
    await capture.press("Enter");

    // Real lease contention keeps the §6.4 behavior: disarm, with the
    // reason named — never a pause, never a bypass.
    await expect(
      page.getByText(/Streaming disarmed: concurrent input: another writer holds the pane input lease/),
    ).toBeVisible();
    await page.screenshot({
      path: `e2e/screenshots/${testInfo.project.name}-interactive-lease-contention-disarm.png`,
    });
  });

  test("old server: arms with no declaration and the honest notice", async ({ page }, testInfo) => {
    const harness = await stubBackend(page); // interactiveStreaming defaults off
    await openNativeTerminal(page);

    await page.getByRole("button", { name: "Streaming" }).click();
    const capture = page.getByRole("textbox", { name: CAPTURE_NAME });
    await expect(
      page.getByText(/interactive declaration unavailable on this server/),
    ).toBeVisible();

    await capture.pressSequentially("hi");
    await capture.press("Enter");
    await expect.poll(() => harness.controlInputPosts.length).toBe(1);
    expect(harness.controlInputPosts[0].payload_class).toBeUndefined();
    await page.screenshot({
      path: `e2e/screenshots/${testInfo.project.name}-interactive-old-server.png`,
    });
  });
});
