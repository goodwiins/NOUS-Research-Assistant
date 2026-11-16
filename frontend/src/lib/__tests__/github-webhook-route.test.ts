// @vitest-environment node
/**
 * GitHub push webhook (GOO-585): a failed Trigger.dev call returns the shared
 * stable error from `triggerUpstreamError`, never the upstream body.
 *
 * Before: the route answered `Trigger.dev error: ${upstream text}` with the
 * upstream status, leaking Trigger's internal error detail to the caller.
 */
import { createHmac } from 'node:crypto';

import { NextRequest } from 'next/server';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const WEBHOOK_SECRET = 'gh_webhook_secret';
const fetchMock = vi.fn();

async function loadRoute(): Promise<
  typeof import('../../../app/api/webhooks/github/route')
> {
  vi.resetModules();
  vi.stubEnv('TRIGGER_SECRET_KEY', 'tr_secret');
  vi.stubEnv('GITHUB_WEBHOOK_SECRET', WEBHOOK_SECRET);
  return import('../../../app/api/webhooks/github/route');
}

function signedPush(body: string): NextRequest {
  const signature = `sha256=${createHmac('sha256', WEBHOOK_SECRET)
    .update(body)
    .digest('hex')}`;
  return new NextRequest('http://localhost/api/webhooks/github', {
    method: 'POST',
    body,
    headers: {
      'x-hub-signature-256': signature,
      'x-github-event': 'push',
    },
  });
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe('POST /api/webhooks/github', () => {
  it('does not return the upstream Trigger error body', async () => {
    const consoleError = vi
      .spyOn(console, 'error')
      .mockImplementation(() => {});
    fetchMock.mockResolvedValue(
      new Response('internal: queue shard eu-3 rejected the task', {
        status: 500,
      })
    );
    const { POST } = await loadRoute();

    const res = await POST(signedPush(JSON.stringify({ ref: 'refs/heads/x' })));
    const text = await res.clone().text();

    expect(res.status).toBe(502);
    expect(await res.json()).toEqual({
      error: 'Agent run service request failed',
    });
    expect(text).not.toContain('queue shard');
    expect(consoleError).toHaveBeenCalledWith(
      expect.stringContaining('queue shard eu-3')
    );
  });

  it('returns the run id when Trigger accepts the push', async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ id: 'run_abc123' }), { status: 200 })
    );
    const { POST } = await loadRoute();

    const res = await POST(signedPush(JSON.stringify({ ref: 'refs/heads/x' })));

    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({
      status: 'triggered',
      runId: 'run_abc123',
    });
  });
});
