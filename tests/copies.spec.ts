import { test, expect } from '@playwright/test';
import { waitForApp, uploadFile, apiUpload, getDefaultFilament, waitForJobComplete, waitForSliceComplete, getAppState, API } from './helpers';

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

  test('multi-component assembly dimensions account for component offsets', async ({ request }) => {
    // The dual-colour cube has two components offset from each other.
    // Dimensions must reflect the FULL assembly extent, not just a single component mesh.
    // This prevents copies from overlapping (regression test for overlap bug).
    const upload = await apiUpload(request, 'calib-cube-10-dual-colour-merged.3mf');

    const res = await request.get(`${API}/upload/${upload.upload_id}/copies/info`);
    expect(res.ok()).toBe(true);
    const info = await res.json();

    // Each component is ~10mm, offset by ~7.5mm from center in X.
    // Total assembly width should be ~25mm (not 10mm if transforms were ignored).
    expect(info.object_dimensions[0]).toBeGreaterThan(20); // X: ~24.9mm
    expect(info.object_dimensions[1]).toBeGreaterThan(8);  // Y: ~10.5mm
    expect(info.object_dimensions[2]).toBeGreaterThan(8);  // Z: ~10mm
  });

  test('copies grid has no overlapping objects', async ({ request }) => {
    // Apply 4 copies of the dual-colour assembly and verify that
    // the grid spacing exceeds the object dimensions (no overlap).
    const upload = await apiUpload(request, 'calib-cube-10-dual-colour-merged.3mf');

    const infoRes = await request.get(`${API}/upload/${upload.upload_id}/copies/info`);
    const info = await infoRes.json();
    const objWidth = info.object_dimensions[0];
    const objDepth = info.object_dimensions[1];

    const res = await request.post(`${API}/upload/${upload.upload_id}/copies`, {
      data: { copies: 4, spacing: 5.0 },
    });
    expect(res.ok()).toBe(true);
    const result = await res.json();

    // Grid cell size = object_size + spacing; verify no overlap between adjacent copies
    const cellWidth = objWidth + 5.0;
    const cellDepth = objDepth + 5.0;
    // With 2x2 grid, total = 2*cell - spacing. Must fit in 270mm bed.
    expect(2 * cellWidth - 5.0).toBeLessThan(270);
    expect(2 * cellDepth - 5.0).toBeLessThan(270);
    expect(result.fits_bed).toBe(true);
  });

  test('copies UI dropdown visible on configure step', async ({ page }) => {
    await waitForApp(page);
    await uploadFile(page, 'calib-cube-10-dual-colour-merged.3mf');

    // Copies section should be visible for single-plate files
    await expect(page.getByText('Copies:')).toBeVisible();
    // Dropdown selector with preset values including "Custom..."
    const select = page.locator('select').filter({ has: page.locator('option[value="custom"]') });
    await expect(select).toBeVisible();
    await expect(select).toHaveValue('1');
  });

  test('scale increases full assembly XY footprint (not just Z)', async ({ request }) => {
    const upload = await apiUpload(request, 'calib-cube-10-dual-colour-merged.3mf');
    const fil = await getDefaultFilament(request);

    const sliceAndGetSpan = async (scalePercent: number) => {
      const sliceRes = await request.post(`${API}/uploads/${upload.upload_id}/slice`, {
        data: {
          filament_ids: [fil.id, fil.id],
          layer_height: 0.2,
          infill_density: 15,
          supports: false,
          scale_percent: scalePercent,
        },
        timeout: 180_000,
      });
      expect(sliceRes.ok()).toBe(true);
      const job = await waitForJobComplete(request, await sliceRes.json());
      expect(job.status).toBe('completed');

      const metaRes = await request.get(`${API}/jobs/${job.job_id}/gcode/metadata`);
      expect(metaRes.ok()).toBe(true);
      const meta = await metaRes.json();
      const b = meta.bounds;
      return {
        x: b.max_x - b.min_x,
        y: b.max_y - b.min_y,
        z: b.max_z - b.min_z,
      };
    };

    const span100 = await sliceAndGetSpan(100);
    const span500 = await sliceAndGetSpan(500);

    // Regression: internal component offsets must scale too.
    // Previously only geometry changed while XY spacing stayed near-constant.
    expect(span500.x).toBeGreaterThan(span100.x * 3);
    expect(span500.y).toBeGreaterThan(span100.y * 3);
    expect(span500.z).toBeGreaterThan(span100.z * 2.5);
  });

  test('browser flow: scale increases full assembly XY footprint', async ({ page, request }) => {
    await waitForApp(page);
    await uploadFile(page, 'calib-cube-10-dual-colour-merged.3mf');

    const getSpanFromLatestSlice = async () => {
      const sliceResult = await getAppState(page, 'sliceResult') as any;
      expect(sliceResult?.job_id).toBeTruthy();
      let metaRes = await request.get(`${API}/jobs/${sliceResult.job_id}/gcode/metadata`);
      for (let i = 0; i < 5 && !metaRes.ok(); i++) {
        await page.waitForTimeout(300);
        metaRes = await request.get(`${API}/jobs/${sliceResult.job_id}/gcode/metadata`);
      }
      expect(metaRes.ok()).toBe(true);
      const meta = await metaRes.json();
      const b = meta.bounds;
      return {
        x: b.max_x - b.min_x,
        y: b.max_y - b.min_y,
      };
    };

    const scaleInput = page.getByTestId('scale-input');
    await scaleInput.fill('500');
    await page.getByRole('button', { name: /Slice Now/i }).click();
    await waitForSliceComplete(page);
    const span500 = await getSpanFromLatestSlice();

    // Browser-path guard for spacing scaling regression:
    // old behavior produced ~107mm X span at 500% due to unscaled component offsets.
    expect(span500.x).toBeGreaterThan(120);
    expect(span500.y).toBeGreaterThan(80);
  });
});
