import { test, expect } from '@playwright/test';
import { waitForApp, uploadFile, waitForSliceComplete, getAppState, API, apiUpload } from './helpers';

test.describe('Slicing Workflow', () => {
  test.setTimeout(180_000);
  test.beforeEach(async ({ page }) => {
    await waitForApp(page);
  });

  test('single-filament slice completes end-to-end', async ({ page }) => {
    await uploadFile(page, 'calib-cube-10-dual-colour-merged.3mf');

    // Click Slice Now
    await page.getByRole('button', { name: /Slice Now/i }).click();

    // Should enter slicing or complete state (fast slices may skip the slicing step)
    await page.waitForFunction(() => {
      const body = document.querySelector('body') as any;
      if (body?._x_dataStack) {
        for (const scope of body._x_dataStack) {
          if ('currentStep' in scope) {
            return scope.currentStep === 'slicing' || scope.currentStep === 'complete';
          }
        }
      }
      return false;
    }, undefined, { timeout: 5_000 });

    // Wait for completion
    await waitForSliceComplete(page);

    // Verify complete step
    await expect(page.getByRole('heading', { name: /G-code is Ready/i })).toBeVisible();
  });

  test('completed slice shows metadata', async ({ page }) => {
    await uploadFile(page, 'calib-cube-10-dual-colour-merged.3mf');
    await page.getByRole('button', { name: /Slice Now/i }).click();
    await waitForSliceComplete(page);

    // Should show summary info in the complete step
    // Scope to the visible complete section to avoid matching sliced-files list items
    const heading = page.getByRole('heading', { name: /G-code is Ready/i });
    await expect(heading).toBeVisible();
    // The summary stats are siblings near the heading — check via the visible step
    await expect(page.getByText('Estimated Time', { exact: true }).first()).toBeVisible();
    await expect(page.getByText('Layers', { exact: true }).first()).toBeVisible();
    await expect(page.getByText('File Size', { exact: true }).first()).toBeVisible();
  });

  test('completed slice has download link', async ({ page }) => {
    await uploadFile(page, 'calib-cube-10-dual-colour-merged.3mf');
    await page.getByRole('button', { name: /Slice Now/i }).click();
    await waitForSliceComplete(page);

    const downloadLink = page.getByRole('link', { name: /Download G-code/i }).first();
    await expect(downloadLink).toBeVisible();
    const href = await downloadLink.getAttribute('href');
    expect(href).toContain('/download');
  });

  test('Start New Slice returns to upload step', async ({ page }) => {
    await uploadFile(page, 'calib-cube-10-dual-colour-merged.3mf');
    await page.getByRole('button', { name: /Slice Now/i }).click();
    await waitForSliceComplete(page);

    await page.getByRole('button', { name: /Start New Slice/i }).click();
    const step = await getAppState(page, 'currentStep');
    expect(step).toBe('upload');
  });

  test('completed slice appears in My Files modal', async ({ page }) => {
    await uploadFile(page, 'calib-cube-10-dual-colour-merged.3mf');
    await page.getByRole('button', { name: /Slice Now/i }).click();
    await waitForSliceComplete(page);

    // Go back to upload step
    await page.getByRole('button', { name: /Start New Slice/i }).click();

    // Open My Files modal — completed slices show as sub-entries under the upload
    await page.getByTitle('My Files').click();
    const modal = page.locator('[x-show="showStorageDrawer"]');
    await expect(modal.getByRole('heading', { name: 'My Files' })).toBeVisible();
    // The sliced job should appear (shows layer count or time)
    await expect(modal.getByText(/layers/).first()).toBeVisible();
  });

  test('G-code preview shows detected colors (not all white)', async ({ page }) => {
    await uploadFile(page, 'calib-cube-10-dual-colour-merged.3mf');
    await page.getByRole('button', { name: /Slice Now/i }).click();
    await waitForSliceComplete(page);

    // Verify we're on the complete step
    await expect(page.getByRole('heading', { name: /G-code is Ready/i })).toBeVisible();

    // Check sliceResult.filament_colors in Alpine state — should not be all #FFFFFF
    const filamentColors = await getAppState(page, 'sliceResult').then(
      (r: any) => r?.filament_colors || []
    );
    expect(filamentColors.length).toBeGreaterThan(0);
    const hasNonWhite = filamentColors.some(
      (c: string) => c.toUpperCase() !== '#FFFFFF'
    );
    expect(hasNonWhite).toBe(true);
  });

  test('slice via API returns job with metadata', async ({ request }) => {
    // Upload file (dual-colour — must send 2 filament_ids to avoid Orca segfault)
    const upload = await apiUpload(request, 'calib-cube-10-dual-colour-merged.3mf');

    // Get filaments (need two for dual-colour file)
    const filRes = await request.get(`${API}/filaments`, { timeout: 30_000 });
    const filaments = (await filRes.json()).filaments;
    const fil1 = filaments[0];
    const fil2 = filaments.length > 1 ? filaments[1] : filaments[0];

    // Slice with two filaments matching the file's colour count
    const sliceRes = await request.post(`${API}/uploads/${upload.upload_id}/slice`, {
      data: {
        filament_ids: [fil1.id, fil2.id],
        layer_height: 0.2,
        infill_density: 15,
        supports: false,
      },
      timeout: 120_000,
    });
    expect(sliceRes.ok()).toBe(true);
    const job = await sliceRes.json();
    expect(job).toHaveProperty('job_id');
    expect(job).toHaveProperty('status');

    // If synchronous completion
    if (job.status === 'completed') {
      expect(job).toHaveProperty('gcode_size_mb');
      expect(job).toHaveProperty('metadata');
      expect(job.metadata).toHaveProperty('layer_count');
    }
  });
});
