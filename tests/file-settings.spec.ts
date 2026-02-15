import { test, expect } from '@playwright/test';
import { API, getDefaultFilament, waitForJobComplete } from './helpers';
import path from 'path';
import fs from 'fs';

function fixture(name: string) {
  return path.resolve(__dirname, '..', 'test-data', name);
}

/** Upload with extended timeout for large files like shashibo (4.7MB) */
async function uploadLargeFile(request: any, fixtureName: string) {
  const filePath = fixture(fixtureName);
  const buffer = fs.readFileSync(filePath);
  const res = await request.post(`${API}/upload`, {
    multipart: {
      file: {
        name: fixtureName,
        mimeType: 'application/octet-stream',
        buffer,
      },
    },
    timeout: 120_000,
  });
  expect(res.ok()).toBe(true);
  return res.json();
}

test.describe('File Print Settings Detection', () => {
  test.setTimeout(180_000);

  // Shashibo has: enable_support=1, support_type=tree(manual),
  // support_threshold_angle=30, brim_type=outer_only, brim_width=10,
  // brim_object_gap=0.1

  let shashibo: any;

  test.beforeAll(async ({ request }, testInfo) => {
    testInfo.setTimeout(180_000);
    shashibo = await uploadLargeFile(request, 'Shashibo-h2s-textured.3mf');
  });

  test('upload response includes file_print_settings', async () => {
    expect(shashibo.file_print_settings).toBeDefined();
    expect(shashibo.file_print_settings.enable_support).toBe(true);
    expect(shashibo.file_print_settings.support_type).toBe('tree(manual)');
    expect(shashibo.file_print_settings.support_threshold_angle).toBe(30);
    expect(shashibo.file_print_settings.brim_type).toBe('outer_only');
    expect(shashibo.file_print_settings.brim_width).toBe(10);
    expect(shashibo.file_print_settings.brim_object_gap).toBe(0.1);
  });

  test('get upload detail includes file_print_settings', async ({ request }) => {
    const res = await request.get(`${API}/upload/${shashibo.upload_id}`, { timeout: 60_000 });
    expect(res.ok()).toBe(true);
    const detail = await res.json();
    expect(detail.file_print_settings).toBeDefined();
    expect(detail.file_print_settings.enable_support).toBe(true);
    expect(detail.file_print_settings.brim_type).toBe('outer_only');
  });

  test('plates endpoint includes file_print_settings', async ({ request }) => {
    const res = await request.get(`${API}/uploads/${shashibo.upload_id}/plates`, { timeout: 120_000 });
    expect(res.ok()).toBe(true);
    const data = await res.json();
    expect(data.file_print_settings).toBeDefined();
    expect(data.file_print_settings.support_type).toBe('tree(manual)');
    expect(data.file_print_settings.brim_width).toBe(10);
  });

  test('slice with file-detected support and brim settings', async ({ request }) => {
    const fil = await getDefaultFilament(request);
    const res = await request.post(`${API}/uploads/${shashibo.upload_id}/slice-plate`, {
      data: {
        plate_id: 1,
        filament_id: fil.id,
        supports: true,
        support_type: 'tree(manual)',
        support_threshold_angle: 30,
        brim_type: 'outer_only',
        brim_width: 10,
        brim_object_gap: 0.1,
      },
      timeout: 120_000,
    });
    expect(res.ok()).toBe(true);
    const job = await waitForJobComplete(request, await res.json());
    expect(job.status).toBe('completed');
  });

  test('slice with brim override to no_brim', async ({ request }) => {
    const fil = await getDefaultFilament(request);
    const res = await request.post(`${API}/uploads/${shashibo.upload_id}/slice-plate`, {
      data: {
        plate_id: 1,
        filament_id: fil.id,
        supports: true,
        support_type: 'tree(manual)',
        brim_type: 'no_brim',
      },
      timeout: 120_000,
    });
    expect(res.ok()).toBe(true);
    const job = await waitForJobComplete(request, await res.json());
    expect(job.status).toBe('completed');
  });
});
