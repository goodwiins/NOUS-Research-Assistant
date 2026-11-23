import { expect, test, type Page } from '@playwright/test';
import { decodeEnvelope, type Captured } from './envelopes';

/**
 * GOO-734 against the real Next router (the app on `next dev`, the real
 * Sentry SDK with the app's own init). The DSN host is routed, so envelopes
 * are captured here and nothing reaches Sentry.
 *
 * What the routed-HTML spec cannot show: that the strip moves Next's own URL
 * (so router updates do not write the token back), and that the guard sees
 * Next's navigations.
 */
const TOKEN = 'TOKENSECRETrouter8';
const SESSION = 'SESSIONSECRETrouter3';
const QUERY = 'QUERYSECRETrouter5';

async function capture(page: Page): Promise<Captured> {
  const captured: Captured = { text: [], recordings: [] };
  await page.route('https://sentry.replay.test/**', async (route) => {
    const body = route.request().postDataBuffer();
    if (body) decodeEnvelope(body, captured);
    await route.fulfill({ status: 200, body: '{}' });
  });
  // Supabase: no backend in this test.
  await page.route('http://127.0.0.1:54321/**', (route) => route.abort());
  return captured;
}

type NextWindow = Window & {
  next?: { router?: { refresh(): void; push(href: string): void } };
};

const router = (page: Page, call: 'refresh' | 'push', href?: string): Promise<void> =>
  page.evaluate(
    ([method, target]) => {
      const r = (window as NextWindow).next?.router;
      if (!r) throw new Error('Next router not exposed');
      if (method === 'refresh') r.refresh();
      else r.push(target ?? '/');
    },
    [call, href] as const
  );

/** Throw from a timer so Sentry's global handler captures it. */
async function throwAndWait(page: Page, captured: Captured, expectReplay: boolean): Promise<void> {
  const before = captured.text.length;
  // A unique message each time: Sentry's Dedupe drops a repeated error.
  await page.evaluate((n) => {
    window.setTimeout(() => {
      throw new Error(`replay router probe ${n}`);
    }, 0);
  }, before);
  await expect.poll(() => captured.text.length, { timeout: 20_000 }).toBeGreaterThan(before);
  if (expectReplay) {
    await expect.poll(() => captured.recordings.length, { timeout: 30_000 }).toBeGreaterThan(0);
  } else {
    // Give a buffered replay time to follow the error, if one were kept.
    await page.waitForTimeout(8_000);
  }
}

const search = (page: Page): string => new URL(page.url()).search;

test('verify-email: the stripped token stays out after refresh, navigation and back/forward', async ({
  page,
}) => {
  const captured = await capture(page);
  await page.goto(`/verify-email?token_hash=${TOKEN}&type=signup`);
  await expect.poll(() => search(page)).toBe('');

  // Next's history entry still carries its own state (back/forward work).
  expect(
    await page.evaluate(() => Boolean(window.history.state?.__PRIVATE_NEXTJS_INTERNALS_TREE))
  ).toBe(true);

  // A router update re-renders from the router's URL: it must be clean too.
  await router(page, 'refresh');
  await page.waitForTimeout(1_500);
  expect(page.url()).not.toContain(TOKEN);

  await router(page, 'push', '/login');
  await expect.poll(() => new URL(page.url()).pathname).toBe('/login');
  await page.goBack();
  await expect.poll(() => new URL(page.url()).pathname).toBe('/verify-email');
  expect(page.url()).not.toContain(TOKEN);
  await page.goForward();
  await expect.poll(() => new URL(page.url()).pathname).toBe('/login');
  await page.goBack();
  await expect.poll(() => new URL(page.url()).pathname).toBe('/verify-email');
  await router(page, 'refresh');
  await page.waitForTimeout(1_500);
  expect(page.url()).not.toContain(TOKEN);

  // Replay resumed on the clean URL and records nothing of the token.
  await throwAndWait(page, captured, true);
  expect(captured.text.join('\n')).not.toContain(TOKEN);
});

test('a client navigation to a sensitive URL sends no replay of it; a clean one resumes', async ({
  page,
}) => {
  const captured = await capture(page);
  await page.goto('/login');
  const sensitive = `/login?next=${encodeURIComponent(`/cli-auth?session_id=${SESSION}`)}`;
  await router(page, 'push', sensitive);
  await expect.poll(() => page.url()).toContain(SESSION);
  await throwAndWait(page, captured, false);
  expect(captured.recordings).toEqual([]);
  expect(captured.text.join('\n')).not.toContain(SESSION);

  await router(page, 'push', '/register');
  await expect.poll(() => new URL(page.url()).pathname).toBe('/register');
  await throwAndWait(page, captured, true);
  expect(captured.text.join('\n')).not.toContain(SESSION);
});

test('a page load with search text in the URL sends no replay of it', async ({ page }) => {
  const captured = await capture(page);
  // Signed out, /search redirects to /login?next=/search?q=… : the query
  // is nested, and still sensitive.
  await page.goto(`/search?q=${QUERY}`);
  await page.waitForLoadState('networkidle');
  expect(decodeURIComponent(page.url())).toContain(QUERY);
  await throwAndWait(page, captured, false);
  expect(captured.recordings).toEqual([]);
  expect(captured.text.join('\n')).not.toContain(QUERY);
});
