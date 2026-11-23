import { defineConfig } from '@playwright/test';

// GOO-734: the replay guard and URL strip against the REAL Next router (the
// app on `next dev`). Sentry uses a made-up DSN host that the spec routes, so
// no request reaches Sentry; Supabase points at a closed local port.
const PORT = 3108;
const baseURL = `http://127.0.0.1:${PORT}`;

export default defineConfig({
  testDir: './e2e/replay',
  testMatch: 'replay-router.spec.ts',
  outputDir: './test-results/replay-router',
  timeout: 120_000,
  workers: 1,
  retries: 0,
  reporter: [['list']],
  use: {
    browserName: 'chromium',
    baseURL,
    headless: true,
    launchOptions: { args: ['--no-sandbox'] },
    trace: 'retain-on-failure',
  },
  webServer: {
    command: `env NEXT_PUBLIC_SENTRY_DSN=https://public@sentry.replay.test/1 NEXT_PUBLIC_API_BASE_URL=${baseURL}/api/v1 NEXT_PUBLIC_SUPABASE_URL=http://127.0.0.1:54321 NEXT_PUBLIC_SUPABASE_ANON_KEY=replay-test-anon-key corepack pnpm@10.18.2 exec next dev --hostname 127.0.0.1 --port ${PORT}`,
    url: `${baseURL}/login`,
    reuseExistingServer: false,
    timeout: 180_000,
  },
});
