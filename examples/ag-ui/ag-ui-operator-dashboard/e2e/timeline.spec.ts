// Browser check and screenshots of the operator dashboard: the six allow-listed
// generative-UI components, and the session timeline panel (#1293).
//
// It needs the private cao-server that e2e/run.sh started (CAO_E2E_BASE). It opens the
// dashboard first, so the page's ambient stream sees everything live (a page does not
// replay what came before it). Then it emits each of the six components from the
// supervisor terminal, and starts two calls on the same session (an assign and a
// handoff, both left open). Every component card and both timeline rows must render, or
// the test fails. The two panels are saved to e2e/artifacts/.
import { mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test } from "@playwright/test";
import { ALLOWED_COMPONENTS } from "../core/genui.ts";

const BASE = process.env.CAO_E2E_BASE ?? "";
const HERE = dirname(fileURLToPath(import.meta.url));
const ARTIFACTS = join(HERE, "artifacts");

test.beforeAll(() => {
  if (!BASE) throw new Error("run this through e2e/run.sh (CAO_E2E_BASE is not set)");
  if (new URL(BASE).port === "9889") throw new Error("refusing the default fleet port 9889");
  mkdirSync(ARTIFACTS, { recursive: true });
});

test("the six components and both timeline calls render, and are saved as screenshots", async ({ page, request, baseURL }) => {
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

  // The six allow-listed components, from the supervisor terminal, as e2e/e2e.ts emits them.
  for (const component of ALLOWED_COMPONENTS) {
    const res = await request.post(`${BASE}/agui/v1/emit_ui`, {
      data: { component, terminal_id: supervisor.id, props: { label: `e2e ${component}`, note: "<b>not markup</b>" } },
    });
    expect(res.status(), `${component}: ${await res.text()}`).toBe(200);
  }
  const generative = page.locator('section[aria-label="Generative UI"]');
  await expect(generative, "the generative UI panel did not render").toBeVisible();
  for (const component of ALLOWED_COMPONENTS) {
    await expect(generative.locator(`article[aria-label="${component}"]`), `${component} did not render`).toBeVisible();
  }
  await generative.screenshot({ path: join(ARTIFACTS, "generative-ui-panel.png") });

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
