import { Page, expect, APIRequestContext } from '@playwright/test';
import path from 'path';
import fs from 'fs';

export const API = 'http://localhost:8000';
export const IS_ARM64 = process.arch === 'arm64';
export const IS_SLOW_TEST_ENV = IS_ARM64 || process.env.PLAYWRIGHT_SLOW_ENV === '1';
export const UPLOAD_TRANSITION_TIMEOUT_MS = IS_SLOW_TEST_ENV ? 240_000 : 90_000;
export const UPLOAD_LIST_TIMEOUT_MS = IS_SLOW_TEST_ENV ? 150_000 : 60_000;
export const CONFIGURE_STEP_TIMEOUT_MS = IS_SLOW_TEST_ENV ? 180_000 : 60_000;
export const GENERIC_API_TIMEOUT_MS = IS_SLOW_TEST_ENV ? 120_000 : 60_000;
export const API_UPLOAD_TIMEOUT_MS = 600_000;
export const API_SLICE_REQUEST_TIMEOUT_MS = 480_000;
export const API_SLICE_POLL_TIMEOUT_MS = 480_000;
export const SLOW_TEST_TIMEOUT_MS = IS_SLOW_TEST_ENV ? 360_000 : 180_000;

/** Wait for Alpine.js to fully initialize the app */
export async function waitForApp(page: Page) {
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  // Wait for Alpine.js v3 to mount (uses _x_dataStack instead of v2's __x).
  // Don't rely on 'networkidle' — pages with many uploads fire 50+ preview
  // requests that keep the network busy long after the app is interactive.
  await page.waitForFunction(() => {
    const body = document.querySelector('body');
    return body && (
      (body as any)._x_dataStack !== undefined ||
      (body as any).__x !== undefined
    );
  }, undefined, { timeout: 15_000 });
}

/** Get Alpine.js app state */
export async function getAppState(page: Page, key: string) {
  return page.evaluate((k) => {
    const body = document.querySelector('body') as any;
    // Alpine.js v3 uses _x_dataStack (array of reactive proxies)
    if (body?._x_dataStack) {
      for (const scope of body._x_dataStack) {
        if (k in scope) return scope[k];
      }
      return undefined;
    }
    // Fallback to Alpine.js v2 API
    return body?.__x?.$data?.[k];
  }, key);
}

/** Resolve path to a test fixture file */
export function fixture(name: string) {
  return path.resolve(__dirname, '..', 'test-data', name);
}

/** Upload a file via the hidden file input */
export async function uploadFile(page: Page, fixtureName: string) {
  const filePath = fixture(fixtureName);
  const fileInput = page.locator('input[type="file"][accept=".3mf,.stl"]');
  await fileInput.setInputFiles(filePath);
  // Wait for upload to complete and move to configure step.
  // Large multi-plate files (e.g. Dragon Scale 3.6MB) need ~40s for
  // server-side parsing + per-plate validation, so allow 60s.
  await page.waitForFunction((expected) => {
    const body = document.querySelector('body') as any;
    if (body?._x_dataStack) {
      for (const scope of body._x_dataStack) {
        if ('currentStep' in scope) return scope.currentStep === expected;
      }
    }
    return body?.__x?.$data?.currentStep === expected;
  }, 'configure', { timeout: UPLOAD_TRANSITION_TIMEOUT_MS });
}

/** Navigate to the configure step for an already-uploaded file by filename */
export async function selectUploadByName(page: Page, filename: string) {
  // Open My Files modal
  await page.getByTitle('My Files').click();
  const modal = page.locator('[x-show="showStorageDrawer"]');
  await expect(modal.getByRole('heading', { name: 'My Files' })).toBeVisible({ timeout: 10_000 });
  // Wait for the uploads list to be populated in the modal
  await page.waitForFunction(() => {
    const body = document.querySelector('body') as any;
    if (body?._x_dataStack) {
      for (const scope of body._x_dataStack) {
        if ('uploads' in scope) return scope.uploads?.length > 0;
      }
    }
    return false;
  }, undefined, { timeout: UPLOAD_LIST_TIMEOUT_MS });
  // Find the file card containing this filename within the modal and click its "Slice" button.
  // Each card is a div.rounded-lg wrapper containing both filename and Slice button.
  const card = modal.locator('.rounded-lg').filter({ hasText: filename }).first();
  await expect(card).toBeVisible({ timeout: 10_000 });
  await card.getByRole('button', { name: 'Slice', exact: true }).click();
  // Wait for configure step (modal closes and app transitions)
  await page.waitForFunction((expected) => {
    const body = document.querySelector('body') as any;
    if (body?._x_dataStack) {
      for (const scope of body._x_dataStack) {
        if ('currentStep' in scope) return scope.currentStep === expected;
      }
    }
    return body?.__x?.$data?.currentStep === expected;
  }, 'configure', { timeout: CONFIGURE_STEP_TIMEOUT_MS });
}

/** Wait for slicing to complete (up to 2.5 minutes).
 *  Fails fast if the app reverts to 'configure' or 'upload' (slice error). */
export async function waitForSliceComplete(page: Page) {
  await page.waitForFunction(() => {
    const body = document.querySelector('body') as any;
    let step: string | undefined;
    if (body?._x_dataStack) {
      for (const scope of body._x_dataStack) {
        if ('currentStep' in scope) { step = scope.currentStep; break; }
      }
    }
    if (!step) step = body?.__x?.$data?.currentStep;
    if (step === 'complete') return true;
    // Fail fast on error — app reverts to configure or upload on failure
    if (step === 'configure' || step === 'upload') {
      throw new Error(`Slice failed — app reverted to '${step}' step`);
    }
    return false;
  }, undefined, { timeout: SLOW_TEST_TIMEOUT_MS });
}

/** Get the current step from Alpine state */
export async function getCurrentStep(page: Page): Promise<string> {
  return getAppState(page, 'currentStep') as Promise<string>;
}

/** Upload a 3MF file via API and return the upload response */
export async function apiUpload(request: APIRequestContext, fixtureName: string, timeout = API_UPLOAD_TIMEOUT_MS) {
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
    timeout,
  });
  expect(res.ok()).toBe(true);
  return res.json();
}

/** Get the default filament (or first available) */
export async function getDefaultFilament(request: APIRequestContext) {
  const res = await request.get(`${API}/filaments`);
  const body = await res.json();
  const filaments = body.filaments;
  return filaments.find((f: any) => f.is_default) || filaments[0];
}

/** Slice via API and wait for completion, returning the finished job */
export async function apiSlice(
  request: APIRequestContext,
  uploadId: string,
  options: Record<string, any> = {},
) {
  const fil = await getDefaultFilament(request);
  const data = {
    filament_id: fil.id,
    layer_height: 0.2,
    infill_density: 15,
    supports: false,
    ...options,
  };
  const res = await request.post(`${API}/uploads/${uploadId}/slice`, {
    data,
    timeout: API_SLICE_REQUEST_TIMEOUT_MS,
  });
  expect(res.ok()).toBe(true);
  const job = await res.json();
  return waitForJobComplete(request, job);
}

/** Slice a specific plate via API and wait for completion */
export async function apiSlicePlate(
  request: APIRequestContext,
  uploadId: string,
  plateId: number,
  options: Record<string, any> = {},
) {
  const fil = await getDefaultFilament(request);
  const data = {
    plate_id: plateId,
    filament_id: fil.id,
    layer_height: 0.2,
    infill_density: 15,
    supports: false,
    ...options,
  };
  const res = await request.post(`${API}/uploads/${uploadId}/slice-plate`, {
    data,
    timeout: API_SLICE_REQUEST_TIMEOUT_MS,
  });
  expect(res.ok()).toBe(true);
  const job = await res.json();
  return waitForJobComplete(request, job);
}

/** Poll a job until completed or failed (max ~2 min).
 *  Uses adaptive backoff: 500ms → 1s → 2s to detect fast slices sooner. */
export async function waitForJobComplete(request: APIRequestContext, job: any) {
  if (job.status === 'completed') return job;
  const jobId = job.job_id;
  let delay = 500;
  const maxDelay = 2_000;
  const deadline = Date.now() + API_SLICE_POLL_TIMEOUT_MS;
  while (Date.now() < deadline) {
    await new Promise(r => setTimeout(r, delay));
    const statusRes = await request.get(`${API}/jobs/${jobId}`, { timeout: 30_000 });
    const status = await statusRes.json();
    if (status.status === 'completed') return status;
    if (status.status === 'failed') throw new Error(`Slice failed: ${status.error || 'unknown'}`);
    delay = Math.min(delay * 2, maxDelay);
  }
  throw new Error(`Slice timed out for job ${jobId}`);
}

/**
 * Delete test-created uploads (cascades to jobs, G-code files, and logs).
 * Only deletes uploads with IDs above the given baseline, preserving
 * user-created data that existed before the test run.
 */
export async function cleanupTestUploads(baselineId: number) {
  const baseUrl = API;
  let offset = 0;
  let deleted = 0;
  while (true) {
    const res = await fetch(`${baseUrl}/upload?limit=200&offset=${offset}`);
    if (!res.ok) break;
    const body = await res.json();
    const uploads = body.uploads || [];
    if (uploads.length === 0) break;
    for (const u of uploads) {
      if (u.upload_id > baselineId) {
        try {
          await fetch(`${baseUrl}/upload/${u.upload_id}`, { method: 'DELETE' });
          deleted++;
        } catch { /* ignore individual failures */ }
      }
    }
    if (!body.has_more) break;
    offset += 200;
  }
  return deleted;
}
