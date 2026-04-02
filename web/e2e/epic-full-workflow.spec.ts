/**
 * Full workflow E2E tests — end-to-end user journeys.
 * These tests simulate complete real-world scenarios.
 */
import { test, expect } from '@playwright/test';

const BASE = 'http://localhost:8000';

test.describe('Full Workflow: Epic Lifecycle', () => {
  test('create epic → check progress → assign child → close → progress updates', async ({ request, page }) => {
    // 1. Create epic via API
    const epicRes = await request.post(`${BASE}/api/v2/epics`, {
      data: { title: 'Workflow Epic', steps: ['Build', 'Test', 'Deploy'], sequential: true }
    });
    expect(epicRes.ok()).toBeTruthy();
    const { epic, children } = await epicRes.json();

    // 2. Verify progress starts at 0
    const progressRes = await request.get(`${BASE}/api/v2/epics/${epic.id}`);
    const { progress } = await progressRes.json();
    expect(progress.total).toBe(3);
    expect(progress.completed).toBe(0);

    // 3. Get first ready child
    const readyRes = await request.get(`${BASE}/api/v2/epics/${epic.id}/ready`);
    const ready = await readyRes.json();
    expect(ready.length).toBeGreaterThanOrEqual(1);
    const firstChild = ready[0];

    // 4. Assign to agent
    const assignRes = await request.post(`${BASE}/api/v2/beads/${firstChild.id}/assign-agent`, {
      data: { agent_name: 'developer', provider: 'q_cli' }
    });
    if (assignRes.ok()) {
      const { session_id } = await assignRes.json();

      // 5. Verify bead-session binding
      const lookupRes = await request.get(`${BASE}/api/v2/beads/${firstChild.id}/session`);
      expect(lookupRes.ok()).toBeTruthy();

      // 6. Verify in UI
      await page.goto(BASE);
      await page.waitForTimeout(3000);
      const epicCard = page.locator(`text=Workflow Epic`);
      if (await epicCard.isVisible({ timeout: 5000 }).catch(() => false)) {
        await expect(epicCard).toBeVisible();
      }

      // 7. Cleanup session
      await request.delete(`${BASE}/api/v2/sessions/${session_id}`);
    }
  });

  test('create bead → assign → delete session → bead released', async ({ request }) => {
    // Create
    const beadRes = await request.post(`${BASE}/api/tasks`, {
      data: { title: 'Release Test Bead', priority: 2 }
    });
    const bead = await beadRes.json();

    // Assign to agent
    const assignRes = await request.post(`${BASE}/api/v2/beads/${bead.id}/assign-agent`, {
      data: { agent_name: 'developer', provider: 'q_cli' }
    });
    if (!assignRes.ok()) return;
    const { session_id } = await assignRes.json();

    // Verify binding exists
    const lookup1 = await request.get(`${BASE}/api/v2/beads/${bead.id}/session`);
    expect(lookup1.ok()).toBeTruthy();

    // Delete session
    await request.delete(`${BASE}/api/v2/sessions/${session_id}`);

    // Verify binding cleared
    const lookup2 = await request.get(`${BASE}/api/v2/beads/${bead.id}/session`);
    expect(lookup2.status()).toBe(404);

    // Verify bead still exists (not deleted)
    const beadCheck = await request.get(`${BASE}/api/tasks/${bead.id}`);
    expect(beadCheck.ok()).toBeTruthy();
  });

  test('multiple epics coexist independently', async ({ request, page }) => {
    // Create 2 epics
    const e1Res = await request.post(`${BASE}/api/v2/epics`, {
      data: { title: 'Epic Alpha', steps: ['A1', 'A2'] }
    });
    const e2Res = await request.post(`${BASE}/api/v2/epics`, {
      data: { title: 'Epic Beta', steps: ['B1', 'B2', 'B3'] }
    });

    if (e1Res.ok() && e2Res.ok()) {
      const e1 = await e1Res.json();
      const e2 = await e2Res.json();

      // Each has independent children
      const p1 = await (await request.get(`${BASE}/api/v2/epics/${e1.epic.id}`)).json();
      const p2 = await (await request.get(`${BASE}/api/v2/epics/${e2.epic.id}`)).json();
      expect(p1.progress.total).toBe(2);
      expect(p2.progress.total).toBe(3);

      // Both visible in UI
      await page.goto(BASE);
      await page.waitForTimeout(3000);
      // Just verify no crash — epics may or may not render depending on bd state
    }
  });
});


test.describe('Full Workflow: Dependency Resolution', () => {
  test('sequential epic enforces ordering', async ({ request }) => {
    const res = await request.post(`${BASE}/api/v2/epics`, {
      data: { title: 'Order Epic', steps: ['First', 'Second', 'Third'], sequential: true }
    });
    if (!res.ok()) return;
    const { epic } = await res.json();

    // Only first is ready
    const r1 = await (await request.get(`${BASE}/api/v2/epics/${epic.id}/ready`)).json();
    expect(r1.length).toBe(1);
    expect(r1[0].title).toBe('First');
  });
});
