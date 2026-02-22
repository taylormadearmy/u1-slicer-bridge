import { test, expect } from '@playwright/test';
import {
  API,
  apiUpload,
  apiSlicePlate,
  getDefaultFilament,
  API_SLICE_REQUEST_TIMEOUT_MS,
  GENERIC_API_TIMEOUT_MS,
  SLOW_TEST_TIMEOUT_MS,
} from './helpers';

test.describe('Plate-Specific Slicing (M7.1)', () => {
  test.setTimeout(SLOW_TEST_TIMEOUT_MS);

  test('slice-plate returns completed job with metadata', async ({ request }) => {
    // Upload multi-plate file
    const upload = await apiUpload(request, 'Dragon Scale infinity.3mf');
    expect(upload.is_multi_plate).toBe(true);

    // Get plates
    const platesRes = await request.get(`${API}/uploads/${upload.upload_id}/plates`, { timeout: GENERIC_API_TIMEOUT_MS });
    const plates = (await platesRes.json()).plates;
    expect(plates.length).toBeGreaterThan(1);

    // Pick first valid plate
    const plate = plates.find((p: any) => p.validation?.fits) || plates[0];

    // Slice it
    const job = await apiSlicePlate(request, upload.upload_id, plate.plate_id);
    expect(job.status).toBe('completed');
    expect(job).toHaveProperty('gcode_size_mb');
    expect(job).toHaveProperty('metadata');
    expect(job.metadata.layer_count).toBeGreaterThan(0);
  });

  test('slice-plate on non-multi-plate file returns 400', async ({ request }) => {
    const upload = await apiUpload(request, 'calib-cube-10-dual-colour-merged.3mf');
    const fil = await getDefaultFilament(request);

    const res = await request.post(`${API}/uploads/${upload.upload_id}/slice-plate`, {
      data: {
        plate_id: 1,
        filament_id: fil.id,
        layer_height: 0.2,
        infill_density: 15,
        supports: false,
      },
      timeout: GENERIC_API_TIMEOUT_MS,
    });
    expect(res.status()).toBe(400);
    const body = await res.json();
    expect(body.detail).toContain('Not a multi-plate file');
  });

  test('slice-plate with invalid plate_id returns 404', async ({ request }) => {
    const upload = await apiUpload(request, 'Dragon Scale infinity.3mf');
    const fil = await getDefaultFilament(request);

    const res = await request.post(`${API}/uploads/${upload.upload_id}/slice-plate`, {
      data: {
        plate_id: 9999,
        filament_id: fil.id,
        layer_height: 0.2,
        infill_density: 15,
        supports: false,
      },
      timeout: GENERIC_API_TIMEOUT_MS,
    });
    expect(res.status()).toBe(404);
    const body = await res.json();
    expect(body.detail).toContain('Plate 9999 not found');
  });

  test('plate preview endpoint returns image or 404', async ({ request }) => {
    const upload = await apiUpload(request, 'Dragon Scale infinity.3mf');

    const platesRes = await request.get(`${API}/uploads/${upload.upload_id}/plates`, { timeout: GENERIC_API_TIMEOUT_MS });
    const plates = (await platesRes.json()).plates;
    const plate = plates[0];

    const previewRes = await request.get(
      `${API}/uploads/${upload.upload_id}/plates/${plate.plate_id}/preview`,
      { timeout: GENERIC_API_TIMEOUT_MS },
    );
    expect([200, 404]).toContain(previewRes.status());
    if (previewRes.status() === 200) {
      const ct = previewRes.headers()['content-type'];
      expect(ct).toMatch(/image/);
    }
  });
});
