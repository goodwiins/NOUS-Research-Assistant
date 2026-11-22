// @vitest-environment node
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock
    .mockReset()
    .mockResolvedValue(
      new Response(JSON.stringify({ status: 'ok' }), { status: 200 })
    );
  vi.stubGlobal('fetch', fetchMock);
  vi.stubEnv('BACKEND_API_URL', '');
  vi.stubEnv('SUPABASE_SERVICE_ROLE_KEY', 'supabase-high-value-secret');
  vi.stubEnv(
    'TRIGGER_SERVICE_SIGNING_KEY',
    'trigger-service-signing-secret-32-chars'
  );
  vi.stubEnv('TRIGGER_SERVICE_USER_ID', '9f181a6c-dcc0-42d8-b810-b20ad1d42bee');
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

it('uses HTTPS and a short-lived service credential for backend calls', async () => {
  vi.resetModules();
  const { backendClient } =
    await import('../../../../src/trigger/_lib/backend-client');
  await backendClient.post('/api/v1/arxiv/ingest', { paper_ids: ['123'] });
  const [url, options] = fetchMock.mock.calls[0];
  expect(url).toBe('https://localhost:8000/api/v1/arxiv/ingest');
  const token = options.headers.Authorization.replace(/^Bearer /, '');
  expect(token).not.toContain('supabase-high-value-secret');
  const claims = JSON.parse(
    Buffer.from(token.split('.')[1], 'base64url').toString()
  );
  expect(claims).toMatchObject({
    aud: 'nous-backend',
    iss: 'nous-trigger',
    scope: 'trigger:service',
    sub: '9f181a6c-dcc0-42d8-b810-b20ad1d42bee',
    method: 'POST',
    path: '/api/v1/arxiv/ingest',
  });
  expect(claims.exp - claims.iat).toBeLessThanOrEqual(300);
});

it('fails closed without the service signing key', async () => {
  vi.stubEnv('TRIGGER_SERVICE_SIGNING_KEY', '');
  vi.resetModules();
  const { backendClient } =
    await import('../../../../src/trigger/_lib/backend-client');
  expect(() => backendClient.post('/api/v1/arxiv/ingest', {})).toThrow();
  expect(fetchMock).not.toHaveBeenCalled();
});

it('uses an explicit agent ticket instead of minting a service credential', async () => {
  vi.resetModules();
  const { backendClient } =
    await import('../../../../src/trigger/_lib/backend-client');
  await backendClient.get('/api/v1/agent/jobs/id', undefined, {
    Authorization: 'Bearer agent-ticket',
  });
  expect(fetchMock.mock.calls[0][1].headers.Authorization).toBe(
    'Bearer agent-ticket'
  );
});
