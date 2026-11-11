// Browser check and screenshot of the dashboard's session timeline panel (#1293).
//
// It needs the private cao-server that e2e/run.sh started (CAO_E2E_BASE). It opens the
// dashboard first, then starts two calls on a fresh session (an assign and a handoff,
// both left open), so the page's ambient stream sees them live. The panel must render
// both as rows, or the test fails. The panel is saved to e2e/artifacts/timeline-panel.png.
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

test("the session timeline renders both calls and is saved as a screenshot", async ({ page, request, baseURL }) => {
  // A supervisor terminal, on a session name no other run uses.
  const session = `cao-timeline-${Date.now()}`;
  const made = await request.post(
    `${BASE}/sessions?${new URLSearchParams({ agent_profile: "developer", provider: "mock_cli", session_name: session })}`,
  );
  expect(made.status(), await made.text()).toBe(201);
  const supervisor = (await made.json()) as { id: string; session_name: string };
  await expect
    .poll(async () => ((await (await request.get(`${BASE}/terminals/${supervisor.id}`)).json()) as { status: string }).status)
    .toMatch(/idle|completed/);

  // Open the dashboard against this server; the ambient stream starts with the page.
  await page.goto(`${baseURL}/?server=${encodeURIComponent(BASE)}`);
  await expect(page.getByText("Ambient stream: open.")).toBeVisible();

  // Two calls from the supervisor: an assign and a handoff, each to its own worker.
  for (const kind of ["assign", "handoff"]) {
    const query = new URLSearchParams({
      agent_profile: "developer",
      provider: "mock_cli",
      caller_id: supervisor.id,
      defer_init: "true",
    });
    const res = await request.post(`${BASE}/sessions/${supervisor.session_name}/terminals?${query}`, {
      data: { initial_message: "Do the assigned task.", initial_message_orchestration_type: kind },
    });
    expect(res.status(), await res.text()).toBe(201);
  }

  // The panel must exist and show both calls, or the run fails.
  const timeline = page.locator('section[aria-label="Session timeline"]');
  await expect(timeline, "the session timeline panel did not render").toBeVisible();
  const rows = timeline.locator('[data-testid="call-state"]');
  await expect(rows, "both calls should be rows in the timeline").toHaveCount(2);
  await expect(timeline).toContainText("assign:");
  await expect(timeline).toContainText("handoff:");

  await timeline.screenshot({ path: join(ARTIFACTS, "timeline-panel.png") });
});
