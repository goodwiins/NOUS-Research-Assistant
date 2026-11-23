/**
 * GOO-727: only a 401 from our own backend origin may sign the user out.
 * The API client passes absolute URLs straight through, so a 401 from a
 * third-party host can reach the same error handlers.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { APIErrorClass } from '@/types/api';

import { isBackendSessionFailure } from '../backendAuthFailure';

function apiError(status: number, requestUrl?: string): APIErrorClass {
  const error = new APIErrorClass({
    message: 'nope',
    status_code: status,
    type: 'auth_error',
  });
  error.requestUrl = requestUrl;
  return error;
}

const PAGE = 'https://app.example';

describe('isBackendSessionFailure', () => {
  it.each([
    ['relative base, relative request', '/api/v1', '/api/v1/documents'],
    [
      'relative base, same-origin absolute request',
      '/api/v1',
      'https://app.example/api/v1/documents',
    ],
    [
      'absolute backend base',
      'https://api.example/api/v1',
      'https://api.example/api/v1/documents/1',
    ],
  ])('accepts a backend 401 (%s)', (_case, base, url) => {
    expect(isBackendSessionFailure(apiError(401, url), base, PAGE)).toBe(true);
  });

  it.each([
    ['third-party host', '/api/v1', 'https://other.example/api/v1/documents'],
    [
      'look-alike host',
      'https://api.example/api/v1',
      'https://api.example.evil.test/api/v1',
    ],
    [
      'different port',
      'https://api.example/api/v1',
      'https://api.example:8443/api/v1',
    ],
  ])('rejects a 401 from another origin (%s)', (_case, base, url) => {
    expect(isBackendSessionFailure(apiError(401, url), base, PAGE)).toBe(false);
  });

  it('rejects non-401s, missing URLs and plain errors', () => {
    expect(
      isBackendSessionFailure(apiError(403, '/api/v1/documents'), '/api/v1', PAGE)
    ).toBe(false);
    expect(isBackendSessionFailure(apiError(401), '/api/v1', PAGE)).toBe(false);
    expect(
      isBackendSessionFailure(new Error('401 Unauthorized'), '/api/v1', PAGE)
    ).toBe(false);
    expect(isBackendSessionFailure(undefined, '/api/v1', PAGE)).toBe(false);
  });
});

describe('APIClient records the request URL on errors', () => {
  beforeEach(() => {
    vi.resetModules();
    vi.doMock('@/lib/supabase/client', () => ({
      createClient: () => ({
        auth: {
          getSession: vi.fn().mockResolvedValue({ data: { session: null } }),
        },
      }),
    }));
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation(
        async () =>
          new Response(JSON.stringify({ detail: 'Not authenticated' }), {
            status: 401,
            headers: { 'content-type': 'application/json' },
          })
      )
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  async function caught(url: string): Promise<APIErrorClass> {
    const { APIClient } = await import('@/services/api-client');
    const { APIErrorClass: Fresh } = await import('@/types/api');
    const error = await new APIClient('https://api.example/api/v1')
      .get(url, { retries: 0 })
      .catch((e: unknown) => e);
    expect(error).toBeInstanceOf(Fresh);
    return error as APIErrorClass;
  }

  it('for a backend path', async () => {
    expect((await caught('/documents')).requestUrl).toBe(
      'https://api.example/api/v1/documents'
    );
  });

  it('for an absolute third-party URL', async () => {
    const error = await caught('https://other.example/x');
    expect(error.requestUrl).toBe('https://other.example/x');
    expect(
      isBackendSessionFailure(error, 'https://api.example/api/v1', PAGE)
    ).toBe(false);
  });
});
