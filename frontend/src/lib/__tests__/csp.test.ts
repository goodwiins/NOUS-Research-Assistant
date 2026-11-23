/**
 * GOO-722: img-src must name the sources the app really uses and must not
 * allow an arbitrary https origin.
 */
import { NextRequest, NextResponse } from 'next/server';
import { describe, expect, it, vi } from 'vitest';

import { buildCsp, GOOGLE_ANALYTICS_IMG_ORIGINS, imgSrc } from '../csp';

vi.mock('@/lib/supabase/middleware', () => ({
  updateSession: vi.fn(async (request: NextRequest, headers?: Headers) =>
    NextResponse.next({ request: { headers: headers ?? request.headers } })
  ),
}));

import { proxy } from '../../../proxy';

function directive(csp: string, name: string): string[] {
  const found = csp
    .split(';')
    .map((part) => part.trim().split(/\s+/))
    .find(([key]) => key === name);
  if (!found) throw new Error(`missing ${name}`);
  return found.slice(1);
}

const PRODUCTION = { NODE_ENV: 'production' };

describe('img-src', () => {
  it('allows only self, data: and blob: by default', () => {
    expect(directive(buildCsp('n', PRODUCTION), 'img-src')).toEqual([
      "'self'",
      'data:',
      'blob:',
    ]);
  });

  it.each([
    PRODUCTION,
    { NODE_ENV: 'development' },
    { NODE_ENV: 'production', NEXT_PUBLIC_ANALYTICS_PROVIDER: 'none' },
    {
      NODE_ENV: 'production',
      NEXT_PUBLIC_ANALYTICS_PROVIDER: 'google-analytics',
    },
  ])('never allows a bare scheme or wildcard (%j)', (env) => {
    const sources = directive(buildCsp('n', env), 'img-src');
    expect(sources).not.toContain('https:');
    expect(sources).not.toContain('http:');
    expect(sources).not.toContain('*');
  });

  it('adds the GA beacon hosts only when GA is the configured provider', () => {
    expect(imgSrc({ NEXT_PUBLIC_ANALYTICS_PROVIDER: 'google-analytics' })).toBe(
      `img-src 'self' data: blob: ${GOOGLE_ANALYTICS_IMG_ORIGINS.join(' ')}`
    );
    expect(imgSrc({ NEXT_PUBLIC_ANALYTICS_PROVIDER: 'mixpanel' })).toBe(
      "img-src 'self' data: blob:"
    );
  });
});

describe('the rest of the policy is unchanged', () => {
  it('keeps the nonce, strict-dynamic and framing rules', () => {
    const csp = buildCsp('abc', PRODUCTION);
    expect(directive(csp, 'script-src')).toEqual([
      "'self'",
      "'nonce-abc'",
      "'strict-dynamic'",
    ]);
    expect(directive(csp, 'frame-ancestors')).toEqual(["'none'"]);
    expect(directive(csp, 'object-src')).toEqual(["'none'"]);
    expect(csp).toContain('upgrade-insecure-requests');
  });

  it('keeps the development-only allowances out of production', () => {
    const dev = buildCsp('abc', { NODE_ENV: 'development' });
    expect(directive(dev, 'script-src')).toContain("'unsafe-eval'");
    expect(directive(dev, 'connect-src')).toContain('http://localhost:8000');
    expect(dev).not.toContain('upgrade-insecure-requests');

    const prod = buildCsp('abc', PRODUCTION);
    expect(directive(prod, 'script-src')).not.toContain("'unsafe-eval'");
    expect(directive(prod, 'connect-src')).not.toContain(
      'http://localhost:8000'
    );
  });
});

describe('proxy', () => {
  it('sends the built policy, without a bare https: image source', async () => {
    const response = await proxy(new NextRequest('https://nous.example/chat'));
    const csp = response.headers.get('Content-Security-Policy') ?? '';
    expect(directive(csp, 'img-src')).not.toContain('https:');
    expect(directive(csp, 'img-src')).toEqual(
      expect.arrayContaining(["'self'", 'data:', 'blob:'])
    );
  });
});
