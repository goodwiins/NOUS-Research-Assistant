import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import type { Breadcrumb, ErrorEvent, Log } from '@sentry/nextjs';
import {
  FILTERED,
  MAX_STRING_LENGTH,
  OBJECT_PLACEHOLDER,
  isSensitiveKey,
  scrubArgument,
  scrubBreadcrumb,
  scrubEvent,
  scrubLog,
  scrubString,
  scrubValue,
  sentryScrubOptions,
  SENTRY_CONSOLE_LOG_LEVELS,
} from '@/lib/sentry-scrub';

// Realistic payloads of the kind the chat and upload paths have logged.
const JWT =
  'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ1c2VyLTEyMyIsImVtYWlsIjoiYUBiLmNvIn0.c2lnbmF0dXJlLXZhbHVlLWhlcmU';
const TOOL_CALL = {
  type: 'tool_call',
  tool_name: 'search_documents',
  args: { query: 'side effects of my lithium prescription', project_id: 'p-1' },
  result: { documents: [{ title: 'Patient notes', text: 'Confidential' }] },
};
const SSE_LINE =
  'data: {"type":"text-delta","delta":"Here is your private answer about the merger"}';
const THREAD_TITLE = 'Divorce settlement strategy with Dr. Smith';

/** Everything the hooks emit, flattened to one string for leak checks. */
const dump = (value: unknown): string => JSON.stringify(value);

describe('sentry-scrub strings', () => {
  it('redacts a JWT, bearer token, email and API key', () => {
    const out = scrubString(
      `auth ${JWT} Bearer abcdefghijklmnop alice@example.com sk-proj-ABCDEFGHIJKLMNOP1234`
    );
    expect(out).not.toContain(JWT);
    expect(out).toContain('[jwt]');
    expect(out).toContain('Bearer [Filtered]');
    expect(out).toContain('[email]');
    expect(out).toContain('[key]');
    expect(out).not.toContain('alice@example.com');
  });

  it('drops the payload of an SSE data line', () => {
    const out = scrubString(`event: message\n${SSE_LINE}\n`);
    expect(out).not.toContain('merger');
    expect(out).toContain('data: [Filtered]');
  });

  it('strips URL query strings and truncates long strings', () => {
    expect(scrubString('GET https://api.test/files?token=abc&q=secret')).toBe(
      'GET https://api.test/files?[Filtered]'
    );
    const long = scrubString('x'.repeat(MAX_STRING_LENGTH + 50));
    expect(long.startsWith('x'.repeat(MAX_STRING_LENGTH))).toBe(true);
    expect(long).toContain('[truncated 50 chars]');
  });

  it('matches sensitive keys in any casing or separator style', () => {
    for (const key of [
      'toolArgs',
      'tool_args',
      'tool-result',
      'thread_title',
      'Authorization',
      'accessToken',
      'userEmail',
      'document_text',
      'prompt',
    ]) {
      expect(isSensitiveKey(key), key).toBe(true);
    }
    for (const key of ['status', 'tool_name', 'project_id', 'duration_ms']) {
      expect(isSensitiveKey(key), key).toBe(false);
    }
  });
});

describe('sentry-scrub console arguments', () => {
  it('drops object and array arguments but keeps primitives and error messages', () => {
    expect(scrubArgument(TOOL_CALL)).toBe(OBJECT_PLACEHOLDER);
    expect(scrubArgument([1, 2, 3])).toBe('[Array(3)]');
    expect(scrubArgument(42)).toBe(42);
    expect(scrubArgument(new TypeError(`bad token ${JWT}`))).toBe(
      'TypeError: bad token [jwt]'
    );
  });

  it('filters sensitive keys in kept structured data', () => {
    const out = scrubValue({ thread_id: 't-1', title: THREAD_TITLE, nested: TOOL_CALL });
    expect(dump(out)).not.toContain('Divorce');
    expect(dump(out)).not.toContain('lithium');
    expect(out).toMatchObject({
      thread_id: 't-1',
      title: FILTERED,
      nested: { tool_name: 'search_documents', args: FILTERED, result: FILTERED },
    });
  });
});

describe('scrubLog (beforeSendLog)', () => {
  it('rebuilds a templated console.error without the object argument', () => {
    // console.error('Tool call failed', TOOL_CALL) as consoleLoggingIntegration builds it.
    const log: Log = {
      level: 'error',
      message: `Tool call failed ${JSON.stringify(TOOL_CALL)}`,
      attributes: {
        'sentry.origin': 'auto.log.console',
        'sentry.message.template': 'Tool call failed {}',
        'sentry.message.parameter.0': TOOL_CALL,
      },
    };
    const out = scrubLog(log);
    expect(out.message).toBe(`Tool call failed ${OBJECT_PLACEHOLDER}`);
    expect(out.attributes?.['sentry.message.parameter.0']).toBe(OBJECT_PLACEHOLDER);
    expect(out.attributes?.['sentry.origin']).toBe('auto.log.console');
    expect(dump(out)).not.toContain('lithium');
  });

  it('scrubs a plain-object first argument spread into attributes', () => {
    // console.log({ title, tool_name }) spreads the object into attributes.
    const log: Log = {
      level: 'info',
      message: JSON.stringify({ title: THREAD_TITLE, tool_name: 'x' }),
      attributes: { title: THREAD_TITLE, tool_name: 'x', step: 3 },
    };
    const out = scrubLog(log);
    expect(out.message).toBe(OBJECT_PLACEHOLDER);
    expect(out.attributes).toMatchObject({ title: FILTERED, tool_name: 'x', step: 3 });
    expect(dump(out)).not.toContain('Divorce');
  });

  it('scrubs string parameters and the plain message', () => {
    const templated = scrubLog({
      level: 'error',
      message: `Stream chunk ${SSE_LINE}`,
      attributes: {
        'sentry.message.template': 'Stream chunk {}',
        'sentry.message.parameter.0': SSE_LINE,
      },
    });
    expect(dump(templated)).not.toContain('merger');

    const plain = scrubLog({ level: 'error', message: `Auth failed for ${JWT}` });
    expect(plain.message).toBe('Auth failed for [jwt]');
  });
});

describe('scrubBreadcrumb (beforeBreadcrumb)', () => {
  it('drops object console arguments from console breadcrumbs', () => {
    const crumb: Breadcrumb = {
      category: 'console',
      level: 'log',
      message: `Loaded thread ${THREAD_TITLE} ${JSON.stringify(TOOL_CALL)}`.slice(0, 50),
      data: { arguments: ['Loaded thread', { title: THREAD_TITLE }, TOOL_CALL], logger: 'console' },
    };
    const out = scrubBreadcrumb(crumb);
    expect(out.data?.arguments).toEqual(['Loaded thread', OBJECT_PLACEHOLDER, OBJECT_PLACEHOLDER]);
    expect(out.data?.logger).toBe('console');
    expect(dump(out.data)).not.toContain('Divorce');
  });

  it('drops a serialized object console message', () => {
    const out = scrubBreadcrumb({ category: 'console', message: JSON.stringify(TOOL_CALL) });
    expect(out.message).toBe(OBJECT_PLACEHOLDER);
  });

  it('strips query strings from fetch, xhr and navigation URLs', () => {
    const fetchCrumb = scrubBreadcrumb({
      category: 'fetch',
      type: 'http',
      data: {
        method: 'GET',
        url: `https://dev-api.test/api/v1/search?q=lithium&access_token=${JWT}`,
        status_code: 200,
        request_body: '{"prompt":"my secret"}',
      },
    });
    expect(fetchCrumb.data).toMatchObject({
      method: 'GET',
      url: 'https://dev-api.test/api/v1/search',
      status_code: 200,
      request_body: FILTERED,
    });
    const nav = scrubBreadcrumb({
      category: 'navigation',
      data: { from: '/chat?thread=abc', to: '/search?q=lithium' },
    });
    expect(nav.data).toEqual({ from: '/chat', to: '/search' });
    expect(dump([fetchCrumb, nav])).not.toContain('lithium');
  });
});

describe('scrubEvent (beforeSend)', () => {
  it('removes cookies, bodies, auth headers, query strings, emails and local vars', () => {
    const event: ErrorEvent = {
      type: undefined,
      message: `Failed for alice@example.com`,
      exception: {
        values: [
          {
            type: 'Error',
            value: `Request failed: ${SSE_LINE}`,
            stacktrace: {
              frames: [{ filename: 'app.js', function: 'send', vars: { prompt: 'secret prompt' } }],
            },
          },
        ],
      },
      request: {
        url: 'https://app.test/chat?thread=t-1',
        cookies: { 'sb-access-token': JWT },
        data: '{"message":"my private question"}',
        query_string: 'thread=t-1',
        headers: {
          Authorization: `Bearer ${JWT}`,
          Cookie: 'sb=1',
          'User-Agent': 'Mozilla/5.0',
          Referer: 'https://app.test/search?q=lithium',
        },
      },
      user: { id: 'user-1', email: 'alice@example.com', ip_address: '1.2.3.4' },
      extra: { threadTitle: THREAD_TITLE, toolCall: TOOL_CALL, attempt: 2 },
      breadcrumbs: [{ category: 'console', data: { arguments: [TOOL_CALL] } }],
    };

    const out = scrubEvent(event);
    const text = dump(out);
    for (const leak of [JWT, 'alice@example.com', 'my private question', 'Divorce', 'lithium', 'merger', 'secret prompt', '1.2.3.4']) {
      expect(text, leak).not.toContain(leak);
    }
    expect(out.request).toEqual({
      url: 'https://app.test/chat',
      headers: { 'User-Agent': 'Mozilla/5.0', Referer: 'https://app.test/search' },
    });
    expect(out.user).toEqual({ id: 'user-1' });
    expect(out.extra).toMatchObject({ threadTitle: FILTERED, attempt: 2 });
    expect(out.exception?.values?.[0].stacktrace?.frames?.[0]).toEqual({
      filename: 'app.js',
      function: 'send',
    });
  });
});

describe('Sentry config wiring', () => {
  it('exposes all three hooks and forwards only console errors', () => {
    expect(sentryScrubOptions.beforeSend).toBe(scrubEvent);
    expect(sentryScrubOptions.beforeBreadcrumb).toBe(scrubBreadcrumb);
    expect(sentryScrubOptions.beforeSendLog).toBe(scrubLog);
    expect(SENTRY_CONSOLE_LOG_LEVELS).toEqual(['error']);
  });

  it.each([
    'sentry.server.config.ts',
    'sentry.client.config.ts',
    'sentry.edge.config.ts',
    'instrumentation-client.ts',
  ])('%s spreads the hooks and forwards only console errors', (file) => {
    const source = readFileSync(resolve(__dirname, '../../..', file), 'utf8');
    expect(source).toContain('...sentryScrubOptions');
    expect(source).toContain(
      'consoleLoggingIntegration({ levels: SENTRY_CONSOLE_LOG_LEVELS })'
    );
    expect(source).not.toMatch(/levels:\s*\[/);
  });
});
