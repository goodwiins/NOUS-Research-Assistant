import { defineConfig, devices } from '@playwright/test';

const PORT = 3109;
const baseURL = `http://127.0.0.1:${PORT}`;

// Credential-free axe scan of pages that render under stub Supabase env.
export default defineConfig({
  testDir: './e2e/accessibility',
  testMatch: 'credential-free-axe.spec.ts',
  outputDir: './test-results/a11y-smoke',
  fullyParallel: false,
  retries: process.env.CI ? 1 : 0,
  reporter: [['list']],
  expect: { timeout: 10_000 },
  use: {
    ...devices['Desktop Chrome'],
    baseURL,
    viewport: { width: 1280, height: 900 },
    colorScheme: 'light',
    deviceScaleFactor: 1,
    trace: 'retain-on-failure',
  },
  projects: [{ name: 'chromium-1280' }],
  webServer: {
    command: `env NEXT_PUBLIC_VISUAL_TEST_FIXTURES=1 NEXT_PUBLIC_API_BASE_URL=${baseURL}/api/v1 NEXT_PUBLIC_SUPABASE_URL=http://127.0.0.1:54321 NEXT_PUBLIC_SUPABASE_ANON_KEY=visual-test-anon-key corepack pnpm@10.18.2 exec next dev --hostname 127.0.0.1 --port ${PORT}`,
    url: `${baseURL}/login`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
