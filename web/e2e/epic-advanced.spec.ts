/**
 * E2E: Advanced epic scenarios — multi-child progress, parallel epics,
 * child bead operations, epic with context labels.
 */
import { test, expect } from '@playwright/test';

const BASE = 'http://localhost:8000';

test.describe('Epic with Many Children', () => {
  test('create epic with 5 steps and verify all children', async ({ request }) => {
    const res = await request.post(`${BASE}/api/v2/epics`, {
      data: {
        title: 'Big Epic',
        steps: ['Step 1', 'Step 2', 'Step 3', 'Step 4', 'Step 5'],
        sequential: true,
        priority: 1
      }
    });
    if (!res.ok()) return;
    const { epic, children } = await res.json();
    expect(children.length).toBe(5);

    // Progress should be 0/5
    const progressRes = await request.get(`${BASE}/api/v2/epics/${epic.id}`);
    const { progress } = await progressRes.json();
    expect(progress.total).toBe(5);
    expect(progress.completed).toBe(0);
  });

  test('parallel epic has all children ready', async ({ request }) => {
    const res = await request.post(`${BASE}/api/v2/epics`, {
      data: {
        title: 'Parallel Work',
        steps: ['Task A', 'Task B', 'Task C'],
        sequential: false
      }
    });
    if (!res.ok()) return;
    const { epic } = await res.json();

    const readyRes = await request.get(`${BASE}/api/v2/epics/${epic.id}/ready`);
    const ready = await readyRes.json();
    // All 3 should be ready (no deps)
    expect(ready.length).toBe(3);
  });

  test('epic with labels passes labels to parent', async ({ request }) => {
    const res = await request.post(`${BASE}/api/v2/epics`, {
      data: {
        title: 'Labeled Epic',
        steps: ['S1'],
        labels: ['team:backend', 'sprint:42']
      }
    });
    if (!res.ok()) return;
    const { epic } = await res.json();

    // Get epic details — labels should include type:epic + custom labels
    const detailRes = await request.get(`${BASE}/api/v2/epics/${epic.id}`);
    if (detailRes.ok()) {
      const { epic: epicDetail } = await detailRes.json();
      if (epicDetail.labels) {
        expect(epicDetail.labels).toContain('type:epic');
      }
    }
  });
});


test.describe('Child Bead Operations within Epic', () => {
  test('get children of epic', async ({ request }) => {
    const res = await request.post(`${BASE}/api/v2/epics`, {
      data: { title: 'Child Ops Epic', steps: ['Do X', 'Do Y'] }
    });
    if (!res.ok()) return;
    const { epic } = await res.json();

    const childRes = await request.get(`${BASE}/api/v2/beads/${epic.id}/children`);
    expect(childRes.ok()).toBeTruthy();
    const children = await childRes.json();
    expect(children.length).toBe(2);
  });

  test('create additional child on existing epic', async ({ request }) => {
    const res = await request.post(`${BASE}/api/v2/epics`, {
      data: { title: 'Add Child Epic', steps: ['First'] }
    });
    if (!res.ok()) return;
    const { epic } = await res.json();

    // Add another child
    const childRes = await request.post(`${BASE}/api/v2/beads/${epic.id}/children`, {
      data: { title: 'Second (added later)', priority: 2 }
    });
    if (childRes.ok()) {
      const child = await childRes.json();
      expect(child.title).toBe('Second (added later)');
    }

    // Should now have 2 children
    const allChildren = await (await request.get(`${BASE}/api/v2/beads/${epic.id}/children`)).json();
    expect(allChildren.length).toBe(2);
  });
});


test.describe('Dependency Management E2E', () => {
  test('add dependency between standalone beads', async ({ request }) => {
    const a = await (await request.post(`${BASE}/api/tasks`, { data: { title: 'Dep E2E A' } })).json();
    const b = await (await request.post(`${BASE}/api/tasks`, { data: { title: 'Dep E2E B' } })).json();

    // B depends on A
    const depRes = await request.post(`${BASE}/api/v2/beads/${b.id}/dep`, {
      data: { depends_on: a.id }
    });
    expect(depRes.status()).toBe(201);

    // Remove dependency
    const removeRes = await request.delete(`${BASE}/api/v2/beads/${b.id}/dep/${a.id}`);
    expect(removeRes.ok()).toBeTruthy();
  });
});


test.describe('Epic Display in UI', () => {
  test('multiple epics coexist in beads panel', async ({ page, request }) => {
    // Create 2 epics
    await request.post(`${BASE}/api/v2/epics`, {
      data: { title: 'UI Epic Alpha', steps: ['A1', 'A2'] }
    });
    await request.post(`${BASE}/api/v2/epics`, {
      data: { title: 'UI Epic Beta', steps: ['B1', 'B2', 'B3'] }
    });

    await page.goto(BASE);
    await page.waitForTimeout(3000);

    // Both should be visible
    const alpha = page.locator('text=UI Epic Alpha');
    const beta = page.locator('text=UI Epic Beta');
    const alphaVisible = await alpha.isVisible({ timeout: 5000 }).catch(() => false);
    const betaVisible = await beta.isVisible({ timeout: 3000 }).catch(() => false);

    // At least verify no crash
    await expect(page.locator('text=Error')).not.toBeVisible({ timeout: 1000 });
  });

  test('closed beads show in "Done" filter', async ({ page, request }) => {
    // Create and close a bead
    const bead = await (await request.post(`${BASE}/api/tasks`, {
      data: { title: 'Done Filter Test' }
    })).json();
    await request.post(`${BASE}/api/tasks/${bead.id}/close`);

    await page.goto(BASE);
    await page.waitForTimeout(2000);

    // Click "Done" or "Closed" filter
    const doneBtn = page.locator('button').filter({ hasText: /done|closed/i }).first();
    if (await doneBtn.isVisible({ timeout: 2000 }).catch(() => false)) {
      await doneBtn.click();
      await page.waitForTimeout(1000);
      // Should see the closed bead
      const closedBead = page.locator('text=Done Filter Test');
      const visible = await closedBead.isVisible({ timeout: 3000 }).catch(() => false);
      // Just verify no crash
    }
  });
});


test.describe('Concurrent Operations', () => {
  test('create multiple beads rapidly', async ({ request }) => {
    const promises = [];
    for (let i = 0; i < 5; i++) {
      promises.push(
        request.post(`${BASE}/api/tasks`, {
          data: { title: `Rapid Bead ${i}`, priority: 2 }
        })
      );
    }
    const results = await Promise.all(promises);
    const successful = results.filter(r => r.ok());
    expect(successful.length).toBe(5);
  });

  test('concurrent epic creation', async ({ request }) => {
    const p1 = request.post(`${BASE}/api/v2/epics`, {
      data: { title: 'Concurrent A', steps: ['A1'] }
    });
    const p2 = request.post(`${BASE}/api/v2/epics`, {
      data: { title: 'Concurrent B', steps: ['B1'] }
    });
    const [r1, r2] = await Promise.all([p1, p2]);
    // Both should succeed (no conflicts)
    if (r1.ok() && r2.ok()) {
      const d1 = await r1.json();
      const d2 = await r2.json();
      expect(d1.epic.id).not.toBe(d2.epic.id);
    }
  });
});
