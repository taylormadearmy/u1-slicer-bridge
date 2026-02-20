import { test, expect } from '@playwright/test';
import { waitForApp, uploadFile, apiUpload, getDefaultFilament, waitForJobComplete, API } from './helpers';

test.describe('Multiple Copies (M32)', () => {
  test.setTimeout(180_000);

  test('copies info returns object dimensions and max estimate', async ({ request }) => {
    const upload = await apiUpload(request, 'calib-cube-10-dual-colour-merged.3mf');

    const res = await request.get(`${API}/upload/${upload.upload_id}/copies/info`);
    expect(res.ok()).toBe(true);

    const info = await res.json();
    expect(info.object_dimensions).toBeDefined();
    expect(info.object_dimensions.length).toBe(3);
    // Calib cube should be roughly 10mm each side
    expect(info.object_dimensions[0]).toBeGreaterThan(5);
    expect(info.object_dimensions[0]).toBeLessThan(50);
    expect(info.max_copies).toBeGreaterThan(10);
    expect(info.current_copies).toBe(1);
  });

  test('apply 4 copies creates grid layout', async ({ request }) => {
    const upload = await apiUpload(request, 'calib-cube-10-dual-colour-merged.3mf');

    const res = await request.post(`${API}/upload/${upload.upload_id}/copies`, {
      data: { copies: 4, spacing: 5.0 },
    });
    expect(res.ok()).toBe(true);

    const result = await res.json();
    expect(result.copies).toBe(4);
    expect(result.cols).toBe(2);
    expect(result.rows).toBe(2);
    expect(result.fits_bed).toBe(true);
    expect(result.max_copies).toBeGreaterThan(10);
    expect(result.object_dimensions).toBeDefined();
  });

  test('reset copies reverts to 1', async ({ request }) => {
    const upload = await apiUpload(request, 'calib-cube-10-dual-colour-merged.3mf');

    // Apply copies
    await request.post(`${API}/upload/${upload.upload_id}/copies`, {
      data: { copies: 4, spacing: 5.0 },
    });

    // Reset
    const res = await request.delete(`${API}/upload/${upload.upload_id}/copies`);
    expect(res.ok()).toBe(true);
    const result = await res.json();
    expect(result.copies).toBe(1);

    // Verify via info endpoint
    const infoRes = await request.get(`${API}/upload/${upload.upload_id}/copies/info`);
    const info = await infoRes.json();
    expect(info.current_copies).toBe(1);
  });

  test('single copy just copies file without changes', async ({ request }) => {
    const upload = await apiUpload(request, 'calib-cube-10-dual-colour-merged.3mf');

    const res = await request.post(`${API}/upload/${upload.upload_id}/copies`, {
      data: { copies: 1, spacing: 5.0 },
    });
    expect(res.ok()).toBe(true);
    const result = await res.json();
    expect(result.copies).toBe(1);
    expect(result.fits_bed).toBe(true);
  });

  test('reject invalid copy counts', async ({ request }) => {
    const upload = await apiUpload(request, 'calib-cube-10-dual-colour-merged.3mf');

    // Zero copies
    const res0 = await request.post(`${API}/upload/${upload.upload_id}/copies`, {
      data: { copies: 0 },
    });
    expect(res0.ok()).toBe(false);

    // Over 100
    const res101 = await request.post(`${API}/upload/${upload.upload_id}/copies`, {
      data: { copies: 101 },
    });
    expect(res101.ok()).toBe(false);
  });

  test('slice with copies produces valid G-code', async ({ request }) => {
    const upload = await apiUpload(request, 'calib-cube-10-dual-colour-merged.3mf');
    const fil = await getDefaultFilament(request);

    // Apply 2 copies
    const copiesRes = await request.post(`${API}/upload/${upload.upload_id}/copies`, {
      data: { copies: 2, spacing: 10.0 },
    });
    expect(copiesRes.ok()).toBe(true);

    // Slice (dual-colour file needs 2 filament_ids)
    const sliceRes = await request.post(`${API}/uploads/${upload.upload_id}/slice`, {
      data: {
        filament_ids: [fil.id, fil.id],
        layer_height: 0.2,
        infill_density: 15,
        supports: false,
      },
      timeout: 120_000,
    });
    expect(sliceRes.ok()).toBe(true);
    const job = await waitForJobComplete(request, await sliceRes.json());
    expect(job.status).toBe('completed');
    expect(job.metadata?.layer_count).toBeGreaterThan(0);
  });

  test('copies UI buttons visible on configure step', async ({ page }) => {
    await waitForApp(page);
    await uploadFile(page, 'calib-cube-10-dual-colour-merged.3mf');

    // Copies section should be visible for single-plate files
    await expect(page.getByText('Copies:')).toBeVisible();
    // Quick-select buttons
    await expect(page.getByRole('button', { name: '1', exact: true }).first()).toBeVisible();
    await expect(page.getByRole('button', { name: '4', exact: true }).first()).toBeVisible();
  });
});
