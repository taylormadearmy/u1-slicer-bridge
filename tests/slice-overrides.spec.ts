import { test, expect } from '@playwright/test';
import { API, apiUpload, getDefaultFilament, waitForJobComplete } from './helpers';

test.describe('Slicing Setting Overrides (M7.2 + M23 + M17)', () => {
  test.setTimeout(180_000);

  // NOTE: calib-cube-10-dual-colour-merged.3mf is a dual-colour file.
  // Slicing it with a single filament_id segfaults in Orca (known issue).
  // We always send filament_ids with 2 entries to match the file's colour count.

  async function getTwoFilamentIds(request: any) {
    const filRes = await request.get(`${API}/filaments`, { timeout: 30_000 });
    const filaments = (await filRes.json()).filaments;
    const fil1 = filaments[0];
    const fil2 = filaments.length > 1 ? filaments[1] : filaments[0];
    return [fil1.id, fil2.id];
  }

  test('slice with temperature overrides succeeds', async ({ request }) => {
    const upload = await apiUpload(request, 'calib-cube-10-dual-colour-merged.3mf');
    const ids = await getTwoFilamentIds(request);

    const res = await request.post(`${API}/uploads/${upload.upload_id}/slice`, {
      data: {
        filament_ids: ids,
        layer_height: 0.2,
        infill_density: 15,
        supports: false,
        nozzle_temp: 215,
        bed_temp: 70,
        bed_type: 'PEI',
      },
      timeout: 120_000,
    });
    expect(res.ok()).toBe(true);
    const job = await waitForJobComplete(request, await res.json());
    expect(job.status).toBe('completed');
  });

  test('slice with wall_count override succeeds', async ({ request }) => {
    const upload = await apiUpload(request, 'calib-cube-10-dual-colour-merged.3mf');
    const ids = await getTwoFilamentIds(request);

    const res = await request.post(`${API}/uploads/${upload.upload_id}/slice`, {
      data: {
        filament_ids: ids,
        layer_height: 0.2,
        infill_density: 20,
        wall_count: 4,
        supports: false,
      },
      timeout: 120_000,
    });
    expect(res.ok()).toBe(true);
    const job = await waitForJobComplete(request, await res.json());
    expect(job.status).toBe('completed');
  });

  test('slice with infill_pattern override succeeds', async ({ request }) => {
    const upload = await apiUpload(request, 'calib-cube-10-dual-colour-merged.3mf');
    const ids = await getTwoFilamentIds(request);

    const res = await request.post(`${API}/uploads/${upload.upload_id}/slice`, {
      data: {
        filament_ids: ids,
        layer_height: 0.2,
        infill_density: 20,
        infill_pattern: 'grid',
        supports: false,
      },
      timeout: 120_000,
    });
    expect(res.ok()).toBe(true);
    const job = await waitForJobComplete(request, await res.json());
    expect(job.status).toBe('completed');
  });

  test('slice with prime tower enabled succeeds', async ({ request }) => {
    const upload = await apiUpload(request, 'calib-cube-10-dual-colour-merged.3mf');
    const ids = await getTwoFilamentIds(request);

    const res = await request.post(`${API}/uploads/${upload.upload_id}/slice`, {
      data: {
        filament_ids: ids,
        layer_height: 0.2,
        infill_density: 15,
        supports: false,
        enable_prime_tower: true,
        prime_tower_width: 40,
        prime_tower_brim_width: 3,
      },
      timeout: 120_000,
    });
    expect(res.ok()).toBe(true);
    const job = await waitForJobComplete(request, await res.json());
    expect(job.status).toBe('completed');
  });

  test('slice with all overrides combined succeeds', async ({ request }) => {
    const upload = await apiUpload(request, 'calib-cube-10-dual-colour-merged.3mf');
    const ids = await getTwoFilamentIds(request);

    const res = await request.post(`${API}/uploads/${upload.upload_id}/slice`, {
      data: {
        filament_ids: ids,
        layer_height: 0.16,
        infill_density: 25,
        wall_count: 5,
        infill_pattern: 'honeycomb',
        supports: false,
        nozzle_temp: 210,
        bed_temp: 65,
        bed_type: 'Glass',
        enable_prime_tower: false,
      },
      timeout: 120_000,
    });
    expect(res.ok()).toBe(true);
    const job = await waitForJobComplete(request, await res.json());
    expect(job.status).toBe('completed');
  });

  test('slice-plate with temperature overrides succeeds', async ({ request }) => {
    const upload = await apiUpload(request, 'Dragon Scale infinity.3mf');

    const platesRes = await request.get(`${API}/uploads/${upload.upload_id}/plates`, { timeout: 60_000 });
    const plates = (await platesRes.json()).plates;
    const plate = plates.find((p: any) => p.validation?.fits) || plates[0];
    const fil = await getDefaultFilament(request);

    const res = await request.post(`${API}/uploads/${upload.upload_id}/slice-plate`, {
      data: {
        plate_id: plate.plate_id,
        filament_id: fil.id,
        layer_height: 0.2,
        infill_density: 15,
        supports: false,
        nozzle_temp: 220,
        bed_temp: 65,
        bed_type: 'PEI',
      },
      timeout: 120_000,
    });
    expect(res.ok()).toBe(true);
    const job = await waitForJobComplete(request, await res.json());
    expect(job.status).toBe('completed');
  });

  test('slice with flow calibrate disabled succeeds', async ({ request }) => {
    const upload = await apiUpload(request, 'calib-cube-10-dual-colour-merged.3mf');
    const ids = await getTwoFilamentIds(request);

    const res = await request.post(`${API}/uploads/${upload.upload_id}/slice`, {
      data: {
        filament_ids: ids,
        layer_height: 0.2,
        infill_density: 15,
        supports: false,
        enable_flow_calibrate: false,
      },
      timeout: 120_000,
    });
    expect(res.ok()).toBe(true);
    const job = await waitForJobComplete(request, await res.json());
    expect(job.status).toBe('completed');
  });

  test('presets API round-trips prime tower settings', async ({ request }) => {
    // Get current presets
    const getRes = await request.get(`${API}/presets/extruders`, { timeout: 30_000 });
    const presets = await getRes.json();

    try {
      // Save with prime tower settings
      const updated = {
        ...presets,
        slicing_defaults: {
          ...presets.slicing_defaults,
          enable_prime_tower: true,
          prime_tower_width: 50,
          prime_tower_brim_width: 4,
        },
      };
      const saveRes = await request.put(`${API}/presets/extruders`, { data: updated, timeout: 30_000 });
      expect(saveRes.ok()).toBe(true);

      // Read back
      const getRes2 = await request.get(`${API}/presets/extruders`, { timeout: 30_000 });
      const presets2 = await getRes2.json();
      expect(presets2.slicing_defaults.enable_prime_tower).toBe(true);
      expect(presets2.slicing_defaults.prime_tower_width).toBe(50);
      expect(presets2.slicing_defaults.prime_tower_brim_width).toBe(4);
    } finally {
      // Always restore original presets
      await request.put(`${API}/presets/extruders`, { data: presets, timeout: 30_000 });
    }
  });
});
