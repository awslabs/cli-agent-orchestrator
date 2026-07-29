import { test, expect } from "@playwright/test";
import { AxeBuilder } from "@axe-core/playwright";
import { stubBackend, openNativeTerminal, wsSentFrames } from "./stub";

/**
 * Lane C §10.5: the image-capable operator-message composer at desktop
 * 1280×800 and mobile 390×844 (plus a 360×800 composition check), against
 * the stubbed new-server — picker and paste staging, the chip strip, the
 * explicit routing status line, text+image send, wrap/height composition,
 * accessibility, and old-server degradation.
 */

// A tiny valid PNG (signature + IHDR for a 120×80 image).
const PNG_BYTES = Buffer.concat([
  Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
  Buffer.from([0x00, 0x00, 0x00, 0x0d]),
  Buffer.from("IHDR"),
  Buffer.from([0x00, 0x00, 0x00, 0x78, 0x00, 0x00, 0x00, 0x50, 0x08, 0x02, 0x00, 0x00, 0x00]),
  Buffer.from([0x00, 0x00, 0x00, 0x00]),
]);

async function pasteImages(page: import("@playwright/test").Page, names: string[]) {
  // The paperclip marks the per-terminal identity controls resolved
  // (image support advertised); pasting before it races the refusal path.
  await expect(page.getByRole("button", { name: "Attach an image" })).toBeVisible();
  await page.evaluate((fileNames) => {
    const input = document.querySelector<HTMLTextAreaElement>(
      '[aria-label="Message to the native composer"]',
    );
    if (!input) throw new Error("composer not mounted");
    const transfer = new DataTransfer();
    for (const fileName of fileNames) {
      transfer.items.add(new File([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], fileName, { type: "image/png" }));
    }
    const event = new ClipboardEvent("paste", {
      clipboardData: transfer,
      bubbles: true,
      cancelable: true,
    });
    input.dispatchEvent(event);
  }, names);
}

async function pasteImage(page: import("@playwright/test").Page, name = "paste.png") {
  await pasteImages(page, [name]);
}

test.describe("Lane C image composer (§8.7, §10.5)", () => {
  test("composer composition: paperclip, routing line, wrap budget", async ({ page }, testInfo) => {
    await stubBackend(page);
    await openNativeTerminal(page);

    const composer = page.getByPlaceholder("Send a message to the native composer…");
    await expect(composer).toBeVisible();
    await expect(page.getByRole("button", { name: "Attach an image" })).toBeVisible();
    await expect(page.getByTestId("composer-route-status")).toHaveText(
      "delivers as control input · 0/512 B",
    );
    // The strip only exists when attachments exist (§8.7.2).
    await expect(page.getByTestId("attachment-strip")).toHaveCount(0);
    await page.screenshot({
      path: `e2e/screenshots/${testInfo.project.name}-composer-empty.png`,
    });
  });

  test("picker stages an image: chip, token, strip budget, axe clean", async ({ page }, testInfo) => {
    await stubBackend(page);
    await openNativeTerminal(page);

    await page.getByTestId("attachment-file-input").setInputFiles({
      name: "shot.png",
      mimeType: "image/png",
      buffer: PNG_BYTES,
    });
    const strip = page.getByTestId("attachment-strip");
    await expect(strip).toBeVisible();
    await expect(strip.getByText("ready")).toBeVisible();
    await expect(
      page.getByPlaceholder("Send a message to the native composer…"),
    ).toHaveValue("[Image #1]");

    // §8.7.2: the strip stays within its 56px budget; the chip's alt text
    // carries number, name, size, state (§8.7.8).
    const stripBox = await strip.boundingBox();
    expect(stripBox?.height ?? 999).toBeLessThanOrEqual(56);
    await expect(page.getByRole("img", { name: /Image #1/ })).toHaveAttribute(
      "alt",
      "Image #1: shot.png, 213 B, ready",
    );
    await expect(page.getByTestId("composer-route-status")).toContainText(
      "operator message",
    );
    await expect(page.getByTestId("composer-route-status")).toContainText("1 image");

    const scan = await new AxeBuilder({ page })
      .include('[data-testid="native-control-area"]')
      .analyze();
    expect(
      scan.violations.filter((v) => v.impact === "serious" || v.impact === "critical"),
    ).toEqual([]);
    await page.screenshot({
      path: `e2e/screenshots/${testInfo.project.name}-attachment-strip.png`,
    });
  });

  test("clipboard image paste stages a chip; plain-text paste stays text", async ({ page }) => {
    await stubBackend(page);
    await openNativeTerminal(page);

    await pasteImage(page);
    await expect(page.getByTestId("attachment-strip")).toBeVisible();
    await expect(
      page.getByPlaceholder("Send a message to the native composer…"),
    ).toHaveValue("[Image #1]");
  });

  test("multiline: Shift+Enter composes, Enter sends over operator-message (P1.3)", async ({ page }) => {
    const harness = await stubBackend(page);
    await openNativeTerminal(page);

    const composer = page.getByPlaceholder("Send a message to the native composer…");
    await composer.fill("line one");
    await expect(page.getByTestId("composer-route-status")).toContainText(
      "delivers as control input",
    );
    // Shift+Enter is the newline gesture; the draft routes multiline.
    await composer.press("Shift+Enter");
    await composer.pressSequentially("line two");
    await expect(composer).toHaveValue("line one\nline two");
    await expect(page.getByTestId("composer-route-status")).toContainText("operator message");

    // Enter sends the multiline draft over the operator-message path with
    // the newline intact; the control-input path stays untouched.
    await composer.press("Enter");
    await expect.poll(() => harness.operatorMessagePosts.length).toBe(1);
    expect(harness.operatorMessagePosts[0].text).toBe("line one\nline two");
    expect(harness.controlInputPosts).toEqual([]);
    await expect(composer).toHaveValue("");
  });

  test("a multi-file picker batch links one token per file and never exceeds the max (P1.3)", async ({ page }) => {
    const harness = await stubBackend(page);
    await openNativeTerminal(page);

    const composer = page.getByPlaceholder("Send a message to the native composer…");
    await page.getByTestId("attachment-file-input").setInputFiles([
      { name: "a.png", mimeType: "image/png", buffer: PNG_BYTES },
      { name: "b.png", mimeType: "image/png", buffer: PNG_BYTES },
      { name: "c.png", mimeType: "image/png", buffer: PNG_BYTES },
    ]);
    // Every accepted file got exactly one editable token; none overwrote
    // an earlier one (the pre-r1 stale-closure defect).
    await expect(composer).toHaveValue("[Image #1][Image #2][Image #3]");
    await expect(page.getByRole("listitem")).toHaveCount(3);
    await expect.poll(() => harness.attachmentUploads.length).toBe(3);
    await expect(page.getByTestId("composer-route-status")).toContainText("3 images");

    // A second batch of two takes the one remaining slot and names it.
    await page.getByTestId("attachment-file-input").setInputFiles([
      { name: "d.png", mimeType: "image/png", buffer: PNG_BYTES },
      { name: "e.png", mimeType: "image/png", buffer: PNG_BYTES },
    ]);
    await expect(composer).toHaveValue("[Image #1][Image #2][Image #3][Image #4]");
    await expect(page.getByRole("listitem")).toHaveCount(4);
    await expect.poll(() => harness.attachmentUploads.length).toBe(4);
    await expect(page.getByTestId("composer-route-status")).toContainText("4 images");
    await expect(
      page.getByText(/at most 4 images ride one operator message — 1 accepted, 1 skipped; 0 slots remain/),
    ).toBeVisible();
  });

  test("a multi-image paste in a single event stages the whole batch (P1.3)", async ({ page }) => {
    const harness = await stubBackend(page);
    await openNativeTerminal(page);

    const composer = page.getByPlaceholder("Send a message to the native composer…");
    await pasteImages(page, ["x.png", "y.png"]);
    await expect(composer).toHaveValue("[Image #1][Image #2]");
    await expect(page.getByRole("listitem")).toHaveCount(2);
    await expect.poll(() => harness.attachmentUploads.length).toBe(2);
  });

  test("text+image sends as one operator message; zero control-input posts, zero raw WS input", async ({ page }, testInfo) => {
    const harness = await stubBackend(page);
    await openNativeTerminal(page);

    const composer = page.getByPlaceholder("Send a message to the native composer…");
    await composer.fill("what is in this image ");
    await page.getByTestId("attachment-file-input").setInputFiles({
      name: "shot.png",
      mimeType: "image/png",
      buffer: PNG_BYTES,
    });
    await expect(page.getByTestId("attachment-strip").getByText("ready")).toBeVisible();
    await page.getByRole("button", { name: "Send" }).click();

    await expect.poll(() => harness.operatorMessagePosts.length).toBe(1);
    const post = harness.operatorMessagePosts[0];
    expect(post.text).toBe("what is in this image [Image #1]");
    expect(post.attachments).toEqual(["att-1"]);
    expect(post.token_map).toEqual({ "1": "att-1" });
    expect(post.expected_identity).toMatchObject({
      terminal_id: "t-native",
      terminal_generation: "generation-1",
    });
    // The draft and chips clear on accepted; the control-input path and the
    // raw websocket input frame stay untouched (§6.6 invariant).
    await expect(composer).toHaveValue("");
    await expect(page.getByTestId("attachment-strip")).toHaveCount(0);
    expect(harness.controlInputPosts).toEqual([]);
    const wsFrames = await wsSentFrames(page);
    expect(wsFrames.filter((f) => f.includes('"input"'))).toEqual([]);
    await page.screenshot({
      path: `e2e/screenshots/${testInfo.project.name}-attachment-send.png`,
    });
  });

  test("a >512-byte draft routes to operator-message, never truncating", async ({ page }) => {
    const harness = await stubBackend(page);
    await openNativeTerminal(page);

    const composer = page.getByPlaceholder("Send a message to the native composer…");
    await composer.fill("x".repeat(600));
    await expect(page.getByTestId("composer-route-status")).toContainText(
      "operator message — 600 B",
    );
    await page.getByRole("button", { name: "Send" }).click();
    await expect.poll(() => harness.operatorMessagePosts.length).toBe(1);
    expect(harness.operatorMessagePosts[0].text).toHaveLength(600);
    expect(harness.controlInputPosts).toEqual([]);
  });

  test("a short text-only draft stays on the deployed control-input path", async ({ page }) => {
    const harness = await stubBackend(page);
    await openNativeTerminal(page);

    const composer = page.getByPlaceholder("Send a message to the native composer…");
    await composer.fill("short command");
    await page.getByRole("button", { name: "Send" }).click();
    await expect.poll(() => harness.controlInputPosts.length).toBe(1);
    expect(harness.controlInputPosts[0]).toMatchObject({ text: "short command", enter: true });
    expect(harness.operatorMessagePosts).toEqual([]);
  });

  test("chip removal deletes the token; token deletion detaches the chip", async ({ page }) => {
    const harness = await stubBackend(page);
    await openNativeTerminal(page);

    const composer = page.getByPlaceholder("Send a message to the native composer…");
    await page.getByTestId("attachment-file-input").setInputFiles({
      name: "shot.png",
      mimeType: "image/png",
      buffer: PNG_BYTES,
    });
    await expect(page.getByTestId("attachment-strip").getByText("ready")).toBeVisible();

    // Chip remove → token gone from the draft, record deleted server-side.
    await page.getByRole("button", { name: "Remove image #1" }).click();
    await expect(composer).toHaveValue("");
    await expect.poll(() => harness.attachments[0].state).toBe("removed");
    await expect(page.getByTestId("attachment-strip")).toHaveCount(0);

    // Token deletion (by editing the draft) detaches the attachment.
    await page.getByTestId("attachment-file-input").setInputFiles({
      name: "shot.png",
      mimeType: "image/png",
      buffer: PNG_BYTES,
    });
    await expect(page.getByTestId("attachment-strip").getByText("ready")).toBeVisible();
    await composer.fill("");
    await expect(page.getByTestId("attachment-strip")).toHaveCount(0);
    await expect.poll(() => harness.attachments[1].state).toBe("removed");
  });

  test("old server: affordances hidden, over-limit draft disables Send with the reason", async ({ page }) => {
    const harness = await stubBackend(page, { laneC: false });
    await openNativeTerminal(page);

    await expect(page.getByRole("button", { name: "Attach an image" })).toHaveCount(0);
    const composer = page.getByPlaceholder("Send a message to the native composer…");
    await composer.fill("x".repeat(600));
    await expect(page.getByTestId("composer-route-status")).toContainText(
      "operator message unavailable for this provider",
    );
    await expect(page.getByRole("button", { name: "Send" })).toBeDisabled();

    // Short drafts still deliver through the deployed path.
    await composer.fill("short");
    await page.getByRole("button", { name: "Send" }).click();
    await expect.poll(() => harness.controlInputPosts.length).toBe(1);
    expect(harness.operatorMessagePosts).toEqual([]);
  });

  test("mobile composition: strip scrolls, terminal keeps ≥50% height (390×844 and 360×800)", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile-chromium", "mobile-only composition check");
    await stubBackend(page);
    await openNativeTerminal(page);

    await page.getByTestId("attachment-file-input").setInputFiles([
      { name: "a.png", mimeType: "image/png", buffer: PNG_BYTES },
      { name: "b.png", mimeType: "image/png", buffer: PNG_BYTES },
    ]);
    await expect(page.getByTestId("attachment-strip").getByText("ready").first()).toBeVisible();

    // §7.1 (measured on the visible fitted .xterm child, PR #50 floor):
    // the terminal keeps ≥ 422px of the 844px viewport with the strip up.
    const measure = async () => {
      const box = await page.locator(".xterm").first().boundingBox();
      if (!box) throw new Error("no fitted .xterm");
      return box.height;
    };
    expect(await measure()).toBeGreaterThanOrEqual(422);

    // 360×800 (the A+B QA's second handset): the same rule at ≥ 400px.
    await page.setViewportSize({ width: 360, height: 800 });
    await expect.poll(measure).toBeGreaterThanOrEqual(400);

    // The strip is horizontally scrollable and within its height budget.
    const stripBox = await page.getByTestId("attachment-strip").boundingBox();
    expect(stripBox?.height ?? 999).toBeLessThanOrEqual(56);
    await page.screenshot({
      path: `e2e/screenshots/${testInfo.project.name}-mobile-attachments.png`,
    });
  });
});
