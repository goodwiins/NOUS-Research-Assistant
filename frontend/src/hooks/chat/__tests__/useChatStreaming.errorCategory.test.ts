// W2: when the SERVER authored the failure, the error bubble carries the
// server's category. The client-authored categories ('empty-response',
// 'exception') stay client-authored — no server frame exists for those.
// The confirm path additionally used to commit a plain content bubble with no
// `error` block at all; it now gets the same {message, category} shape.
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createElement, type ReactNode } from 'react';
import type { ChatPageMessage } from '@/components/chat/shared/cloudMessageView';
import { useChatStore } from '@/store/chat-store';
import { useAgentActivityStore } from '@/stores/agentActivityStore';
import type { UseChatStreamingParams } from '@/hooks/chat/useChatStreaming';
import { workspaceService } from '@/services/workspaceService';

function wrapper({ children }: { children: ReactNode }): ReactNode {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return createElement(QueryClientProvider, { client }, children);
}

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

const streamMessageMock = vi.fn();
const streamConfirmMock = vi.fn();
const resumeStreamMock = vi.fn();
vi.mock('@/services/agentChatService', () => ({
  agentChatService: {
    streamMessage: (...args: unknown[]) => streamMessageMock(...args),
    streamConfirm: (...args: unknown[]) => streamConfirmMock(...args),
    resumeStream: (...args: unknown[]) => resumeStreamMock(...args),
  },
}));

vi.mock('@/services/workspaceService', () => ({
  workspaceService: {
    createThread: vi.fn(),
    createMessage: vi.fn().mockResolvedValue({ id: 'db-msg-1' }),
    listMessages: vi.fn().mockResolvedValue({ messages: [], has_more: false }),
  },
}));

const THREAD_ID = 'thread-A';

type Setter = (
  next: ChatPageMessage[] | ((prev: ChatPageMessage[]) => ChatPageMessage[])
) => void;

function makeParams(setMessages: Setter): UseChatStreamingParams {
  useChatStore.setState({ currentThreadId: THREAD_ID });
  return {
    messages: [] as ChatPageMessage[],
    displayedMessages: [] as ChatPageMessage[],
    setMessages,
    conversations: [],
    setConversations: vi.fn(),
    activeConversationId: THREAD_ID,
    setActiveConversationId: vi.fn(),
    activeConversationIdRef: { current: THREAD_ID as string | null },
    dbConversation: null,
    setCurrentThread: vi.fn(),
    enableRAG: false,
  };
}

/**
 * Last assistant bubble that carries an `error` block.
 *
 * `setMessages` is a `React.Dispatch<React.SetStateAction<...>>`, so a caller
 * may pass either the next array or a functional updater. Replay the calls in
 * order the way React would — feeding each updater the state the previous call
 * produced — otherwise an updater-form call is invisible here and the bubble
 * it carries reads as `undefined`.
 */
function lastErrorBubble(
  calls: Array<[ChatPageMessage[] | unknown]>
): ChatPageMessage | undefined {
  const states: ChatPageMessage[][] = [];
  let prev: ChatPageMessage[] = [];
  for (const [arg] of calls) {
    const next =
      typeof arg === 'function'
        ? (arg as (p: ChatPageMessage[]) => ChatPageMessage[])(prev)
        : arg;
    if (!Array.isArray(next)) continue;
    prev = next as ChatPageMessage[];
    states.push(prev);
  }
  for (let i = states.length - 1; i >= 0; i -= 1) {
    const withError = [...states[i]].reverse().find((m) => m?.error);
    if (withError) return withError;
  }
  return undefined;
}

async function submitWithStreamError(
  category?: string
): Promise<ChatPageMessage | undefined> {
  streamMessageMock.mockImplementation(
    (
      _req: unknown,
      cb: { onError?: (error: string, category?: string) => void }
    ) => {
      cb.onError?.('upstream exploded', category);
      return Promise.resolve();
    }
  );
  const setMessages = vi.fn();
  const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
  const { result } = renderHook(
    () => useChatStreaming(makeParams(setMessages as Setter)),
    { wrapper }
  );
  await act(async () => {
    await result.current.handleSubmit('hello');
  });
  return lastErrorBubble(setMessages.mock.calls as never);
}

describe('useChatStreaming maps the server error category onto the bubble', () => {
  beforeEach(() => {
    streamMessageMock.mockReset();
    streamConfirmMock.mockReset();
    resumeStreamMock.mockReset();
    resumeStreamMock.mockResolvedValue({ status: 'idle' });
    useAgentActivityStore.setState({ runs: {}, currentThreadId: null });
    useChatStore.setState({
      currentThreadId: THREAD_ID,
      isStreaming: false,
      streamingThreadId: null,
    });
  });

  it('uses the server category when the error frame carried one', async () => {
    const bubble = await submitWithStreamError('rate_limited');
    expect(bubble?.error?.category).toBe('rate_limited');
  });

  it('passes through every server category unchanged', async () => {
    for (const category of ['upstream_timeout', 'conflict', 'model_error']) {
      const bubble = await submitWithStreamError(category);
      expect(bubble?.error?.category).toBe(category);
    }
  });

  it("falls back to the client 'stream-error' when the server sent none", async () => {
    const bubble = await submitWithStreamError(undefined);
    expect(bubble?.error?.category).toBe('stream-error');
  });

  it('keeps the user-facing error message stable regardless of category', async () => {
    const bubble = await submitWithStreamError('conflict');
    expect(bubble?.error?.message).toBe(
      'This response failed to generate. Please try again.'
    );
  });

  it("leaves the client-detected empty response authored as 'empty-response'", async () => {
    streamMessageMock.mockImplementation(
      (_req: unknown, cb: { onDone?: (p?: unknown) => void }) => {
        cb.onDone?.({});
        return Promise.resolve();
      }
    );
    const setMessages = vi.fn();
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const { result } = renderHook(
      () => useChatStreaming(makeParams(setMessages as Setter)),
      { wrapper }
    );
    await act(async () => {
      await result.current.handleSubmit('hello');
    });
    // No server frame exists for "stream ended with no tokens" — the client
    // detected it, so the client keeps authorship of the category.
    expect(
      lastErrorBubble(setMessages.mock.calls as never)?.error?.category
    ).toBe('empty-response');
  });
});

// GOO-600: the service reports a clean EOF with no terminal frame as lost
// observation for the NOUS provider too. The turn must not finish as a failed
// generation; the run stays live and the resume effect reattaches to it.
describe('useChatStreaming NOUS stream that ends without a terminal frame', () => {
  beforeEach(() => {
    streamMessageMock.mockReset();
    streamConfirmMock.mockReset();
    resumeStreamMock.mockReset();
    resumeStreamMock.mockResolvedValue({ status: 'resumed' });
    useAgentActivityStore.setState({ runs: {}, currentThreadId: null });
    useChatStore.setState({
      currentThreadId: THREAD_ID,
      isStreaming: false,
      streamingThreadId: null,
    });
  });

  type LostStreamCallbacks = {
    onConnectionLost?: () => void;
    onError?: (error: string) => void;
    onToken?: (content: string) => void;
  };

  /**
   * Submit a turn whose live stream ends with no terminal frame, then let the
   * resume effect reattach. The stream yields a tick first so the loading
   * state actually renders (the resume effect re-runs on its release), and
   * optionally streams one token before the drop. Returns the setMessages spy
   * and the resumeStream calls made after the submit (the mount-time
   * confirmation probe also calls resumeStream).
   */
  async function loseStream(opts: {
    tokenBeforeDrop: boolean;
    resume: { status: string };
    persisted?: Array<Record<string, unknown>>;
  }): Promise<{
    setMessages: ReturnType<typeof vi.fn>;
    resumeCalls: unknown[][];
  }> {
    vi.mocked(workspaceService.listMessages).mockResolvedValue({
      messages: opts.persisted ?? [],
      has_more: false,
    } as never);
    resumeStreamMock.mockResolvedValue({ status: 'idle' });
    // The drop is held until the loading state has rendered: the resume
    // effect only re-runs when isLoading actually flips back to false.
    let dropStream!: () => void;
    const dropped = new Promise<void>((resolve) => {
      dropStream = resolve;
    });
    streamMessageMock.mockImplementation(
      async (_req: unknown, cb: LostStreamCallbacks) => {
        await dropped;
        if (opts.tokenBeforeDrop) cb.onToken?.('partial');
        cb.onConnectionLost?.();
        cb.onError?.('Stream ended before completion. Please retry.');
      }
    );
    const setMessages = vi.fn();
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const { result } = renderHook(
      () => useChatStreaming(makeParams(setMessages as Setter)),
      { wrapper }
    );
    await act(async () => {
      await new Promise((r) => setTimeout(r, 10));
    });
    const probeCalls = resumeStreamMock.mock.calls.length;
    resumeStreamMock.mockResolvedValue(opts.resume);
    let submitted!: Promise<void>;
    await act(async () => {
      submitted = result.current.handleSubmit('hello');
      await new Promise((r) => setTimeout(r, 10));
    });
    expect(result.current.isLoading).toBe(true);
    await act(async () => {
      dropStream();
      await submitted;
    });
    await waitFor(() =>
      expect(resumeStreamMock.mock.calls.length).toBeGreaterThan(probeCalls)
    );
    await act(async () => {
      await new Promise((r) => setTimeout(r, 30));
    });
    return {
      setMessages,
      resumeCalls: resumeStreamMock.mock.calls.slice(probeCalls),
    };
  }

  it('reattaches to the run instead of showing a failure bubble', async () => {
    const { setMessages, resumeCalls } = await loseStream({
      tokenBeforeDrop: true,
      resume: { status: 'resumed' },
    });
    expect(resumeCalls[0][0]).toBe(THREAD_ID);
    expect(lastErrorBubble(setMessages.mock.calls as never)).toBeUndefined();
    expect(useAgentActivityStore.getState().runs[THREAD_ID]?.state).not.toBe(
      'error'
    );
  });

  // The reattach finds no active run (the worker died, or the stream closed
  // before its first frame and the backend cleared the active stream without
  // an assistant row). The turn must not finish silently: reconcile from the
  // server, show the persisted reply if there is one, and otherwise the
  // generation-failure bubble.
  it.each([
    ['before the first frame', false],
    ['after a streamed token', true],
  ])(
    'shows the failure bubble when the run is gone and no reply was persisted (%s)',
    async (_label, tokenBeforeDrop) => {
      const { setMessages } = await loseStream({
        tokenBeforeDrop,
        resume: { status: 'idle' },
      });
      await waitFor(() =>
        expect(
          lastErrorBubble(setMessages.mock.calls as never)?.error?.message
        ).toBe('This response failed to generate. Please try again.')
      );
      expect(useAgentActivityStore.getState().runs[THREAD_ID]?.state).toBe(
        'error'
      );
    }
  );

  it('shows no error when the run is gone but its reply was persisted', async () => {
    // Newest first, as the API pages them.
    const { setMessages } = await loseStream({
      tokenBeforeDrop: false,
      resume: { status: 'idle' },
      persisted: [
        {
          id: 'a-1',
          thread_id: THREAD_ID,
          role: 'assistant',
          content: 'the persisted answer',
          created_at: '2026-11-16T00:00:02Z',
        },
        {
          id: 'u-1',
          thread_id: THREAD_ID,
          role: 'user',
          content: 'hello',
          created_at: '2026-11-16T00:00:01Z',
        },
      ],
    });
    await waitFor(() =>
      expect(useAgentActivityStore.getState().runs[THREAD_ID]?.state).toBe(
        'done'
      )
    );
    expect(lastErrorBubble(setMessages.mock.calls as never)).toBeUndefined();
    expect(
      useChatStore
        .getState()
        .messages[THREAD_ID]?.some((m) => m.content === 'the persisted answer')
    ).toBe(true);
    expect(useAgentActivityStore.getState().runs[THREAD_ID]?.state).toBe(
      'done'
    );
  });
});

describe('useChatStreaming confirm-path failure bubble', () => {
  const CONFIRMATION = {
    tool_name: 'create_note',
    tool_args: { title: 'parked' },
  };

  beforeEach(() => {
    streamMessageMock.mockReset();
    streamConfirmMock.mockReset();
    resumeStreamMock.mockReset();
    resumeStreamMock.mockImplementation(
      async (
        threadId: string,
        _after: number,
        cb: {
          onConfirmation?: (t: string, c: Record<string, unknown>) => void;
        }
      ) => {
        cb.onConfirmation?.(threadId, CONFIRMATION);
        return { status: 'resumed' };
      }
    );
    useAgentActivityStore.setState({ runs: {}, currentThreadId: null });
    useChatStore.setState({
      currentThreadId: THREAD_ID,
      isStreaming: false,
      streamingThreadId: null,
    });
  });

  async function confirmWithError(
    category?: string
  ): Promise<ChatPageMessage | undefined> {
    streamConfirmMock.mockImplementation(
      async (
        _body: unknown,
        cb: { onError?: (error: string, category?: string) => void }
      ) => {
        cb.onError?.('confirm exploded', category);
      }
    );
    const setMessages = vi.fn();
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const { result } = renderHook(
      () => useChatStreaming(makeParams(setMessages as Setter)),
      { wrapper }
    );
    await waitFor(() =>
      expect(result.current.pendingConfirmation).not.toBeNull()
    );
    await act(async () => {
      await result.current.handleConfirmation(true);
    });
    return lastErrorBubble(setMessages.mock.calls as never);
  }

  it('now commits an error block at all (it used to be a plain content bubble)', async () => {
    const bubble = await confirmWithError('conflict');
    expect(bubble?.error).toBeDefined();
    expect(bubble?.error?.message).toBe(
      'This confirmation failed to complete. Please try again.'
    );
  });

  it('carries the server category on the confirm failure', async () => {
    const bubble = await confirmWithError('conflict');
    expect(bubble?.error?.category).toBe('conflict');
  });

  it('falls back to the client category when the server sent none', async () => {
    const bubble = await confirmWithError(undefined);
    expect(bubble?.error?.category).toBe('stream-error');
  });

  it('keeps raw failure text out of the bubble content', async () => {
    const bubble = await confirmWithError('checkpoint_unavailable');
    expect(bubble?.content).toBe('');
    expect(bubble?.content).not.toContain('confirm exploded');
    expect(bubble?.error?.category).toBe('checkpoint_unavailable');
  });
});
