import { test, expect } from "@playwright/test";
import { AxeBuilder } from "@axe-core/playwright";
import { openNativeTerminal, stubBackend } from "./stub";

// §10.5 dashboard evidence: session-card header (§7.6), header/favorite
// strip/macro modal, layout acceptance (§7.1), and axe-core — at desktop
// 1280×800 and mobile 390×844 (both configured projects).

test.describe("session-card header (§7.6)", () => {
  test("selection/copy intent never toggles; the chevron alone expands", async ({
    page,
  }, testInfo) => {
    await stubBackend(page);
    await page.goto("/");

    const chevron = page.getByRole("button", { name: /session cao-fleet/ });
    // The dashboard auto-expands newly seen sessions; collapse first so the
    // negative case starts from a known state.
    await expect(chevron).toHaveAttribute("aria-expanded", "true");
    await chevron.click();
    await expect(chevron).toHaveAttribute("aria-expanded", "false");
    await expect(page.locator("#session-cao-fleet-terminals")).toHaveCount(0);

    // Drag-select the session name: selection happens, the card does not move.
    const name = page.locator(".select-text").getByText("cao-fleet");
    const nameBox = await name.boundingBox();
    if (!nameBox) throw new Error("session name not laid out");
    await page.mouse.move(nameBox.x + 2, nameBox.y + nameBox.height / 2);
    await page.mouse.down();
    await page.mouse.move(nameBox.x + nameBox.width - 2, nameBox.y + nameBox.height / 2, {
      steps: 6,
    });
    await page.mouse.up();
    const selected = await page.evaluate(() => window.getSelection()?.toString() ?? "");
    expect(selected).toContain("cao-fleet");
    // Copy intent (Ctrl/Cmd+C) leaves the card untouched as well.
    await page.keyboard.press(process.platform === "darwin" ? "Meta+c" : "Control+c");
    await expect(chevron).toHaveAttribute("aria-expanded", "false");
    await expect(page.locator("#session-cao-fleet-terminals")).toHaveCount(0);

    // Click and double-click on metadata likewise never toggle. On touch
    // projects a double-click is a double-TAP (which the browser may turn
    // into a zoom gesture), so the intent is two spaced taps there.
    await name.click();
    if (testInfo.project.name === "mobile-chromium") {
      await name.tap();
      await page.waitForTimeout(350);
      await name.tap();
    } else {
      await name.dblclick();
    }
    await expect(chevron).toHaveAttribute("aria-expanded", "false");

    // Metadata shows no pointer/press affordance.
    const cursor = await name.evaluate((el) => getComputedStyle(el).cursor);
    expect(cursor).not.toBe("pointer");

    // The chevron: pointer toggle, aria flips, aria-controls resolves.
    await chevron.click();
    await expect(chevron).toHaveAttribute("aria-expanded", "true");
    await expect(chevron).toHaveAccessibleName("Collapse session cao-fleet");
    const controls = await chevron.getAttribute("aria-controls");
    expect(controls).toBe("session-cao-fleet-terminals");
    await expect(page.locator(`#${controls}`)).toBeVisible();

    // Keyboard: native Enter and Space each toggle exactly once.
    await chevron.focus();
    await page.keyboard.press("Enter");
    await expect(chevron).toHaveAttribute("aria-expanded", "false");
    await expect(chevron).toHaveAccessibleName("Expand session cao-fleet");
    await page.keyboard.press(" ");
    await expect(chevron).toHaveAttribute("aria-expanded", "true");

    // ≥44×44 touch target.
    const box = await chevron.boundingBox();
    if (!box) throw new Error("chevron not laid out");
    expect(box.width).toBeGreaterThanOrEqual(44);
    expect(box.height).toBeGreaterThanOrEqual(44);

    await page.screenshot({
      path: `e2e/screenshots/${testInfo.project.name}-session-card.png`,
    });
  });
});

test.describe("terminal header, favorite strip, macro modal (§7.1-7.4)", () => {
  test("strip renders server order; one tap sends one v3 request; modal opens and scans clean", async ({
    page,
  }, testInfo) => {
    const harness = await stubBackend(page);
    await openNativeTerminal(page);

    // The favorite strip: scope groups in server order (provider built-ins
    // and user favorite first), Compact/Stop distinguished as built-ins.
    const strip = page.getByTestId("favorite-strip");
    await expect(strip).toBeVisible();
    await expect(strip.getByRole("button", { name: "Compact" })).toBeVisible();
    await expect(strip.getByRole("button", { name: "Stop" })).toBeVisible();
    await expect(strip.getByRole("button", { name: "Model K2.7" })).toBeVisible();
    await expect(strip.getByRole("button", { name: "Max effort" })).toBeVisible();
    // The profile favorite is grouped under This agent.
    await expect(strip.getByText("This agent:")).toBeVisible();
    // No header Compact button anymore (§7.1).
    await expect(
      page.getByTestId("composer-row").getByRole("button", { name: "Compact" }),
    ).toHaveCount(0);

    // One tap = one v3 request, no payload_class on a user macro.
    await strip.getByRole("button", { name: "Model K2.7" }).click();
    await expect.poll(() => harness.controlInputPosts.length).toBe(1);
    expect(harness.controlInputPosts[0].events).toEqual([
      { type: "text", text: "/model" },
      { type: "key", key: "Enter" },
      { type: "key", key: "Up" },
      { type: "key", key: "Up" },
      { type: "key", key: "Up" },
      { type: "key", key: "Enter" },
    ]);
    expect(harness.controlInputPosts[0].payload_class).toBeUndefined();

    // The registry Compact built-in declares command-class (§4.1; the stub
    // advertises command_controls).
    await strip.getByRole("button", { name: "Compact" }).click();
    await expect.poll(() => harness.controlInputPosts.length).toBe(2);
    expect(harness.controlInputPosts[1].payload_class).toBe("command");

    await page.screenshot({
      path: `e2e/screenshots/${testInfo.project.name}-terminal-strip.png`,
    });

    // axe: header + strip surfaces, zero serious violations.
    const headerScan = await new AxeBuilder({ page })
      .include('[data-testid="native-control-area"]')
      .include('[data-testid="favorite-strip-row"]')
      .analyze();
    expect(
      headerScan.violations.filter((v) => v.impact === "serious" || v.impact === "critical"),
    ).toEqual([]);

    // Macro modal / sheet.
    await page.getByRole("button", { name: /Macros/ }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    // Built-in rows carry their badge and no delete affordance.
    await expect(dialog.getByText("built-in").first()).toBeVisible();

    // Mobile sheet: list → editor navigation with back (§7.4).
    if (testInfo.project.name === "mobile-chromium") {
      await dialog.getByText("Model K2.7").first().click();
      await expect(dialog.getByRole("button", { name: "Save" })).toBeVisible();
      await page.screenshot({
        path: `e2e/screenshots/${testInfo.project.name}-macro-sheet-editor.png`,
      });
      await dialog.getByRole("button", { name: /back/i }).click();
    }

    await page.screenshot({
      path: `e2e/screenshots/${testInfo.project.name}-macro-modal.png`,
    });

    // axe: the modal surface.
    const modalScan = await new AxeBuilder({ page }).include('[role="dialog"]').analyze();
    expect(
      modalScan.violations.filter((v) => v.impact === "serious" || v.impact === "critical"),
    ).toEqual([]);

    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
  });

  test("layout acceptance: row heights and the terminal's height share (§7.1)", async ({
    page,
  }, testInfo) => {
    await stubBackend(page);
    await openNativeTerminal(page);
    await expect(page.getByTestId("favorite-strip")).toBeVisible();

    const row = await page.getByTestId("composer-row").boundingBox();
    if (!row) throw new Error("composer row not laid out");
    if (testInfo.project.name === "desktop-chromium") {
      expect(row.height).toBeLessThanOrEqual(48);
    } else {
      expect(row.height).toBeLessThanOrEqual(96);
      // The terminal viewport keeps ≥ 50% of the 844px viewport height with
      // header + favorite strip visible.
      const xterm = await page.locator(".xterm").boundingBox();
      if (!xterm) throw new Error("terminal not laid out");
      expect(xterm.height).toBeGreaterThanOrEqual(844 / 2);
    }
  });
});
