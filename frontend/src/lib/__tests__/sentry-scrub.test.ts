import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import type { Breadcrumb, ErrorEvent, Log } from '@sentry/nextjs';
import {
  FILTERED,
  MAX_STRING_LENGTH,
  OBJECT_PLACEHOLDER,
  isSensitiveKey,
  minimalEvent,
  safeBeforeBreadcrumb,
  safeBeforeSend,
  safeBeforeSendLog,
  safeBeforeSendSpan,
  safeBeforeSendTransaction,
  scrubArgument,
  scrubBreadcrumb,
  scrubData,
  scrubEvent,
  scrubLog,
  scrubReplayEventsIntegration,
  scrubReplayRecordingEvent,
  scrubSpan,
  scrubString,
  scrubTransaction,
  sentryScrubOptions,
  SENTRY_CONSOLE_LOG_LEVELS,
} from '@/lib/sentry-scrub';
import { REPLAY_PRIVACY_OPTIONS } from '@/lib/replay-privacy';

// Secret-shaped fixtures are assembled at runtime so no literal credential
// is committed (secret scanners match on the source text).
const b64url = (text: string): string =>
  Buffer.from(text).toString('base64').replace(/=+$/, '').replace(/\+/g, '-').replace(/\//g, '_');
const JWT = [
  b64url('{"alg":"HS256","typ":"JWT"}'),
  b64url('{"sub":"user-123","email":"a@b.co"}'),
  b64url('signature-value-here'),
].join('.');
const AWS_KEY = ['AK', 'IA', 'Q'.repeat(16)].join('');
const SB_SECRET = ['sb', 'secret', 'x'.repeat(24)].join('_');
const OPENAI_KEY = ['sk', 'proj', 'A'.repeat(20)].join('-');
const BASIC = Buffer.from('user:pass-word').toString('base64');

// Realistic payloads of the kind the chat and upload paths have logged.
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
const expectNoLeak = (value: unknown, leaks: string[]): void => {
  const text = dump(value);
  for (const leak of leaks) expect(text, leak).not.toContain(leak);
};

describe('strings', () => {
  it('redacts tokens, keys, headers and emails', () => {
    const out = scrubString(
      [
        `auth ${JWT}`,
        `enc ${JWT.replace(/\./g, '%2E')}`,
        'Bearer abcdefghijklmnop',
        `Authorization: Basic ${BASIC}`,
        'cookie: sb-access-token=abc123; theme=dark',
        `key ${OPENAI_KEY} ${SB_SECRET} ${AWS_KEY}`,
        'alice@example.com',
      ].join('\n'),
      2000
    );
    expectNoLeak(out, [JWT, 'abcdefghijklmnop', BASIC, 'abc123', OPENAI_KEY, SB_SECRET, AWS_KEY, 'alice@example.com', b64url('signature-value-here')]);
    expect(out).toContain('[jwt]');
    expect(out).toContain('Authorization: [Filtered]');
    expect(out).toContain('Cookie: [Filtered]');
    expect(out).toContain('[key]');
    expect(out).toContain('[email]');
  });

  it('redacts a JWT fragment left over from truncation', () => {
    expect(scrubString(`header ${JWT.split('.')[0]}`)).toBe('header [jwt]');
  });

  it('drops SSE payloads but keeps plain prose about data', () => {
    expect(scrubString(`event: message\n${SSE_LINE}\n`)).not.toContain('merger');
    expect(scrubString(`Request failed: ${SSE_LINE}`)).not.toContain('merger');
    expect(scrubString('Invalid data: expected a number')).toBe(
      'Invalid data: expected a number'
    );
  });

  it('drops JSON embedded mid-string', () => {
    const out = scrubString(`Tool call failed ${JSON.stringify(TOOL_CALL)} at step 2`);
    expect(out).toBe(`Tool call failed ${OBJECT_PLACEHOLDER}`);
  });

  it('strips query strings and sensitive params, absolute and relative', () => {
    expect(scrubString('GET https://api.test/files?token=abc&q=secret')).toBe(
      'GET https://api.test/files?[Filtered]'
    );
    expect(scrubString('fetch /api/v1/threads/search?q=lithium failed')).toBe(
      'fetch /api/v1/threads/search?[Filtered] failed'
    );
    expect(scrubString('callback access_token=abc.def&state=1')).not.toContain('abc.def');
  });

  it('truncates long strings', () => {
    const long = scrubString('x '.repeat(MAX_STRING_LENGTH));
    expect(long.length).toBeLessThan(MAX_STRING_LENGTH + 40);
    expect(long).toContain('[truncated');
  });
});

describe('cost is bounded (no quadratic patterns)', () => {
  const HEX_5MB = 'a1b2c3d4e5'.repeat(500_000);
  const EMAILISH_5MB = 'abc.def_ghi'.repeat(500_000);

  it.each([
    ['5 MB hex run', HEX_5MB],
    ['5 MB email-like run with no @', EMAILISH_5MB],
  ])('scrubs a %s in well under 50 ms', (_label, input) => {
    scrubString('warm up');
    const start = performance.now();
    const out = scrubString(input);
    const crumb = safeBeforeBreadcrumb({
      category: 'console',
      message: input,
      data: { arguments: [input, input] },
    });
    const elapsed = performance.now() - start;
    expect(elapsed).toBeLessThan(50);
    expect(out.length).toBeLessThan(MAX_STRING_LENGTH + 40);
    expect(dump(crumb).length).toBeLessThan(2000);
  });
});

describe('keys and structured data (allowlist)', () => {
  it('matches the content and credential denylist in any casing', () => {
    for (const key of [
      'toolArgs', 'tool_args', 'thread_title', 'Authorization', 'accessToken',
      'userEmail', 'document_text', 'prompt', 'name', 'description', 'snippet',
      'filename', 'question', 'answer', 'headers', 'username', 'ip', 'phone',
      'credentials', 'access_key', 'http.query', 'url.query',
    ]) {
      expect(isSensitiveKey(key), key).toBe(true);
    }
    for (const key of ['status', 'tool_name', 'project_id', 'duration_ms']) {
      expect(isSensitiveKey(key), key).toBe(false);
    }
  });

  it('keeps allowlisted and id values, filters other strings, keeps counts', () => {
    const out = scrubData({
      thread_id: 't-1',
      status: 'failed',
      tool_name: 'search_documents',
      attempt: 2,
      title: THREAD_TITLE,
      phone_number: 5551234,
      nested: TOOL_CALL,
      url: 'https://app.test/search?q=lithium',
    });
    expect(out).toEqual({
      thread_id: 't-1',
      status: 'failed',
      tool_name: FILTERED,
      attempt: 2,
      title: FILTERED,
      phone_number: 5551234,
      nested: { type: 'tool_call', tool_name: FILTERED, args: FILTERED, result: FILTERED },
      url: 'https://app.test/search',
    });
  });

  it('drops object arguments but keeps primitives and error messages', () => {
    expect(scrubArgument(TOOL_CALL)).toBe(OBJECT_PLACEHOLDER);
    expect(scrubArgument([1, 2, 3])).toBe('[Array(3)]');
    expect(scrubArgument(42)).toBe(42);
    expect(scrubArgument(new TypeError(`bad credential ${JWT}`))).toBe(
      'TypeError: bad credential [jwt]'
    );
  });
});

describe('scrubLog (beforeSendLog)', () => {
  it('rebuilds a templated console.error without the object argument', () => {
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
    expect(out.attributes?.['sentry.origin']).toBe('auto.log.console');
    expectNoLeak(out, ['lithium']);
  });

  it('filters a plain-object first argument spread into attributes', () => {
    const out = scrubLog({
      level: 'info',
      message: JSON.stringify({ title: THREAD_TITLE }),
      attributes: { title: THREAD_TITLE, tool_name: 'x', thread_id: 't-1', step: 3 },
    });
    expect(out.message).toBe(OBJECT_PLACEHOLDER);
    expect(out.attributes).toEqual({ title: FILTERED, tool_name: FILTERED, thread_id: 't-1', step: 3 });
  });
});

describe('scrubBreadcrumb (beforeBreadcrumb)', () => {
  it('drops object console arguments', () => {
    const out = scrubBreadcrumb({
      category: 'console',
      level: 'log',
      message: 'Loaded thread',
      data: { arguments: ['Loaded thread', { title: THREAD_TITLE }, TOOL_CALL], logger: 'console' },
    });
    expect(out.data?.arguments).toEqual(['Loaded thread', OBJECT_PLACEHOLDER, OBJECT_PLACEHOLDER]);
    expectNoLeak(out, ['Divorce', 'lithium']);
  });

  it('strips query strings from fetch and navigation URLs', () => {
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
    expect(fetchCrumb.data).toEqual({
      method: 'GET',
      url: 'https://dev-api.test/api/v1/search',
      status_code: 200,
      request_body: FILTERED,
    });
    const nav = scrubBreadcrumb({ category: 'navigation', data: { from: '/chat?thread=abc', to: '/search?q=lithium' } });
    expect(nav.data).toEqual({ from: '/chat', to: '/search' });
  });
});

const fullEvent = (): ErrorEvent =>
  ({
    type: undefined,
    event_id: 'e1',
    release: 'abc123',
    server_name: 'pod-7.internal',
    message: 'Failed for alice@example.com',
    logentry: { message: 'Saved %s', params: [THREAD_TITLE, TOOL_CALL, 3] },
    exception: {
      values: [
        {
          type: 'Error',
          value: `Request failed: ${SSE_LINE}`,
          mechanism: { type: 'onerror', handled: false, data: { prompt: 'secret prompt' } },
          stacktrace: {
            frames: [
              {
                filename: 'app.js?v=1',
                function: 'send',
                lineno: 3,
                vars: { prompt: 'secret prompt' },
                context_line: 'const q = "lithium dose"',
                pre_context: ['// Divorce notes'],
                post_context: ['send(q)'],
              },
            ],
          },
        },
      ],
    },
    threads: { values: [{ id: 1, stacktrace: { frames: [{ function: 'f', vars: { title: THREAD_TITLE } }] } }] },
    request: {
      url: 'https://app.test/chat?thread=t-1',
      cookies: { 'sb-access-token': JWT },
      data: '{"message":"my private question"}',
      query_string: 'thread=t-1',
      env: { REMOTE_ADDR: '1.2.3.4' },
      headers: {
        Authorization: `Bearer ${JWT}`,
        Cookie: 'sb=1',
        'User-Agent': 'Mozilla/5.0',
        Referer: 'https://app.test/search?q=lithium',
      },
    },
    user: { id: 'user-1', email: 'alice@example.com', ip_address: '1.2.3.4' },
    contexts: {
      trace: { trace_id: 'tr1', span_id: 'sp1', data: { 'http.query': 'q=lithium', 'url.full': 'https://a.test/x?q=lithium', 'http.method': 'GET' } },
      browser: { name: 'Chrome', version: '131' },
      thread: { title: THREAD_TITLE },
    },
    tags: { thread_title: THREAD_TITLE, route: '/chat' },
    extra: { threadTitle: THREAD_TITLE, toolCall: TOOL_CALL, attempt: 2 },
    breadcrumbs: [{ category: 'console', data: { arguments: [TOOL_CALL] } }],
    spans: [{ span_id: 's', trace_id: 't', start_timestamp: 1, description: 'GET /api/search?q=lithium', data: { 'url.full': 'https://a.test/x?q=lithium' } }],
  }) as unknown as ErrorEvent;

const EVENT_LEAKS = [JWT, 'alice@example.com', 'my private question', 'Divorce', 'lithium', 'merger', 'secret prompt', '1.2.3.4', 'pod-7'];

describe('scrubEvent (beforeSend)', () => {
  it('covers every content-bearing event field', () => {
    const out = JSON.parse(dump(scrubEvent(fullEvent())));
    expectNoLeak(out, EVENT_LEAKS);
    expect(out.request).toEqual({
      url: 'https://app.test/chat',
      headers: { 'User-Agent': 'Mozilla/5.0', Referer: 'https://app.test/search' },
    });
    expect(out.user).toEqual({ id: 'user-1' });
    expect(out.exception.values[0].stacktrace.frames[0]).toEqual({ filename: 'app.js', function: 'send', lineno: 3 });
    expect(out.exception.values[0].mechanism).toEqual({ type: 'onerror', handled: false });
    expect(out.threads.values[0].stacktrace.frames[0]).toEqual({ function: 'f' });
    expect(out.logentry).toEqual({ message: 'Saved %s', params: [FILTERED, FILTERED, 3] });
    expect(out.contexts.trace).toEqual({ trace_id: 'tr1', span_id: 'sp1', data: { 'http.query': FILTERED, 'url.full': 'https://a.test/x', 'http.method': 'GET' } });
    expect(out.contexts.browser).toEqual({ name: 'Chrome', version: '131' });
    expect(out.contexts.thread).toEqual({ title: FILTERED });
    expect(out.tags).toEqual({ thread_title: FILTERED, route: '/chat' });
    expect(out.extra).toMatchObject({ threadTitle: FILTERED, attempt: 2 });
    expect(out.release).toBe('abc123');
    expect(out.server_name).toBeUndefined();
  });
});

describe('transactions and spans', () => {
  it('scrubs transaction events, their spans and trace data', () => {
    const transaction = {
      ...(fullEvent() as unknown as Record<string, unknown>),
      type: 'transaction',
      transaction: '/search?q=lithium',
    };
    const out = JSON.parse(dump(safeBeforeSendTransaction(transaction as never, {})));
    expectNoLeak(out, EVENT_LEAKS);
    expect(out.transaction).toBe('/search');
    expect(out.spans[0]).toMatchObject({ description: 'GET /api/search?[Filtered]', data: { 'url.full': 'https://a.test/x' } });
    expect(JSON.parse(dump(scrubTransaction(transaction as never)))).toEqual(out);
  });

  it('scrubs a span sent through beforeSendSpan', () => {
    const span = {
      span_id: 's',
      trace_id: 't',
      start_timestamp: 1,
      op: 'http.client',
      description: 'GET https://api.test/search?q=lithium',
      data: { 'http.query': 'q=lithium', 'http.response.status_code': 200, 'user.email': 'alice@example.com' },
    };
    const out = safeBeforeSendSpan(span as never);
    expect(out).toEqual(scrubSpan(span as never));
    expect(out.description).toBe('GET https://api.test/search?[Filtered]');
    expect(out.data).toEqual({ 'http.query': FILTERED, 'http.response.status_code': 200, 'user.email': FILTERED });
  });
});

describe('replay', () => {
  it('strips query strings from navigation, resource and request frames', () => {
    const nav = scrubReplayRecordingEvent({
      type: 5,
      timestamp: 1,
      data: { tag: 'breadcrumb', payload: { category: 'navigation', timestamp: 1, type: 'default', data: { from: '/a?q=lithium', to: '/b?token=x' } } },
    });
    expect(nav?.data.payload).toMatchObject({ data: { from: '/a', to: '/b' } });

    const resource = scrubReplayRecordingEvent({
      type: 5,
      timestamp: 1,
      data: {
        tag: 'performanceSpan',
        payload: {
          op: 'resource.fetch',
          description: 'https://api.test/threads/search?q=lithium',
          startTimestamp: 1,
          endTimestamp: 2,
          data: { method: 'GET', statusCode: 200, request: { size: 10, headers: { cookie: 'sb=1' } }, response: { size: 20, body: 'Divorce' } },
        },
      },
    });
    expect(resource?.data.payload).toEqual({
      op: 'resource.fetch',
      description: 'https://api.test/threads/search',
      startTimestamp: 1,
      endTimestamp: 2,
      data: { method: 'GET', statusCode: 200, request: { size: 10 }, response: { size: 20 } },
    });

    const consoleFrame = scrubReplayRecordingEvent({
      type: 5,
      timestamp: 1,
      data: { tag: 'breadcrumb', payload: { category: 'console', timestamp: 1, type: 'default', level: 'log', message: 'x', data: { logger: 'console', arguments: [TOOL_CALL] } } },
    });
    expectNoLeak(consoleFrame, ['lithium']);

    const options = { type: 5, timestamp: 1, data: { tag: 'options', payload: { maskAllText: true } } };
    expect(scrubReplayRecordingEvent(options)).toBe(options);
  });

  it('strips query strings from the replay event URL list only', () => {
    const integration = scrubReplayEventsIntegration();
    const replay = integration.processEvent({ type: 'replay_event', urls: ['https://app.test/search?q=lithium', '/chat#t-1'] });
    expect(replay).toEqual({ type: 'replay_event', urls: ['https://app.test/search', '/chat'] });
    const other = { type: undefined, message: 'x' };
    expect(integration.processEvent(other)).toBe(other);
  });
});

describe('fail closed', () => {
  const throwing = <T extends object>(base: T, key: string): T =>
    Object.defineProperty({ ...base }, key, {
      enumerable: true,
      get() {
        throw new Error('boom');
      },
    });

  it('drops breadcrumbs and logs whose scrubbing throws', () => {
    expect(safeBeforeBreadcrumb(throwing({ category: 'console' } as Breadcrumb, 'message'))).toBeNull();
    const hostile = new Proxy({}, { ownKeys() { throw new Error('boom'); } });
    expect(safeBeforeSendLog({ level: 'error', message: 'm', attributes: hostile })).toBeNull();
    expect(scrubReplayRecordingEvent(throwing({ type: 5 }, 'data'))).toBeNull();
  });

  it('replaces an event whose scrubbing throws with a minimal one', () => {
    const out = safeBeforeSend(throwing(fullEvent() as unknown as Record<string, unknown>, 'extra') as unknown as ErrorEvent, {});
    expect(out).toEqual(minimalEvent(fullEvent()));
    expect(out).toEqual({
      tags: { scrub_failed: true },
      event_id: 'e1',
      release: 'abc123',
      exception: { values: [{ type: 'Error', stacktrace: { frames: [{ filename: 'app.js', function: 'send', lineno: 3 }] } }] },
    });
    expectNoLeak(out, EVENT_LEAKS);
  });

  it('drops transactions and blanks spans whose scrubbing throws', () => {
    expect(safeBeforeSendTransaction(throwing({ type: 'transaction' }, 'message') as never, {})).toBeNull();
    const span = throwing({ span_id: 's', trace_id: 't', start_timestamp: 1, op: 'http' }, 'description');
    expect(safeBeforeSendSpan(span as never)).toEqual({ span_id: 's', trace_id: 't', start_timestamp: 1, op: 'http', data: {} });
  });
});

describe('Sentry config wiring', () => {
  it('exposes every hook as its fail-closed wrapper and forwards only console errors', () => {
    expect(sentryScrubOptions).toEqual({
      beforeSend: safeBeforeSend,
      beforeSendTransaction: safeBeforeSendTransaction,
      beforeSendSpan: safeBeforeSendSpan,
      beforeBreadcrumb: safeBeforeBreadcrumb,
      beforeSendLog: safeBeforeSendLog,
    });
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
    expect(source).toContain('consoleLoggingIntegration({ levels: SENTRY_CONSOLE_LOG_LEVELS })');
    expect(source).not.toMatch(/levels:\s*\[/);
    expect(source).not.toMatch(/\bbefore(Send|SendTransaction|SendSpan|Breadcrumb|SendLog)\s*:/);
  });

  it('instrumentation-client scrubs replay frames and the replay URL list', () => {
    const source = readFileSync(resolve(__dirname, '../../..', 'instrumentation-client.ts'), 'utf8');
    expect(source).toContain('Sentry.replayIntegration(REPLAY_PRIVACY_OPTIONS)');
    expect(source).toContain('scrubReplayEventsIntegration()');
    // The page-load snapshot is checked right after init (GOO-734).
    expect(source).toMatch(/syncReplayWithUrl\(\s*window\.location\.href,\s*Sentry\.getReplay\(\)/);
    expect(REPLAY_PRIVACY_OPTIONS).toMatchObject({
      maskAllText: true,
      blockAllMedia: true,
      beforeAddRecordingEvent: scrubReplayRecordingEvent,
    });
    expect(REPLAY_PRIVACY_OPTIONS.block).toEqual(
      expect.arrayContaining(['a[href*="?"]'])
    );
  });
});
