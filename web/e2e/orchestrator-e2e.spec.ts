/**
 * E2E tests for Master Orchestrator feature on cao-fresh.
 * Tests API endpoints, UI sidebar, and full workflows.
 */
import { test, expect } from '@playwright/test';

const BASE = 'http://localhost:9889';

// ── API Endpoint Tests ──────────────────────────────────────

test.describe('Orchestrator API', () => {
  test('health check', async ({ request }) => {
    const res = await request.get(`${BASE}/health`);
    expect(res.ok()).toBeTruthy();
    expect((await res.json()).status).toBe('ok');
  });

  test('orchestrator status when not running', async ({ request }) => {
    // Stop any running orchestrator first
    await request.delete(`${BASE}/orchestrator/stop`);
    const res = await request.get(`${BASE}/orchestrator/status`);
    expect(res.ok()).toBeTruthy();
    const data = await res.json();
    expect(data.running).toBe(false);
    expect(data.session_id).toBeNull();
  });

  test('orchestrator stop is idempotent', async ({ request }) => {
    const res = await request.delete(`${BASE}/orchestrator/stop`);
    expect(res.ok()).toBeTruthy();
    expect((await res.json()).success).toBe(true);
  });

  test('master_orchestrator agent profile exists', async ({ request }) => {
    const res = await request.get(`${BASE}/agents/profiles`);
    expect(res.ok()).toBeTruthy();
    const profiles = await res.json();
    const master = profiles.find((p: any) => p.name === 'master_orchestrator');
    expect(master).toBeDefined();
    expect(master.description).toContain('AI');
  });

  test('list sessions returns array', async ({ request }) => {
    const res = await request.get(`${BASE}/sessions`);
    expect(res.ok()).toBeTruthy();
    expect(Array.isArray(await res.json())).toBeTruthy();
  });

  test('list providers shows installed providers', async ({ request }) => {
    const res = await request.get(`${BASE}/agents/providers`);
    expect(res.ok()).toBeTruthy();
    const providers = await res.json();
    expect(providers.length).toBeGreaterThan(0);
    expect(providers.some((p: any) => p.installed)).toBeTruthy();
  });

  test('list flows returns array', async ({ request }) => {
    const res = await request.get(`${BASE}/flows`);
    expect(res.ok()).toBeTruthy();
    expect(Array.isArray(await res.json())).toBeTruthy();
  });
});


// ── UI Tests ────────────────────────────────────────────────

test.describe('App UI', () => {
  test('app loads without errors', async ({ page }) => {
    const errors: string[] = [];
    page.on('pageerror', (err) => errors.push(err.message));
    await page.goto(BASE);
    await page.waitForTimeout(3000);
    const real = errors.filter(e => !e.includes('ResizeObserver'));
    expect(real).toHaveLength(0);
  });

  test('app shows header with title', async ({ page }) => {
    await page.goto(BASE);
    await page.waitForTimeout(2000);
    await expect(page.locator('text=CLI Agent Orchestrator')).toBeVisible({ timeout: 5000 });
  });

  test('all 4 tabs visible', async ({ page }) => {
    await page.goto(BASE);
    await page.waitForTimeout(2000);
    await expect(page.locator('button[role="tab"]').filter({ hasText: 'Home' })).toBeVisible();
    await expect(page.locator('button[role="tab"]').filter({ hasText: 'Agents' })).toBeVisible();
    await expect(page.locator('button[role="tab"]').filter({ hasText: 'Flows' })).toBeVisible();
    await expect(page.locator('button[role="tab"]').filter({ hasText: 'Settings' })).toBeVisible();
  });

  test('tab switching works', async ({ page }) => {
    await page.goto(BASE);
    await page.waitForTimeout(2000);
    // Click each tab
    for (const label of ['Agents', 'Flows', 'Settings', 'Home']) {
      const tab = page.locator('button[role="tab"]').filter({ hasText: label });
      await tab.click();
      await page.waitForTimeout(500);
    }
    // No crash
    await expect(page.locator('text=CLI Agent Orchestrator')).toBeVisible();
  });
});


// ── Orchestrator Sidebar Tests ──────────────────────────────

test.describe('Orchestrator Sidebar', () => {
  test('collapsed sidebar strip visible on right edge', async ({ page }) => {
    await page.goto(BASE);
    await page.waitForTimeout(2000);
    // Look for the "Assistant" text in the collapsed strip
    const strip = page.locator('text=Assistant');
    await expect(strip).toBeVisible({ timeout: 5000 });
  });

  test('clicking strip expands sidebar', async ({ page }) => {
    await page.goto(BASE);
    await page.waitForTimeout(2000);
    // Click the collapsed strip
    const strip = page.locator('button').filter({ hasText: 'Assistant' });
    await strip.click();
    await page.waitForTimeout(500);
    // Should see "AI Orchestrator" header in expanded view
    await expect(page.locator('text=AI Orchestrator')).toBeVisible({ timeout: 3000 });
  });

  test('expanded sidebar shows launch button when not running', async ({ page }) => {
    await page.goto(BASE);
    await page.waitForTimeout(2000);
    // Expand
    const strip = page.locator('button').filter({ hasText: 'Assistant' });
    await strip.click();
    await page.waitForTimeout(500);
    // Launch button
    await expect(page.locator('text=Launch Orchestrator')).toBeVisible({ timeout: 3000 });
  });

  test('provider dropdown has options', async ({ page }) => {
    await page.goto(BASE);
    await page.waitForTimeout(2000);
    const strip = page.locator('button').filter({ hasText: 'Assistant' });
    await strip.click();
    await page.waitForTimeout(500);
    // Provider dropdown
    const select = page.locator('select').first();
    await expect(select).toBeVisible({ timeout: 3000 });
    const options = await select.locator('option').allTextContents();
    expect(options).toContain('Claude Code');
    expect(options).toContain('Kiro CLI');
    expect(options).toContain('Q CLI');
  });

  test('collapse button hides sidebar', async ({ page }) => {
    await page.goto(BASE);
    await page.waitForTimeout(2000);
    // Expand
    const strip = page.locator('button').filter({ hasText: 'Assistant' });
    await strip.click();
    await page.waitForTimeout(500);
    // Now collapse (ChevronRight button)
    const collapseBtn = page.locator('button[title="Collapse"]');
    await collapseBtn.click();
    await page.waitForTimeout(500);
    // AI Orchestrator header should be gone
    await expect(page.locator('text=AI Orchestrator')).not.toBeVisible({ timeout: 2000 });
    // But strip should be back
    await expect(page.locator('text=Assistant')).toBeVisible();
  });

  test('sidebar shows description text', async ({ page }) => {
    await page.goto(BASE);
    await page.waitForTimeout(2000);
    const strip = page.locator('button').filter({ hasText: 'Assistant' });
    await strip.click();
    await page.waitForTimeout(500);
    await expect(page.locator('text=manage sessions')).toBeVisible({ timeout: 3000 });
  });
});


// ── Cross-cutting ───────────────────────────────────────────

test.describe('No Regressions', () => {
  test('settings tab loads', async ({ page }) => {
    await page.goto(BASE);
    await page.waitForTimeout(2000);
    await page.locator('button[role="tab"]').filter({ hasText: 'Settings' }).click();
    await page.waitForTimeout(1000);
    // Should show some settings content
    await expect(page.locator('text=Error')).not.toBeVisible({ timeout: 1000 });
  });

  test('flows tab loads', async ({ page }) => {
    await page.goto(BASE);
    await page.waitForTimeout(2000);
    await page.locator('button[role="tab"]').filter({ hasText: 'Flows' }).click();
    await page.waitForTimeout(1000);
    await expect(page.locator('text=Error')).not.toBeVisible({ timeout: 1000 });
  });

  test('agents tab loads', async ({ page }) => {
    await page.goto(BASE);
    await page.waitForTimeout(2000);
    await page.locator('button[role="tab"]').filter({ hasText: 'Agents' }).click();
    await page.waitForTimeout(1000);
    await expect(page.locator('text=Error')).not.toBeVisible({ timeout: 1000 });
  });

  test('sidebar accessible from any tab', async ({ page }) => {
    await page.goto(BASE);
    await page.waitForTimeout(2000);
    for (const tab of ['Agents', 'Flows', 'Settings', 'Home']) {
      await page.locator('button[role="tab"]').filter({ hasText: tab }).click();
      await page.waitForTimeout(300);
      // Sidebar strip should be visible from every tab
      await expect(page.locator('text=Assistant')).toBeVisible({ timeout: 2000 });
    }
  });
});
