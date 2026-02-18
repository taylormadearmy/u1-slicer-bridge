import fs from 'fs';
import path from 'path';
import { API } from './helpers';

const BASELINE_FILE = path.join(__dirname, '.test-baseline');

/**
 * Playwright global setup — runs once before all tests start.
 * Records the current max upload ID so the teardown only deletes
 * uploads created during the test run (preserving user data).
 *
 * Retries the API call to avoid wiping user data when the API is
 * slow to start (baseline 0 would delete everything).
 */
export default async function globalSetup() {
  let maxId = 0;
  const maxRetries = 10;
  for (let attempt = 1; attempt <= maxRetries; attempt++) {
    try {
      const res = await fetch(`${API}/upload?limit=200&offset=0`);
      if (res.ok) {
        const body = await res.json();
        const uploads = body.uploads || [];
        // Find the actual max upload_id (don't rely on sort order)
        for (const u of uploads) {
          if (u.upload_id > maxId) maxId = u.upload_id;
        }
        break; // API is up — we have a reliable baseline
      }
    } catch {
      if (attempt < maxRetries) {
        await new Promise(r => setTimeout(r, 2000));
      }
    }
  }
  fs.writeFileSync(BASELINE_FILE, String(maxId));
}
