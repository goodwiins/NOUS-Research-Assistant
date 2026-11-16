// @vitest-environment node
import { NextRequest } from 'next/server';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const getSession = vi.fn();
const getUser = vi.fn();
vi.mock('@/lib/supabase/server', () => ({
  createClient: async () => ({ auth: { getUser, getSession } }),
}));

const fetchMock = vi.fn();

beforeEach(() => {
  getUser.mockResolvedValue({
    data: { user: { id: 'verified-user', email: 'user@example.org' } },
    error: null,
  });
  getSession.mockImplementation(() => {
    throw new Error('user bearer must not be read');
  });
  fetchMock
    .mockReset()
    .mockResolvedValue(
      new Response(JSON.stringify({ id: 'run_123' }), { status: 200 })
    );
  vi.stubGlobal('fetch', fetchMock);
  vi.stubEnv('TRIGGER_SECRET_KEY', 'trigger-secret');
  vi.stubEnv(
    'TRIGGER_AGENT_SIGNING_KEY',
    'agent-task-signing-secret-at-least-32-chars'
  );
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

function request(): NextRequest {
  return new NextRequest('http://localhost/api/trigger/agent/execute', {
    method: 'POST',
    body: JSON.stringify({ messages: [{ role: 'user', content: 'hello' }] }),
  });
}

it('sends only a short-lived agent ticket and verified user identity to Trigger', async () => {
  vi.resetModules();
  const { POST } = await import('../../../app/api/trigger/agent/execute/route');
  const response = await POST(request());
  expect(response.status).toBe(200);
  const body = JSON.parse(fetchMock.mock.calls[0][1].body);
  expect(body.payload.userId).toBe('verified-user');
  expect(body.payload.backendToken).toEqual(expect.any(String));
  expect(body.payload).not.toHaveProperty('accessToken');
  const claims = JSON.parse(
    Buffer.from(body.payload.backendToken.split('.')[1], 'base64url').toString()
  );
  expect(claims).toMatchObject({
    sub: 'verified-user',
    aud: 'nous-backend',
    scope: 'trigger:agent',
  });
  expect(claims).not.toHaveProperty('email');
  expect(claims.exp - claims.iat).toBe(90 * 60);
  expect(getSession).not.toHaveBeenCalled();
});

it('fails closed when the agent signing key is absent', async () => {
  vi.stubEnv('TRIGGER_AGENT_SIGNING_KEY', '');
  vi.resetModules();
  const { POST } = await import('../../../app/api/trigger/agent/execute/route');
  const response = await POST(request());
  expect(response.status).toBe(503);
  expect(fetchMock).not.toHaveBeenCalled();
});
