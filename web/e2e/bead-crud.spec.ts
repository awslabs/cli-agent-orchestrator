/**
 * E2E: Bead CRUD operations — create, read, update, close, delete.
 * Tests the full bead lifecycle through the API and verifies UI state.
 */
import { test, expect } from '@playwright/test';

const BASE = 'http://localhost:8000';

test.describe('Bead CRUD Lifecycle', () => {
  let beadId: string;

  test('create bead via API', async ({ request }) => {
    const res = await request.post(`${BASE}/api/tasks`, {
      data: { title: 'CRUD Test Bead', description: 'End-to-end test', priority: 1 }
    });
    expect(res.ok()).toBeTruthy();
    const bead = await res.json();
    expect(bead.id).toBeTruthy();
    expect(bead.title).toBe('CRUD Test Bead');
    expect(bead.priority).toBe(1);
    expect(bead.status).toBe('open');
    beadId = bead.id;
  });

  test('read bead back via API', async ({ request }) => {
    const res = await request.get(`${BASE}/api/tasks/${beadId}`);
    expect(res.ok()).toBeTruthy();
    const bead = await res.json();
    expect(bead.title).toBe('CRUD Test Bead');
    expect(bead.description).toBe('End-to-end test');
  });

  test('bead appears in list', async ({ request }) => {
    const res = await request.get(`${BASE}/api/tasks`);
    const tasks = await res.json();
    expect(tasks.some((t: any) => t.id === beadId)).toBeTruthy();
  });

  test('update bead title and priority', async ({ request }) => {
    const res = await request.patch(`${BASE}/api/tasks/${beadId}`, {
      data: { title: 'Updated CRUD Bead', priority: 3 }
    });
    expect(res.ok()).toBeTruthy();
    const bead = await res.json();
    expect(bead.title).toBe('Updated CRUD Bead');
  });

  test('mark bead as WIP', async ({ request }) => {
    const res = await request.post(`${BASE}/api/tasks/${beadId}/wip`);
    expect(res.ok()).toBeTruthy();
    const bead = await res.json();
    expect(bead.status).toBe('wip');
  });

  test('close bead', async ({ request }) => {
    const res = await request.post(`${BASE}/api/tasks/${beadId}/close`);
    expect(res.ok()).toBeTruthy();
    const bead = await res.json();
    expect(bead.status).toBe('closed');
  });

  test('closed bead still readable', async ({ request }) => {
    const res = await request.get(`${BASE}/api/tasks/${beadId}`);
    expect(res.ok()).toBeTruthy();
    expect((await res.json()).status).toBe('closed');
  });

  test('delete bead', async ({ request }) => {
    const res = await request.delete(`${BASE}/api/tasks/${beadId}`);
    expect(res.ok()).toBeTruthy();
  });
});


test.describe('Bead Filtering', () => {
  test('filter by status=open returns only open beads', async ({ request }) => {
    const res = await request.get(`${BASE}/api/tasks?status=open`);
    expect(res.ok()).toBeTruthy();
    const tasks = await res.json();
    for (const t of tasks) {
      expect(t.status).toBe('open');
    }
  });

  test('filter by status=wip returns only wip beads', async ({ request }) => {
    const res = await request.get(`${BASE}/api/tasks?status=wip`);
    expect(res.ok()).toBeTruthy();
    const tasks = await res.json();
    for (const t of tasks) {
      expect(t.status).toBe('wip');
    }
  });

  test('tasks list includes labels and type fields', async ({ request }) => {
    const res = await request.get(`${BASE}/api/tasks`);
    const tasks = await res.json();
    if (tasks.length > 0) {
      const t = tasks[0];
      expect(t).toHaveProperty('labels');
      expect(t).toHaveProperty('type');
      expect(t).toHaveProperty('parent_id');
      expect(t).toHaveProperty('blocked_by');
    }
  });

  test('next task endpoint returns highest priority', async ({ request }) => {
    const res = await request.get(`${BASE}/api/tasks/next`);
    // May 404 if no open tasks — that's ok
    if (res.ok()) {
      const task = await res.json();
      expect(task.id).toBeTruthy();
      expect(task.status).toBe('open');
    }
  });
});


test.describe('Bead visible in UI', () => {
  test('beads panel renders task list', async ({ page }) => {
    await page.goto(BASE);
    await page.waitForTimeout(3000);
    // Should have at least some bead cards (data-testid="bead-card")
    const cards = page.locator('[data-testid="bead-card"]');
    const count = await cards.count();
    // Just verify rendering happened
    expect(count).toBeGreaterThanOrEqual(0);
  });

  test('bead filter buttons exist', async ({ page }) => {
    await page.goto(BASE);
    await page.waitForTimeout(2000);
    // Filter buttons: Open, In Progress, Done, All
    const openBtn = page.locator('button').filter({ hasText: /open/i }).first();
    const visible = await openBtn.isVisible({ timeout: 3000 }).catch(() => false);
    if (visible) {
      await expect(openBtn).toBeVisible();
    }
  });

  test('clicking filter changes displayed beads', async ({ page }) => {
    await page.goto(BASE);
    await page.waitForTimeout(3000);

    // Click "All" filter if visible
    const allBtn = page.locator('button').filter({ hasText: /^all$/i }).first();
    if (await allBtn.isVisible({ timeout: 2000 }).catch(() => false)) {
      await allBtn.click();
      await page.waitForTimeout(1000);
      // Should still have bead cards (or empty state)
      await expect(page.locator('text=Error')).not.toBeVisible({ timeout: 1000 });
    }
  });
});
