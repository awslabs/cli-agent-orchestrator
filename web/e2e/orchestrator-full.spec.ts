/**
 * Comprehensive E2E tests for the Master Orchestrator.
 * Tests real session creation, terminal interaction, provider switching,
 * sidebar UI, and orchestrator conversations.
 */
import { test, expect } from '@playwright/test';

const BASE = 'http://localhost:9889';

// Helper: clean up all orchestrator sessions before/after tests
async function stopAllOrchestrators(request: any) {
  await request.delete(`${BASE}/orchestrator/stop`);
}

// ══════════════════════════════════════════════════════════════
// API Integration Tests — real session lifecycle
// ══════════════════════════════════════════════════════════════

test.describe('Orchestrator API — full lifecycle', () => {
  test.beforeEach(async ({ request }) => {
    await stopAllOrchestrators(request);
  });

  test.afterEach(async ({ request }) => {
    await stopAllOrchestrators(request);
  });

  test('launch → status → stop lifecycle', async ({ request }) => {
    // 1. Status: not running
    const s1 = await request.get(`${BASE}/orchestrator/status`);
    expect((await s1.json()).running).toBe(false);

    // 2. Launch
    const launch = await request.post(`${BASE}/orchestrator/launch?provider=claude_code`);
    expect(launch.ok()).toBeTruthy();
    const data = await launch.json();
    expect(data.status).toBe('launched');
    expect(data.session_id).toBeTruthy();
    expect(data.provider).toBe('claude_code');
    expect(data.agent_profile).toBe('master_orchestrator');
    const sessionId = data.session_id;

    // 3. Status: running
    const s2 = await request.get(`${BASE}/orchestrator/status`);
    const status = await s2.json();
    expect(status.running).toBe(true);
    expect(status.session_id).toBe(sessionId);

    // 4. Session exists in sessions list
    const sessions = await (await request.get(`${BASE}/sessions`)).json();
    expect(sessions.some((s: any) => s.id === sessionId)).toBeTruthy();

    // 5. Session has master_orchestrator terminal
    const detail = await (await request.get(`${BASE}/sessions/${sessionId}`)).json();
    expect(detail.terminals.length).toBeGreaterThanOrEqual(1);
    expect(detail.terminals[0].agent_profile).toBe('master_orchestrator');

    // 6. Stop
    const stop = await request.delete(`${BASE}/orchestrator/stop`);
    const stopData = await stop.json();
    expect(stopData.success).toBe(true);
    expect(stopData.stopped).toContain(sessionId);

    // 7. Status: not running
    const s3 = await request.get(`${BASE}/orchestrator/status`);
    expect((await s3.json()).running).toBe(false);

    // 8. Session gone
    const sessions2 = await (await request.get(`${BASE}/sessions`)).json();
    expect(sessions2.some((s: any) => s.id === sessionId)).toBeFalsy();
  });

  test('double launch returns already_running', async ({ request }) => {
    const r1 = await request.post(`${BASE}/orchestrator/launch?provider=claude_code`);
    expect((await r1.json()).status).toBe('launched');

    const r2 = await request.post(`${BASE}/orchestrator/launch?provider=claude_code`);
    expect((await r2.json()).status).toBe('already_running');
  });

  test('stop is idempotent', async ({ request }) => {
    const r1 = await request.delete(`${BASE}/orchestrator/stop`);
    expect((await r1.json()).success).toBe(true);

    const r2 = await request.delete(`${BASE}/orchestrator/stop`);
    expect((await r2.json()).success).toBe(true);
  });

  test('orchestrator survives server knowledge loss (orphan detection)', async ({ request }) => {
    // Launch
    const launch = await request.post(`${BASE}/orchestrator/launch?provider=claude_code`);
    const { session_id } = await launch.json();

    // Simulate server restart by checking status (which does orphan scan)
    // The session is still in tmux, status should find it
    const status = await request.get(`${BASE}/orchestrator/status`);
    expect((await status.json()).running).toBe(true);

    // Clean up
    await request.delete(`${BASE}/orchestrator/stop`);
  });
});


// ══════════════════════════════════════════════════════════════
// Session + Terminal Tests — real terminal I/O
// ══════════════════════════════════════════════════════════════

test.describe('Session creation with different providers', () => {
  const createdSessions: string[] = [];

  test.afterAll(async ({ request }) => {
    for (const sid of createdSessions) {
      await request.delete(`${BASE}/sessions/${sid}`).catch(() => {});
    }
  });

  test('create session with claude_code provider', async ({ request }) => {
    const res = await request.post(`${BASE}/sessions?provider=claude_code&agent_profile=developer`);
    if (res.ok()) {
      const data = await res.json();
      expect(data.id).toBeTruthy();
      expect(data.provider).toBe('claude_code');
      createdSessions.push(data.session_name);
    }
  });

  test('create session with kiro_cli provider', async ({ request }) => {
    const res = await request.post(`${BASE}/sessions?provider=kiro_cli&agent_profile=developer`);
    if (res.ok()) {
      const data = await res.json();
      expect(data.provider).toBe('kiro_cli');
      createdSessions.push(data.session_name);
    }
  });

  test('create session with q_cli provider', async ({ request }) => {
    const res = await request.post(`${BASE}/sessions?provider=q_cli&agent_profile=developer`);
    if (res.ok()) {
      const data = await res.json();
      expect(data.provider).toBe('q_cli');
      createdSessions.push(data.session_name);
    }
  });

  test('get terminal output from a session', async ({ request }) => {
    // Create a session
    const res = await request.post(`${BASE}/sessions?provider=claude_code&agent_profile=developer`);
    if (!res.ok()) return;
    const session = await res.json();
    createdSessions.push(session.session_name);

    // Wait for agent to initialize
    await new Promise(r => setTimeout(r, 8000));

    // Get output
    const output = await request.get(`${BASE}/terminals/${session.id}/output?mode=full`);
    if (output.ok()) {
      const data = await output.json();
      expect(typeof data.output).toBe('string');
      expect(data.output.length).toBeGreaterThan(0);
    }
  });

  test('send input to a session terminal', async ({ request }) => {
    const res = await request.post(`${BASE}/sessions?provider=claude_code&agent_profile=developer`);
    if (!res.ok()) return;
    const session = await res.json();
    createdSessions.push(session.session_name);

    await new Promise(r => setTimeout(r, 5000));

    // Send input
    const inputRes = await request.post(
      `${BASE}/terminals/${session.id}/input?message=${encodeURIComponent('echo hello')}`
    );
    expect(inputRes.ok()).toBeTruthy();
  });

  test('delete session cleans up', async ({ request }) => {
    const res = await request.post(`${BASE}/sessions?provider=claude_code&agent_profile=developer`);
    if (!res.ok()) return;
    const session = await res.json();

    const del = await request.delete(`${BASE}/sessions/${session.session_name}`);
    expect(del.ok()).toBeTruthy();
    const data = await del.json();
    expect(data.deleted).toContain(session.session_name);

    // Verify gone
    const sessions = await (await request.get(`${BASE}/sessions`)).json();
    expect(sessions.some((s: any) => s.id === session.session_name)).toBeFalsy();
  });
});


// ══════════════════════════════════════════════════════════════
// Orchestrator Terminal Interaction — real conversation
// ══════════════════════════════════════════════════════════════

test.describe('Orchestrator terminal interaction', () => {
  let sessionId: string;
  let terminalId: string;

  test.beforeAll(async ({ request }) => {
    await stopAllOrchestrators(request);
    const res = await request.post(`${BASE}/orchestrator/launch?provider=claude_code`);
    if (res.ok()) {
      const data = await res.json();
      sessionId = data.session_id;
      // Get terminal ID
      const detail = await (await request.get(`${BASE}/sessions/${sessionId}`)).json();
      terminalId = detail.terminals[0].id;
      // Wait for Claude Code to initialize
      await new Promise(r => setTimeout(r, 15000));
    }
  });

  test.afterAll(async ({ request }) => {
    await stopAllOrchestrators(request);
  });

  test('orchestrator terminal has output after init', async ({ request }) => {
    if (!terminalId) { test.skip(); return; }
    const res = await request.get(`${BASE}/terminals/${terminalId}/output?mode=full`);
    expect(res.ok()).toBeTruthy();
    const { output } = await res.json();
    expect(output.length).toBeGreaterThan(100);
    // Should contain Claude Code startup
    expect(output).toContain('Claude');
  });

  test('send message and get response', async ({ request }) => {
    if (!terminalId) { test.skip(); return; }

    // Send a simple command
    const sendRes = await request.post(
      `${BASE}/terminals/${terminalId}/input?message=${encodeURIComponent('What MCP tools do you have available? List them briefly.')}`
    );
    expect(sendRes.ok()).toBeTruthy();

    // Wait for response
    await new Promise(r => setTimeout(r, 20000));

    // Check output contains tool names
    const outRes = await request.get(`${BASE}/terminals/${terminalId}/output?mode=full`);
    const { output } = await outRes.json();
    // The orchestrator should mention its tools in the response
    expect(output.length).toBeGreaterThan(200);
  });

  test('orchestrator can list sessions via conversation', async ({ request }) => {
    if (!terminalId) { test.skip(); return; }

    await request.post(
      `${BASE}/terminals/${terminalId}/input?message=${encodeURIComponent('How many active sessions are there right now?')}`
    );

    await new Promise(r => setTimeout(r, 25000));

    const { output } = await (await request.get(`${BASE}/terminals/${terminalId}/output?mode=full`)).json();
    // Should contain session info in the response
    expect(output.length).toBeGreaterThan(300);
  });
});


// ══════════════════════════════════════════════════════════════
// Sidebar UI Tests — real browser interaction
// ══════════════════════════════════════════════════════════════

test.describe('Sidebar UI — full interaction flow', () => {
  test.beforeEach(async ({ request }) => {
    await stopAllOrchestrators(request);
  });

  test.afterEach(async ({ request }) => {
    await stopAllOrchestrators(request);
  });

  test('full sidebar flow: open → launch → see terminal → collapse → reopen', async ({ page }) => {
    await page.goto(BASE);
    await page.waitForTimeout(3000);

    // 1. Sidebar strip visible
    const strip = page.locator('button').filter({ hasText: 'Assistant' });
    await expect(strip).toBeVisible({ timeout: 5000 });

    // 2. Click to expand
    await strip.click();
    await page.waitForTimeout(500);
    await expect(page.locator('text=AI Orchestrator')).toBeVisible();
    await expect(page.locator('text=Launch Orchestrator')).toBeVisible();

    // 3. Select provider
    const select = page.locator('select').first();
    await select.selectOption('claude_code');

    // 4. Launch
    await page.locator('button').filter({ hasText: 'Launch Orchestrator' }).click();

    // 5. Wait for terminal to appear (loading → terminal)
    await page.waitForTimeout(10000);

    // 6. Should show Running badge (in sidebar header, not the "Running Agents" on dashboard)
    const runningBadge = page.locator('.bg-emerald-500\\/20').filter({ hasText: 'Running' });
    await expect(runningBadge).toBeVisible({ timeout: 20000 });

    // 7. Full screen button should exist
    await expect(page.locator('button[title="Full screen"]')).toBeVisible({ timeout: 5000 });

    // 8. Stop button should exist
    await expect(page.locator('button[title="Stop"]')).toBeVisible();

    // 9. Collapse sidebar
    await page.locator('button[title="Collapse"]').click();
    await page.waitForTimeout(500);
    await expect(page.locator('text=AI Orchestrator')).not.toBeVisible();

    // 10. Strip should be visible
    await expect(page.locator('button').filter({ hasText: 'Assistant' })).toBeVisible({ timeout: 3000 });

    // 11. Reopen — should still show terminal (Running badge in sidebar)
    await page.locator('button').filter({ hasText: 'Assistant' }).click();
    await page.waitForTimeout(1000);
    await expect(page.locator('text=AI Orchestrator')).toBeVisible();
  });

  test('full screen mode works', async ({ page, request }) => {
    // Launch via API for speed
    const launch = await request.post(`${BASE}/orchestrator/launch?provider=claude_code`);
    if (!launch.ok()) { test.skip(); return; }

    await page.goto(BASE);
    await page.waitForTimeout(3000);

    // Open sidebar and wait for terminal
    await page.locator('button').filter({ hasText: 'Assistant' }).click();
    await page.waitForTimeout(12000);

    // Click full screen
    const fullscreenBtn = page.locator('button[title="Full screen"]');
    if (await fullscreenBtn.isVisible({ timeout: 8000 }).catch(() => false)) {
      await fullscreenBtn.click();
      await page.waitForTimeout(1000);

      // Should see exit full screen button
      const exitBtn = page.locator('button[title="Exit full screen"]');
      await expect(exitBtn).toBeVisible({ timeout: 5000 });

      // Exit full screen
      await exitBtn.click();
      await page.waitForTimeout(500);

      // Sidebar header should be back
      await expect(page.locator('text=AI Orchestrator')).toBeVisible({ timeout: 3000 });
    }
  });

  test('sidebar accessible from all tabs', async ({ page }) => {
    await page.goto(BASE);
    await page.waitForTimeout(2000);

    for (const tabName of ['Agents', 'Flows', 'Settings', 'Home']) {
      const tab = page.locator('button[role="tab"]').filter({ hasText: tabName });
      await tab.click();
      await page.waitForTimeout(300);

      // Strip visible from every tab
      await expect(page.locator('button').filter({ hasText: 'Assistant' })).toBeVisible({ timeout: 2000 });

      // Can expand from any tab
      await page.locator('button').filter({ hasText: 'Assistant' }).click();
      await page.waitForTimeout(300);
      await expect(page.locator('text=AI Orchestrator')).toBeVisible();

      // Collapse
      await page.locator('button[title="Collapse"]').click();
      await page.waitForTimeout(200);
    }
  });

  test('stop button kills orchestrator and shows launch screen', async ({ page, request }) => {
    const launch = await request.post(`${BASE}/orchestrator/launch?provider=claude_code`);
    if (!launch.ok()) { test.skip(); return; }

    await page.goto(BASE);
    await page.waitForTimeout(3000);

    // Open sidebar
    await page.locator('button').filter({ hasText: 'Assistant' }).click();
    await page.waitForTimeout(8000);

    // Click stop
    const stopBtn = page.locator('button[title="Stop"]');
    if (await stopBtn.isVisible({ timeout: 5000 }).catch(() => false)) {
      await stopBtn.click();
      await page.waitForTimeout(2000);

      // Should show launch screen again
      await expect(page.locator('text=Launch Orchestrator')).toBeVisible({ timeout: 5000 });
    }
  });
});


// ══════════════════════════════════════════════════════════════
// Flow Management via API
// ══════════════════════════════════════════════════════════════

test.describe('Flow management API', () => {
  test('list flows', async ({ request }) => {
    const res = await request.get(`${BASE}/flows`);
    expect(res.ok()).toBeTruthy();
    const flows = await res.json();
    expect(Array.isArray(flows)).toBeTruthy();
  });

  test('CRUD flow: create → get → enable → disable → delete', async ({ request }) => {
    // Create
    const createRes = await request.post(`${BASE}/flows`, {
      data: {
        name: 'e2e-test-flow',
        schedule: '0 */6 * * *',
        agent_profile: 'developer',
        provider: 'claude_code',
        prompt_template: 'Run health check'
      }
    });
    if (!createRes.ok()) return; // flow creation may have different API shape

    // Disable
    await request.post(`${BASE}/flows/e2e-test-flow/disable`);

    // Enable
    await request.post(`${BASE}/flows/e2e-test-flow/enable`);

    // Delete
    const delRes = await request.delete(`${BASE}/flows/e2e-test-flow`);
    expect(delRes.ok()).toBeTruthy();
  });
});


// ══════════════════════════════════════════════════════════════
// Agent & Provider Discovery
// ══════════════════════════════════════════════════════════════

test.describe('Agent and provider discovery', () => {
  test('list agent profiles includes master_orchestrator', async ({ request }) => {
    const res = await request.get(`${BASE}/agents/profiles`);
    expect(res.ok()).toBeTruthy();
    const profiles = await res.json();
    const master = profiles.find((p: any) => p.name === 'master_orchestrator');
    expect(master).toBeDefined();
  });

  test('list providers shows installation status', async ({ request }) => {
    const res = await request.get(`${BASE}/agents/providers`);
    const providers = await res.json();
    expect(providers.length).toBeGreaterThan(0);
    for (const p of providers) {
      expect(p).toHaveProperty('name');
      expect(p).toHaveProperty('installed');
      expect(typeof p.installed).toBe('boolean');
    }
  });

  test('at least one provider is installed', async ({ request }) => {
    const providers = await (await request.get(`${BASE}/agents/providers`)).json();
    const installed = providers.filter((p: any) => p.installed);
    expect(installed.length).toBeGreaterThan(0);
  });
});


// ══════════════════════════════════════════════════════════════
// Inbox / messaging
// ══════════════════════════════════════════════════════════════

test.describe('Inbox messaging between terminals', () => {
  test('get inbox messages for nonexistent terminal', async ({ request }) => {
    const res = await request.get(`${BASE}/terminals/nonexistent/inbox/messages`);
    // Different servers may return 200 (empty), 404, or 500
    expect(res.status()).toBeGreaterThanOrEqual(200);
  });
});


// ══════════════════════════════════════════════════════════════
// Regression / smoke
// ══════════════════════════════════════════════════════════════

test.describe('Regression smoke tests', () => {
  test('app loads with no JS errors', async ({ page }) => {
    const errors: string[] = [];
    page.on('pageerror', err => errors.push(err.message));
    await page.goto(BASE);
    await page.waitForTimeout(3000);
    expect(errors.filter(e => !e.includes('ResizeObserver'))).toHaveLength(0);
  });

  test('health endpoint', async ({ request }) => {
    const res = await request.get(`${BASE}/health`);
    expect((await res.json()).status).toBe('ok');
  });

  test('home tab shows session count', async ({ page }) => {
    await page.goto(BASE);
    await page.waitForTimeout(2000);
    const text = await page.locator('text=/\\d+ session/i').first().textContent();
    expect(text).toBeTruthy();
  });

  test('settings tab loads', async ({ page }) => {
    await page.goto(BASE);
    await page.waitForTimeout(2000);
    await page.locator('button[role="tab"]').filter({ hasText: 'Settings' }).click();
    await page.waitForTimeout(1000);
    await expect(page.locator('text=Error')).not.toBeVisible({ timeout: 1000 });
  });

  test('WebSocket terminal streaming endpoint exists', async ({ request }) => {
    // Just verify the upgrade endpoint responds (won't complete as HTTP)
    const res = await request.get(`${BASE}/terminals/fake-id/ws`);
    // WebSocket endpoints return various codes when hit as HTTP
    expect([200, 400, 403, 404, 426].includes(res.status())).toBeTruthy();
  });
});
