import { test, expect } from '@playwright/test';
import { waitForApp, getAppState, fixture } from './helpers';
import fs from 'fs';
import path from 'path';

test.describe('Settings Modal', () => {
  test.beforeEach(async ({ page }) => {
    await waitForApp(page);
    await page.getByTitle('Settings').click();
  });

  test('Printer Defaults section is visible', async ({ page }) => {
    await expect(page.getByRole('heading', { name: 'Printer Defaults' })).toBeVisible();
  });

  test('Filament Library section is visible', async ({ page }) => {
    await expect(page.getByRole('heading', { name: 'Filament Library' })).toBeVisible();
  });

  test('extruder preset slots E1-E4 are shown', async ({ page }) => {
    await expect(page.locator('span').filter({ hasText: /^E1$/ }).first()).toBeVisible();
    await expect(page.locator('span').filter({ hasText: /^E2$/ }).first()).toBeVisible();
    await expect(page.locator('span').filter({ hasText: /^E3$/ }).first()).toBeVisible();
    await expect(page.locator('span').filter({ hasText: /^E4$/ }).first()).toBeVisible();
  });

  test('settings auto-save on modal close (no Save button)', async ({ page }) => {
    // "Save as Defaults" was removed — settings auto-save when modal closes.
    await expect(page.getByRole('button', { name: /Save as Defaults/i })).not.toBeVisible();
  });

  test('Flow Calibration checkbox is visible in Machine Behavior section', async ({ page }) => {
    // Scroll to the Machine Behavior section
    const label = page.locator('label[for="settings-flow-calibrate"]');
    await label.scrollIntoViewIfNeeded();
    await expect(label).toBeVisible();
    await expect(label).toHaveText('Flow Calibration');
    // Checkbox should default to checked
    const checkbox = page.locator('#settings-flow-calibrate');
    await expect(checkbox).toBeChecked();
  });

  test('filament library shows entries', async ({ page }) => {
    const filaments = await getAppState(page, 'filaments') as any[];
    if (filaments.length > 0) {
      // Scroll to the Filament Library section (below viewport)
      const heading = page.getByRole('heading', { name: 'Filament Library' });
      await heading.scrollIntoViewIfNeeded();
      // At least one filament card should be visible (use <p> to avoid hidden dropdown spans)
      await expect(page.locator('p').filter({ hasText: filaments[0].name }).first()).toBeVisible();
    }
  });

  test('Add Filament button opens form', async ({ page }) => {
    await page.getByRole('button', { name: 'Add Filament' }).click();
    const showForm = await getAppState(page, 'showFilamentForm');
    expect(showForm).toBe(true);
  });

  test('filament form has required fields', async ({ page }) => {
    await page.getByRole('button', { name: 'Add Filament' }).click();
    // Form uses label text without for= attributes, so use text locators
    const form = page.locator('[x-show="showFilamentForm"]');
    await expect(form.getByText('Name')).toBeVisible();
    await expect(form.getByText('Material')).toBeVisible();
    await expect(form.getByText('Nozzle Temp')).toBeVisible();
  });

  test('filament form cancel closes form', async ({ page }) => {
    await page.getByRole('button', { name: 'Add Filament' }).click();
    await page.getByRole('button', { name: 'Cancel' }).click();
    const showForm = await getAppState(page, 'showFilamentForm');
    expect(showForm).toBe(false);
  });

  test('Reset to Starter Library button is present', async ({ page }) => {
    // Scroll to Filament Library section (may be below viewport)
    const heading = page.getByRole('heading', { name: 'Filament Library' });
    await heading.scrollIntoViewIfNeeded();
    const initBtn = page.getByRole('button', { name: /Reset to Starter Library/i });
    await expect(initBtn).toBeVisible();
    // Button is disabled when filaments already exist — just check it's rendered
    const filaments = await getAppState(page, 'filaments') as any[];
    if (filaments.length > 0) {
      await expect(initBtn).toBeDisabled();
    } else {
      // If no filaments, click and verify it populates
      await initBtn.click();
      await page.waitForTimeout(2_000);
      const after = await getAppState(page, 'filaments') as any[];
      expect(after.length).toBeGreaterThan(0);
    }
  });
});

test.describe('Settings API', () => {
  test('save and reload extruder presets', async ({ request }) => {
    const API = 'http://localhost:8000';

    // Get current presets
    const getRes = await request.get(`${API}/presets/extruders`);
    expect(getRes.ok()).toBe(true);
    const presets = await getRes.json();

    // Save them back (round-trip)
    const saveRes = await request.put(`${API}/presets/extruders`, {
      data: {
        extruders: presets.extruders,
        slicing_defaults: presets.slicing_defaults,
      },
    });
    expect(saveRes.ok()).toBe(true);

    // Verify they persisted
    const getRes2 = await request.get(`${API}/presets/extruders`);
    const presets2 = await getRes2.json();
    expect(presets2.slicing_defaults.layer_height).toBe(presets.slicing_defaults.layer_height);
  });

  test('filament CRUD lifecycle', async ({ request }) => {
    const API = 'http://localhost:8000';
    let filId: number | undefined;
    try {
      // Create
      const createRes = await request.post(`${API}/filaments`, {
        data: {
          name: `Test Filament ${Date.now()}`,
          material: 'PLA',
          nozzle_temp: 200,
          bed_temp: 60,
          print_speed: 60,
          bed_type: 'PEI',
        },
      });
      expect(createRes.ok()).toBe(true);
      const created = await createRes.json();
      filId = created.id;

      // Read
      const listRes = await request.get(`${API}/filaments`);
      const filaments = (await listRes.json()).filaments;
      const found = filaments.find((f: any) => f.id === filId);
      expect(found).toBeDefined();

      // Update (all required fields must be sent)
      const updateRes = await request.put(`${API}/filaments/${filId}`, {
        data: {
          name: `Updated ${Date.now()}`,
          material: 'PETG',
          nozzle_temp: 230,
          bed_temp: 70,
          print_speed: 50,
          bed_type: 'PEI',
        },
      });
      expect(updateRes.ok()).toBe(true);

      // Delete
      const delRes = await request.delete(`${API}/filaments/${filId}`);
      expect(delRes.ok()).toBe(true);
      filId = undefined; // Already deleted

      // Verify gone
      const listRes2 = await request.get(`${API}/filaments`);
      const filaments2 = (await listRes2.json()).filaments;
      const gone = filaments2.find((f: any) => f.id === created.id);
      expect(gone).toBeUndefined();
    } finally {
      if (filId) await request.delete(`${API}/filaments/${filId}`);
    }
  });
});

test.describe('Filament Import/Export (M13)', () => {
  const API = 'http://localhost:8000';

  test('preview import shows recognized OrcaSlicer profile with slicer settings', async ({ request }) => {
    const filePath = fixture('test-filament-profile.json');
    const fileBuffer = fs.readFileSync(filePath);

    const res = await request.post(`${API}/filaments/import/preview`, {
      multipart: {
        file: {
          name: 'test-filament-profile.json',
          mimeType: 'application/json',
          buffer: fileBuffer,
        },
      },
    });
    expect(res.ok()).toBe(true);

    const data = await res.json();
    expect(data.preview).toBeDefined();
    expect(data.preview.name).toBe('Test PLA Custom');
    expect(data.preview.material).toBe('PLA');
    expect(data.preview.nozzle_temp).toBe(215);
    expect(data.preview.bed_temp).toBe(65);
    expect(data.preview.is_recognized).toBe(true);
    expect(data.preview.has_slicer_settings).toBe(true);
    expect(data.preview.slicer_setting_count).toBeGreaterThan(5);
    expect(data.preview.color_hex).toBe('#FF6600');
  });

  test('import stores slicer_settings and has_slicer_settings flag', async ({ request }) => {
    let importedId: number | undefined;
    try {
      const filePath = fixture('test-filament-profile.json');
      const fileBuffer = fs.readFileSync(filePath);

      // Import the profile
      const importRes = await request.post(`${API}/filaments/import`, {
        multipart: {
          file: {
            name: 'test-filament-profile.json',
            mimeType: 'application/json',
            buffer: fileBuffer,
          },
        },
      });
      expect(importRes.ok()).toBe(true);
      const imported = await importRes.json();
      importedId = imported.id;
      expect(imported.id).toBeDefined();
      expect(imported.has_slicer_settings).toBe(true);

      // Verify in filament list
      const listRes = await request.get(`${API}/filaments`);
      const filaments = (await listRes.json()).filaments;
      const found = filaments.find((f: any) => f.id === imported.id);
      expect(found).toBeDefined();
      expect(found.has_slicer_settings).toBe(true);
    } finally {
      if (importedId) await request.delete(`${API}/filaments/${importedId}`);
    }
  });

  test('export returns OrcaSlicer-compatible JSON with slicer_settings', async ({ request }) => {
    let importedId: number | undefined;
    try {
      const filePath = fixture('test-filament-profile.json');
      const fileBuffer = fs.readFileSync(filePath);

      // Import first
      const importRes = await request.post(`${API}/filaments/import`, {
        multipart: {
          file: {
            name: 'test-filament-profile.json',
            mimeType: 'application/json',
            buffer: fileBuffer,
          },
        },
      });
      const imported = await importRes.json();
      importedId = imported.id;

      // Export
      const exportRes = await request.get(`${API}/filaments/${imported.id}/export`);
      expect(exportRes.ok()).toBe(true);
      const exported = await exportRes.json();

      // Should be OrcaSlicer-shaped
      expect(exported.type).toBe('filament');
      expect(exported.name).toContain('Test PLA Custom');
      expect(exported.filament_type).toBeDefined();
      expect(exported.nozzle_temperature).toBeDefined();

      // Should contain the passthrough slicer settings
      expect(exported.filament_max_volumetric_speed).toBeDefined();
      expect(exported.filament_flow_ratio).toBeDefined();
      expect(exported.filament_retraction_length).toBeDefined();
      expect(exported.fan_max_speed).toBeDefined();
    } finally {
      if (importedId) await request.delete(`${API}/filaments/${importedId}`);
    }
  });

  test('export for starter filament (no slicer_settings) returns basic profile', async ({ request }) => {
    // Get existing filaments
    const listRes = await request.get(`${API}/filaments`);
    const filaments = (await listRes.json()).filaments;
    if (filaments.length === 0) {
      // Initialize starter library first
      await request.post(`${API}/filaments/init-defaults`);
      const listRes2 = await request.get(`${API}/filaments`);
      const filaments2 = (await listRes2.json()).filaments;
      if (filaments2.length === 0) return; // Skip if no filaments
    }

    const refetch = await request.get(`${API}/filaments`);
    const all = (await refetch.json()).filaments;
    const starter = all.find((f: any) => !f.has_slicer_settings) || all[0];

    const exportRes = await request.get(`${API}/filaments/${starter.id}/export`);
    expect(exportRes.ok()).toBe(true);
    const exported = await exportRes.json();
    expect(exported.type).toBe('filament');
    expect(exported.name).toBeDefined();
    expect(exported.nozzle_temperature).toBeDefined();
  });

  test('import round-trip preserves slicer settings', async ({ request }) => {
    let importedId: number | undefined;
    let reImportedId: number | undefined;
    try {
      const filePath = fixture('test-filament-profile.json');
      const fileBuffer = fs.readFileSync(filePath);

      // Import
      const importRes = await request.post(`${API}/filaments/import`, {
        multipart: {
          file: {
            name: 'test-filament-profile.json',
            mimeType: 'application/json',
            buffer: fileBuffer,
          },
        },
      });
      const imported = await importRes.json();
      importedId = imported.id;

      // Export
      const exportRes = await request.get(`${API}/filaments/${imported.id}/export`);
      const exported = await exportRes.json();

      // Re-import the exported profile
      const reImportRes = await request.post(`${API}/filaments/import`, {
        multipart: {
          file: {
            name: 're-exported.json',
            mimeType: 'application/json',
            buffer: Buffer.from(JSON.stringify(exported)),
          },
        },
      });
      expect(reImportRes.ok()).toBe(true);
      const reImported = await reImportRes.json();
      reImportedId = reImported.id;
      expect(reImported.has_slicer_settings).toBe(true);

      // Re-export and compare key slicer settings
      const reExportRes = await request.get(`${API}/filaments/${reImported.id}/export`);
      const reExported = await reExportRes.json();
      expect(reExported.filament_max_volumetric_speed).toEqual(exported.filament_max_volumetric_speed);
      expect(reExported.filament_flow_ratio).toEqual(exported.filament_flow_ratio);
    } finally {
      if (importedId) await request.delete(`${API}/filaments/${importedId}`);
      if (reImportedId) await request.delete(`${API}/filaments/${reImportedId}`);
    }
  });

  test('Bambu profile derives speed from volumetric flow limit', async ({ request }) => {
    const filePath = fixture('Bambu PLA Basic @BBL P1S 0.4 nozzle.json');
    const fileBuffer = fs.readFileSync(filePath);

    const res = await request.post(`${API}/filaments/import/preview`, {
      multipart: {
        file: {
          name: 'Bambu PLA Basic @BBL P1S 0.4 nozzle.json',
          mimeType: 'application/json',
          buffer: fileBuffer,
        },
      },
    });
    expect(res.ok()).toBe(true);

    const data = await res.json();
    // filament_max_volumetric_speed: ["21", "29"] → 21 / 0.08 = 262
    expect(data.preview.print_speed).toBe(262);
    expect(data.preview.nozzle_temp).toBe(220);
    expect(data.preview.is_recognized).toBe(true);
  });

  test('import preview includes density field', async ({ request }) => {
    const filePath = fixture('test-filament-profile.json');
    const fileBuffer = fs.readFileSync(filePath);

    const res = await request.post(`${API}/filaments/import/preview`, {
      multipart: {
        file: {
          name: 'test-filament-profile.json',
          mimeType: 'application/json',
          buffer: fileBuffer,
        },
      },
    });
    expect(res.ok()).toBe(true);
    const data = await res.json();
    // Density should be present (either from profile or default 1.24)
    expect(data.preview.density).toBeDefined();
    expect(data.preview.density).toBeGreaterThanOrEqual(0.5);
    expect(data.preview.density).toBeLessThanOrEqual(5.0);
  });

  test('export includes filament_density field', async ({ request }) => {
    let importedId: number | undefined;
    try {
      const filePath = fixture('test-filament-profile.json');
      const fileBuffer = fs.readFileSync(filePath);

      // Import first
      const importRes = await request.post(`${API}/filaments/import`, {
        multipart: {
          file: {
            name: 'test-filament-profile.json',
            mimeType: 'application/json',
            buffer: fileBuffer,
          },
        },
      });
      const imported = await importRes.json();
      importedId = imported.id;

      // Export
      const exportRes = await request.get(`${API}/filaments/${imported.id}/export`);
      expect(exportRes.ok()).toBe(true);
      const exported = await exportRes.json();

      // Should include filament_density
      expect(exported.filament_density).toBeDefined();
    } finally {
      if (importedId) await request.delete(`${API}/filaments/${importedId}`);
    }
  });
});

test.describe('Filament Density (M19)', () => {
  const API = 'http://localhost:8000';

  test('create filament with density and verify via list', async ({ request }) => {
    const name = `Density Test ${Date.now()}`;
    let createdId: number | undefined;
    try {
      const createRes = await request.post(`${API}/filaments`, {
        data: {
          name,
          material: 'PLA',
          nozzle_temp: 200,
          bed_temp: 60,
          print_speed: 60,
          bed_type: 'PEI',
          density: 1.24,
        },
      });
      expect(createRes.ok()).toBe(true);
      const created = await createRes.json();
      createdId = created.id;

      // Verify density in filament list
      const listRes = await request.get(`${API}/filaments`);
      const filaments = (await listRes.json()).filaments;
      const found = filaments.find((f: any) => f.id === created.id);
      expect(found).toBeDefined();
      expect(found.density).toBe(1.24);
    } finally {
      if (createdId) await request.delete(`${API}/filaments/${createdId}`);
    }
  });

  test('update filament density', async ({ request }) => {
    const name = `Density Update ${Date.now()}`;
    let createdId: number | undefined;
    try {
      const createRes = await request.post(`${API}/filaments`, {
        data: {
          name,
          material: 'PLA',
          nozzle_temp: 200,
          bed_temp: 60,
          print_speed: 60,
          bed_type: 'PEI',
          density: 1.24,
        },
      });
      const created = await createRes.json();
      createdId = created.id;

      // Update to PETG density
      const updateRes = await request.put(`${API}/filaments/${created.id}`, {
        data: {
          name,
          material: 'PETG',
          nozzle_temp: 230,
          bed_temp: 70,
          print_speed: 50,
          bed_type: 'PEI',
          density: 1.27,
        },
      });
      expect(updateRes.ok()).toBe(true);

      // Verify updated
      const listRes = await request.get(`${API}/filaments`);
      const filaments = (await listRes.json()).filaments;
      const found = filaments.find((f: any) => f.id === created.id);
      expect(found.density).toBe(1.27);
    } finally {
      if (createdId) await request.delete(`${API}/filaments/${createdId}`);
    }
  });

  test('filament without explicit density gets default 1.24', async ({ request }) => {
    let createdId: number | undefined;
    try {
      const createRes = await request.post(`${API}/filaments`, {
        data: {
          name: `No Density ${Date.now()}`,
          material: 'PLA',
          nozzle_temp: 200,
          bed_temp: 60,
          print_speed: 60,
          bed_type: 'PEI',
        },
      });
      expect(createRes.ok()).toBe(true);
      const created = await createRes.json();
      createdId = created.id;

      // Verify default density in filament list
      const listRes = await request.get(`${API}/filaments`);
      const filaments = (await listRes.json()).filaments;
      const found = filaments.find((f: any) => f.id === created.id);
      expect(found).toBeDefined();
      expect(found.density).toBe(1.24);
    } finally {
      if (createdId) await request.delete(`${API}/filaments/${createdId}`);
    }
  });
});

test.describe('Slice Response Metadata', () => {
  const API = 'http://localhost:8000';
  test.setTimeout(180_000);

  test('slice response includes filament_used_g array', async ({ request }) => {
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
      timeout: 60_000,
    });
    expect(uploadRes.ok()).toBe(true);
    const upload = await uploadRes.json();

    // Get filament
    const filRes = await request.get(`${API}/filaments`, { timeout: 30_000 });
    const filaments = (await filRes.json()).filaments;
    const fil = filaments[0];

    // Slice
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
    const job = await sliceRes.json();

    // filament_used_g should be an array with weight values
    expect(job.metadata.filament_used_g).toBeDefined();
    expect(Array.isArray(job.metadata.filament_used_g)).toBe(true);
    // At least one entry should be > 0 (actual filament usage)
    const totalWeight = job.metadata.filament_used_g.reduce((a: number, b: number) => a + b, 0);
    expect(totalWeight).toBeGreaterThan(0);
  });
});

test.describe('Settings Auto-Save', () => {
  const API = 'http://localhost:8000';

  test('slicing defaults persist after save and reload via API', async ({ request }) => {
    // Snapshot full presets so we can restore on failure
    const getRes = await request.get(`${API}/presets/extruders`);
    expect(getRes.ok()).toBe(true);
    const originalPresets = await getRes.json();
    try {
      const originalWallCount = originalPresets.slicing_defaults.wall_count;

      // Change wall_count to something different
      const newWallCount = originalWallCount === 3 ? 4 : 3;
      const saveRes = await request.put(`${API}/presets/extruders`, {
        data: {
          extruders: originalPresets.extruders,
          slicing_defaults: {
            ...originalPresets.slicing_defaults,
            wall_count: newWallCount,
          },
        },
      });
      expect(saveRes.ok()).toBe(true);

      // Verify it persisted
      const getRes2 = await request.get(`${API}/presets/extruders`);
      const presets2 = await getRes2.json();
      expect(presets2.slicing_defaults.wall_count).toBe(newWallCount);
    } finally {
      // Always restore original presets
      await request.put(`${API}/presets/extruders`, {
        data: {
          extruders: originalPresets.extruders,
          slicing_defaults: originalPresets.slicing_defaults,
        },
      });
    }
  });

  test('enable_flow_calibrate persists across save/load', async ({ request }) => {
    const getRes = await request.get(`${API}/presets/extruders`);
    expect(getRes.ok()).toBe(true);
    const originalPresets = await getRes.json();
    try {
      // Default should be true
      expect(originalPresets.slicing_defaults.enable_flow_calibrate).toBe(true);

      // Save with flow calibrate disabled
      const saveRes = await request.put(`${API}/presets/extruders`, {
        data: {
          extruders: originalPresets.extruders,
          slicing_defaults: {
            ...originalPresets.slicing_defaults,
            enable_flow_calibrate: false,
          },
        },
      });
      expect(saveRes.ok()).toBe(true);

      // Verify it persisted
      const getRes2 = await request.get(`${API}/presets/extruders`);
      const presets2 = await getRes2.json();
      expect(presets2.slicing_defaults.enable_flow_calibrate).toBe(false);
    } finally {
      await request.put(`${API}/presets/extruders`, {
        data: {
          extruders: originalPresets.extruders,
          slicing_defaults: originalPresets.slicing_defaults,
        },
      });
    }
  });

  test('setting_modes persist across save/load', async ({ request }) => {
    // Snapshot full presets so we can restore on failure
    const getRes = await request.get(`${API}/presets/extruders`);
    expect(getRes.ok()).toBe(true);
    const originalPresets = await getRes.json();
    try {
      // Save with setting_modes
      const testModes = {
        layer_height: 'override',
        infill_density: 'orca',
        supports: 'model',
      };
      const saveRes = await request.put(`${API}/presets/extruders`, {
        data: {
          extruders: originalPresets.extruders,
          slicing_defaults: {
            ...originalPresets.slicing_defaults,
            setting_modes: testModes,
          },
        },
      });
      expect(saveRes.ok()).toBe(true);

      // Verify modes persisted
      const getRes2 = await request.get(`${API}/presets/extruders`);
      const presets2 = await getRes2.json();
      expect(presets2.slicing_defaults.setting_modes).toBeDefined();
      expect(presets2.slicing_defaults.setting_modes.layer_height).toBe('override');
      expect(presets2.slicing_defaults.setting_modes.infill_density).toBe('orca');
      expect(presets2.slicing_defaults.setting_modes.supports).toBe('model');
    } finally {
      // Always restore original presets
      await request.put(`${API}/presets/extruders`, {
        data: {
          extruders: originalPresets.extruders,
          slicing_defaults: originalPresets.slicing_defaults,
        },
      });
    }
  });
});
