import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import type { Breadcrumb, ErrorEvent, Log } from '@sentry/nextjs';
import {
  FILTERED,
  MAX_KEYS,
  MAX_STRING_LENGTH,
  TRUNCATED_KEYS,
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
    expect(source).toContain('beforeAddRecordingEvent: scrubReplayRecordingEvent');
    expect(source).toContain('scrubReplayEventsIntegration()');
    expect(source).toContain('maskAllText: true');
    expect(source).toContain('blockAllMedia: true');
  });
});

// GOO-735: tuning from the #2265 review. Credentials are assembled at
// runtime (see the note at the top of the file).
describe('tuning: ordinary error messages stay readable', () => {
  it.each([
    'Unexpected token undefined in JSON at position 0',
    'token validation failed',
    'Token expired, please sign in again',
    'basic authentication failed',
    'Basic authentication required',
    'authorization: required',
    'cookie: not set',
    'Cookie: missing',
    'Invalid data: expected a number',
  ])('keeps %j', (message) => {
    expect(scrubString(message)).toBe(message);
  });

  it('keeps http.route and next.route span attributes', () => {
    const span = {
      span_id: 's',
      trace_id: 't',
      start_timestamp: 1,
      data: { 'http.route': '/chat/[threadId]', 'next.route': '/api/threads/[id]' },
    };
    expect(safeBeforeSendSpan(span as never).data).toEqual(span.data);
  });

  it('still filters a JSON SSE frame and real credentials after those words', () => {
    expect(scrubString(SSE_LINE)).toBe('data: [Filtered]');
    const tokenValue = ['tok', 'A1b2C3d4E5f6'].join('_');
    expect(scrubString(`Token ${tokenValue}`)).toBe('Token [Filtered]');
    expect(scrubString(`Basic ${BASIC}`)).toBe('Basic [Filtered]');
    expect(scrubString('Bearer abcdefghijklmnop')).toBe('Bearer [Filtered]');
    expect(scrubString(`authorization: ${tokenValue}`)).toBe('Authorization: [Filtered]');
    expect(scrubString('cookie: sid=abc123; theme=dark')).toBe('Cookie: [Filtered]');
  });

  it('filters Bearer even before a word Token/Basic would keep (#2278 delta review)', () => {
    // The scheme rule needs 8+ characters (as at base), so these are the
    // SCHEME_PROSE words; after Bearer none of them is kept.
    for (const word of ['required', 'undefined', 'validation', 'authentication', 'exchange', 'rejected']) {
      expect(scrubString(`Bearer ${word}`), word).toBe('Bearer [Filtered]');
      expect(scrubString(`Token ${word}`), word).toBe(`Token ${word}`);
      expect(scrubString(`Token ${word}.`), word).toBe(`Token ${word}.`);
      expect(scrubString(`Bearer ${word}.`), word).toBe('Bearer [Filtered]');
    }
  });

  it('stays linear on a long run of dots after a scheme (#2278 delta review)', () => {
    // A run of dots that does not end the value is the slow shape for
    // /[.!]+$/: every start position scans to the end and fails.
    for (const value of [
      `Token ${'.'.repeat(4080)}x`,
      `Authorization: ${'.'.repeat(4000)}x`,
    ]) {
      scrubString(value); // warm up
      const start = performance.now();
      for (let i = 0; i < 100; i += 1) scrubString(value);
      // The quadratic regex took 8-21 ms per call here (0.8 s+ for 100).
      expect(performance.now() - start, value.slice(0, 14)).toBeLessThan(250);
    }
  });
});

describe('tuning: secrets that used to pass', () => {
  it('filters key ids in any case and separator style (#2278 delta review)', () => {
    for (const key of [
      'keyId', 'key_id', 'apiKeyId', 'KEY_ID', 'API_KEY_ID', 'AWS_ACCESS_KEY_ID',
      'X-API-KEY-ID', 'apiKey_ID', 'apiKey.id', 'APIKeyId', 'passkeyId', 'KeyIds',
    ]) {
      expect(isSensitiveKey(key), key).toBe(true);
      expect(scrubData({ [key]: 'AKIAABCDEFGH1234' }), key).toEqual({ [key]: FILTERED });
    }
  });

  it('does not treat a word ending in "key" as a key id', () => {
    for (const key of ['monkeyId', 'hockeyIds', 'turkeyID']) {
      expect(isSensitiveKey(key), key).toBe(false);
    }
    for (const key of ['keyId', 'key_id', 'apiKeyID', 'signing_key_ids']) {
      expect(isSensitiveKey(key), key).toBe(true);
    }
  });

  it('denies session and key identifiers but keeps other ids', () => {
    for (const key of ['sessionId', 'session_id', 'apiKeyId', 'api_key_id', 'keyIds']) {
      expect(isSensitiveKey(key), key).toBe(true);
    }
    const out = scrubData({
      sessionId: 'sess-1',
      apiKeyId: 'key-1',
      threadId: 'th-1',
      threadID: 'th-2',
      span_id: 'sp-1',
      paid: 'yes-paid',
      valid: 'valid-text',
    });
    expect(out).toEqual({
      sessionId: FILTERED,
      apiKeyId: FILTERED,
      threadId: 'th-1',
      threadID: 'th-2',
      span_id: 'sp-1',
      paid: FILTERED,
      valid: FILTERED,
    });
  });

  it('filters x-api-key, password, api_key and client_secret written name: value', () => {
    const secret = ['s3cr', 'et-', 'v4lue'].join('');
    const out = scrubString(
      [
        `x-api-key: ${secret}`,
        `X-API-KEY:${secret}`,
        `password: "${secret}"`,
        `api_key: ${secret}`,
        `client_secret: ${secret}`,
      ].join('\n')
    );
    expectNoLeak(out, [secret]);
    expect(out).toContain('x-api-key: [Filtered]');
    expect(out).toContain('password: [Filtered]');
    expect(out).toContain('api_key: [Filtered]');
    // Passwords are often plain words, so the name alone is enough here.
    expect(scrubString(`password: ${'sunflower'}`)).toBe('password: [Filtered]');
  });

  it('filters credentials in a URL', () => {
    const pass = ['hun', 'ter2', '-x'].join('');
    const url = ['https://', 'deploy', ':', pass, '@db.example.test/app'].join('');
    const out = scrubString(`connect failed: ${url}`);
    expectNoLeak(out, [pass, 'deploy:']);
    expect(out).toBe('connect failed: https://[Filtered]@db.example.test/app');
    const pg = ['postgres://', 'admin', ':', pass, '@10.0.0.5:5432/db'].join('');
    expect(scrubString(pg)).toBe('postgres://[Filtered]@10.0.0.5:5432/db');
    expect(scrubString('see https://example.test:8443/path')).toBe(
      'see https://example.test:8443/path'
    );
  });

  it('keeps only known technical strings in device, app and os contexts', () => {
    const event = {
      event_id: 'e',
      contexts: {
        device: { name: "Alice's iPhone", model: 'iPhone15,2', family: 'iOS', memory_size: 4096 },
        os: { name: 'macOS', version: '15.1', raw: 'Mozilla/5.0 (custom alice build)' },
        app: { app_name: 'NOUS', app_version: '1.2.3', device_app_hash: 'free text alice' },
        browser: { name: 'Chrome', version: '131' },
      },
    };
    const out = JSON.parse(dump(scrubEvent(event as never)));
    expect(out.contexts.device).toEqual({ model: 'iPhone15,2', family: 'iOS', memory_size: 4096 });
    expect(out.contexts.os).toEqual({ name: 'macOS', version: '15.1' });
    expect(out.contexts.app).toEqual({ app_name: 'NOUS', app_version: '1.2.3' });
    expect(out.contexts.browser).toEqual({ name: 'Chrome', version: '131' });
    expectNoLeak(out, ['alice', 'Alice']);
  });
});

describe('tuning: robustness', () => {
  const boom = (): never => {
    throw new Error('boom');
  };

  it('reads each span field safely in the fallback', () => {
    const span = Object.defineProperties(
      { trace_id: 't', start_timestamp: 1, op: 'http' },
      {
        span_id: { enumerable: true, get: boom },
        description: { enumerable: true, get: boom },
      }
    );
    expect(safeBeforeSendSpan(span as never)).toEqual({
      span_id: '',
      trace_id: 't',
      start_timestamp: 1,
      op: 'http',
      data: {},
    });
  });

  it(`scans at most ${MAX_KEYS} keys per object and counts the rest`, () => {
    const wide: Record<string, number> = {};
    for (let i = 0; i < MAX_KEYS + 25; i += 1) wide[`k${i}`] = i;
    const out = scrubData(wide);
    expect(Object.keys(out)).toHaveLength(MAX_KEYS + 1);
    expect(out[TRUNCATED_KEYS]).toBe(25);
    expect(out.k0).toBe(0);
    expect(out[`k${MAX_KEYS}`]).toBeUndefined();
  });

  it.each([
    ['5 MB of scheme-like text', 'ab://cd:'.repeat(625_000)],
    ['5 MB of password: prefixes', 'password: '.repeat(500_000)],
    ['5 MB of Token words', 'Token abcdefgh '.repeat(333_334)],
    ['5 MB of cookie: prose', 'cookie: not set '.repeat(312_500)],
  ])('scrubs %s in well under 50 ms', (_label, input) => {
    scrubString('warm up');
    const start = performance.now();
    scrubString(input);
    expect(performance.now() - start).toBeLessThan(50);
  });

  it('scrubs a 100k-key object quickly', () => {
    const wide: Record<string, string> = {};
    for (let i = 0; i < 100_000; i += 1) wide[`title${i}`] = 'x';
    const start = performance.now();
    scrubData(wide);
    expect(performance.now() - start).toBeLessThan(50);
  });
});

describe('tuning: nothing the earlier scrubber caught may pass (#2278 review R1-R7)', () => {
  // Credential shapes, assembled at runtime.
  const letters = ['xQ', 'pLmN', 'oRtz'].join('');
  const hex = ['9f8e', '7d6c', '5b4a', '3921'].join('');
  const uuid = ['3f2a1b4c', '9d8e', '4f7a', 'b6c5', 'd4e3f2a1b0c9'].join('-');
  const b64 = Buffer.from('svc:letmein-now').toString('base64');
  const SHAPES = [letters, letters.toLowerCase(), hex, uuid, b64, JWT.split('.')[2]];

  const expectFiltered = (input: string, secret: string): void => {
    const out = scrubString(input);
    expect(out, input).not.toContain(secret);
    expect(out, input).toContain(FILTERED);
  };

  it.each(['ApiKey', 'Key', 'SSWS', 'OAuth', 'DPoP', 'Custom', 'Bearer', 'Basic'])(
    'R1: Authorization with the %s scheme is filtered for every credential shape',
    (scheme) => {
      for (const cred of SHAPES) {
        expectFiltered(`Authorization: ${scheme} ${cred}`, cred);
        expectFiltered(`proxy-authorization=${scheme} ${cred}`, cred);
      }
    }
  );

  it('R2: a bare letters-only Authorization value is filtered', () => {
    for (const cred of [letters, letters.toLowerCase(), 'abcdefghijklmnopqrstuvwxyzabcde']) {
      expectFiltered(`Authorization: ${cred}`, cred);
      expectFiltered(`Proxy-Authorization: ${cred}`, cred);
    }
    // Only a single prose word stays.
    expect(scrubString('Authorization: required')).toBe('Authorization: required');
    expectFiltered('Authorization: required abcdefgh', 'abcdefgh');
  });

  it('R3: a letters-only credential after Token or Basic is filtered', () => {
    for (const cred of [letters, letters.toLowerCase(), 'qwertyuiopasdf']) {
      expectFiltered(`Token ${cred}`, cred);
      expectFiltered(`basic ${cred}`, cred);
    }
  });

  it('R4: any value after cookie:, Set-Cookie: or cookie= is filtered', () => {
    for (const prefix of ['cookie: ', 'Cookie:', 'COOKIE: ', 'Set-Cookie: ', 'set-cookie:', 'cookie=']) {
      for (const cred of SHAPES) expectFiltered(`${prefix}${cred}`, cred);
    }
  });

  it.each([
    'sid = v4lue',
    '; sid=v4lue',
    '     sid=v4lue',
    'sid:v4lue',
    '{sid: v4lue}',
    'missing, sid=v4lue',
    'not set; sid=v4lue',
  ])('R5: Cookie: %j is filtered', (value) => {
    expectFiltered(`Cookie: ${value}`, 'v4lue');
  });

  it.each([
    `data: ${letters}`,
    `data:${letters}`,
    `   data: ${letters}`,
    `event: message\ndata: ${letters}`,
    `id: 7\ndata: ${letters}`,
  ])('R6: a plain-text SSE frame %j is filtered', (input) => {
    expectFiltered(input, letters);
  });

  it('R7: SSE frames carrying message content are filtered', () => {
    expectFiltered('data: my password is hunter-two', 'hunter-two');
    expectFiltered('data: user said my diagnosis is bipolar', 'diagnosis');
  });

  it('keeps the readability fixes only for exact prose', () => {
    expect(scrubString('Unexpected token undefined in JSON')).toBe('Unexpected token undefined in JSON');
    expect(scrubString('basic authentication failed')).toBe('basic authentication failed');
    expect(scrubString('cookie: not set')).toBe('cookie: not set');
    expect(scrubString('Invalid data: expected a number')).toBe('Invalid data: expected a number');
  });
});
