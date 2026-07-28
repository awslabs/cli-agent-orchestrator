import { test, expect } from "@playwright/test";
import { AxeBuilder } from "@axe-core/playwright";
import {
  openNativeTerminal,
  pushTerminalBytes,
  stubBackend,
  wsSentFrames,
} from "./stub";

// §10.5 streaming evidence: arm → capture → trace → Stop with touch,
// websocket-silence while armed (§6.6), and wheel scrolling with streaming
// off and on (synthetic wheel events) — desktop and mobile projects.

async function wheelInputFrames(page: Parameters<typeof wsSentFrames>[0]): Promise<string[]> {
  const frames = await wsSentFrames(page);
  return frames.filter((frame) => frame.includes('"input"'));
}

test.describe("streaming mode (§6, §7.3)", () => {
  test("arms, captures a fused utterance, traces, and Stop restores the draft", async ({
    page,
  }, testInfo) => {
    const isMobile = testInfo.project.name === "mobile-chromium";
    const harness = await stubBackend(page);
    await openNativeTerminal(page);

    const composer = page.getByPlaceholder("Send a message to the native composer…");
    await composer.fill("keep me");

    await page.getByRole("button", { name: "Streaming" }).click();
    const capture = page.getByRole("textbox", { name: /Streaming keystroke capture/ });
    await expect(capture).toBeVisible();
    await expect(
      page.getByText(/STREAMING TO kimi_cli \/ spec-writer-k3 · gen genera/),
    ).toBeVisible();

    if (isMobile) await capture.tap();
    else await capture.click();
    await page.keyboard.type("hi");
    await page.keyboard.press("Enter");

    // Enter-after-text fusion: one request, text+Enter, never payload_class.
    await expect.poll(() => harness.controlInputPosts.length).toBe(1);
    expect(harness.controlInputPosts[0].events).toEqual([
      { type: "text", text: "hi" },
      { type: "key", key: "Enter" },
    ]);
    expect(harness.controlInputPosts[0].payload_class).toBeUndefined();
    expect(harness.controlInputPosts[0].expected_identity).toMatchObject({
      terminal_id: "t-native",
      terminal_generation: "generation-1",
    });

    // The trace records the accepted batch (aria-live status region).
    await expect(page.getByLabel("Streaming trace").getByText("accepted")).toBeVisible();

    // §6.6: zero websocket input frames while armed.
    expect(await wheelInputFrames(page)).toEqual([]);

    await page.screenshot({
      path: `e2e/screenshots/${testInfo.project.name}-streaming-armed.png`,
    });

    // axe: the streaming surface.
    const scan = await new AxeBuilder({ page })
      .include('[data-testid="native-control-area"]')
      .analyze();
    expect(
      scan.violations.filter((v) => v.impact === "serious" || v.impact === "critical"),
    ).toEqual([]);

    // Stop streaming is always visible and reachable (touch on mobile).
    const stop = page.getByRole("button", { name: "Stop streaming" });
    if (isMobile) await stop.tap();
    else await stop.click();
    await expect(page.getByText(/Streaming disarmed: operator stopped streaming/)).toBeVisible();
    await expect(composer).toHaveValue("keep me");
  });

  test("wheel scrolling produces wheel-only websocket frames with streaming off and on", async ({
    page,
  }) => {
    await stubBackend(page);
    await openNativeTerminal(page);

    // The "server" enables SGR mouse reporting + tracking in the pane app.
    await pushTerminalBytes(page, "\x1b[?1006h\x1b[?1000h");
    const xterm = page.locator(".xterm");

    // Streaming OFF: synthetic wheel events reach the server as wheel
    // mouse reports on the managed pane's wheel-only input channel.
    await xterm.hover();
    for (let i = 0; i < 4; i += 1) {
      await xterm.dispatchEvent("wheel", { deltaY: -120, deltaMode: 0, bubbles: true, cancelable: true });
    }
    await expect.poll(async () => (await wheelInputFrames(page)).length).toBeGreaterThan(0);
    const offFrames = await wheelInputFrames(page);
    for (const frame of offFrames) {
      const data = (JSON.parse(frame) as { data: string }).data;
      // SGR wheel reports only — never keystroke content (§6.6).
      expect(data).toMatch(/^\x1b\[<(64|65);\d+;\d+M$/);
    }

    // Streaming ON: capture holds keystrokes; the wheel channel is unchanged.
    await page.getByRole("button", { name: "Streaming" }).click();
    await page.getByRole("textbox", { name: /Streaming keystroke capture/ }).waitFor();
    await xterm.hover();
    for (let i = 0; i < 4; i += 1) {
      await xterm.dispatchEvent("wheel", { deltaY: 120, deltaMode: 0, bubbles: true, cancelable: true });
    }
    const allFrames = await wheelInputFrames(page);
    expect(allFrames.length).toBeGreaterThan(offFrames.length);
    for (const frame of allFrames) {
      const data = (JSON.parse(frame) as { data: string }).data;
      expect(data).toMatch(/^\x1b\[<(64|65);\d+;\d+[Mm]$/);
    }
  });
});
