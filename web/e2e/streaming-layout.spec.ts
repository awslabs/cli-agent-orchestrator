import { test, expect } from "@playwright/test";
import { openNativeTerminal, stubBackend, wsSentFrames } from "./stub";

// Sol P1 regression + mobile terminal-overlay layout acceptance at the exact
// QA viewports (390×844 and 360×800), plus a desktop sanity size. The P1: the
// streaming engine stored bare `setTimeout`/`clearTimeout` as its defaults
// and invoked them as methods of its config object, which browsers reject
// with `TypeError: Illegal invocation` — the first printable key threw from
// `armQuietTimer` and no batch ever formed. Node/vitest timers never check
// the receiver, so this spec drives a real browser: the quiet timer must
// flush a batch with zero page errors. The layout cases pin the fullscreen
// overlay (no 24px dashboard strip), an in-viewport touch/keyboard Close,
// and the armed terminal's ≥50% viewport-height floor — measured on the
// fitted `.xterm` element itself, not its wrapper: FitAddon floors the fit
// to whole rows, so the wrapper can meet the floor while the visible
// terminal is one row short (the 390×844 P2: wrapper 422px, .xterm 416px).

const GEOMETRY_SIZES = [
  { width: 390, height: 844, floor: 422 },
  { width: 360, height: 800, floor: 400 },
  // Desktop sanity: the floor padding must not disturb the desktop layout.
  { width: 1280, height: 800, floor: 400 },
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
  for (const { width, height, floor } of GEOMETRY_SIZES) {
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

      // Measure the fitted .xterm — the actual visible terminal — never its
      // wrapper: the wrapper meets the floor by CSS while the row-quantized
      // fit can leave the .xterm one row short.
      const xterm = page.locator(".xterm");
      const wrapper = page
        .locator('[title="Close terminal"]')
        .locator('xpath=ancestor::div[contains(@class,"fixed")][1]')
        .locator(":scope > div:last-child");
      // After arming, the control area grows synchronously but the refit is
      // debounced (~50ms), so a fresh measurement can catch the stale
      // pre-arm height. Post-fit the .xterm is never taller than its wrapper;
      // pre-refit it still is. Gate on that to wait for the armed fit.
      const fittedXtermHeight = async () => {
        const x = await xterm.boundingBox();
        const w = await wrapper.boundingBox();
        if (!x || !w) return 0;
        return x.height <= w.height ? x.height : 0;
      };

      // Baseline (streaming off): the terminal already meets the floor.
      await expect.poll(fittedXtermHeight).toBeGreaterThanOrEqual(floor);

      await page.getByRole("button", { name: "Streaming" }).click();
      await page.getByRole("textbox", { name: /Streaming keystroke capture/ }).waitFor();

      // Armed: the visible terminal still meets the floor and nothing is
      // clipped past the viewport bottom.
      await expect.poll(fittedXtermHeight).toBeGreaterThanOrEqual(floor);
      const armedBox = await xterm.boundingBox();
      if (!armedBox) throw new Error("terminal not laid out while armed");
      expect(armedBox.y + armedBox.height).toBeLessThanOrEqual(height);

      // Stop stays reachable (it scrolls into view if the control area is tall).
      const stop = page.getByRole("button", { name: "Stop streaming" });
      await stop.scrollIntoViewIfNeeded();
      await expect(stop).toBeInViewport();
      await page.getByRole("button", { name: "Stop streaming" }).click();
    });
  }
});

// Installed-QA P2 follow-up (post-PR-#51): while Streaming is armed at the
// two mobile QA widths, the fitted .xterm must not cover the Compact/Stop
// favorite controls — the strip rides a reserved row directly above the
// terminal, so its buttons stay fully visible and operable no matter how
// tall the (scrolling) control area gets. The ordinary composer textarea
// and its Send / Streaming / Macros primary controls must be real 44×44px
// touch targets with no horizontal overflow. The prior fitted-.xterm floors
// (422px @390×844, 400px @360×800) are retained.
const MOBILE_SIZES = [
  { width: 390, height: 844, floor: 422 },
  { width: 360, height: 800, floor: 400 },
] as const;

test.describe("mobile control geometry (installed-QA P2 follow-up)", () => {
  for (const { width, height, floor } of MOBILE_SIZES) {
    test(`armed: Compact/Stop favorites stay fully visible above the fitted xterm at ${width}×${height}`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height });
      const harness = await stubBackend(page);
      await openNativeTerminal(page);

      const strip = page.getByTestId("favorite-strip");
      await expect(strip).toBeVisible();

      await page.getByRole("button", { name: "Streaming" }).click();
      await page.getByRole("textbox", { name: /Streaming keystroke capture/ }).waitFor();

      // Wait for the armed refit before measuring (same gate as above).
      const xterm = page.locator(".xterm");
      const wrapper = page
        .locator('[title="Close terminal"]')
        .locator('xpath=ancestor::div[contains(@class,"fixed")][1]')
        .locator(":scope > div:last-child");
      const fittedXtermHeight = async () => {
        const x = await xterm.boundingBox();
        const w = await wrapper.boundingBox();
        if (!x || !w) return 0;
        return x.height <= w.height ? x.height : 0;
      };
      await expect.poll(fittedXtermHeight).toBeGreaterThanOrEqual(floor);

      // The strip is fully inside the viewport and entirely above the fitted
      // terminal — no cover, no overlap, no horizontal bleed.
      const stripBox = await strip.boundingBox();
      const xtermBox = await xterm.boundingBox();
      if (!stripBox || !xtermBox) throw new Error("strip or terminal not laid out while armed");
      expect(stripBox.x).toBeGreaterThanOrEqual(0);
      expect(stripBox.y).toBeGreaterThanOrEqual(0);
      expect(stripBox.x + stripBox.width).toBeLessThanOrEqual(width);
      expect(stripBox.y + stripBox.height).toBeLessThanOrEqual(height);
      expect(stripBox.y + stripBox.height).toBeLessThanOrEqual(xtermBox.y);

      // The Compact and Stop favorites themselves: fully visible, fully above
      // the terminal, real touch targets, and operable (one tap = one v3
      // request, even while armed).
      for (const name of ["Compact", "Stop"] as const) {
        const favorite = strip.getByRole("button", { name, exact: true });
        await expect(favorite).toBeEnabled();
        const box = await favorite.boundingBox();
        if (!box) throw new Error(`${name} favorite not laid out while armed`);
        expect(box.width).toBeGreaterThanOrEqual(44);
        expect(box.height).toBeGreaterThanOrEqual(44);
        expect(box.x).toBeGreaterThanOrEqual(0);
        expect(box.y).toBeGreaterThanOrEqual(0);
        expect(box.x + box.width).toBeLessThanOrEqual(width);
        expect(box.y + box.height).toBeLessThanOrEqual(xtermBox.y);
      }
      await strip.getByRole("button", { name: "Compact", exact: true }).click();
      await expect.poll(() => harness.controlInputPosts.length).toBe(1);

      // Stop streaming and Close stay reachable.
      const stop = page.getByRole("button", { name: "Stop streaming" });
      await stop.scrollIntoViewIfNeeded();
      await expect(stop).toBeInViewport();
      await expect(page.locator('[title="Close terminal"]')).toBeInViewport();
      await stop.click();
      await expect(page.getByText(/Streaming disarmed: operator stopped streaming/)).toBeVisible();
    });

    test(`composer controls meet 44×44 touch targets without horizontal overflow at ${width}×${height}`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height });
      await stubBackend(page);
      await openNativeTerminal(page);

      const controls = [
        page.getByRole("textbox", { name: "Message to the native composer" }),
        page.getByTestId("composer-row").getByRole("button", { name: "Send", exact: true }),
        page.getByTestId("composer-row").getByRole("button", { name: "Streaming", exact: true }),
        page.getByTestId("composer-row").getByRole("button", { name: /^Macros/ }),
      ];
      for (const control of controls) {
        const box = await control.boundingBox();
        if (!box) throw new Error("composer control not laid out");
        expect(box.width).toBeGreaterThanOrEqual(44);
        expect(box.height).toBeGreaterThanOrEqual(44);
        expect(box.x).toBeGreaterThanOrEqual(0);
        expect(box.x + box.width).toBeLessThanOrEqual(width);
      }

      // No horizontal overflow on the page or the composer row.
      const docScrollWidth = await page.evaluate(() => document.documentElement.scrollWidth);
      expect(docScrollWidth).toBeLessThanOrEqual(width);
      const rowBox = await page.getByTestId("composer-row").boundingBox();
      if (!rowBox) throw new Error("composer row not laid out");
      expect(rowBox.x).toBeGreaterThanOrEqual(0);
      expect(rowBox.x + rowBox.width).toBeLessThanOrEqual(width);
    });
  }
});
