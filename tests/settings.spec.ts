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

  test('Save as Defaults button exists', async ({ page }) => {
    await expect(page.getByRole('button', { name: /Save as Defaults/i })).toBeVisible();
  });

  test('filament library shows entries', async ({ page }) => {
    const filaments = await getAppState(page, 'filaments') as any[];
    if (filaments.length > 0) {
      // At least one filament card should be visible in the library section
      await expect(page.getByText(filaments[0].name, { exact: true }).first()).toBeVisible();
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

  test('Initialize Starter Library button is present', async ({ page }) => {
    const initBtn = page.getByRole('button', { name: /Initialize Starter Library/i });
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
    const filId = created.id;

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

    // Verify gone
    const listRes2 = await request.get(`${API}/filaments`);
    const filaments2 = (await listRes2.json()).filaments;
    const gone = filaments2.find((f: any) => f.id === filId);
    expect(gone).toBeUndefined();
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
    expect(imported.id).toBeDefined();
    expect(imported.has_slicer_settings).toBe(true);

    // Verify in filament list
    const listRes = await request.get(`${API}/filaments`);
    const filaments = (await listRes.json()).filaments;
    const found = filaments.find((f: any) => f.id === imported.id);
    expect(found).toBeDefined();
    expect(found.has_slicer_settings).toBe(true);

    // Cleanup
    await request.delete(`${API}/filaments/${imported.id}`);
  });

  test('export returns OrcaSlicer-compatible JSON with slicer_settings', async ({ request }) => {
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

    // Cleanup
    await request.delete(`${API}/filaments/${imported.id}`);
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
    expect(reImported.has_slicer_settings).toBe(true);

    // Re-export and compare key slicer settings
    const reExportRes = await request.get(`${API}/filaments/${reImported.id}/export`);
    const reExported = await reExportRes.json();
    expect(reExported.filament_max_volumetric_speed).toEqual(exported.filament_max_volumetric_speed);
    expect(reExported.filament_flow_ratio).toEqual(exported.filament_flow_ratio);

    // Cleanup
    await request.delete(`${API}/filaments/${imported.id}`);
    await request.delete(`${API}/filaments/${reImported.id}`);
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
});
