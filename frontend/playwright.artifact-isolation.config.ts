import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e/artifacts',
  // html-isolation: sandboxed artifact HTML. csp-images: the production
  // CSP img-src allows self, blob: and data: and blocks remote https (GOO-722).
  testMatch: ['html-isolation.spec.ts', 'csp-images.spec.ts'],
  workers: 1,
  retries: 0,
  use: {
    browserName: 'chromium',
    headless: true,
    bypassCSP: false,
    launchOptions: { args: ['--no-sandbox'] },
  },
});
