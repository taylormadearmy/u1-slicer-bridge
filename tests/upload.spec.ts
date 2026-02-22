import { test, expect } from '@playwright/test';
import { waitForApp, uploadFile, getAppState, fixture } from './helpers';

test.describe('Upload Workflow', () => {
  test.beforeEach(async ({ page }) => {
    await waitForApp(page);
  });

  test('service worker bypasses non-GET requests (upload stall regression)', async ({ page }) => {
    const swResponse = await page.request.get('/sw.js');
    expect(swResponse.ok()).toBe(true);
    const swText = await swResponse.text();
    expect(swText).toContain("if (req.method !== 'GET') return;");
  });

  test('uploading a single-plate 3MF reaches configure step', async ({ page }) => {
    await uploadFile(page, 'calib-cube-10-dual-colour-merged.3mf');
    const step = await getAppState(page, 'currentStep');
    expect(step).toBe('configure');
    await expect(page.getByRole('heading', { name: 'Configure Print Settings' })).toBeVisible();
  });

  test('uploaded file appears in My Files modal', async ({ page }) => {
    await uploadFile(page, 'calib-cube-10-dual-colour-merged.3mf');
    // Go back to upload step via back arrow
    await page.getByTitle('Back to upload').click();
    await page.getByTestId('confirm-ok').click();
    // Open My Files modal and scope searches within it
    await page.getByTitle('My Files').click();
    const modal = page.locator('[x-show="showStorageDrawer"]');
    await expect(modal.getByRole('heading', { name: 'My Files' })).toBeVisible();
    await expect(modal.getByText('calib-cube-10-dual-colour-merged.3mf').first()).toBeVisible();
  });

  test('configure step shows filament selection', async ({ page }) => {
    await uploadFile(page, 'calib-cube-10-dual-colour-merged.3mf');
    // Should see either detected colors or filament dropdown
    // Should see the Colours/Filaments accordion.
    await expect(page.getByText(/Colours.*Filaments/i)).toBeVisible();
  });

  test('configure step shows Slice Now button', async ({ page }) => {
    await uploadFile(page, 'calib-cube-10-dual-colour-merged.3mf');
    await expect(page.getByRole('button', { name: /Slice Now/i })).toBeVisible();
  });

  test('back arrow returns to upload step', async ({ page }) => {
    await uploadFile(page, 'calib-cube-10-dual-colour-merged.3mf');
    await page.getByTitle('Back to upload').click();
    await page.getByTestId('confirm-ok').click();
    const step = await getAppState(page, 'currentStep');
    expect(step).toBe('upload');
  });

  test('customize mode preserves detected colors (not all white)', async ({ page }) => {
    await uploadFile(page, 'calib-cube-10-dual-colour-merged.3mf');

    // File has 2 detected colors - verify they are loaded.
    const detectedColors = await getAppState(page, 'detectedColors') as string[];
    expect(detectedColors?.length).toBeGreaterThan(0);

    // Expand the Colours/Filaments accordion.
    await page.getByText(/Colours.*Filaments/i).click();
    // Click Customise button to toggle filament override mode
    await page.getByRole('button', { name: 'Customise' }).click();

    // filament_colors in sliceSettings should not be all #FFFFFF
    const sliceSettings = await getAppState(page, 'sliceSettings') as any;
    const colors = sliceSettings?.filament_colors || [];
    expect(colors.length).toBeGreaterThan(0);
    const hasNonWhite = colors.some(
      (c: string) => c.toUpperCase() !== '#FFFFFF'
    );
    expect(hasNonWhite).toBe(true);
  });

  test('selecting an existing upload from My Files goes to configure', async ({ page }) => {
    // First ensure there's at least one upload
    await uploadFile(page, 'calib-cube-10-dual-colour-merged.3mf');
    await page.getByTitle('Back to upload').click();
    await page.getByTestId('confirm-ok').click();

    // Open My Files modal and click Slice on the first upload
    await page.getByTitle('My Files').click();
    const modal = page.locator('[x-show="showStorageDrawer"]');
    await expect(modal.getByRole('heading', { name: 'My Files' })).toBeVisible();
    await modal.getByRole('button', { name: 'Slice' }).first().click();
    await page.waitForTimeout(1_000);
    const step = await getAppState(page, 'currentStep');
    expect(step).toBe('configure');
  });
});

