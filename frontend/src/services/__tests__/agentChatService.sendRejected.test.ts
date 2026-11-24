/**
 * GOO-772: the chat must be able to tell "the server never accepted this
 * send" from "the reply failed after it started", so a rejected send keeps
 * its text. streamMessage reports both rejection shapes distinctly:
 *  - an HTTP-level failure (the stream never opened) passes the status to
 *    `onError` as its fourth argument;
 *  - a request that never got a response (network down, DNS, CORS) rejects
 *    with `AgentStreamRequestFailed`.
 * A failure after the stream opened carries neither.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/services/apiClient', () => ({
  apiClient: { get: vi.fn(), post: vi.fn() },
}));

vi.mock('@/lib/supabase/client', () => ({
  createClient: () => ({
    auth: { getSession: async () => ({ data: { session: null } }) },
  }),
}));

import {
  AgentStreamRequestFailed,
  agentChatService,
} from '../agentChatService';

const request = {
  messages: [{ role: 'user', content: 'hi' }],
  page_context: { type: 'chat' },
};

function fetchFailing(status: number, body = ''): typeof fetch {
  return vi.fn(async () => ({
    ok: false,
    status,
    body: null,
    text: async () => body,
  })) as unknown as typeof fetch;
}

function fetchStreaming(chunks: string[]): typeof fetch {
  const encoder = new TextEncoder();
  const queue = chunks.map((chunk) => encoder.encode(chunk));
  let index = 0;
  return vi.fn(async () => ({
    ok: true,
    status: 200,
    headers: new Headers({ 'content-type': 'text/event-stream' }),
    body: {
      getReader: () => ({
        async read() {
          if (index >= queue.length) return { done: true, value: undefined };
          return { done: false, value: queue[index++] };
        },
        releaseLock() {},
      }),
    },
  })) as unknown as typeof fetch;
}

describe('streamMessage reports a send the server never accepted (GOO-772)', () => {
  const realFetch = global.fetch;
  afterEach(() => {
    global.fetch = realFetch;
  });

  it.each([400, 403, 409, 422, 500, 503])(
    'passes HTTP %i to onError as the rejected status',
    async (status) => {
      global.fetch = fetchFailing(status, '{"detail":"nope"}');
      const onError = vi.fn();

      await agentChatService.streamMessage(request, { onError });

      expect(onError).toHaveBeenCalledTimes(1);
      expect(onError.mock.calls[0][3]).toBe(status);
    }
  );

  it('rejects with AgentStreamRequestFailed when the request gets no response', async () => {
    const networkDown = new TypeError('Failed to fetch');
    global.fetch = vi.fn(async () => {
      throw networkDown;
    }) as unknown as typeof fetch;
    const onError = vi.fn();

    const sent = agentChatService.streamMessage(request, { onError });

    await expect(sent).rejects.toBeInstanceOf(AgentStreamRequestFailed);
    await expect(sent).rejects.toMatchObject({ cause: networkDown });
    expect(onError).not.toHaveBeenCalled();
  });

  it('still returns quietly when the request is aborted before a response', async () => {
    global.fetch = vi.fn(async () => {
      throw new DOMException('aborted', 'AbortError');
    }) as unknown as typeof fetch;

    await expect(
      agentChatService.streamMessage(request, { onError: vi.fn() })
    ).resolves.toBeUndefined();
  });

  it('rethrows an abort with a custom reason as is, not as a failed send', async () => {
    const controller = new AbortController();
    const reason = new Error('account switched');
    global.fetch = vi.fn(async () => {
      controller.abort(reason);
      throw reason;
    }) as unknown as typeof fetch;

    await expect(
      agentChatService.streamMessage(
        request,
        { onError: vi.fn() },
        controller.signal
      )
    ).rejects.toBe(reason);
  });

  it('gives no rejected status for a server error frame after the stream opened', async () => {
    global.fetch = fetchStreaming([
      'data: {"error":"model failed","category":"internal"}\n\n',
    ]);
    const onError = vi.fn();

    await agentChatService.streamMessage(request, { onError });

    expect(onError).toHaveBeenCalledTimes(1);
    expect(onError.mock.calls[0][3]).toBeUndefined();
  });
});
