// @vitest-environment node
import { beforeEach, expect, it, vi } from 'vitest';
import type { AgentExecutePayload } from '../../../../src/trigger/_lib/schemas';

const { postMock, metadataSetMock } = vi.hoisted(() => ({
  postMock: vi.fn(),
  metadataSetMock: vi.fn(),
}));

vi.mock('@trigger.dev/sdk', () => ({
  task: (definition: unknown) => definition,
  logger: { info: vi.fn(), error: vi.fn() },
  metadata: { set: metadataSetMock, del: vi.fn() },
  wait: { for: vi.fn(), createToken: vi.fn(), forToken: vi.fn() },
}));

vi.mock('../../../../src/trigger/_lib/backend-client', () => ({
  backendClient: { post: postMock, get: vi.fn() },
  backendTokenHeaders: (token: string) => ({
    Authorization: `Bearer ${token}`,
  }),
  BackendApiError: class BackendApiError extends Error {},
}));

beforeEach(() => {
  postMock.mockReset();
  metadataSetMock.mockReset();
});

it('records the verified user before backend job creation can fail', async () => {
  const userId = 'user-1';
  const backendToken = `x.${Buffer.from(JSON.stringify({ sub: userId })).toString('base64url')}.x`;
  const payload: AgentExecutePayload = {
    userId,
    backendToken,
    messages: [{ role: 'user', content: 'hello' }],
    pageContext: { type: 'chat' },
  };
  postMock.mockRejectedValue(new Error('backend unavailable'));

  const { executeAgent } =
    await import('../../../../src/trigger/agent/execute-agent');
  const run = (
    executeAgent as unknown as {
      run: (input: AgentExecutePayload) => Promise<unknown>;
    }
  ).run;

  await expect(run(payload)).rejects.toThrow('backend unavailable');
  expect(metadataSetMock).toHaveBeenCalledWith('userId', userId);
});
