import { defineConfig } from '@playwright/test';

// GOO-734: records a real Sentry replay in Chromium with the app's replay
// options and checks the captured payload. Routed origins only: no app
// server, and no request reaches Sentry.
export default defineConfig({
  testDir: './e2e/replay',
  testMatch: 'replay-privacy.spec.ts',
  timeout: 60_000,
  workers: 1,
  retries: 0,
  use: {
    browserName: 'chromium',
    headless: true,
    launchOptions: { args: ['--no-sandbox'] },
  },
});
