import { test, expect } from '@playwright/test';
import { waitForApp, API } from './helpers';

test.describe('Settings Backup & Restore (M35)', () => {
  test('export returns valid JSON with version and all sections', async ({ request }) => {
    const res = await request.get(`${API}/settings/export`);
    expect(res.ok()).toBe(true);

    const data = await res.json();
    expect(data.version).toBe(1);
    expect(data.exported_at).toBeDefined();
    expect(data.settings).toBeDefined();
    expect(data.settings.printer).toBeDefined();
    expect(data.settings.filaments).toBeDefined();
    expect(data.settings.extruder_presets).toBeDefined();
    expect(data.settings.slicing_defaults).toBeDefined();
    expect(Array.isArray(data.settings.filaments)).toBe(true);
    expect(Array.isArray(data.settings.extruder_presets)).toBe(true);
  });

  test('export includes filament profiles with expected fields', async ({ request }) => {
    const res = await request.get(`${API}/settings/export`);
    const data = await res.json();

    if (data.settings.filaments.length > 0) {
      const fil = data.settings.filaments[0];
      expect(fil.name).toBeDefined();
      expect(fil.material).toBeDefined();
      expect(fil.nozzle_temp).toBeDefined();
      expect(fil.bed_temp).toBeDefined();
      expect(fil.color_hex).toBeDefined();
      // Should NOT contain database-specific fields
      expect(fil.id).toBeUndefined();
      expect(fil.created_at).toBeUndefined();
    }
  });

  test('export does NOT include sensitive data', async ({ request }) => {
    const res = await request.get(`${API}/settings/export`);
    const data = await res.json();
    const raw = JSON.stringify(data);
    // MakerWorld cookies should never be in the export
    expect(data.settings.printer.makerworld_cookies).toBeUndefined();
    expect(raw).not.toContain('makerworld_cookies');
  });

  test('export extruder presets use filament_name not filament_id', async ({ request }) => {
    const res = await request.get(`${API}/settings/export`);
    const data = await res.json();

    for (const ep of data.settings.extruder_presets) {
      expect(ep.slot).toBeDefined();
      expect(ep.color_hex).toBeDefined();
      // Should use name for portability, not numeric id
      expect(ep.filament_id).toBeUndefined();
      expect('filament_name' in ep).toBe(true);
    }
  });

  test('import round-trip preserves settings', async ({ request }) => {
    // Snapshot current settings via export
    const exportRes = await request.get(`${API}/settings/export`);
    const backup = await exportRes.json();

    // Modify a setting via API so we can detect the restore
    const presetsRes = await request.get(`${API}/presets/extruders`);
    const originalPresets = await presetsRes.json();

    try {
      // Change wall_count to detect round-trip
      const newWallCount = (originalPresets.slicing_defaults.wall_count || 3) === 3 ? 5 : 3;
      await request.put(`${API}/presets/extruders`, {
        data: {
          extruders: originalPresets.extruders,
          slicing_defaults: {
            ...originalPresets.slicing_defaults,
            wall_count: newWallCount,
          },
        },
      });

      // Verify it changed
      const changedRes = await request.get(`${API}/presets/extruders`);
      const changed = await changedRes.json();
      expect(changed.slicing_defaults.wall_count).toBe(newWallCount);

      // Import the original backup to restore
      const importRes = await request.post(`${API}/settings/import`, {
        multipart: {
          file: {
            name: 'backup.json',
            mimeType: 'application/json',
            buffer: Buffer.from(JSON.stringify(backup)),
          },
        },
      });
      expect(importRes.ok()).toBe(true);
      const importResult = await importRes.json();
      expect(importResult.success).toBe(true);

      // Verify settings were restored
      const restoredRes = await request.get(`${API}/presets/extruders`);
      const restored = await restoredRes.json();
      expect(restored.slicing_defaults.wall_count).toBe(backup.settings.slicing_defaults.wall_count);
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

  test('import rejects invalid JSON', async ({ request }) => {
    const res = await request.post(`${API}/settings/import`, {
      multipart: {
        file: {
          name: 'bad.json',
          mimeType: 'application/json',
          buffer: Buffer.from('not json at all'),
        },
      },
    });
    expect(res.ok()).toBe(false);
    expect(res.status()).toBe(400);
  });

  test('import rejects wrong version', async ({ request }) => {
    const res = await request.post(`${API}/settings/import`, {
      multipart: {
        file: {
          name: 'v99.json',
          mimeType: 'application/json',
          buffer: Buffer.from(JSON.stringify({ version: 99, settings: {} })),
        },
      },
    });
    expect(res.ok()).toBe(false);
    expect(res.status()).toBe(400);
  });

  test('backup/restore UI elements are visible in settings modal', async ({ page }) => {
    await waitForApp(page);
    await page.getByTitle('Settings').click();

    // Scroll to bottom of settings modal
    const heading = page.getByRole('heading', { name: 'Backup & Restore' });
    await heading.scrollIntoViewIfNeeded();
    await expect(heading).toBeVisible();

    // Export button
    await expect(page.getByRole('button', { name: /Export Settings/i })).toBeVisible();

    // Import file chooser
    await expect(page.getByText('Choose Backup File')).toBeVisible();
  });
});
