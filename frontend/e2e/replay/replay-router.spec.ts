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
const HASH_TOKEN = 'HASHSECRETrouter2';

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
  __NEXT_HYDRATED?: boolean;
};

/**
 * Load a page and wait until React has hydrated it. `window.next.router`
 * exists as soon as the client bundle loads, but a push issued before
 * hydration commits is dropped (#2277 CI flake). Next sets
 * `window.__NEXT_HYDRATED` from its root effect in test mode
 * (`__NEXT_TEST_MODE`, set by playwright.replay-router.config.ts); child
 * effects, including the app router's, have run by then.
 */
async function gotoHydrated(page: Page, url: string): Promise<void> {
  await page.goto(url);
  await page
    .waitForFunction(() => (window as NextWindow).__NEXT_HYDRATED === true, undefined, {
      timeout: 60_000,
    })
    .catch(() => {
      throw new Error('Next never reported hydration: is __NEXT_TEST_MODE set on the server?');
    });
}

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
  await gotoHydrated(page, `/verify-email?token_hash=${TOKEN}&type=signup`);
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
  await gotoHydrated(page, '/login');
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

test('back/forward into a sensitive history entry sends no replay of it', async ({ page }) => {
  const captured = await capture(page);
  await gotoHydrated(page, '/login');
  const sensitive = `/login?next=${encodeURIComponent(`/cli-auth?session_id=${SESSION}`)}`;
  await router(page, 'push', sensitive);
  await expect.poll(() => page.url()).toContain(SESSION);
  await router(page, 'push', '/register');
  await expect.poll(() => new URL(page.url()).pathname).toBe('/register');

  // Back into the sensitive entry: the guard stops replay on popstate.
  await page.goBack();
  await expect.poll(() => page.url()).toContain(SESSION);
  await throwAndWait(page, captured, false);
  expect(captured.recordings).toEqual([]);

  // Forward out of it: replay resumes on the clean URL.
  await page.goForward();
  await expect.poll(() => new URL(page.url()).pathname).toBe('/register');
  await throwAndWait(page, captured, true);

  // Back once more, then a replay from the next clean page: no envelope
  // (errors or recordings) ever carries the session id.
  await page.goBack();
  await expect.poll(() => page.url()).toContain(SESSION);
  await page.goForward();
  await expect.poll(() => new URL(page.url()).pathname).toBe('/register');
  await throwAndWait(page, captured, true);
  expect(captured.text.join('\n')).not.toContain(SESSION);
});

test('back into a native #access_token entry sends no replay of it', async ({ page }) => {
  // GOO-746 (GOO-734 follow-up). A fragment navigation the browser makes on
  // its own (a link to `#access_token=…`, `location.hash`, an auth
  // redirect) leaves an entry with no history state. Next ignores a Back
  // into such an entry (`handlePopState` returns on a null state), so it
  // never writes the URL with replaceState: only the guard's own listeners
  // see it. The test the router-managed entries above cannot be.
  const captured = await capture(page);
  await gotoHydrated(page, '/login');
  await page.evaluate((token) => {
    window.location.hash = `access_token=${token}&type=recovery`;
  }, HASH_TOKEN);
  await expect.poll(() => page.url()).toContain(HASH_TOKEN);
  await router(page, 'push', '/register');
  await expect.poll(() => new URL(page.url()).pathname).toBe('/register');
  // Clean page: replay records again.
  await throwAndWait(page, captured, true);
  const sent = captured.recordings.length;

  await page.goBack();
  await expect.poll(() => page.url()).toContain(HASH_TOKEN);
  await throwAndWait(page, captured, false);
  expect(captured.recordings.length).toBe(sent);
  expect(captured.text.join('\n')).not.toContain(HASH_TOKEN);
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
