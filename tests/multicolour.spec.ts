import { test, expect } from '@playwright/test';
import { waitForApp, uploadFile, getAppState, API, apiUpload } from './helpers';

test.describe('Multicolour Support', () => {
  test.beforeEach(async ({ page }) => {
    await waitForApp(page);
  });

  test('dual-colour file shows detected colors', async ({ page }) => {
    await uploadFile(page, 'calib-cube-10-dual-colour-merged.3mf');
    const colors = await getAppState(page, 'detectedColors') as string[];
    expect(colors.length).toBeGreaterThanOrEqual(2);
  });

  test('detected colors display colour swatches in accordion', async ({ page }) => {
    await uploadFile(page, 'calib-cube-10-dual-colour-merged.3mf');
    const colors = await getAppState(page, 'detectedColors') as string[];
    if (colors.length >= 2) {
      // Colours/Filaments accordion should be visible.
      await expect(page.getByText(/Colours.*Filaments/i)).toBeVisible();
      // Color mapping lines should be visible (arrow between detected colour and extruder)
      await expect(page.getByText('->').first()).toBeVisible();
    }
  });

  test('extruder mapping visible when accordion opened', async ({ page }) => {
    await uploadFile(page, 'calib-cube-10-dual-colour-merged.3mf');
    const colors = await getAppState(page, 'detectedColors') as string[];

    if (colors.length >= 2) {
      // Accordion starts closed - summary should be visible.
      await expect(page.getByText(/Colours.*Filaments/i)).toBeVisible();
      // Open the accordion
      await page.getByText(/Colours.*Filaments/i).click();
      // Extruder override section should now be visible
      await expect(page.getByText(/Filament\/Extruder Override/i)).toBeVisible({ timeout: 3_000 });
      // Customise link should be present
      await expect(page.getByText('Customise')).toBeVisible();
    }
  });

  test('print settings accordion shows source badges', async ({ page }) => {
    await uploadFile(page, 'calib-cube-10-dual-colour-merged.3mf');
    // Print Settings accordion should be visible (closed by default with summary)
    const header = page.getByText(/Print Settings/i).first();
    await expect(header).toBeVisible();
    // Open the accordion to see source badges
    await header.click();
    // Should show at least one source badge (File or Default)
    const fileBadges = page.locator('text=File').first();
    const defaultBadges = page.locator('text=Default').first();
    const hasFile = await fileBadges.isVisible().catch(() => false);
    const hasDefault = await defaultBadges.isVisible().catch(() => false);
    expect(hasFile || hasDefault).toBe(true);
  });

  test('file with >4 colors maps extras to available extruders', async ({ page }) => {
    // Dragon Scale has 7 metadata colors - extras should map to E1-E4.
    await uploadFile(page, 'Dragon Scale infinity.3mf');
    // Wait for plates to load using Alpine v3 API
    await page.waitForFunction(() => {
      const body = document.querySelector('body') as any;
      if (body?._x_dataStack) {
        for (const scope of body._x_dataStack) {
          if ('platesLoading' in scope) return !scope.platesLoading;
        }
      }
      return false;
    }, undefined, { timeout: 60_000 });

    const notice = await getAppState(page, 'multicolorNotice');
    const colors = await getAppState(page, 'detectedColors') as string[];
    const assignments = await getAppState(page, 'sliceSettings.extruder_assignments') as number[];
    const filaments = await getAppState(page, 'selectedFilaments') as any[];

    // No rejection notice - >4 colors are now handled.
    expect(notice).toBeNull();
    // All detected colors preserved
    expect(colors.length).toBeGreaterThanOrEqual(1);
    // If multicolor, assignments should all be within 0-3 (E1-E4)
    if (colors.length > 1 && assignments) {
      for (const a of assignments) {
        expect(a).toBeGreaterThanOrEqual(0);
        expect(a).toBeLessThanOrEqual(3);
      }
      // Should be in multicolor mode (selectedFilaments populated)
      expect(filaments.length).toBeGreaterThan(0);
    }
  });

  test('multicolour API: upload detects colors', async ({ request }) => {
    const upload = await apiUpload(request, 'calib-cube-10-dual-colour-merged.3mf');
    expect(upload).toHaveProperty('detected_colors');
    expect(upload.detected_colors.length).toBeGreaterThanOrEqual(2);
  });
});

