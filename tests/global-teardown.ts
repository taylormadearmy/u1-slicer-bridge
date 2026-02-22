import fs from 'fs';
import path from 'path';
import { cleanupTestUploads } from './helpers';

const BASELINE_FILE = path.join(__dirname, '.test-baseline');

/**
 * Playwright global teardown — runs once after all tests complete.
 * Only deletes uploads created during this test run (IDs above the
 * baseline recorded in global-setup), preserving user data.
 */
export default async function globalTeardown() {
  try {
    if (process.env.TEST_CLEANUP_UPLOADS !== '1') {
      if (fs.existsSync(BASELINE_FILE)) fs.unlinkSync(BASELINE_FILE);
      console.log('\n🛡️ Skipping upload cleanup (set TEST_CLEANUP_UPLOADS=1 to enable).\n');
      return;
    }

    let baselineId = 0;
    if (fs.existsSync(BASELINE_FILE)) {
      baselineId = parseInt(fs.readFileSync(BASELINE_FILE, 'utf-8'), 10) || 0;
      fs.unlinkSync(BASELINE_FILE);
    }
    const deleted = await cleanupTestUploads(baselineId);
    if (deleted > 0) {
      console.log(`\n🧹 Cleaned up ${deleted} test upload(s)\n`);
    }
  } catch (err) {
    // Don't fail the test run if cleanup fails (services may be down)
    console.warn('\n⚠️  Test cleanup failed:', (err as Error).message, '\n');
  }
}
