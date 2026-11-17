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
    // Persisted rows from an earlier test must not answer this one's turn.
    useChatStore.getState().reset();
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
    /** Persisted rows, newest first as the API pages them, given the
     * client_message_id the submitted user turn carried. */
    persisted?: (userClientId: string) => Array<Record<string, unknown>>;
    /** Every message refresh fails, so refreshMessages returns false. */
    refreshFails?: boolean;
  }): Promise<{
    setMessages: ReturnType<typeof vi.fn>;
    resumeCalls: unknown[][];
  }> {
    let userClientId = '';
    vi.mocked(workspaceService.listMessages).mockImplementation((async () => {
      if (opts.refreshFails) throw new Error('listMessages down');
      return {
        messages: opts.persisted?.(userClientId) ?? [],
        has_more: false,
      };
    }) as never);
    resumeStreamMock.mockResolvedValue({ status: 'idle' });
    // The drop is held until the loading state has rendered: the resume
    // effect only re-runs when isLoading actually flips back to false.
    let dropStream!: () => void;
    const dropped = new Promise<void>((resolve) => {
      dropStream = resolve;
    });
    streamMessageMock.mockImplementation(
      async (
        req: { messages: Array<{ client_message_id?: string }> },
        cb: LostStreamCallbacks
      ) => {
        userClientId =
          [...req.messages].reverse().find((m) => m.client_message_id)
            ?.client_message_id ?? '';
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
        persisted: (cid) => [userRow('u-1', cid, 1)],
      });
      await expectFailureBubble(setMessages);
    }
  );

  // Rows are newest first, as the API pages them. `at` orders them.
  function userRow(
    id: string,
    clientMessageId: string | null,
    at: number
  ): Record<string, unknown> {
    return {
      id,
      thread_id: THREAD_ID,
      role: 'user',
      content: `question ${id}`,
      client_message_id: clientMessageId,
      created_at: `2026-11-16T00:00:0${at}Z`,
    };
  }
  function assistantRow(id: string, at: number): Record<string, unknown> {
    return {
      id,
      thread_id: THREAD_ID,
      role: 'assistant',
      content: `answer ${id}`,
      created_at: `2026-11-16T00:00:0${at}Z`,
    };
  }

  async function expectFailureBubble(
    setMessages: ReturnType<typeof vi.fn>
  ): Promise<void> {
    await waitFor(() =>
      expect(
        lastErrorBubble(setMessages.mock.calls as never)?.error?.message
      ).toBe('This response failed to generate. Please try again.')
    );
    expect(useAgentActivityStore.getState().runs[THREAD_ID]?.state).toBe(
      'error'
    );
  }

  async function expectNoVerdictFailure(
    setMessages: ReturnType<typeof vi.fn>
  ): Promise<void> {
    await waitFor(() =>
      expect(useAgentActivityStore.getState().runs[THREAD_ID]?.state).toBe(
        'done'
      )
    );
    expect(lastErrorBubble(setMessages.mock.calls as never)).toBeUndefined();
  }

  it("shows the persisted reply with no error when the turn's user row has one", async () => {
    const { setMessages } = await loseStream({
      tokenBeforeDrop: false,
      resume: { status: 'idle' },
      persisted: (cid) => [assistantRow('a-1', 2), userRow('u-1', cid, 1)],
    });
    await expectNoVerdictFailure(setMessages);
    expect(
      useChatStore
        .getState()
        .messages[THREAD_ID]?.some((m) => m.content === 'answer a-1')
    ).toBe(true);
  });

  it("fails the turn when only an earlier turn's reply precedes its user row", async () => {
    const { setMessages } = await loseStream({
      tokenBeforeDrop: false,
      resume: { status: 'idle' },
      persisted: (cid) => [
        userRow('u-2', cid, 3),
        assistantRow('a-1', 2),
        userRow('u-1', 'older-turn', 1),
      ],
    });
    await expectFailureBubble(setMessages);
  });

  it("does not count a later turn's reply after an intervening user row", async () => {
    // Another tab sent a newer turn that was answered; ours was not.
    const { setMessages } = await loseStream({
      tokenBeforeDrop: false,
      resume: { status: 'idle' },
      persisted: (cid) => [
        assistantRow('a-2', 3),
        userRow('u-2', 'other-tab-turn', 2),
        userRow('u-1', cid, 1),
      ],
    });
    await expectFailureBubble(setMessages);
  });

  it('gives no failure verdict when the refresh fails', async () => {
    const { setMessages } = await loseStream({
      tokenBeforeDrop: false,
      resume: { status: 'idle' },
      refreshFails: true,
    });
    await expectNoVerdictFailure(setMessages);
  });

  it("gives no failure verdict when the turn's user row is not loaded", async () => {
    const { setMessages } = await loseStream({
      tokenBeforeDrop: false,
      resume: { status: 'idle' },
      // Newest row is someone else's unanswered turn: guessing from it would
      // wrongly fail ours.
      persisted: () => [
        userRow('u-2', 'other-tab-turn', 3),
        assistantRow('a-1', 2),
        userRow('u-1', null, 1),
      ],
    });
    await expectNoVerdictFailure(setMessages);
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
