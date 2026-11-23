/**
 * GOO-638 (FL-4): the SSE dispatcher's catch wraps every stream callback.
 * When a frame fails (a handler throws, or the JSON is truncated), its
 * warning reaches Sentry through consoleLoggingIntegration, so it must not
 * carry the frame text or the error message, either of which can contain
 * tool args, tool results or approval payloads.
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

import { agentChatService } from '../agentChatService';

const SECRET = 'patient-record-7731';

function fetchWith(chunks: string[]): typeof fetch {
  const encoder = new TextEncoder();
  const queue = chunks.map((c) => encoder.encode(c));
  let i = 0;
  return vi.fn(async () => ({
    ok: true,
    status: 200,
    body: {
      getReader: () => ({
        async read(): Promise<ReadableStreamReadResult<Uint8Array>> {
          if (i >= queue.length) return { done: true, value: undefined };
          return { done: false, value: queue[i++] };
        },
        releaseLock(): void {},
      }),
    },
  })) as unknown as typeof fetch;
}

const request = {
  messages: [{ role: 'user', content: 'hi' }],
  page_context: { type: 'chat' },
};

function warnedText(warn: ReturnType<typeof vi.spyOn>): string {
  return JSON.stringify(
    warn.mock.calls.map((args: unknown[]) =>
      args.map((a) => (a instanceof Error ? `${a.name}: ${a.message}` : a))
    )
  );
}

describe('SSE dispatch failure logging', () => {
  const realFetch = global.fetch;
  afterEach(() => {
    global.fetch = realFetch;
    vi.restoreAllMocks();
  });

  it('a throwing tool callback logs neither the frame nor the error message', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    global.fetch = fetchWith([
      `event: tool_start\ndata: {"tool":"create_project_note","args":{"content":"${SECRET}"}}\n\n`,
    ]);
    await agentChatService.streamMessage(request, {
      onToolStart: (_tool, args) => {
        throw new Error(`bad args ${JSON.stringify(args)}`);
      },
    });

    expect(warn).toHaveBeenCalled();
    const text = warnedText(warn);
    expect(text).not.toContain(SECRET);
    expect(text).toContain('tool_start');
  });

  it('a truncated frame logs neither the frame nor the parse error message', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    global.fetch = fetchWith([
      `event: tool_end\ndata: {"tool":"create_draft","result":"${SECRET}\n\n`,
    ]);
    await agentChatService.streamMessage(request, {});

    expect(warn).toHaveBeenCalled();
    expect(warnedText(warn)).not.toContain(SECRET);
  });
});
