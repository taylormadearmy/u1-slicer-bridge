import { test, expect } from '@playwright/test';
import { waitForApp, uploadFile, waitForSliceComplete, getAppState, API, apiUpload, apiSlice } from './helpers';

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
    await expect(page.getByRole('heading', { name: /G-code Ready/i })).toBeVisible();
  });

  test('completed slice shows metadata', async ({ page }) => {
    await uploadFile(page, 'calib-cube-10-dual-colour-merged.3mf');
    await page.getByRole('button', { name: /Slice Now/i }).click();
    await waitForSliceComplete(page);

    // Should show summary info in the complete step
    // Scope to the visible complete section to avoid matching sliced-files list items
    const heading = page.getByRole('heading', { name: /G-code Ready/i });
    await expect(heading).toBeVisible();
    // The summary stats are siblings near the heading — check via the visible step
    await expect(page.getByText('Time', { exact: true }).first()).toBeVisible();
    await expect(page.getByText('Layers', { exact: true }).first()).toBeVisible();
    await expect(page.getByText('Size', { exact: true }).first()).toBeVisible();
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

  test('home button returns to upload step from complete', async ({ page }) => {
    await uploadFile(page, 'calib-cube-10-dual-colour-merged.3mf');
    await page.getByRole('button', { name: /Slice Now/i }).click();
    await waitForSliceComplete(page);

    await page.getByTitle('Back to home').click();
    await page.getByTestId('confirm-ok').click();
    const step = await getAppState(page, 'currentStep');
    expect(step).toBe('upload');
  });

  test('completed slice appears in My Files modal', async ({ page }) => {
    await uploadFile(page, 'calib-cube-10-dual-colour-merged.3mf');
    await page.getByRole('button', { name: /Slice Now/i }).click();
    await waitForSliceComplete(page);

    // Go back to upload step via home button
    await page.getByTitle('Back to home').click();
    await page.getByTestId('confirm-ok').click();

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
    await expect(page.getByRole('heading', { name: /G-code Ready/i })).toBeVisible();

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

  test('Bambu file with modifier parts slices without crash', async ({ request }) => {
    // Regression: Bambu 3MFs with modifier parts (type="other" objects) caused
    // trimesh to duplicate geometry, and the multi-file component reference
    // format triggered segfaults in Orca Slicer.
    const upload = await apiUpload(request, 'u1-auxiliary-fan-cover-hex_mw.3mf');

    const job = await apiSlice(request, upload.upload_id);
    expect(job.status).toBe('completed');
    expect(job.metadata.layer_count).toBeGreaterThan(0);
  });

  test('single-color Bambu file slices via browser UI', async ({ page }) => {
    // Regression: single-color Bambu files entered multicolor mode in the UI
    // when extruder presets were configured, sending 2 filament_ids and causing
    // Orca segfault. The UI must use single-filament mode for 1-color files.
    await uploadFile(page, 'u1-auxiliary-fan-cover-hex_mw.3mf');

    // Verify single-filament mode (selectedFilament set, not selectedFilaments)
    const selectedFilament = await getAppState(page, 'selectedFilament');
    const selectedFilaments = await getAppState(page, 'selectedFilaments');
    expect(selectedFilament).toBeTruthy();
    expect(selectedFilaments.length).toBe(0);

    // Slice via UI — exercises the full browser filament selection path
    await page.getByRole('button', { name: /Slice Now/i }).click();
    await waitForSliceComplete(page);
    await expect(page.getByRole('heading', { name: /G-code Ready/i })).toBeVisible();
  });
});
