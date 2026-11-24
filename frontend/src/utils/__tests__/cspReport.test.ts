import { readdirSync, statSync } from 'node:fs';
import { join, resolve } from 'node:path';

import { describe, expect, it, vi } from 'vitest';

import {
  createCspReportSink,
  CSP_REPORT_ROUTES,
  type CspReportSinkOptions,
  KNOWN_CSP_DIRECTIVES,
  MAX_CSP_REPORT_BYTES,
  MAX_REPORTS_PER_REQUEST,
  parseCspReports,
  scrubBlocked,
  scrubDocument,
  type ScrubbedCspReport,
} from '../cspReport';

const LEGACY = 'application/csp-report';
const REPORTING = 'application/reports+json';
const ORIGIN = 'https://dev-app.one.test';

// Values that must never survive scrubbing.
const SECRETS = [
  'token_hash',
  'pkce-code-123',
  'cli-session-77',
  'my private search',
  'thread-4f1c',
  'referrer.example',
  'alert(document.cookie)',
  'bundle.js',
  'john-doe',
];

const legacyBody = (report: Record<string, unknown>): string =>
  JSON.stringify({ 'csp-report': report });

const legacy = (report: Record<string, unknown>): string =>
  legacyBody({
    'effective-directive': 'connect-src',
    'blocked-uri': 'https://exfil.example/',
    'document-uri': `${ORIGIN}/chat`,
    ...report,
  });

const LEAKY_LEGACY = legacyBody({
  'document-uri': `${ORIGIN}/verify-email?token_hash=abc&type=email#frag`,
  referrer: 'https://referrer.example/?q=my%20private%20search',
  'violated-directive': "connect-src 'self' https://api.one.test",
  'effective-directive': 'connect-src',
  'original-policy': "connect-src 'self'; report-uri /api/csp-report",
  disposition: 'report',
  'blocked-uri':
    'https://exfil.example/c?code=pkce-code-123&session_id=cli-session-77',
  'status-code': 200,
  'script-sample': 'alert(document.cookie)',
  'source-file': `${ORIGIN}/_next/bundle.js?v=thread-4f1c`,
  'line-number': 12,
});

function expectNoSecrets(value: unknown): void {
  const text = JSON.stringify(value);
  for (const secret of SECRETS) expect(text).not.toContain(secret);
  expect(text).not.toMatch(/[?#]/);
}

const reportsOf = (type: string | null, body: string): ScrubbedCspReport[] =>
  parseCspReports(type, body, ORIGIN).reports;

describe('parseCspReports (report-uri, application/csp-report)', () => {
  it('keeps only directive, disposition, origins and a listed route', () => {
    const parsed = parseCspReports(LEGACY, LEAKY_LEGACY, ORIGIN);
    expect(parsed).toEqual({
      reports: [
        {
          directive: 'connect-src',
          disposition: 'report',
          blocked: 'https://exfil.example',
          documentOrigin: ORIGIN,
          documentRoute: '/verify-email',
        },
      ],
      overflow: 0,
    });
    expectNoSecrets(parsed);
  });

  it('accepts a charset parameter on the content type', () => {
    expect(reportsOf(`${LEGACY}; charset=utf-8`, LEAKY_LEGACY)).toHaveLength(1);
  });

  it('falls back to the first token of violated-directive', () => {
    const [report] = reportsOf(
      LEGACY,
      legacyBody({
        'violated-directive': "connect-src 'self' https://x.test",
        'blocked-uri': 'wss://ws.other.test/socket?auth=t',
        'document-uri': `${ORIGIN}/`,
      })
    );
    expect(report.directive).toBe('connect-src');
    expect(report.blocked).toBe('wss://ws.other.test');
    expect(report.disposition).toBe('unknown');
  });

  it('maps any path not on the route list to /', () => {
    for (const path of [
      '/john-doe',
      '/hunter-two-password',
      '/cli-session-77?session_id=cli-session-77',
      '/4f1c9e2a-1111-4222-8333-944445555666/chat',
      '/Chat',
      '/api/csp-report',
      '/oauth/callback?code=pkce-code-123',
    ]) {
      const [report] = reportsOf(
        LEGACY,
        legacy({ 'document-uri': `${ORIGIN}${path}` })
      );
      expect(report.documentRoute).toBe('/');
      expectNoSecrets(report);
    }
  });

  it('keeps only the first segment of a listed route', () => {
    const [report] = reportsOf(
      LEGACY,
      legacy({ 'document-uri': `${ORIGIN}/chat/thread-4f1c?q=x` })
    );
    expect(report.documentRoute).toBe('/chat');
  });

  it('drops a report whose page is not on the request origin', () => {
    for (const page of [
      'https://evil.example/chat',
      'https://dev-app.one.test.evil.example/chat',
      'http://dev-app.one.test/chat',
      'https://dev-app.one.test:8443/chat',
      'not a url',
    ]) {
      expect(reportsOf(LEGACY, legacy({ 'document-uri': page }))).toEqual([]);
    }
  });

  it('forwards only known CSP directive names', () => {
    expect(KNOWN_CSP_DIRECTIVES.has('connect-src')).toBe(true);
    for (const directive of ['connect-srcx', 'made-up-src', 'x', '<script>']) {
      expect(
        reportsOf(LEGACY, legacy({ 'effective-directive': directive }))
      ).toEqual([]);
    }
    expect(
      reportsOf(LEGACY, legacy({ 'effective-directive': 'Script-Src-Elem' }))[0]
        .directive
    ).toBe('script-src-elem');
  });
});

describe('parseCspReports (report-to, application/reports+json)', () => {
  const violation = (
    body: Record<string, unknown>
  ): Record<string, unknown> => ({
    type: 'csp-violation',
    age: 3,
    url: `${ORIGIN}/search?q=my%20private%20search`,
    user_agent: 'UA',
    body,
  });

  it('scrubs each csp-violation entry and skips other report types', () => {
    const body = JSON.stringify([
      violation({
        documentURL: `${ORIGIN}/search?q=my%20private%20search`,
        referrer: 'https://referrer.example/',
        blockedURL: 'https://exfil.example/c?code=pkce-code-123',
        effectiveDirective: 'connect-src',
        originalPolicy: "connect-src 'self'",
        sourceFile: `${ORIGIN}/bundle.js?v=thread-4f1c`,
        sample: 'alert(document.cookie)',
        disposition: 'report',
        statusCode: 200,
      }),
      { type: 'deprecation', body: { id: 'x' } },
      violation({
        effectiveDirective: 'script-src-elem',
        blockedURL: 'inline',
        documentURL: `${ORIGIN}/`,
      }),
    ]);
    const parsed = parseCspReports(REPORTING, body, ORIGIN);
    expect(parsed).toEqual({
      reports: [
        {
          directive: 'connect-src',
          disposition: 'report',
          blocked: 'https://exfil.example',
          documentOrigin: ORIGIN,
          documentRoute: '/search',
        },
        {
          directive: 'script-src-elem',
          disposition: 'unknown',
          blocked: 'inline',
          documentOrigin: ORIGIN,
          documentRoute: '/',
        },
      ],
      overflow: 0,
    });
    expectNoSecrets(parsed);
  });

  it('caps a batch and counts what it dropped', () => {
    const entry = violation({
      effectiveDirective: 'connect-src',
      blockedURL: 'https://e.test/',
      documentURL: `${ORIGIN}/`,
    });
    const body = JSON.stringify(Array.from({ length: 8 }, () => entry));
    const parsed = parseCspReports(REPORTING, body, ORIGIN);
    expect(parsed.reports).toHaveLength(MAX_REPORTS_PER_REQUEST);
    expect(parsed.overflow).toBe(8 - MAX_REPORTS_PER_REQUEST);
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
      legacy({ 'effective-directive': '<script>' }),
    ],
    [
      'a missing directive',
      LEGACY,
      legacyBody({ 'blocked-uri': 'https://e.test/' }),
    ],
    ['an empty body', LEGACY, ''],
  ])('%s', (_name, type, body) => {
    expect(parseCspReports(type, body, ORIGIN)).toEqual({
      reports: [],
      overflow: 0,
    });
  });

  it('a valid reports+json batch under a non-report content type', () => {
    const batch = JSON.stringify([
      {
        type: 'csp-violation',
        body: {
          effectiveDirective: 'connect-src',
          blockedURL: 'https://e.test/',
          documentURL: `${ORIGIN}/`,
        },
      },
    ]);
    expect(reportsOf(REPORTING, batch)).toHaveLength(1);
    for (const type of ['application/json', 'text/plain', null]) {
      expect(reportsOf(type, batch)).toEqual([]);
    }
  });

  it('an oversize body, even when otherwise valid', () => {
    expect(
      reportsOf(LEGACY, legacy({ pad: 'x'.repeat(MAX_CSP_REPORT_BYTES) }))
    ).toEqual([]);
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
    for (const input of [42, null, {}, ['https://a.test/x'], 'file:///etc/x']) {
      expect(scrubBlocked(input)).toBe('other');
      expect(scrubDocument(input)).toEqual({ origin: 'other', route: '/' });
    }
  });
});

describe('CSP_REPORT_ROUTES', () => {
  const APP = resolve(__dirname, '../../../app');
  const hasPage = (dir: string): boolean =>
    readdirSync(dir).some((name) => {
      const path = join(dir, name);
      if (statSync(path).isDirectory()) return hasPage(path);
      return /^page\.(t|j)sx?$/.test(name);
    });

  /** First URL segments of every page under app/, route groups flattened. */
  function topLevelRoutes(dir: string): string[] {
    const out: string[] = [];
    for (const name of readdirSync(dir)) {
      const path = join(dir, name);
      if (!statSync(path).isDirectory()) continue;
      if (name.startsWith('_') || name.startsWith('@')) continue;
      if (/^\(.+\)$/.test(name)) {
        out.push(...topLevelRoutes(path));
      } else if (hasPage(path)) {
        out.push(name);
      }
    }
    return out;
  }

  it('matches the top-level page routes under app/ exactly', () => {
    const routes = [...new Set(topLevelRoutes(APP))].sort();
    // A dynamic top-level segment would let any first segment through.
    expect(routes.filter((r) => r.includes('['))).toEqual([]);
    expect(routes).toEqual([...CSP_REPORT_ROUTES].sort());
  });
});

describe('createCspReportSink', () => {
  const report = (blocked = 'https://e.test'): ScrubbedCspReport => ({
    directive: 'connect-src',
    disposition: 'report',
    blocked,
    documentOrigin: ORIGIN,
    documentRoute: '/chat',
  });

  function sink(overrides: Partial<CspReportSinkOptions> = {}): {
    clock: { time: number };
    forward: ReturnType<typeof vi.fn>;
    capReached: ReturnType<typeof vi.fn>;
    sink: ReturnType<typeof createCspReportSink>;
  } {
    const clock = { time: 0 };
    const forward = vi.fn();
    const capReached = vi.fn();
    return {
      clock,
      forward,
      capReached,
      sink: createCspReportSink({
        windowMs: 1000,
        maxForwardsPerWindow: 100,
        maxKeys: 100,
        forward,
        forwardCapReached: capReached,
        now: () => clock.time,
        ...overrides,
      }),
    };
  }

  it('forwards every first-seen key, with no sampling', () => {
    const { sink: s, forward } = sink();
    for (let i = 0; i < 20; i += 1) {
      expect(s.record(report(`https://h${i}.test`))).toBe('forwarded');
    }
    expect(forward).toHaveBeenCalledTimes(20);
  });

  it('forwards a key once per window, then again after it', () => {
    const { sink: s, clock, forward } = sink();
    expect(s.record(report())).toBe('forwarded');
    clock.time = 999;
    expect(s.record(report())).toBe('duplicate');
    expect(s.record(report('https://other.test'))).toBe('forwarded');
    clock.time = 1000;
    expect(s.record(report())).toBe('forwarded');
    expect(forward).toHaveBeenCalledTimes(3);
  });

  it('passes the batch overflow count to the forwarded event', () => {
    const { sink: s, forward } = sink();
    s.record(report(), 3);
    expect(forward).toHaveBeenCalledWith(report(), 3);
  });

  it('caps forwards per window with one cap event, then resumes', () => {
    const {
      sink: s,
      clock,
      forward,
      capReached,
    } = sink({
      maxForwardsPerWindow: 3,
    });
    for (const host of ['a', 'b', 'c']) {
      expect(s.record(report(`https://${host}.test`))).toBe('forwarded');
    }
    expect(s.record(report('https://d.test'))).toBe('capped');
    expect(s.record(report('https://e.test'))).toBe('capped');
    expect(capReached).toHaveBeenCalledTimes(1);
    expect(capReached).toHaveBeenCalledWith(3);
    expect(forward).toHaveBeenCalledTimes(3);

    // A capped key was not remembered, so it forwards in the next window.
    clock.time = 1000;
    expect(s.record(report('https://d.test'))).toBe('forwarded');
    expect(s.record(report('https://f.test'))).toBe('forwarded');
    expect(s.record(report('https://g.test'))).toBe('forwarded');
    expect(s.record(report('https://h.test'))).toBe('capped');
    expect(capReached).toHaveBeenCalledTimes(2);
  });

  it('does not remember a key whose forward failed', () => {
    const { sink: s, forward } = sink();
    forward.mockImplementationOnce(() => {
      throw new Error('sentry down');
    });
    expect(s.record(report())).toBe('failed');
    expect(s.record(report())).toBe('forwarded');
  });

  it('swallows a throwing cap event', () => {
    const { sink: s, capReached } = sink({ maxForwardsPerWindow: 0 });
    capReached.mockImplementation(() => {
      throw new Error('sentry down');
    });
    expect(s.record(report())).toBe('capped');
  });

  it('evicts the oldest keys, not all of them', () => {
    const { sink: s, clock } = sink({ maxKeys: 2 });
    s.record(report('https://a.test'));
    clock.time = 1;
    s.record(report('https://b.test'));
    clock.time = 2;
    s.record(report('https://c.test'));
    // a was evicted; b and c are still deduped.
    expect(s.record(report('https://b.test'))).toBe('duplicate');
    expect(s.record(report('https://c.test'))).toBe('duplicate');
    expect(s.record(report('https://a.test'))).toBe('forwarded');
  });
});
