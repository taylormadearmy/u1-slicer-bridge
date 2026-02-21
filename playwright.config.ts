import { defineConfig } from '@playwright/test';

const baseURL = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:8080';
const apiHealthURL = process.env.PLAYWRIGHT_API_HEALTH_URL || 'http://localhost:8000/healthz';
const isArm64 = process.arch === 'arm64';
const isRemoteBaseUrl = !/^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?\/?$/i.test(baseURL);
const isSlowEnv = isArm64 || isRemoteBaseUrl || process.env.PLAYWRIGHT_SLOW_ENV === '1';

export default defineConfig({
  globalSetup: './tests/global-setup.ts',
  globalTeardown: './tests/global-teardown.ts',
  testDir: './tests',
  timeout: isSlowEnv ? 240_000 : 120_000,
  expect: {
    timeout: 10_000,
  },
  fullyParallel: false,
  retries: 0,
  workers: 1,
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    actionTimeout: 15_000,
    navigationTimeout: 15_000,
  },
  projects: [
    {
      name: 'chromium',
      use: { browserName: 'chromium' },
    },
  ],
  /* Ensure Docker services are running before tests */
  webServer: {
    command: `node -e "fetch('${apiHealthURL}').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"`,
    url: baseURL,
    reuseExistingServer: true,
    timeout: 5_000,
  },
});
