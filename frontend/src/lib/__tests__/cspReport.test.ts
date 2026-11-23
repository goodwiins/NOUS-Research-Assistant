import { describe, expect, it, vi } from 'vitest';

import {
  createCspReportSink,
  MAX_CSP_REPORT_BYTES,
  MAX_REPORTS_PER_REQUEST,
  parseCspReports,
  scrubBlocked,
  scrubDocument,
  type ScrubbedCspReport,
} from '../cspReport';

const LEGACY = 'application/csp-report';
const REPORTING = 'application/reports+json';

// Values that must never survive scrubbing.
const SECRETS = [
  'token_hash',
  'pkce-code-123',
  'cli-session-77',
  'my private search',
  'thread-4f1c',
  'referrer.example',
  'alert(document.cookie)',
  'bundle.js?v=',
];

const legacyBody = (report: Record<string, unknown>): string =>
  JSON.stringify({ 'csp-report': report });

const LEAKY_LEGACY = legacyBody({
  'document-uri':
    'https://dev-app.one.test/verify-email?token_hash=abc&type=email#frag',
  referrer: 'https://referrer.example/?q=my%20private%20search',
  'violated-directive': "connect-src 'self' https://api.one.test",
  'effective-directive': 'connect-src',
  'original-policy': "connect-src 'self'; report-uri /api/csp-report",
  disposition: 'report',
  'blocked-uri':
    'https://exfil.example/c?code=pkce-code-123&session_id=cli-session-77',
  'status-code': 200,
  'script-sample': 'alert(document.cookie)',
  'source-file': 'https://dev-app.one.test/_next/bundle.js?v=thread-4f1c',
  'line-number': 12,
});

function expectNoSecrets(value: unknown): void {
  const text = JSON.stringify(value);
  for (const secret of SECRETS) expect(text).not.toContain(secret);
  expect(text).not.toMatch(/[?#]/);
}

describe('parseCspReports (report-uri, application/csp-report)', () => {
  it('keeps only directive, disposition and origins', () => {
    const reports = parseCspReports(LEGACY, LEAKY_LEGACY);
    expect(reports).toEqual([
      {
        directive: 'connect-src',
        disposition: 'report',
        blocked: 'https://exfil.example',
        documentOrigin: 'https://dev-app.one.test',
        documentRoute: '/verify-email',
      },
    ]);
    expectNoSecrets(reports);
  });

  it('accepts a charset parameter on the content type', () => {
    expect(
      parseCspReports(`${LEGACY}; charset=utf-8`, LEAKY_LEGACY)
    ).toHaveLength(1);
  });

  it('falls back to the first token of violated-directive', () => {
    const [report] = parseCspReports(
      LEGACY,
      legacyBody({
        'violated-directive': "connect-src 'self' https://x.test",
        'blocked-uri': 'wss://ws.other.test/socket?auth=t',
        'document-uri': 'https://one.test/',
      })
    );
    expect(report.directive).toBe('connect-src');
    expect(report.blocked).toBe('wss://ws.other.test');
    expect(report.disposition).toBe('unknown');
  });

  it('drops ids and tokens in the page path', () => {
    for (const path of [
      '/cli-auth/cli-session-77?session_id=cli-session-77',
      '/4f1c9e2a-1111-4222-8333-944445555666/chat',
      '/Thread-4f1c/x',
      '/auth/callback?code=pkce-code-123',
    ]) {
      const [report] = parseCspReports(
        LEGACY,
        legacyBody({
          'effective-directive': 'connect-src',
          'blocked-uri': 'https://exfil.example/',
          'document-uri': `https://one.test${path}`,
        })
      );
      expectNoSecrets(report);
      expect(report.documentRoute).toMatch(/^\/[a-z-]*$/);
    }
  });
});

describe('parseCspReports (report-to, application/reports+json)', () => {
  const violation = (
    body: Record<string, unknown>
  ): Record<string, unknown> => ({
    type: 'csp-violation',
    age: 3,
    url: 'https://one.test/search?q=my%20private%20search',
    user_agent: 'UA',
    body,
  });

  it('scrubs each csp-violation entry and skips other report types', () => {
    const body = JSON.stringify([
      violation({
        documentURL: 'https://one.test/search?q=my%20private%20search',
        referrer: 'https://referrer.example/',
        blockedURL: 'https://exfil.example/c?code=pkce-code-123',
        effectiveDirective: 'connect-src',
        originalPolicy: "connect-src 'self'",
        sourceFile: 'https://one.test/bundle.js?v=thread-4f1c',
        sample: 'alert(document.cookie)',
        disposition: 'report',
        statusCode: 200,
      }),
      { type: 'deprecation', body: { id: 'x' } },
      violation({
        effectiveDirective: 'script-src-elem',
        blockedURL: 'inline',
      }),
    ]);
    const reports = parseCspReports(REPORTING, body);
    expect(reports).toEqual([
      {
        directive: 'connect-src',
        disposition: 'report',
        blocked: 'https://exfil.example',
        documentOrigin: 'https://one.test',
        documentRoute: '/search',
      },
      {
        directive: 'script-src-elem',
        disposition: 'unknown',
        blocked: 'inline',
        documentOrigin: 'other',
        documentRoute: '/',
      },
    ]);
    expectNoSecrets(reports);
  });

  it('caps a batch', () => {
    const entry = violation({
      effectiveDirective: 'connect-src',
      blockedURL: 'https://e.test/',
      documentURL: 'https://one.test/',
    });
    const body = JSON.stringify(Array.from({ length: 8 }, () => entry));
    expect(parseCspReports(REPORTING, body)).toHaveLength(
      MAX_REPORTS_PER_REQUEST
    );
  });
});

describe('parseCspReports rejects', () => {
  it.each([
    ['a wrong content type', 'application/json', LEAKY_LEGACY],
    ['no content type', null, LEAKY_LEGACY],
    ['text/plain', 'text/plain', LEAKY_LEGACY],
    ['malformed JSON', LEGACY, '{"csp-report": {'],
    ['a JSON string', LEGACY, '"connect-src"'],
    ['null', LEGACY, 'null'],
    ['an array for csp-report', LEGACY, '[{"csp-report":{}}]'],
    ['csp-report not an object', LEGACY, '{"csp-report":"x"}'],
    ['an object for reports+json', REPORTING, LEAKY_LEGACY],
    [
      'a bogus directive',
      LEGACY,
      legacyBody({ 'effective-directive': '<script>' }),
    ],
    [
      'a missing directive',
      LEGACY,
      legacyBody({ 'blocked-uri': 'https://e.test/' }),
    ],
    ['an empty body', LEGACY, ''],
  ])('%s', (_name, type, body) => {
    expect(parseCspReports(type, body)).toEqual([]);
  });

  it('a valid reports+json batch under a non-report content type', () => {
    const batch = JSON.stringify([
      {
        type: 'csp-violation',
        body: {
          effectiveDirective: 'connect-src',
          blockedURL: 'https://e.test/',
          documentURL: 'https://one.test/',
        },
      },
    ]);
    expect(parseCspReports(REPORTING, batch)).toHaveLength(1);
    for (const type of ['application/json', 'text/plain', null]) {
      expect(parseCspReports(type, batch)).toEqual([]);
    }
  });

  it('an oversize body, even when otherwise valid', () => {
    const padded = legacyBody({
      'effective-directive': 'connect-src',
      'blocked-uri': 'https://e.test/',
      'document-uri': 'https://one.test/',
      pad: 'x'.repeat(MAX_CSP_REPORT_BYTES),
    });
    expect(parseCspReports(LEGACY, padded)).toEqual([]);
  });
});

describe('scrubBlocked / scrubDocument', () => {
  it.each([
    ['https://a.test:8443/p?q=1#f', 'https://a.test:8443'],
    ['wss://a.test/ws?token=t', 'wss://a.test'],
    ['inline', 'inline'],
    ['eval', 'eval'],
    ['data', 'data'],
    ['blob:', 'blob'],
    ['javascript:alert(1)', 'other'],
    ['data:text/html,secret', 'other'],
    ['https://user:pass@a.test/', 'https://a.test'],
    ['not a url', 'other'],
    ['', 'other'],
  ])('blocked %s -> %s', (input, expected) => {
    expect(scrubBlocked(input)).toBe(expected);
  });

  it('never returns a non-string or a path for odd input', () => {
    for (const input of [42, null, {}, ['https://a.test/x']]) {
      expect(scrubBlocked(input)).toBe('other');
      expect(scrubDocument(input)).toEqual({ origin: 'other', route: '/' });
    }
  });
});

describe('createCspReportSink', () => {
  const report = (blocked = 'https://e.test'): ScrubbedCspReport => ({
    directive: 'connect-src',
    disposition: 'report',
    blocked,
    documentOrigin: 'https://one.test',
    documentRoute: '/chat',
  });

  it('forwards a key once per window, then again after it', () => {
    let time = 0;
    const forward = vi.fn();
    const sink = createCspReportSink({
      sampleRate: 1,
      dedupeMs: 1000,
      maxKeys: 10,
      forward,
      now: () => time,
      random: () => 0,
    });
    expect(sink.record(report())).toBe(true);
    time = 999;
    expect(sink.record(report())).toBe(false);
    expect(sink.record(report('https://other.test'))).toBe(true);
    time = 1000;
    expect(sink.record(report())).toBe(true);
    expect(forward).toHaveBeenCalledTimes(3);
  });

  it('samples first-seen reports and still dedupes the dropped ones', () => {
    const forward = vi.fn();
    const rolls = [0.5, 0.05];
    const sink = createCspReportSink({
      sampleRate: 0.1,
      dedupeMs: 1000,
      maxKeys: 10,
      forward,
      now: () => 0,
      random: () => rolls.shift() ?? 0,
    });
    expect(sink.record(report('https://a.test'))).toBe(false);
    expect(sink.record(report('https://a.test'))).toBe(false);
    expect(sink.record(report('https://b.test'))).toBe(true);
    expect(forward).toHaveBeenCalledTimes(1);
  });

  it('keeps memory bounded', () => {
    const forward = vi.fn();
    const sink = createCspReportSink({
      sampleRate: 1,
      dedupeMs: 1000,
      maxKeys: 2,
      forward,
      now: () => 0,
      random: () => 0,
    });
    sink.record(report('https://a.test'));
    sink.record(report('https://b.test'));
    // The third key resets the map, so a is forgotten and forwards again.
    sink.record(report('https://c.test'));
    expect(sink.record(report('https://a.test'))).toBe(true);
  });

  it('swallows a throwing forwarder', () => {
    const sink = createCspReportSink({
      sampleRate: 1,
      dedupeMs: 1000,
      maxKeys: 10,
      forward: () => {
        throw new Error('sentry down');
      },
      random: () => 0,
    });
    expect(() => sink.record(report())).not.toThrow();
  });
});
