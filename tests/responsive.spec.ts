import { test, expect } from '@playwright/test';

test.describe('Responsive Design', () => {
  const viewports = [
    { name: 'desktop', width: 1920, height: 1080 },
    { name: 'tablet', width: 768, height: 1024 },
    { name: 'mobile', width: 375, height: 667 },
  ];

  for (const vp of viewports) {
    test(`renders correctly at ${vp.name} (${vp.width}x${vp.height})`, async ({ page }) => {
      await page.setViewportSize({ width: vp.width, height: vp.height });
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // Header should be visible at all sizes
      const header = page.locator('h1');
      await expect(header).toBeVisible();

      // Upload dropzone should be visible
      const dropzone = page.locator('[x-ref="dropzone"]');
      await expect(dropzone).toBeVisible();

      // Settings gear icon should be visible in header
      await expect(page.getByTitle('Settings')).toBeVisible();
    });
  }
});
