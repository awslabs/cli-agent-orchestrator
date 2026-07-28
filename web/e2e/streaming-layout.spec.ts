import { test, expect } from "@playwright/test";
import { openNativeTerminal, stubBackend, wsSentFrames } from "./stub";

// Sol P1 regression + mobile terminal-overlay layout acceptance at the exact
// QA viewports (390×844 and 360×800). The P1: the streaming engine stored
// bare `setTimeout`/`clearTimeout` as its defaults and invoked them as
// methods of its config object, which browsers reject with
// `TypeError: Illegal invocation` — the first printable key threw from
// `armQuietTimer` and no batch ever formed. Node/vitest timers never check
// the receiver, so this spec drives a real browser: the quiet timer must
// flush a batch with zero page errors. The layout cases pin the fullscreen
// overlay (no 24px dashboard strip), an in-viewport touch/keyboard Close,
// and the armed terminal's ≥50% viewport-height floor.

const MOBILE_SIZES = [
  { width: 390, height: 844, floor: 422 },
  { width: 360, height: 800, floor: 400 },
] as const;

test.describe("streaming quiet-timer flush (Sol P1 regression)", () => {
  test("first printable capture throws nothing and forms a traced batch on the quiet timer alone", async ({
    page,
  }) => {
    const pageErrors: string[] = [];
    page.on("pageerror", (error) => pageErrors.push(String(error)));
    const harness = await stubBackend(page);
    await openNativeTerminal(page);

    await page.getByRole("button", { name: "Streaming" }).click();
    const capture = page.getByRole("textbox", { name: /Streaming keystroke capture/ });
    await capture.click();
    // One printable key and NO Enter: only the quiet timer can flush this.
    await page.keyboard.type("a");

    await expect.poll(() => harness.controlInputPosts.length).toBe(1);
    expect(harness.controlInputPosts[0].events).toEqual([{ type: "text", text: "a" }]);
    // Identity-bound control route only.
    expect(harness.controlInputPosts[0].expected_identity).toMatchObject({
      terminal_id: "t-native",
      terminal_generation: "generation-1",
    });
    // The P1 exception must not occur.
    expect(pageErrors).toEqual([]);
    // The batch is trace-visible.
    await expect(page.getByLabel("Streaming trace").getByText("accepted")).toBeVisible();
    // §6.6 retained: zero websocket input frames while armed.
    const frames = await wsSentFrames(page);
    expect(frames.filter((frame) => frame.includes('"input"'))).toEqual([]);

    await page.getByRole("button", { name: "Stop streaming" }).click();
    await expect(page.getByText(/Streaming disarmed: operator stopped streaming/)).toBeVisible();
  });
});

test.describe("terminal overlay layout at the exact QA sizes", () => {
  for (const { width, height, floor } of MOBILE_SIZES) {
    test(`fullscreen overlay with in-viewport touch/keyboard Close at ${width}×${height}`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height });
      await stubBackend(page);
      await openNativeTerminal(page);

      const close = page.locator('[title="Close terminal"]');
      const overlay = close.locator('xpath=ancestor::div[contains(@class,"fixed")][1]');
      const geometry = await overlay.evaluate((el) => {
        const rect = el.getBoundingClientRect();
        return {
          top: rect.top,
          bottom: rect.bottom,
          width: rect.width,
          scrollWidth: (el as HTMLElement).scrollWidth,
        };
      });
      // Truly fullscreen: no 24px dashboard strip above, no horizontal bleed.
      expect(geometry.top).toBe(0);
      expect(geometry.bottom).toBe(height);
      expect(geometry.width).toBe(width);
      expect(geometry.scrollWidth).toBeLessThanOrEqual(width);

      // Close is fully inside the viewport and a real touch target.
      const closeBox = await close.boundingBox();
      if (!closeBox) throw new Error("Close control not laid out");
      expect(closeBox.x).toBeGreaterThanOrEqual(0);
      expect(closeBox.y).toBeGreaterThanOrEqual(0);
      expect(closeBox.x + closeBox.width).toBeLessThanOrEqual(width);
      expect(closeBox.y + closeBox.height).toBeLessThanOrEqual(height);
      expect(closeBox.width).toBeGreaterThanOrEqual(44);
      expect(closeBox.height).toBeGreaterThanOrEqual(44);

      // Keyboard operable: focus + Enter closes the overlay.
      await close.focus();
      await page.keyboard.press("Enter");
      await expect(close).toHaveCount(0);
    });

    test(`streaming-armed terminal keeps ≥50% viewport height at ${width}×${height}`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height });
      await stubBackend(page);
      await openNativeTerminal(page);

      const terminalWrapper = page
        .locator('[title="Close terminal"]')
        .locator('xpath=ancestor::div[contains(@class,"fixed")][1]')
        .locator(":scope > div:last-child");

      // Baseline (streaming off): the terminal already meets the floor.
      const offBox = await terminalWrapper.boundingBox();
      if (!offBox) throw new Error("terminal not laid out");
      expect(offBox.height).toBeGreaterThanOrEqual(floor);

      await page.getByRole("button", { name: "Streaming" }).click();
      await page.getByRole("textbox", { name: /Streaming keystroke capture/ }).waitFor();

      const armedBox = await terminalWrapper.boundingBox();
      if (!armedBox) throw new Error("terminal not laid out while armed");
      expect(armedBox.height).toBeGreaterThanOrEqual(floor);
      // The terminal still ends at the viewport bottom — nothing clipped.
      expect(armedBox.y + armedBox.height).toBeLessThanOrEqual(height);

      // Stop stays reachable (it scrolls into view if the control area is tall).
      const stop = page.getByRole("button", { name: "Stop streaming" });
      await stop.scrollIntoViewIfNeeded();
      await expect(stop).toBeInViewport();
      await page.getByRole("button", { name: "Stop streaming" }).click();
    });
  }
});
