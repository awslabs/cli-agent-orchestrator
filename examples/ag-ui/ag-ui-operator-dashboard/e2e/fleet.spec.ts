// Browser check and evidence for the Fleet panel: the fleet grouped by runner.
//
// Two tests, kept apart because they prove different things:
//
//  1. REAL stream. Against the private cao-server e2e/run.sh started (CAO_E2E_BASE), two
//     more mock_cli sessions are launched and the panel must show their terminals under
//     `local`, each with an agent_card composed from the profile catalog. On `main` no
//     terminal has a runner, so this can only ever show the `local` group.
//  2. SYNTHETIC runners. A recorded-shape STATE_SNAPSHOT that puts one terminal on a
//     runner is served to the page in place of the stream, because no server on `main`
//     can produce one. It proves the grouping and the rendering of a remote group, and
//     NOT that a real remote runner works; the screenshot says "synthetic" for that
//     reason.
//
// Screenshots and a video of the real stream are saved to e2e/artifacts/.
import { mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test } from "@playwright/test";

const BASE = process.env.CAO_E2E_BASE ?? "";
const HERE = dirname(fileURLToPath(import.meta.url));
const ARTIFACTS = join(HERE, "artifacts");

test.beforeAll(() => {
  if (!BASE) throw new Error("run this through e2e/run.sh (CAO_E2E_BASE is not set)");
  if (new URL(BASE).port === "9889") throw new Error("refusing the default fleet port 9889");
  mkdirSync(ARTIFACTS, { recursive: true });
});

// Top level: Playwright forbids changing the video setting inside a describe group.
test.use({ video: { mode: "on", size: { width: 1100, height: 800 } }, viewport: { width: 1100, height: 800 } });

test.describe("real stream", () => {
  test("launched sessions appear under `local` with a card from the catalog", async ({ page, request, baseURL }) => {
    await page.goto(`${baseURL}/?server=${encodeURIComponent(BASE)}`);
    await expect(page.getByText("Ambient stream: open.")).toBeVisible();
    const fleet = page.locator('section[aria-label="Fleet"]');
    // e2e.ts ran first against this server and left its terminals; count from there.
    await expect(fleet.getByRole("heading", { level: 3 })).toHaveText(["local"]);
    const cards = fleet.locator('article[aria-label="agent_card"]');
    const before = await cards.count();

    const stamp = Date.now();
    for (const n of ["a", "b"]) {
      const made = await request.post(
        `${BASE}/sessions?${new URLSearchParams({ agent_profile: "developer", provider: "mock_cli", session_name: `cao-fleet-${n}-${stamp}` })}`,
      );
      expect(made.status(), await made.text()).toBe(201);
    }

    await expect(fleet.getByRole("heading", { level: 3 })).toHaveText(["local"]);
    await expect(cards, "one more card per launched terminal").toHaveCount(before + 2);
    await expect(cards.first()).toContainText("developer");
    // The role and source come from the profile catalog, not from the terminal row.
    await expect(cards.first()).toContainText("built-in");
    await expect(fleet.getByRole("button")).toHaveCount(0);
    await page.waitForTimeout(1500); // let the clip settle on the finished panel
    await fleet.screenshot({ path: join(ARTIFACTS, "fleet-panel-real.png") });
  });
});

test("SYNTHETIC runners: a terminal on rn-a is grouped apart from local", async ({ page, baseURL }) => {
  const term = (id: string, extra: Record<string, unknown> = {}) => ({
    id,
    session_name: "main",
    provider: "mock_cli",
    agent_profile: "developer",
    window: "w",
    status: "idle",
    last_active: null,
    ...extra,
  });
  const frames = [
    { type: "RUN_STARTED", threadId: "synthetic", runId: "synthetic-1" },
    {
      type: "STATE_SNAPSHOT",
      snapshot: {
        sessions: [{ id: "main", name: "main", status: "active" }],
        terminals: [
          term("t-local-1"),
          term("t-rn-a-1", { runner_id: "rn-a" }),
          term("t-rn-a-2", { runner_id: "rn-a", status: "waiting_user_answer" }),
        ],
        counts: { sessions: 1, terminals: 3 },
        scopes: ["cao:read"],
        profiles: [{ name: "developer", source: "built-in", role: "developer", loadable: true }],
      },
    },
  ];
  await page.route("**/agui/v1/stream**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: frames.map((f) => `data: ${JSON.stringify(f)}\n\n`).join(""),
    }),
  );
  await page.goto(`${baseURL}/?server=${encodeURIComponent(BASE)}`);
  const fleet = page.locator('section[aria-label="Fleet"]');
  await expect(fleet.getByRole("heading", { level: 3 })).toHaveText(["local", "rn-a"]);
  await expect(fleet.locator('section[aria-label="Runner rn-a"]')).toContainText("2 terminals, 1 waiting for an answer");
  // Say so in the picture: this group was not produced by a real runner.
  await page.evaluate(() => {
    const note = document.createElement("p");
    note.textContent = "SYNTHETIC: served by the test, not by a runner. main has no /runners yet.";
    note.style.cssText = "background:#fff3cd;padding:4px;font-weight:bold";
    document.querySelector('section[aria-label="Fleet"]')?.prepend(note);
  });
  await fleet.screenshot({ path: join(ARTIFACTS, "fleet-panel-synthetic-runners.png") });
});
