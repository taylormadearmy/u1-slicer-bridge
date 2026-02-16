import { test, expect } from '@playwright/test';
import { waitForApp, getAppState } from './helpers';

test.describe('Smoke Tests', () => {
  test.beforeEach(async ({ page }) => {
    await waitForApp(page);
  });

  test('page loads with correct title', async ({ page }) => {
    await expect(page).toHaveTitle(/U1 Slicer Bridge/);
  });

  test('header is visible', async ({ page }) => {
    const header = page.locator('h1');
    await expect(header).toContainText('U1 Slicer Bridge');
  });

  test('Alpine.js app initializes without errors', async ({ page }) => {
    const errors: string[] = [];
    page.on('console', msg => {
      if (msg.type() === 'error') errors.push(msg.text());
    });
    // Give Alpine time to settle
    await page.waitForTimeout(2_000);
    // Filter out expected errors (e.g. moonraker offline)
    const unexpected = errors.filter(e =>
      !e.includes('printer') && !e.includes('moonraker') && !e.includes('404'));

    expect(unexpected).toHaveLength(0);
  });

  test('upload tab is active by default', async ({ page }) => {
    const tab = await getAppState(page, 'activeTab');
    expect(tab).toBe('upload');
  });

  test('upload dropzone is visible', async ({ page }) => {
    const dropzone = page.locator('[x-ref="dropzone"]');
    await expect(dropzone).toBeVisible();
  });

  test('file input accepts .3mf', async ({ page }) => {
    const input = page.locator('input[type="file"][accept=".3mf"]');
    await expect(input).toBeAttached();
  });

  test('printer status indicator is shown', async ({ page }) => {
    const status = page.locator('header').locator('text=/Checking|Connected|Offline|Error/i');
    await expect(status.first()).toBeVisible();
  });

  test('API health check passes from browser', async ({ page }) => {
    const health = await page.evaluate(async () => {
      const res = await fetch('/api/healthz');
      return res.json();
    });
    expect(health.status).toBe('ok');
  });

  test('settings modal opens and closes via gear icon', async ({ page }) => {
    // Open settings modal via gear icon
    await page.getByTitle('Settings').click();
    await expect(page.getByRole('heading', { name: 'Settings', exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Printer Defaults' })).toBeVisible();

    // Close settings modal via X button (scope to the visible modal)
    await page.getByTitle('Close').first().click();
    await expect(page.getByRole('heading', { name: 'Printer Defaults' })).not.toBeVisible();
    await expect(page.getByText('Upload 3MF File')).toBeVisible();
  });
});
