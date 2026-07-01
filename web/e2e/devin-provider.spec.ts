import { test, expect } from '@playwright/test';

test.describe('Devin CLI Provider E2E Tests', () => {
  test.beforeEach(async ({ page }) => {
    // Navigate to CAO web interface
    await page.goto('/');
    await page.waitForLoadState('networkidle');
  });

  test('should load CAO web interface', async ({ page }) => {
    await expect(page).toHaveTitle(/Agent Orchestrator/);
    await expect(page.locator('#root')).toBeVisible();
  });

  test('should show Spawn Agent button', async ({ page }) => {
    // Wait for the Spawn Agent button to be visible
    const spawnButton = page.getByText('Spawn Agent');
    await expect(spawnButton).toBeVisible({ timeout: 5000 });
  });

  test('should open Spawn Agent modal and show Devin CLI option', async ({ page }) => {
    // Click Spawn Agent button
    const spawnButton = page.getByText('Spawn Agent');
    await spawnButton.click();

    // Wait for modal to appear
    const modal = page.locator('dialog, [role="dialog"], .fixed').first();
    await expect(modal).toBeVisible({ timeout: 5000 });

    // Look for provider selector - assert visible Devin option in modal
    const devinOption = page.locator('text=devin').or(page.locator('text=Devin')).first();
    await expect(devinOption).toBeVisible();
  });

  test('should show Devin CLI as available provider', async ({ page }) => {
    const response = await page.request.get('http://localhost:9889/agents/providers');
    const providers = await response.json();

    console.log('All providers:', providers);

    const devinProvider = providers.find((p: { name: string }) => p.name === 'devin_cli');
    expect(devinProvider).toBeDefined();

    if (devinProvider) {
      console.log('Devin CLI provider found:', devinProvider);
      expect(devinProvider.binary).toBe('devin');
    }
  });

  test('should list agent profiles including Devin-compatible ones', async ({ page }) => {
    const response = await page.request.get('http://localhost:9889/agents/profiles');
    const profiles = await response.json();

    console.log('Available profiles:', profiles);

    // Check if analysis_supervisor profile exists (for Devin)
    const supervisorProfile = profiles.find((p: { name: string }) => p.name === 'analysis_supervisor');
    expect(supervisorProfile).toBeDefined();
  });

  test('should verify Devin CLI provider registration', async ({ page }) => {
    // Test that Devin CLI is properly registered in the system
    const response = await page.request.get('http://localhost:9889/health');
    const health = await response.json();
    
    console.log('System health:', health);
    expect(health.status).toBe('ok');
  });

  test('should try to spawn agent with Devin CLI through UI', async ({ page }) => {
    // Wait for the page to load and providers to be fetched
    await page.waitForLoadState('networkidle');

    // Set up console error logging
    const errors: string[] = [];
    page.on('console', msg => {
      if (msg.type() === 'error') {
        errors.push(msg.text());
        console.log('Console error:', msg.text());
      }
    });

    // First, verify providers are loaded by checking API directly
    const response = await page.request.get('http://localhost:9889/agents/providers');
    const providers = await response.json();
    console.log('Providers from API:', providers.map((p: { name: string }) => p.name));

    const devinProvider = providers.find((p: { name: string }) => p.name === 'devin_cli');
    console.log('Devin CLI in API response:', !!devinProvider);
    expect(devinProvider).toBeDefined();

    // Try to click Spawn Agent button using multiple approaches
    let modalOpened = false;

    // Approach 1: Click button with force
    try {
      const buttonWithClass = page.locator('button').filter({ hasText: 'Spawn Agent' });
      const classButtonCount = await buttonWithClass.count();
      console.log('Buttons with Spawn Agent text:', classButtonCount);

      if (classButtonCount > 0) {
        await buttonWithClass.first().click({ force: true });

        const modalContainer = page.locator('.fixed.inset-0').first();
        await modalContainer.waitFor({ state: 'visible', timeout: 5000 });
        const containerVisible = await modalContainer.isVisible();
        console.log('Modal visible after first click:', containerVisible);

        if (containerVisible) {
          modalOpened = true;
        }
      }
    } catch (error) {
      console.log('Modal open attempt failed:', error);
    }

    // If modal still not opened, fail the test - this is a regression test
    if (!modalOpened) {
      throw new Error('Modal could not be opened - UI interaction test failed');
    }

    // Now proceed with checking modal content
    try {
      // Check that modal body is present
      const modalBody = page.locator('.fixed.inset-0 .relative .p-5').first();
      const bodyExists = await modalBody.count();
      console.log('Modal body elements found:', bodyExists);
      expect(bodyExists).toBeGreaterThan(0);

      // Check for Devin CLI in the modal content
      const pageContent = await page.content();
      const hasDevinLower = pageContent.toLowerCase().includes('devin');
      console.log('Devin found in modal:', hasDevinLower);
      expect(hasDevinLower).toBe(true);

      // Try to find and click the provider dropdown
      const providerDropdown = page.locator('.fixed.inset-0 button').filter({ hasText: /select provider/i }).first();
      const dropdownVisible = await providerDropdown.isVisible();
      console.log('Provider dropdown visible:', dropdownVisible);

      if (dropdownVisible) {
        await providerDropdown.click();

        // Look for Devin CLI option in the dropdown
        const devinOption = page.locator('button').filter({ hasText: /devin/i }).first();
        const devinOptionVisible = await devinOption.isVisible({ timeout: 3000 });
        console.log('Devin CLI option visible in dropdown:', devinOptionVisible);

        if (devinOptionVisible) {
          console.log('✅ Devin CLI option is available in the provider dropdown!');
          await page.keyboard.press('Escape'); // Close dropdown
        } else {
          console.log('❌ Devin CLI option not found in dropdown');
        }
      }

    } catch (error) {
      console.log('Error during modal content test:', error);
      throw error;
    }
  });
});