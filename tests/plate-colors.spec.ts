import { test, expect } from '@playwright/test';
import {
  waitForApp,
  uploadFile,
  waitForSliceComplete,
  getAppState,
  API,
  API_UPLOAD_TIMEOUT_MS,
  GENERIC_API_TIMEOUT_MS,
  UPLOAD_TRANSITION_TIMEOUT_MS,
  UPLOAD_LIST_TIMEOUT_MS,
  SLOW_TEST_TIMEOUT_MS,
  API_SLICE_REQUEST_TIMEOUT_MS,
} from './helpers';
import path from 'path';
import fs from 'fs';

test.describe('Per-Plate Color Detection', () => {
  test.setTimeout(SLOW_TEST_TIMEOUT_MS);

  test('Shashibo plates API returns correct color counts per plate', async ({ request }) => {
    // Upload with generous timeout for 4.8MB file
    const filePath = path.resolve(__dirname, '..', 'test-data', 'Shashibo-h2s-textured.3mf');
    const buffer = fs.readFileSync(filePath);
    const uploadRes = await request.post(`${API}/upload`, {
      multipart: {
        file: {
          name: 'Shashibo-h2s-textured.3mf',
          mimeType: 'application/octet-stream',
          buffer,
        },
      },
      timeout: API_UPLOAD_TIMEOUT_MS,
    });
    expect(uploadRes.ok()).toBe(true);
    const upload = await uploadRes.json();
    expect(upload.is_multi_plate).toBe(true);

    const platesRes = await request.get(`${API}/uploads/${upload.upload_id}/plates`, { timeout: GENERIC_API_TIMEOUT_MS });
    expect(platesRes.ok()).toBe(true);
    const data = await platesRes.json();

    // Build a map: plate_name -> detected_colors array
    const plateMap: Record<string, string[]> = {};
    for (const p of data.plates) {
      plateMap[p.plate_name] = p.detected_colors || [];
    }

    // Plate 1 "Small" - single color (yellow only)
    expect(plateMap['Small']?.length).toBe(1);

    // Plate 2 "Large" - single color
    expect(plateMap['Large']?.length).toBe(1);

    // Plate 3 "Small Dual Colour - filament swap" - 2 colors
    expect(plateMap['Small Dual Colour - filament swap']?.length).toBe(2);

    // Plate 4 "Large Dual Colour - filament swap" - 2 colors
    expect(plateMap['Large Dual Colour - filament swap']?.length).toBe(2);

    // Plate 5 "Small - H2D" - should be 2 colors (not 3)
    expect(plateMap['Small - H2D']?.length).toBe(2);

    // Plate 6 "Large - H2D" - should be 2 colors (not 1)
    expect(plateMap['Large - H2D']?.length).toBe(2);
  });

  test('Shashibo H2D plates show dual color in browser', async ({ page }) => {
    await waitForApp(page);

    // Upload via UI (file input) with extended timeout
    const filePath = path.resolve(__dirname, '..', 'test-data', 'Shashibo-h2s-textured.3mf');
    const fileInput = page.locator('input[type="file"][accept=".3mf,.stl"]');
    await fileInput.setInputFiles(filePath);

    // Wait for upload + parse to complete (large multi-plate file)
    await page.waitForFunction((expected) => {
      const body = document.querySelector('body') as any;
      if (body?._x_dataStack) {
        for (const scope of body._x_dataStack) {
          if ('currentStep' in scope) return scope.currentStep === expected;
        }
      }
      return false;
    }, 'configure', { timeout: UPLOAD_TRANSITION_TIMEOUT_MS });

    // Wait for plates to load
    await page.waitForFunction(() => {
      const body = document.querySelector('body') as any;
      if (body?._x_dataStack) {
        for (const scope of body._x_dataStack) {
          if ('plates' in scope) return scope.plates?.length > 0 && !scope.platesLoading;
        }
      }
      return false;
    }, undefined, { timeout: UPLOAD_LIST_TIMEOUT_MS });

    // Get all plate cards from state
    const plates = await getAppState(page, 'plates') as any[];
    expect(plates.length).toBe(6);

    // "Large - H2D" (plate 6) should show 2 detected_colors
    const largeH2D = plates.find((p: any) => p.plate_name === 'Large - H2D');
    expect(largeH2D).toBeTruthy();
    expect(largeH2D.detected_colors?.length).toBe(2);

    // "Small - H2D" (plate 5) should show 2 detected_colors
    const smallH2D = plates.find((p: any) => p.plate_name === 'Small - H2D');
    expect(smallH2D).toBeTruthy();
    expect(smallH2D.detected_colors?.length).toBe(2);
  });
});

test.describe('Slice Progress Animation', () => {
  test.setTimeout(SLOW_TEST_TIMEOUT_MS);

  test('progress increments during slicing (not stuck at 0%)', async ({ page }) => {
    await waitForApp(page);
    await uploadFile(page, 'calib-cube-10-dual-colour-merged.3mf');

    // Click Slice Now
    await page.getByRole('button', { name: /Slice Now/i }).click();

    // Wait for slicing step to appear
    await expect(page.getByText(/Slicing Your Print/i)).toBeVisible({ timeout: 5_000 });

    // Collect progress samples over 8 seconds
    const samples: number[] = [];
    for (let i = 0; i < 8; i++) {
      await page.waitForTimeout(1_000);
      const progress = await getAppState(page, 'sliceProgress') as number;
      samples.push(progress);
    }

    // At least one sample should be > 0 (proves progress animates)
    const maxProgress = Math.max(...samples);
    expect(maxProgress).toBeGreaterThan(0);

    // Progress should increase over time (not all the same value)
    const uniqueValues = new Set(samples);
    expect(uniqueValues.size).toBeGreaterThan(1);

    // Wait for slice to complete
    await waitForSliceComplete(page);
    const finalProgress = await getAppState(page, 'sliceProgress') as number;
    expect(finalProgress).toBe(100);
  });
});

test.describe('Filament Colors in Slice Response', () => {
  test.setTimeout(SLOW_TEST_TIMEOUT_MS);

  test('slice response includes non-white filament_colors when colors are sent', async ({ request }) => {
    const filePath = path.resolve(__dirname, '..', 'test-data', 'calib-cube-10-dual-colour-merged.3mf');
    const buffer = fs.readFileSync(filePath);
    const uploadRes = await request.post(`${API}/upload`, {
      multipart: {
        file: {
          name: 'calib-cube-10-dual-colour-merged.3mf',
          mimeType: 'application/octet-stream',
          buffer,
        },
      },
      timeout: API_UPLOAD_TIMEOUT_MS,
    });
    expect(uploadRes.ok()).toBe(true);
    const upload = await uploadRes.json();

    // Get filament list
    const filRes = await request.get(`${API}/filaments`, { timeout: 30_000 });
    const filaments = (await filRes.json()).filaments;
    const fil = filaments[0];

    // Slice with explicit colors
    const sliceRes = await request.post(`${API}/uploads/${upload.upload_id}/slice`, {
      data: {
        filament_ids: [fil.id, fil.id],
        filament_colors: ['#FF0000', '#00FF00'],
        layer_height: 0.2,
        infill_density: 15,
        supports: false,
      },
      timeout: API_SLICE_REQUEST_TIMEOUT_MS,
    });
    expect(sliceRes.ok()).toBe(true);
    const job = await sliceRes.json();

    // Response should echo back the custom colors
    expect(job.filament_colors).toBeTruthy();
    expect(job.filament_colors).toContain('#FF0000');
    expect(job.filament_colors).toContain('#00FF00');

    // Stored job should also have the colors
    const jobRes = await request.get(`${API}/jobs/${job.job_id}`, { timeout: GENERIC_API_TIMEOUT_MS });
    const stored = await jobRes.json();
    expect(stored.filament_colors).toBeTruthy();
    expect(stored.filament_colors.length).toBeGreaterThanOrEqual(2);
    // Stored colors should include our overrides
    expect(stored.filament_colors[0]).toBe('#FF0000');
    expect(stored.filament_colors[1]).toBe('#00FF00');
  });

  test('all-white filament_colors falls back to detected_colors', async ({ request }) => {
    // Upload a dual-color file that has non-white detected colors
    const filePath = path.resolve(__dirname, '..', 'test-data', 'calib-cube-10-dual-colour-merged.3mf');
    const buffer = fs.readFileSync(filePath);
    const uploadRes = await request.post(`${API}/upload`, {
      multipart: {
        file: {
          name: 'calib-cube-10-dual-colour-merged.3mf',
          mimeType: 'application/octet-stream',
          buffer,
        },
      },
      timeout: API_UPLOAD_TIMEOUT_MS,
    });
    expect(uploadRes.ok()).toBe(true);
    const upload = await uploadRes.json();
    // The file should have detected colors
    expect(upload.detected_colors?.length).toBeGreaterThan(0);

    const filRes = await request.get(`${API}/filaments`, { timeout: 30_000 });
    const filaments = (await filRes.json()).filaments;
    const fil = filaments[0];

    // Slice with all-#FFFFFF colors (simulating the old bug)
    const sliceRes = await request.post(`${API}/uploads/${upload.upload_id}/slice`, {
      data: {
        filament_ids: [fil.id, fil.id],
        filament_colors: ['#FFFFFF', '#FFFFFF'],
        layer_height: 0.2,
        infill_density: 15,
        supports: false,
      },
      timeout: API_SLICE_REQUEST_TIMEOUT_MS,
    });
    expect(sliceRes.ok()).toBe(true);
    const job = await sliceRes.json();

    // Backend should fall back to detected_colors, NOT echo back #FFFFFF
    expect(job.filament_colors).toBeTruthy();
    const hasNonWhite = job.filament_colors.some(
      (c: string) => c.toUpperCase() !== '#FFFFFF'
    );
    expect(hasNonWhite).toBe(true);
  });
});
