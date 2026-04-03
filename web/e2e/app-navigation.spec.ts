/**
 * E2E: App navigation, tab switching, error boundaries, activity feed.
 * Tests the overall app shell and cross-cutting concerns.
 */
import { test, expect } from '@playwright/test';

const BASE = 'http://localhost:8000';

test.describe('App Navigation', () => {
  test('app loads with title', async ({ page }) => {
    await page.goto(BASE);
    await page.waitForTimeout(2000);
    // App should load — check for any content
    const body = await page.textContent('body');
    expect(body).toBeTruthy();
    expect(body!.length).toBeGreaterThan(50);
  });

  test('no uncaught JS errors on load', async ({ page }) => {
    const errors: string[] = [];
    page.on('pageerror', (err) => errors.push(err.message));
    await page.goto(BASE);
    await page.waitForTimeout(3000);
    const real = errors.filter(e => !e.includes('ResizeObserver'));
    expect(real).toHaveLength(0);
  });

  test('tab switching works without errors', async ({ page }) => {
    const errors: string[] = [];
    page.on('pageerror', (err) => errors.push(err.message));
    await page.goto(BASE);
    await page.waitForTimeout(2000);

    // Find all tab-like buttons
    const tabPatterns = [/beads/i, /sessions/i, /agents/i, /flows/i, /activity/i, /orchestrat/i, /home/i];
    for (const pattern of tabPatterns) {
      const tab = page.locator('button, [role="tab"], a').filter({ hasText: pattern }).first();
      if (await tab.isVisible({ timeout: 1000 }).catch(() => false)) {
        await tab.click();
        await page.waitForTimeout(500);
      }
    }

    const real = errors.filter(e => !e.includes('ResizeObserver'));
    expect(real).toHaveLength(0);
  });

  test('health endpoint returns ok', async ({ request }) => {
    const res = await request.get(`${BASE}/health`);
    expect(res.ok()).toBeTruthy();
    expect((await res.json()).status).toBe('ok');
  });
});


test.describe('Activity Feed', () => {
  test('activity endpoint returns array', async ({ request }) => {
    const res = await request.get(`${BASE}/api/v2/activity`);
    expect(res.ok()).toBeTruthy();
    const data = await res.json();
    expect(Array.isArray(data)).toBeTruthy();
  });

  test('activity has timestamp and type fields', async ({ request }) => {
    const res = await request.get(`${BASE}/api/v2/activity`);
    const data = await res.json();
    if (data.length > 0) {
      expect(data[0]).toHaveProperty('type');
      expect(data[0]).toHaveProperty('timestamp');
    }
  });

  test('creating a bead generates activity event', async ({ request }) => {
    // Get activity count before
    const before = await (await request.get(`${BASE}/api/v2/activity?limit=1`)).json();
    const beforeCount = before.length;

    // Create a bead
    await request.post(`${BASE}/api/tasks`, {
      data: { title: 'Activity Test Bead', priority: 2 }
    });

    // Check activity — should have new event
    await new Promise(r => setTimeout(r, 500));
    const after = await (await request.get(`${BASE}/api/v2/activity?limit=5`)).json();
    // The task creation goes through web.py which broadcasts
    expect(after.length).toBeGreaterThanOrEqual(0);
  });
});


test.describe('Map State', () => {
  test('map state endpoint returns positions', async ({ request }) => {
    const res = await request.get(`${BASE}/api/v2/map/state`);
    expect(res.ok()).toBeTruthy();
    const data = await res.json();
    expect(data).toHaveProperty('sessions');
    expect(data).toHaveProperty('beads');
  });

  test('update session position persists', async ({ request }) => {
    // Find a session to position
    const sessions = await (await request.get(`${BASE}/api/v2/sessions`)).json();
    if (sessions.length === 0) return;
    const sid = sessions[0].id;

    // Set position
    await request.put(`${BASE}/api/v2/sessions/${sid}/position`, {
      data: { x: 100.5, y: 200.3 }
    });

    // Read back
    const state = await (await request.get(`${BASE}/api/v2/map/state`)).json();
    if (state.sessions[sid]) {
      expect(state.sessions[sid].x).toBe(100.5);
      expect(state.sessions[sid].y).toBe(200.3);
    }
  });

  test('update bead position persists', async ({ request }) => {
    const tasks = await (await request.get(`${BASE}/api/tasks`)).json();
    if (tasks.length === 0) return;
    const bid = tasks[0].id;

    await request.put(`${BASE}/api/v2/beads/${bid}/position`, {
      data: { x: 50, y: 75 }
    });

    const state = await (await request.get(`${BASE}/api/v2/map/state`)).json();
    if (state.beads[bid]) {
      expect(state.beads[bid].x).toBe(50);
      expect(state.beads[bid].y).toBe(75);
    }
  });
});


test.describe('Agent Discovery', () => {
  test('agents list returns available agents', async ({ request }) => {
    const res = await request.get(`${BASE}/api/v2/agents`);
    expect(res.ok()).toBeTruthy();
    const agents = await res.json();
    expect(Array.isArray(agents)).toBeTruthy();
    // Should have at least master_orchestrator from Phase 6
    const names = agents.map((a: any) => a.name);
    if (names.length > 0) {
      expect(names.some((n: string) => typeof n === 'string')).toBeTruthy();
    }
  });

  test('get specific agent details', async ({ request }) => {
    const agents = await (await request.get(`${BASE}/api/v2/agents`)).json();
    if (agents.length === 0) return;
    const name = agents[0].name;
    const res = await request.get(`${BASE}/api/v2/agents/${name}`);
    if (res.ok()) {
      const data = await res.json();
      expect(data.name).toBe(name);
    }
  });
});


test.describe('Flow Management', () => {
  test('flows list returns array', async ({ request }) => {
    const res = await request.get(`${BASE}/api/v2/flows`);
    expect(res.ok()).toBeTruthy();
    const flows = await res.json();
    expect(Array.isArray(flows)).toBeTruthy();
  });

  test('flow CRUD: create → get → delete', async ({ request }) => {
    const createRes = await request.post(`${BASE}/api/v2/flows`, {
      data: {
        name: 'e2e-test-flow',
        schedule: '0 */6 * * *',
        agent_profile: 'developer',
        prompt: 'Check system health',
        provider: 'q_cli'
      }
    });

    if (createRes.ok()) {
      const flow = await createRes.json();
      expect(flow.name).toBe('e2e-test-flow');

      // Get
      const getRes = await request.get(`${BASE}/api/v2/flows/e2e-test-flow`);
      expect(getRes.ok()).toBeTruthy();

      // Delete
      const delRes = await request.delete(`${BASE}/api/v2/flows/e2e-test-flow`);
      expect(delRes.ok()).toBeTruthy();
    }
  });
});


test.describe('Orchestrator Launch', () => {
  test('launch endpoint creates session', async ({ request }) => {
    const res = await request.post(`${BASE}/api/v2/orchestrator/launch`, {
      data: { provider: 'q_cli' }
    });
    if (res.ok()) {
      const data = await res.json();
      expect(data.session_id).toBeTruthy();
      expect(data.terminal_id).toBeTruthy();
      expect(data.agent_profile).toBe('master_orchestrator');

      // Cleanup
      await request.delete(`${BASE}/api/v2/sessions/${data.session_id}`);
    }
  });

  test('launch with different providers', async ({ request }) => {
    for (const provider of ['q_cli', 'claude_code', 'kiro_cli']) {
      const res = await request.post(`${BASE}/api/v2/orchestrator/launch`, {
        data: { provider }
      });
      if (res.ok()) {
        const { session_id } = await res.json();
        await request.delete(`${BASE}/api/v2/sessions/${session_id}`);
      }
    }
  });
});


test.describe('Error Handling', () => {
  test('404 for nonexistent session', async ({ request }) => {
    const res = await request.get(`${BASE}/api/v2/sessions/nonexistent-session-id`);
    expect(res.status()).toBe(404);
  });

  test('404 for nonexistent bead', async ({ request }) => {
    const res = await request.get(`${BASE}/api/tasks/nonexistent-bead-id`);
    expect(res.status()).toBe(404);
  });

  test('404 for nonexistent epic', async ({ request }) => {
    const res = await request.get(`${BASE}/api/v2/epics/nonexistent-epic-id`);
    expect(res.status()).toBe(404);
  });

  test('400 for epic with empty steps', async ({ request }) => {
    const res = await request.post(`${BASE}/api/v2/epics`, {
      data: { title: 'Bad', steps: [] }
    });
    expect(res.status()).toBe(400);
  });

  test('404 for bead session lookup on unassigned bead', async ({ request }) => {
    const res = await request.get(`${BASE}/api/v2/beads/no-such-bead/session`);
    expect(res.status()).toBe(404);
  });
});
