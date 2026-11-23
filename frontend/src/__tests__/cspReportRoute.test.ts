// @vitest-environment node
import { beforeEach, describe, expect, it, vi } from 'vitest';

const captureMessage = vi.fn();
vi.mock('@sentry/nextjs', () => ({ captureMessage }));

const URL_PATH = 'https://dev-app.one.test/api/csp-report';
const LEAKY = JSON.stringify({
  'csp-report': {
    'document-uri':
      'https://dev-app.one.test/auth/callback?code=pkce-code-123&next=/chat',
    referrer: 'https://dev-app.one.test/search?q=my%20private%20search',
    'effective-directive': 'connect-src',
    disposition: 'report',
    'blocked-uri': 'https://exfil.example/c?session_id=cli-session-77',
    'script-sample': 'alert(document.cookie)',
  },
});

function post(body: BodyInit, headers: Record<string, string>): Request {
  return new Request(URL_PATH, { method: 'POST', body, headers });
}

async function load(): Promise<
  typeof import('../../app/api/csp-report/route')
> {
  vi.resetModules();
  return import('../../app/api/csp-report/route');
}

async function expectEmpty204(response: Response): Promise<void> {
  expect(response.status).toBe(204);
  expect(await response.text()).toBe('');
}

describe('POST /api/csp-report', () => {
  beforeEach(() => {
    captureMessage.mockReset();
    vi.spyOn(Math, 'random').mockReturnValue(0);
  });

  it('forwards only the scrubbed record through the server Sentry SDK', async () => {
    const { POST } = await load();
    await expectEmpty204(
      await POST(post(LEAKY, { 'content-type': 'application/csp-report' }))
    );
    expect(captureMessage).toHaveBeenCalledTimes(1);
    const [message, context] = captureMessage.mock.calls[0];
    expect(message).toBe('CSP violation');
    expect(context.tags).toEqual({
      csp_directive: 'connect-src',
      csp_disposition: 'report',
      csp_blocked: 'https://exfil.example',
    });
    expect(context.extra).toEqual({
      document_origin: 'https://dev-app.one.test',
      document_route: '/auth',
    });
    const sent = JSON.stringify(captureMessage.mock.calls);
    for (const secret of [
      'pkce-code-123',
      'cli-session-77',
      'private',
      'cookie',
    ]) {
      expect(sent).not.toContain(secret);
    }
  });

  it('dedupes a repeat and samples first-seen reports', async () => {
    const { POST } = await load();
    const headers = { 'content-type': 'application/csp-report' };
    await POST(post(LEAKY, headers));
    await POST(post(LEAKY, headers));
    expect(captureMessage).toHaveBeenCalledTimes(1);

    vi.spyOn(Math, 'random').mockReturnValue(0.5);
    const fresh = await load();
    await fresh.POST(post(LEAKY, headers));
    expect(captureMessage).toHaveBeenCalledTimes(1);
  });

  it.each([
    ['a wrong content type', LEAKY, { 'content-type': 'application/json' }],
    ['no content type', LEAKY, {}],
    [
      'malformed JSON',
      '{"csp-report":',
      { 'content-type': 'application/csp-report' },
    ],
    [
      'a body over the cap',
      JSON.stringify({
        'csp-report': {
          'effective-directive': 'connect-src',
          pad: 'x'.repeat(8000),
        },
      }),
      { 'content-type': 'application/csp-report' },
    ],
  ])(
    'returns an empty 204 and forwards nothing for %s',
    async (_name, body, headers) => {
      const { POST } = await load();
      await expectEmpty204(await POST(post(body, headers)));
      expect(captureMessage).not.toHaveBeenCalled();
    }
  );

  it('stops reading a streamed body once it passes the cap', async () => {
    const { POST } = await load();
    let pulled = 0;
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) {
        pulled += 1;
        controller.enqueue(new Uint8Array(1024).fill(0x20));
        if (pulled > 1000) controller.close();
      },
    });
    const request = new Request(URL_PATH, {
      method: 'POST',
      body: stream,
      headers: { 'content-type': 'application/csp-report' },
      // @ts-expect-error Node's fetch needs duplex for a stream body.
      duplex: 'half',
    });
    await expectEmpty204(await POST(request));
    expect(pulled).toBeLessThan(10);
    expect(captureMessage).not.toHaveBeenCalled();
  });

  it('rejects a wrong content type without reading the body', async () => {
    const { POST } = await load();
    const request = post(LEAKY, { 'content-type': 'application/json' });
    await expectEmpty204(await POST(request));
    expect(request.bodyUsed).toBe(false);
    expect(captureMessage).not.toHaveBeenCalled();
  });

  it('returns an empty 204 when reading the body fails', async () => {
    const { POST } = await load();
    const request = new Request(URL_PATH, {
      method: 'POST',
      body: new ReadableStream<Uint8Array>({
        pull() {
          throw new Error('connection reset: secret-body-text');
        },
      }),
      headers: { 'content-type': 'application/csp-report' },
      // @ts-expect-error Node's fetch needs duplex for a stream body.
      duplex: 'half',
    });
    await expectEmpty204(await POST(request));
    expect(captureMessage).not.toHaveBeenCalled();
  });

  it('never throws when Sentry does', async () => {
    captureMessage.mockImplementation(() => {
      throw new Error('sentry down');
    });
    const { POST } = await load();
    await expectEmpty204(
      await POST(post(LEAKY, { 'content-type': 'application/csp-report' }))
    );
  });

  it('exports only POST, so other methods get 405 from Next', async () => {
    const route = await load();
    expect(Object.keys(route).sort()).toEqual(['POST', 'dynamic']);
  });
});

describe('proxy matcher', () => {
  it('skips the report route and nothing else under /api', async () => {
    vi.doMock('@/lib/supabase/middleware', () => ({ updateSession: vi.fn() }));
    const { config } = await import('../../proxy');
    const matcher = new RegExp(`^${config.matcher[0]}$`);
    expect(matcher.test('/api/csp-report')).toBe(false);
    expect(matcher.test('/api/csp-reports')).toBe(true);
    expect(matcher.test('/api/trigger/agent/runs')).toBe(true);
    expect(matcher.test('/chat')).toBe(true);
  });
});
