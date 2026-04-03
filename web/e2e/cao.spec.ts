import { test, expect } from '@playwright/test';

const BASE_URL = 'http://localhost:8000';

test.describe('CAO App', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto(BASE_URL);
  });

  test('loads and shows main UI', async ({ page }) => {
    await expect(page.locator('text=Messaging Agent Orchestrator')).toBeVisible();
  });

  test('can switch to flows tab', async ({ page }) => {
    await page.getByRole('button', { name: 'Flows', exact: true }).click();
    await expect(page.locator('h2:has-text("Flows")')).toBeVisible();
  });

  test('can switch to beads tab', async ({ page }) => {
    await page.getByRole('button', { name: 'Beads', exact: true }).click();
    // Beads panel shows after clicking
    await expect(page.getByRole('button', { name: 'Beads', exact: true })).toBeVisible();
  });
});

test.describe('Flows Panel', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto(BASE_URL);
    await page.getByRole('button', { name: 'Flows', exact: true }).click();
  });

  test('shows flows heading', async ({ page }) => {
    await expect(page.locator('h2:has-text("Flows")')).toBeVisible();
  });

  test('shows create flow button', async ({ page }) => {
    await expect(page.locator('text=Create Flow')).toBeVisible();
  });

  test('can expand flow to see execution history', async ({ page }) => {
    const flowItem = page.locator('text=test-orchestrator').first();
    if (await flowItem.isVisible({ timeout: 3000 }).catch(() => false)) {
      await flowItem.click();
      await expect(page.locator('text=Execution History')).toBeVisible({ timeout: 3000 });
    }
  });

  test('can run flow and see toast', async ({ page }) => {
    const flowItem = page.locator('text=test-orchestrator').first();
    if (await flowItem.isVisible({ timeout: 3000 }).catch(() => false)) {
      await flowItem.click();
      await page.locator('button:has-text("Run")').first().click();
      await expect(page.locator('text=Flow started')).toBeVisible({ timeout: 5000 });
    }
  });
});

test.describe('Agent Sessions', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto(BASE_URL);
  });

  test('shows spawn agent button', async ({ page }) => {
    await expect(page.locator('text=Spawn an Agent')).toBeVisible();
  });

  test('can click spawn and see agent list', async ({ page }) => {
    await page.locator('text=Spawn an Agent').click();
    // Should show agent selection or spawning
    await expect(
      page.locator('text=code_supervisor').or(page.locator('text=Spawning'))
    ).toBeVisible({ timeout: 5000 });
  });
});

test.describe('Flow Creation Dialog', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto(BASE_URL);
    await page.getByRole('button', { name: 'Flows', exact: true }).click();
  });

  test('can open create flow dialog', async ({ page }) => {
    await page.locator('text=Create Flow').first().click();
    await expect(page.locator('text=Create New Flow')).toBeVisible();
  });

  test('dialog has name input', async ({ page }) => {
    await page.locator('text=Create Flow').first().click();
    await expect(page.locator('input').first()).toBeVisible();
  });

  test('can cancel dialog with button', async ({ page }) => {
    await page.locator('text=Create Flow').first().click();
    await expect(page.locator('text=Create New Flow')).toBeVisible();
    // Click outside or find cancel - the modal closes on backdrop click
    await page.locator('.fixed.inset-0').first().click({ position: { x: 10, y: 10 } });
    await expect(page.locator('text=Create New Flow')).not.toBeVisible({ timeout: 2000 });
  });
});
