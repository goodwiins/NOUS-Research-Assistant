import { expect, test, type Page } from '@playwright/test';
import { resolve } from 'node:path';
import { decodeEnvelope, type Captured } from './envelopes';
import { build } from 'vite';

/**
 * GOO-734: an on-error replay recorded by the real SDK, with the app's replay
 * options, must carry no page-URL query string or link target. The page is
 * served from a routed origin and the Sentry envelopes are captured by a
 * routed DSN host, so nothing leaves the test.
 */
const APP = 'https://app.replay.test';
const TOKEN = 'TOKENSECRET4b1d';
const SESSION = 'SESSIONSECRET77';
const QUERY = 'QUERYSECRETlithium';
const LINK = 'LINKSECRETq9';
const ARTIFACT = 'ARTIFACTSECRETsrcdoc';
const DOC_URL = 'DOCURLSECRETsig';
const DOC_BODY = 'DOCBODYSECRETpdf';

let harnessJs = '';

test.beforeAll(async () => {
  const frontend = resolve(__dirname, '../..');
  const result = await build({
    configFile: false,
    logLevel: 'silent',
    root: frontend,
    resolve: { alias: { '@': resolve(frontend, 'src') } },
    define: { 'process.env.NODE_ENV': '"production"' },
    build: {
      write: false,
      minify: false,
      // Only the test's own Chromium runs this bundle.
      target: 'esnext',
      lib: {
        entry: resolve(__dirname, 'harness.ts'),
        formats: ['iife'],
        name: 'ReplayHarness',
        fileName: () => 'harness.js',
      },
    },
  });
  const outputs = Array.isArray(result) ? result : [result];
  const chunk = outputs
    .flatMap((out) => ('output' in out ? out.output : []))
    .find((file) => file.type === 'chunk');
  if (!chunk || chunk.type !== 'chunk') throw new Error('harness bundle missing');
  harnessJs = chunk.code;
});

async function openPage(page: Page, path: string, extraBody = ''): Promise<Captured> {
  const captured: Captured = { text: [], recordings: [] };
  await page.route('https://sentry.replay.test/**', async (route) => {
    const body = route.request().postDataBuffer();
    if (body) decodeEnvelope(body, captured);
    await route.fulfill({ status: 200, body: '{}' });
  });
  await page.route(`${APP}/**`, (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === '/harness.js') {
      return route.fulfill({ contentType: 'text/javascript', body: harnessJs });
    }
    if (url.pathname === '/doc.pdf') {
      return route.fulfill({ contentType: 'text/html', body: `<p>${DOC_BODY}</p>` });
    }
    return route.fulfill({
      contentType: 'text/html',
      body: `<!doctype html><html><head><title>probe</title>
        <script src="/harness.js"></script></head>
        <body><main><h1>Verify your email</h1>
        <a id="deep" href="/search?q=${LINK}">Open search</a>
        ${extraBody}
        </main></body></html>`,
    });
  });
  await page.goto(`${APP}${path}`);
  await page.waitForFunction(() => '__replayHarness' in window);
  return captured;
}

type Harness = {
  init(useGuard: boolean, unblock?: string): void;
  strip(keys: string[]): void;
  navigate(path: string): void;
  mode(): string | undefined;
  fail(): Promise<void>;
};
const call = <T>(page: Page, fn: (h: Harness) => T | Promise<T>): Promise<T> =>
  page.evaluate(
    `(${fn.toString()})(window.__replayHarness)`
  ) as Promise<T>;

const navigate = (page: Page, path: string): Promise<void> =>
  page.evaluate(
    (target) =>
      (window as unknown as { __replayHarness: Harness }).__replayHarness.navigate(target),
    path
  );

async function failAndCollect(page: Page, captured: Captured): Promise<void> {
  await call(page, (h) => h.fail());
  // The error event is sent first; a buffered replay follows it.
  await expect
    .poll(() => captured.text.length, { timeout: 10_000 })
    .toBeGreaterThan(0);
  await page.waitForTimeout(1_000);
}

test('control: without the guard the page-load snapshot records the token', async ({
  page,
}) => {
  const captured = await openPage(page, `/verify-email?token_hash=${TOKEN}&type=signup`);
  await call(page, (h) => h.init(false));
  expect(await call(page, (h) => h.mode())).toBe('buffer');
  await failAndCollect(page, captured);
  await expect.poll(() => captured.recordings.length, { timeout: 15_000 }).toBeGreaterThan(0);
  // The Meta event's href is the full URL: the leak this change closes.
  expect(captured.recordings.join('\n')).toContain(TOKEN);
  // Links with a query are blocked even here (REPLAY_BLOCK_SELECTORS).
  expect(captured.text.join('\n')).not.toContain(LINK);
});

test('stripped URL: replay restarts clean and no token or link target is sent', async ({
  page,
}) => {
  const captured = await openPage(page, `/verify-email?token_hash=${TOKEN}&type=signup`);
  await call(page, (h) => h.init(true));
  // Sensitive URL: the buffer holding the page-load snapshot is dropped.
  expect(await call(page, (h) => h.mode())).toBeUndefined();
  // The strip's replaceState passes through the guard, which resumes.
  await call(page, (h) => h.strip(['token_hash', 'type']));
  expect(await call(page, (h) => h.mode())).toBe('buffer');
  await failAndCollect(page, captured);
  await expect.poll(() => captured.recordings.length, { timeout: 15_000 }).toBeGreaterThan(0);
  const all = captured.text.join('\n');
  expect(all).not.toContain(TOKEN);
  expect(all).not.toContain(LINK);
  // The fresh snapshot records the clean URL; the link is a blocked
  // placeholder (rr_width/rr_height only, no href).
  const recording = captured.recordings.join('\n');
  expect(recording).toContain(`"href":"${APP}/verify-email"`);
  expect(recording).toMatch(/"tagName":"a","attributes":\{"rr_width":"[^"]+","rr_height":"[^"]+"\}/);
});

test('a URL that keeps its secret sends no replay of it', async ({ page }) => {
  const captured = await openPage(page, `/cli-auth?session_id=${SESSION}`);
  await call(page, (h) => h.init(true));
  expect(await call(page, (h) => h.mode())).toBeUndefined();
  await failAndCollect(page, captured);
  expect(captured.recordings).toEqual([]);
  expect(captured.text.join('\n')).not.toContain(SESSION);
});

test('search text stays in the URL; replay is off until the URL is clean', async ({ page }) => {
  const captured = await openPage(page, `/search?q=${QUERY}`);
  await call(page, (h) => h.init(true));
  expect(await call(page, (h) => h.mode())).toBeUndefined();
  expect(new URL(page.url()).searchParams.get('q')).toBe(QUERY);
  await failAndCollect(page, captured);
  expect(captured.recordings).toEqual([]);
  expect(captured.text.join('\n')).not.toContain(QUERY);

  // Leaving the query resumes replay with a fresh snapshot of the clean URL.
  await navigate(page, '/dashboard');
  expect(await call(page, (h) => h.mode())).toBe('buffer');
  // A client navigation back to a query stops it before the URL changes.
  await navigate(page, `/search?q=${QUERY}`);
  expect(await call(page, (h) => h.mode())).toBeUndefined();
  await navigate(page, '/dashboard');
  await failAndCollect(page, captured);
  await expect.poll(() => captured.recordings.length, { timeout: 15_000 }).toBeGreaterThan(0);
  expect(captured.text.join('\n')).not.toContain(QUERY);
});


/**
 * GOO-746: an artifact preview (`srcdoc`, model-written HTML) and a document
 * viewer are iframes. SDK 10.78 already blocks `iframe[srcdoc]:not([src])`
 * by default, and drops an iframe's `src` when it can read the frame. A
 * frame it cannot read (cross-origin, such as a signed storage link) keeps
 * its full URL as `rr_src`, query string and fragment included. Blocking
 * every iframe covers both, whatever the SDK's defaults become.
 */
const FILES = 'https://files.replay.test';
const IFRAMES = `
  <iframe id="artifact" title="Interactive HTML preview" sandbox="allow-scripts"
    srcdoc="<p>${ARTIFACT}</p>"></iframe>
  <iframe id="viewer" title="Preview of paper"
    src="${FILES}/doc.pdf?X-Amz-Signature=${DOC_URL}#page=2"></iframe>`;

async function openFramedPage(page: Page): Promise<Captured> {
  await page.route(`${FILES}/**`, (route) =>
    route.fulfill({ contentType: 'text/html', body: `<p>${DOC_BODY}</p>` })
  );
  return openPage(page, '/chat', IFRAMES);
}

test('control: without the iframe block, a cross-origin viewer URL is recorded', async ({
  page,
}) => {
  const captured = await openFramedPage(page);
  await call(page, (h) => h.init(true, 'iframe'));
  expect(await call(page, (h) => h.mode())).toBe('buffer');
  await failAndCollect(page, captured);
  await expect.poll(() => captured.recordings.length, { timeout: 15_000 }).toBeGreaterThan(0);
  const recording = captured.recordings.join('\n');
  expect(recording).toContain(`"rr_src":"${FILES}/doc.pdf?X-Amz-Signature=${DOC_URL}`);
  // The SDK's own default still blocks the srcdoc frame.
  expect(recording).not.toContain(ARTIFACT);
});

test('iframes are blocked: no artifact HTML, viewer URL or framed content is sent', async ({
  page,
}) => {
  const captured = await openFramedPage(page);
  await call(page, (h) => h.init(true));
  expect(await call(page, (h) => h.mode())).toBe('buffer');
  await failAndCollect(page, captured);
  await expect.poll(() => captured.recordings.length, { timeout: 15_000 }).toBeGreaterThan(0);
  const all = captured.text.join('\n');
  expect(all).not.toContain(ARTIFACT);
  expect(all).not.toContain(DOC_URL);
  expect(all).not.toContain(DOC_BODY);
  // Every recorded iframe is a sized placeholder with no other attribute.
  const recording = captured.recordings.join('\n');
  const frames = recording.match(/"tagName":"iframe","attributes":\{[^}]*\}/g) ?? [];
  expect(frames.length).toBeGreaterThanOrEqual(2);
  for (const frame of frames) {
    expect(frame).toMatch(
      /^"tagName":"iframe","attributes":\{"rr_width":"[^"]+","rr_height":"[^"]+"\}$/
    );
  }
});
