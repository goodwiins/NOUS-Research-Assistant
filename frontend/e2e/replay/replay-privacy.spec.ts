import { expect, test, type Page } from '@playwright/test';
import { resolve } from 'node:path';
import { gunzipSync, inflateSync } from 'node:zlib';
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

interface Captured {
  /** Every envelope, decoded to text (recordings decompressed). */
  text: string[];
  recordings: string[];
}

/** Decode a Sentry envelope; replay_recording payloads may be compressed. */
function decodeEnvelope(body: Buffer, captured: Captured): void {
  const parts: string[] = [];
  let offset = body.indexOf(0x0a) + 1; // skip envelope header
  parts.push(body.subarray(0, offset).toString('utf8'));
  while (offset < body.length) {
    const headerEnd = body.indexOf(0x0a, offset);
    if (headerEnd < 0) break;
    const headerText = body.subarray(offset, headerEnd).toString('utf8');
    parts.push(headerText);
    const header = JSON.parse(headerText) as { type: string; length?: number };
    const start = headerEnd + 1;
    const end =
      header.length !== undefined
        ? start + header.length
        : (() => {
            const next = body.indexOf(0x0a, start);
            return next < 0 ? body.length : next;
          })();
    const payload = body.subarray(start, end);
    if (header.type === 'replay_recording') {
      const split = payload.indexOf(0x0a);
      const meta = payload.subarray(0, split).toString('utf8');
      const data = payload.subarray(split + 1);
      let recording: string;
      try {
        recording = inflateSync(data).toString('utf8');
      } catch {
        try {
          recording = gunzipSync(data).toString('utf8');
        } catch {
          recording = data.toString('utf8');
        }
      }
      captured.recordings.push(recording);
      parts.push(meta, recording);
    } else {
      parts.push(payload.toString('utf8'));
    }
    offset = end + 1;
  }
  captured.text.push(parts.join('\n'));
}

async function openPage(page: Page, path: string): Promise<Captured> {
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
    return route.fulfill({
      contentType: 'text/html',
      body: `<!doctype html><html><head><title>probe</title>
        <script src="/harness.js"></script></head>
        <body><main><h1>Verify your email</h1>
        <a id="deep" href="/search?q=${LINK}">Open search</a>
        </main></body></html>`,
    });
  });
  await page.goto(`${APP}${path}`);
  await page.waitForFunction(() => '__replayHarness' in window);
  return captured;
}

type Harness = {
  init(useGuard: boolean): Promise<void>;
  strip(keys: string[]): void;
  sync(): Promise<void>;
  mode(): string | undefined;
  fail(): Promise<void>;
};
const call = <T>(page: Page, fn: (h: Harness) => T | Promise<T>): Promise<T> =>
  page.evaluate(
    `(${fn.toString()})(window.__replayHarness)`
  ) as Promise<T>;

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
  await call(page, (h) => h.strip(['token_hash', 'type']));
  await call(page, (h) => h.sync());
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

test('search text is dropped from the URL and the replay', async ({ page }) => {
  const captured = await openPage(page, `/search?q=${QUERY}`);
  await call(page, (h) => h.init(true));
  await call(page, (h) => h.strip(['q']));
  await call(page, (h) => h.sync());
  expect(new URL(page.url()).search).toBe('');
  await failAndCollect(page, captured);
  await expect.poll(() => captured.recordings.length, { timeout: 15_000 }).toBeGreaterThan(0);
  expect(captured.text.join('\n')).not.toContain(QUERY);
});
