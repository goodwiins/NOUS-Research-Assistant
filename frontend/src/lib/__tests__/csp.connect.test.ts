/**
 * GOO-729: the explicit connect-src list, built from public config and the
 * request host, shipped as Content-Security-Policy-Report-Only.
 */
import { NextRequest, NextResponse } from 'next/server';
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  buildCsp,
  connectSources,
  connectSrcReportOnly,
  GOOGLE_ANALYTICS_CONNECT_ORIGINS,
  CSP_REPORT_PATH,
  reportingEndpoints,
  STATSIG_CONNECT_ORIGINS,
  type CspEnv,
} from '../csp';

vi.mock('@/lib/supabase/middleware', () => ({
  updateSession: vi.fn(async (request: NextRequest, headers?: Headers) =>
    NextResponse.next({ request: { headers: headers ?? request.headers } })
  ),
}));

const PROD: CspEnv = { NODE_ENV: 'production' };
const page = (url: string): URL => new URL(url);
const BARE = ['https:', 'http:', 'wss:', 'ws:', '*', 'https://*', 'wss://*'];

describe('connectSources', () => {
  it('uses the configured API origin and its WebSocket twin', () => {
    expect(
      connectSources(
        { ...PROD, NEXT_PUBLIC_API_URL: 'https://api.one.test/api/v1' },
        page('https://one.test/chat')
      )
    ).toEqual(["'self'", 'https://api.one.test', 'wss://api.one.test']);
  });

  it('adds a separately configured WebSocket origin', () => {
    expect(
      connectSources(
        {
          ...PROD,
          NEXT_PUBLIC_API_URL: 'https://api.one.test',
          NEXT_PUBLIC_WS_URL: 'wss://ws.one.test/socket',
        },
        page('https://one.test/')
      )
    ).toContain('wss://ws.one.test');
  });

  it('derives the API host from the page host when no API URL is set', () => {
    expect(connectSources(PROD, page('https://dev-app.two.test/x'))).toEqual([
      "'self'",
      'https://dev-api.two.test',
      'wss://dev-api.two.test',
    ]);
    expect(connectSources(PROD, page('https://app.two.test/'))).toContain(
      'https://api.two.test'
    );
  });

  it('adds nothing beyond self for a same-origin relative API base', () => {
    expect(
      connectSources(
        { ...PROD, NEXT_PUBLIC_API_BASE_URL: '/api/v1' },
        page('https://three.test/')
      )
    ).toEqual(["'self'", 'https://three.test', 'wss://three.test']);
  });

  it('adds Supabase and the Sentry ingest host from config', () => {
    const sources = connectSources(
      {
        ...PROD,
        NEXT_PUBLIC_API_URL: 'https://api.one.test',
        NEXT_PUBLIC_SUPABASE_URL: 'https://abcd.supabase.co',
        NEXT_PUBLIC_SENTRY_DSN: 'https://k3y@o1.ingest.sentry.io/42',
      },
      page('https://one.test/')
    );
    expect(sources).toContain('https://abcd.supabase.co');
    expect(sources).toContain('https://o1.ingest.sentry.io');
  });

  it.each([
    ['google-analytics', {}, [...GOOGLE_ANALYTICS_CONNECT_ORIGINS]],
    ['plausible', {}, ['https://plausible.io']],
    [
      'custom',
      { NEXT_PUBLIC_ANALYTICS_ENDPOINT: 'https://collect.one.test/e' },
      ['https://collect.one.test'],
    ],
  ])('adds analytics hosts for provider %s', (provider, extra, expected) => {
    const sources = connectSources(
      { ...PROD, NEXT_PUBLIC_ANALYTICS_PROVIDER: provider, ...extra },
      page('https://one.test/')
    );
    for (const origin of expected) expect(sources).toContain(origin);
  });

  it('adds no analytics host when analytics is off', () => {
    const sources = connectSources(
      { ...PROD, NEXT_PUBLIC_ANALYTICS_PROVIDER: 'none' },
      page('https://one.test/')
    );
    expect(sources.some((s) => s.includes('google'))).toBe(false);
    expect(sources).not.toContain('https://plausible.io');
  });

  it('adds the Statsig hosts only when a client key is configured', () => {
    const on = connectSources(
      { ...PROD, NEXT_PUBLIC_STATSIG_CLIENT_KEY: 'client-key' },
      page('https://one.test/')
    );
    const off = connectSources(
      { ...PROD, NEXT_PUBLIC_STATSIG_CLIENT_KEY: '  ' },
      page('https://one.test/')
    );
    for (const origin of STATSIG_CONNECT_ORIGINS) {
      expect(on).toContain(origin);
      expect(off).not.toContain(origin);
    }
  });

  it('allows the local backend only in development', () => {
    expect(
      connectSources(
        { NODE_ENV: 'development' },
        page('http://localhost:3000/')
      )
    ).toContain('http://localhost:8000');
    expect(connectSources(PROD, page('https://one.test/'))).not.toContain(
      'http://localhost:8000'
    );
  });

  it('ignores malformed or wrong-scheme config', () => {
    expect(
      connectSources(
        {
          ...PROD,
          NEXT_PUBLIC_API_URL: 'https://api.one.test',
          NEXT_PUBLIC_SUPABASE_URL: 'not a url',
          NEXT_PUBLIC_ANALYTICS_PROVIDER: 'custom',
          NEXT_PUBLIC_ANALYTICS_ENDPOINT: 'javascript:alert(1)',
        },
        page('https://one.test/')
      )
    ).toEqual(["'self'", 'https://api.one.test', 'wss://api.one.test']);
  });

  it.each([
    [PROD, page('https://one.test/')],
    [{ ...PROD, NEXT_PUBLIC_API_URL: 'https://api.one.test' }, null],
    [{ NODE_ENV: 'development' }, page('http://localhost:3000/')],
    [
      {
        ...PROD,
        NEXT_PUBLIC_ANALYTICS_PROVIDER: 'google-analytics',
        NEXT_PUBLIC_STATSIG_CLIENT_KEY: 'k',
      },
      page('https://app.one.test/'),
    ],
  ])('never contains a bare scheme or wildcard host (%j)', (env, url) => {
    const sources = connectSources(env, url);
    for (const bare of BARE) expect(sources).not.toContain(bare);
  });
});

describe('the report-only header', () => {
  const SHAPES: [string, CspEnv, URL | null][] = [
    ['production, host-derived API', PROD, page('https://dev-app.one.test/')],
    [
      'configured API + Sentry DSN',
      {
        ...PROD,
        NEXT_PUBLIC_API_URL: 'https://api.one.test',
        NEXT_PUBLIC_SENTRY_DSN: 'https://k@o1.ingest.sentry.io/42',
      },
      page('https://one.test/'),
    ],
    [
      'development',
      { NODE_ENV: 'development' },
      page('http://localhost:3000/'),
    ],
    [
      'analytics + Statsig',
      {
        ...PROD,
        NEXT_PUBLIC_ANALYTICS_PROVIDER: 'google-analytics',
        NEXT_PUBLIC_STATSIG_CLIENT_KEY: 'k',
      },
      null,
    ],
  ];

  it.each(SHAPES)(
    'holds only connect-src and reports to the same-origin route (%s)',
    (_name, env, url) => {
      const policy = connectSrcReportOnly(env, url);
      const directives = policy.split('; ');
      expect(directives.map((d) => d.split(' ')[0])).toEqual([
        'connect-src',
        'report-uri',
        'report-to',
      ]);
      expect(directives[1]).toBe(`report-uri ${CSP_REPORT_PATH}`);
      expect(directives[2]).toBe('report-to csp-endpoint');
      expect(policy).not.toContain('sentry_key');
      expect(policy).not.toContain('/security/');
    }
  );

  it('points Reporting-Endpoints at the request origin', () => {
    expect(reportingEndpoints(page('https://dev-app.one.test/chat?q=x'))).toBe(
      'csp-endpoint="https://dev-app.one.test/api/csp-report"'
    );
    expect(reportingEndpoints(page('http://localhost:3000/'))).toBe(
      'csp-endpoint="http://localhost:3000/api/csp-report"'
    );
  });

  it('leaves the enforced connect-src unchanged until reports are clean', () => {
    expect(buildCsp('n', PROD)).toContain("connect-src 'self' https: wss:");
  });
});

describe('proxy', () => {
  afterEach(() => vi.unstubAllEnvs());

  it('sends the report-only connect-src and same-origin reporting for the request host', async () => {
    vi.stubEnv('NEXT_PUBLIC_API_URL', '');
    vi.stubEnv('NEXT_PUBLIC_API_BASE_URL', '');
    vi.stubEnv('NEXT_PUBLIC_SENTRY_DSN', 'https://k@o1.ingest.sentry.io/42');
    const { proxy } = await import('../../../proxy');
    const response = await proxy(
      new NextRequest('https://dev-app.four.test/chat')
    );
    const reportOnly =
      response.headers.get('Content-Security-Policy-Report-Only') ?? '';
    expect(reportOnly).toContain('https://dev-api.four.test');
    expect(reportOnly).not.toMatch(/(^| )(https|wss):( |;|$)/);
    expect(reportOnly).toContain('report-uri /api/csp-report');
    expect(response.headers.get('Reporting-Endpoints')).toBe(
      'csp-endpoint="https://dev-app.four.test/api/csp-report"'
    );
    expect(response.headers.get('Content-Security-Policy')).toContain(
      "default-src 'self'"
    );
  });
});
