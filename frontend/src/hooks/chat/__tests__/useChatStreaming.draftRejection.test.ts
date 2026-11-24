/**
 * GOO-751: a rejected send must not lose the user's draft, must say why, and
 * must not leave old text in the composer for Enter to re-send.
 *
 * - A new turn owns the draft. The composer clears its text before
 *   handleSubmit runs, so every rejection puts the text back (unless the user
 *   has typed something new since) and shows a toast.
 * - A regenerate or edit (`historyOverride`) never owns it: the draft stays
 *   exactly as typed whether the send is accepted or rejected.
 * - `isBusy` is the one busy check: loading, streaming, a confirmation
 *   pending on the displayed thread, or an active harness run.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  act,
  cleanup,
  renderHook,
  waitFor,
  type RenderHookResult,
} from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createElement, type ReactElement, type ReactNode } from 'react';
import type { ChatPageMessage } from '@/components/chat/shared/cloudMessageView';
import type {
  UseChatStreamingParams,
  UseChatStreamingReturn,
} from '@/hooks/chat/useChatStreaming';
import { makeChatPageMessage } from '@/test/chatMessageFactory';
import { useChatStore } from '@/store/chat-store';
import { useAgentActivityStore } from '@/stores/agentActivityStore';

const mocks = vi.hoisted(() => ({
  toastError: vi.fn(),
  streamMessage: vi.fn(),
  resumeStream: vi.fn(),
  params: new URLSearchParams(),
  harness: {
    executionProvider: 'nous',
    deviceId: null,
    workspaceId: null,
    devices: [],
    workspaces: [],
    pendingRequests: [],
    connectionState: 'idle',
    statusLabel: 'NOUS',
    disabledReason: null as string | null,
    canSend: true,
    runId: null as string | null,
    loadApproval: vi.fn(),
    receive: vi.fn(),
    markConnectionLost: vi.fn(),
  },
}));

vi.mock('react-hot-toast', () => ({
  default: Object.assign(vi.fn(), {
    error: (...args: unknown[]) => mocks.toastError(...args),
    success: vi.fn(),
  }),
}));

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => mocks.params,
}));

vi.mock('@/hooks/chat/useHarnessConnection', () => ({
  useHarnessConnection: () => mocks.harness,
}));

vi.mock('@/services/agentChatService', async (importOriginal) => ({
  AgentStreamRequestFailed: (
    await importOriginal<typeof import('@/services/agentChatService')>()
  ).AgentStreamRequestFailed,
  agentChatService: {
    streamMessage: (...args: unknown[]) => mocks.streamMessage(...args),
    streamConfirm: vi.fn(),
    resumeStream: (...args: unknown[]) => mocks.resumeStream(...args),
    cancelPendingConfirmation: vi.fn().mockResolvedValue(undefined),
  },
}));

vi.mock('@/services/workspaceService', () => ({
  workspaceService: {
    createThread: vi.fn(),
    createMessage: vi.fn().mockResolvedValue({ id: 'db-msg-1' }),
    listMessages: vi.fn().mockResolvedValue({ messages: [], has_more: false }),
  },
}));

const HISTORY: ChatPageMessage[] = [
  makeChatPageMessage({
    id: 'u1',
    role: 'user',
    content: 'old question',
    timestamp: 1,
  }),
  makeChatPageMessage({
    id: 'a1',
    role: 'assistant',
    content: 'old answer',
    timestamp: 2,
  }),
];

const BUSY = 'Wait for the current reply to finish before sending.';

function wrapper({ children }: { children: ReactNode }): ReactElement {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return createElement(QueryClientProvider, { client }, children);
}

function makeParams(): UseChatStreamingParams {
  return {
    messages: [] as ChatPageMessage[],
    displayedMessages: HISTORY,
    setMessages: vi.fn(),
    conversations: [],
    setConversations: vi.fn(),
    dbConversation: null,
    enableRAG: false,
  };
}

async function renderStreaming(): Promise<
  RenderHookResult<UseChatStreamingReturn, unknown>
> {
  const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
  const params = makeParams();
  return renderHook(() => useChatStreaming(params), { wrapper });
}

/** Type into the composer, then let the composer clear it the way ChatInput
 * does just before the runtime calls handleSubmit with the text. */
function composerSent(
  result: { current: UseChatStreamingReturn },
  text: string
): void {
  act(() => result.current.setInput(text));
  act(() => result.current.setInput(''));
}

beforeEach(() => {
  mocks.toastError.mockReset();
  mocks.streamMessage.mockReset();
  mocks.streamMessage.mockImplementation(
    (_req: unknown, cb: { onDone?: (p?: unknown) => void }) => {
      cb.onDone?.({});
      return Promise.resolve();
    }
  );
  mocks.resumeStream.mockReset();
  mocks.resumeStream.mockResolvedValue({ status: 'idle' });
  mocks.params = new URLSearchParams();
  Object.assign(mocks.harness, {
    canSend: true,
    runId: null,
    disabledReason: null,
  });
  useChatStore.getState().reset();
  useChatStore.setState({
    currentThreadId: 'thread-A',
    isStreaming: false,
    messages: { 'thread-A': [] },
    messagePagination: {
      'thread-A': {
        hasMore: false,
        loadingOlder: false,
        loadedCount: 0,
        message_branching: false,
        branch_document_id: null,
        selected_message_id: null,
      },
    },
    messageFreshness: { 'thread-A': 'fresh' },
  });
  useAgentActivityStore.setState({ runs: {} });
});

afterEach(() => cleanup());

describe('a rejected new turn keeps the draft and says why', () => {
  it('restores the text when a pre-send check rejects it', async () => {
    mocks.params = new URLSearchParams('pdfDocumentId=doc-1');
    const { result } = await renderStreaming();
    composerSent(result, 'my question');

    await act(async () => {
      await result.current.handleSubmit('my question');
    });

    expect(mocks.toastError).toHaveBeenCalledWith(
      'Resolve or dismiss the PDF request before sending.'
    );
    expect(result.current.input).toBe('my question');
    expect(mocks.streamMessage).not.toHaveBeenCalled();
  });

  it('restores the text and toasts while a turn is streaming', async () => {
    useChatStore.setState({ isStreaming: true });
    const { result } = await renderStreaming();
    composerSent(result, 'my question');

    await act(async () => {
      await result.current.handleSubmit('my question');
    });

    expect(mocks.toastError).toHaveBeenCalledWith(BUSY);
    expect(result.current.input).toBe('my question');
    expect(mocks.streamMessage).not.toHaveBeenCalled();
  });

  it('treats an active harness run as busy', async () => {
    mocks.harness.runId = 'run-1';
    const { result } = await renderStreaming();
    expect(result.current.isBusy).toBe(true);
    composerSent(result, 'my question');

    await act(async () => {
      await result.current.handleSubmit('my question');
    });

    expect(mocks.toastError).toHaveBeenCalledWith(BUSY);
    expect(result.current.input).toBe('my question');
    expect(mocks.streamMessage).not.toHaveBeenCalled();
  });

  it('treats a confirmation pending on the displayed thread as busy, with its own message', async () => {
    mocks.resumeStream.mockImplementation(
      async (
        threadId: string,
        _after: number,
        cb: { onConfirmation?: (t: string, c: Record<string, unknown>) => void }
      ) => {
        cb.onConfirmation?.(threadId, {
          approval_id: 'a'.repeat(64),
          tool_name: 'create_note',
          tool_args: {},
        });
        return { status: 'resumed' };
      }
    );
    const { result } = await renderStreaming();
    await waitFor(() => expect(result.current.isBusy).toBe(true));
    composerSent(result, 'my question');

    await act(async () => {
      await result.current.handleSubmit('my question');
    });

    expect(mocks.toastError).toHaveBeenCalledWith(
      'Approve or reject the pending action before sending.'
    );
    expect(result.current.input).toBe('my question');
    expect(mocks.streamMessage).not.toHaveBeenCalled();
  });

  it('is not busy when nothing is in flight', async () => {
    const { result } = await renderStreaming();
    expect(result.current.isBusy).toBe(false);
  });

  it("shows the harness's reason when the agent can't take a message", async () => {
    mocks.harness.canSend = false;
    mocks.harness.disabledReason = 'Reconnect your computer to send.';
    const { result } = await renderStreaming();
    composerSent(result, 'my question');

    await act(async () => {
      await result.current.handleSubmit('my question');
    });

    expect(mocks.toastError).toHaveBeenCalledWith(
      'Reconnect your computer to send.'
    );
    expect(result.current.input).toBe('my question');
    expect(mocks.streamMessage).not.toHaveBeenCalled();
  });

  it('falls back to a generic reason when the harness gives none', async () => {
    mocks.harness.canSend = false;
    const { result } = await renderStreaming();

    await act(async () => {
      await result.current.handleSubmit('my question');
    });

    expect(mocks.toastError).toHaveBeenCalledWith(
      'The selected agent can’t take a message right now.'
    );
  });

  it('rejects a second send while the first holds the submit lock, keeping its text', async () => {
    let finish: () => void = () => {};
    mocks.streamMessage.mockImplementation(
      (_req: unknown, cb: { onDone?: (p?: unknown) => void }) =>
        new Promise<void>((resolve) => {
          finish = () => {
            cb.onDone?.({});
            resolve();
          };
        })
    );
    const { result } = await renderStreaming();
    const submit = result.current.handleSubmit;

    let first: Promise<void> = Promise.resolve();
    await act(async () => {
      first = submit('first');
      await Promise.resolve();
      await submit('second');
    });

    expect(mocks.toastError).toHaveBeenCalledWith(BUSY);
    expect(result.current.input).toBe('second');
    expect(mocks.streamMessage).toHaveBeenCalledTimes(1);

    await act(async () => {
      finish();
      await first;
    });
  });

  it('does not overwrite text the user has typed since', async () => {
    useChatStore.setState({ isStreaming: true });
    const { result } = await renderStreaming();
    act(() => result.current.setInput('typed since'));

    await act(async () => {
      await result.current.handleSubmit('my question');
    });

    expect(mocks.toastError).toHaveBeenCalledWith(BUSY);
    expect(result.current.input).toBe('typed since');
  });

  it('ignores an empty send without a toast', async () => {
    const { result } = await renderStreaming();

    await act(async () => {
      await result.current.handleSubmit('   ');
    });

    expect(mocks.toastError).not.toHaveBeenCalled();
    expect(mocks.streamMessage).not.toHaveBeenCalled();
  });
});

describe('a regenerate or edit never touches the draft', () => {
  it('keeps the draft when the regenerate is rejected', async () => {
    mocks.params = new URLSearchParams('pdfDocumentId=doc-1');
    const { result } = await renderStreaming();
    act(() => result.current.setInput('my draft'));

    await act(async () => {
      await result.current.handleSubmit('old question', []);
    });

    expect(mocks.toastError).toHaveBeenCalled();
    expect(result.current.input).toBe('my draft');
  });

  it('does not put the old message in an empty composer when rejected', async () => {
    useChatStore.setState({ isStreaming: true });
    const { result } = await renderStreaming();

    await act(async () => {
      await result.current.handleSubmit('old question', []);
    });

    expect(mocks.toastError).toHaveBeenCalledWith(BUSY);
    expect(result.current.input).toBe('');
  });

  it('keeps the draft when the regenerate is accepted', async () => {
    const { result } = await renderStreaming();
    act(() => result.current.setInput('my draft'));

    await act(async () => {
      await result.current.handleSubmit('old question', []);
    });

    expect(mocks.streamMessage).toHaveBeenCalledTimes(1);
    expect(result.current.input).toBe('my draft');
  });

  it('does not write the old message into the composer when the preflight rolls back', async () => {
    useChatStore.setState({ currentThreadId: null });
    const { result } = await renderStreaming();
    act(() => result.current.setInput('my draft'));

    await act(async () => {
      await result.current.handleSubmit('old question', []);
    });

    expect(mocks.streamMessage).not.toHaveBeenCalled();
    expect(result.current.input).toBe('my draft');
  });

  it('still clears the draft when a new turn is accepted', async () => {
    const { result } = await renderStreaming();
    act(() => result.current.setInput('new question'));

    await act(async () => {
      await result.current.handleSubmit();
    });

    expect(mocks.streamMessage).toHaveBeenCalledTimes(1);
    expect(result.current.input).toBe('');
  });
});

/** What the real streamMessage does for an HTTP-level failure: onError with
 * the client-derived category, the local auth failure and the status, then
 * resolve (agentChatService.sendRejected.test.ts pins this). */
function serverRejects(status: number) {
  return (
    _req: unknown,
    cb: {
      onError?: (
        e: string,
        c?: string,
        l?: string,
        rejectedStatus?: number
      ) => void;
    }
  ): Promise<void> => {
    const category =
      status === 429
        ? 'rate_limited'
        : status >= 400 && status < 500 && status !== 401 && status !== 403
          ? 'invalid_request'
          : undefined;
    const local =
      status === 401
        ? 'authentication_required'
        : status === 403
          ? 'permission_denied'
          : undefined;
    cb.onError?.(`Stream failed (${status}): nope`, category, local, status);
    return Promise.resolve();
  };
}

type Written = ChatPageMessage & {
  sendFailed?: { message: string };
};

/** Replay every setMessages call (arrays and updater functions) the way
 * React would, and return the resulting local transcript. */
function lastWrittenMessages(
  setMessages: ReturnType<typeof vi.fn>
): Written[] {
  return setMessages.mock.calls.reduce<Written[]>(
    (state, [next]) =>
      typeof next === 'function'
        ? (next as (prev: Written[]) => Written[])(state)
        : (next as Written[]),
    []
  );
}

async function renderWithParams(): Promise<{
  result: { current: UseChatStreamingReturn };
  params: UseChatStreamingParams;
}> {
  const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
  const params = makeParams();
  const { result } = renderHook(() => useChatStreaming(params), { wrapper });
  return { result, params };
}

const NOT_SENT_SERVER = 'The server rejected your message, so it wasn’t sent. Edit it or retry.';
const NOT_SENT_DOWN = 'The server couldn’t take your message, so it wasn’t sent. Retry in a moment.';
const NOT_SENT_NETWORK = 'Couldn’t reach the server, so your message wasn’t sent. Check your connection and retry.';
const NOT_SENT_BUSY = 'This chat is busy, so your message wasn’t sent. Retry when the current reply finishes.';

describe('a send the server rejects keeps its text (GOO-772)', () => {
  it.each([
    ['400', () => serverRejects(400), NOT_SENT_SERVER],
    ['403', () => serverRejects(403), NOT_SENT_SERVER],
    ['422', () => serverRejects(422), NOT_SENT_SERVER],
    ['409 (busy)', () => serverRejects(409), NOT_SENT_BUSY],
    ['500', () => serverRejects(500), NOT_SENT_DOWN],
    ['503', () => serverRejects(503), NOT_SENT_DOWN],
    [
      'a network error',
      () => async (): Promise<void> => {
        const { AgentStreamRequestFailed } = await import(
          '@/services/agentChatService'
        );
        throw new AgentStreamRequestFailed(new TypeError('Failed to fetch'));
      },
      NOT_SENT_NETWORK,
    ],
  ])(
    'on %s: marks the bubble failed, restores the text and says why',
    async (_label, reject, toastText) => {
      mocks.streamMessage.mockImplementation(reject());
      const { result, params } = await renderWithParams();
      composerSent(result, 'my question');

      await act(async () => {
        await result.current.handleSubmit('my question');
      });

      expect(mocks.toastError).toHaveBeenCalledWith(toastText);
      expect(result.current.input).toBe('my question');
      const written = lastWrittenMessages(
        params.setMessages as ReturnType<typeof vi.fn>
      );
      // The failed bubble stays, marked, with no "response failed" bubble
      // after it: there was no response.
      expect(written).toHaveLength(1);
      expect(written[0]).toMatchObject({
        role: 'user',
        content: 'my question',
        source: 'local-only',
        sendFailed: { message: toastText },
      });
      // The server never stored the turn, so nothing may supersede it.
      expect(written[0].clientMessageId).toBeUndefined();
      expect(result.current.isLoading).toBe(false);
    }
  );

  it('keeps the failed bubble but not the composer when the user has typed since', async () => {
    let reject: () => void = () => {};
    mocks.streamMessage.mockImplementation(
      (
        _req: unknown,
        cb: {
          onError?: (e: string, c?: string, l?: string, s?: number) => void;
        }
      ) =>
        new Promise<void>((resolve) => {
          reject = () => {
            cb.onError?.('Stream failed (500): down', undefined, undefined, 500);
            resolve();
          };
        })
    );
    const { result, params } = await renderWithParams();
    composerSent(result, 'first question');

    let sent: Promise<void> = Promise.resolve();
    await act(async () => {
      sent = result.current.handleSubmit('first question');
      await Promise.resolve();
    });
    act(() => result.current.setInput('typed since'));
    await act(async () => {
      reject();
      await sent;
    });

    expect(result.current.input).toBe('typed since');
    expect(mocks.toastError).toHaveBeenCalledWith(NOT_SENT_DOWN);
    const written = lastWrittenMessages(
      params.setMessages as ReturnType<typeof vi.fn>
    );
    expect(written.at(-1)).toMatchObject({
      content: 'first question',
      sendFailed: { message: NOT_SENT_DOWN },
    });
  });

  it('does not put a rejected regenerate into the composer', async () => {
    mocks.streamMessage.mockImplementation(serverRejects(500));
    const { result, params } = await renderWithParams();

    await act(async () => {
      await result.current.handleSubmit('old question', []);
    });

    expect(result.current.input).toBe('');
    expect(
      lastWrittenMessages(params.setMessages as ReturnType<typeof vi.fn>).at(-1)
    ).toMatchObject({ content: 'old question', sendFailed: {} });
  });

  it('releases the submit lock and loading state, so the next send goes out', async () => {
    mocks.streamMessage.mockImplementationOnce(serverRejects(500));
    const { result } = await renderWithParams();

    await act(async () => {
      await result.current.handleSubmit('first');
    });
    expect(result.current.isLoading).toBe(false);
    expect(result.current.isBusy).toBe(false);

    await act(async () => {
      await result.current.handleSubmit('second');
    });

    expect(mocks.streamMessage).toHaveBeenCalledTimes(2);
    expect(mocks.toastError).not.toHaveBeenCalledWith(BUSY);
  });

  it('leaves a 401 to sign-in recovery instead of marking the send failed', async () => {
    mocks.streamMessage.mockImplementation(serverRejects(401));
    const { result, params } = await renderWithParams();
    composerSent(result, 'my question');

    await act(async () => {
      await result.current.handleSubmit('my question');
    });

    expect(mocks.toastError).not.toHaveBeenCalledWith(
      expect.stringContaining('wasn’t sent')
    );
    expect(
      lastWrittenMessages(params.setMessages as ReturnType<typeof vi.fn>).some(
        (message) => message.sendFailed
      )
    ).toBe(false);
  });

  it('treats a failure after the server accepted the turn as a failed reply, not a failed send', async () => {
    mocks.streamMessage.mockImplementation(
      (
        _req: unknown,
        cb: {
          onRunId?: (id: string) => void;
          onError?: (e: string, c?: string) => void;
        }
      ) => {
        cb.onRunId?.('run-1');
        cb.onError?.('model failed', 'internal');
        return Promise.resolve();
      }
    );
    const { result, params } = await renderWithParams();
    composerSent(result, 'my question');

    await act(async () => {
      await result.current.handleSubmit('my question');
    });

    expect(result.current.input).toBe('');
    const written = lastWrittenMessages(
      params.setMessages as ReturnType<typeof vi.fn>
    );
    expect(written.some((message) => message.sendFailed)).toBe(false);
    expect(written.at(-1)).toMatchObject({ role: 'assistant' });
  });

  it('treats a stream that breaks after it opened as a failed reply, not a failed send', async () => {
    mocks.streamMessage.mockImplementation(
      (_req: unknown, cb: { onStatus?: (p: string) => void }) => {
        cb.onStatus?.('thinking');
        return Promise.reject(new TypeError('network error'));
      }
    );
    const { result, params } = await renderWithParams();
    composerSent(result, 'my question');

    await act(async () => {
      await result.current.handleSubmit('my question');
    });

    expect(result.current.input).toBe('');
    const written = lastWrittenMessages(
      params.setMessages as ReturnType<typeof vi.fn>
    );
    expect(written.some((message) => message.sendFailed)).toBe(false);
    expect(written.at(-1)).toMatchObject({ role: 'assistant' });
  });

  it('retrying an earlier unsent bubble keeps the later ones, in order', async () => {
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const unsentTurn = (content: string, timestamp: number): Written => ({
      ...makeChatPageMessage({ role: 'user', content, timestamp }),
      source: 'local-only',
      sendFailed: { message: NOT_SENT_DOWN },
    });
    const one = unsentTurn('unsent one', 3);
    const two = unsentTurn('unsent two', 4);
    const three = unsentTurn('unsent three', 5);
    const params = { ...makeParams(), messages: [one, two, three] };
    const { result } = renderHook(() => useChatStreaming(params), { wrapper });

    // What useChatComposerActions passes for a retry of an unsent turn:
    // every other row on screen.
    await act(async () => {
      await result.current.handleSubmit('unsent one', [two, three]);
    });

    const firstWrite = (params.setMessages as ReturnType<typeof vi.fn>).mock
      .calls[0][0] as Written[];
    expect(firstWrite.map((message) => message.content)).toEqual([
      'unsent two',
      'unsent three',
      'unsent one',
    ]);
    const request = mocks.streamMessage.mock.calls[0][0] as {
      messages: Array<{ content: string }>;
    };
    expect(request.messages.map((message) => message.content)).toEqual([
      'unsent one',
    ]);
  });

  it('keeps an earlier unsent bubble when the next message goes out', async () => {
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const unsent: Written = {
      ...makeChatPageMessage({
        role: 'user',
        content: 'unsent question',
        timestamp: 3,
      }),
      source: 'local-only',
      sendFailed: { message: NOT_SENT_DOWN },
    };
    const params = { ...makeParams(), messages: [unsent] };
    const { result } = renderHook(() => useChatStreaming(params), { wrapper });

    await act(async () => {
      await result.current.handleSubmit('next question');
    });

    const firstWrite = (params.setMessages as ReturnType<typeof vi.fn>).mock
      .calls[0][0] as Written[];
    expect(firstWrite.map((message) => message.content)).toEqual([
      'unsent question',
      'next question',
    ]);
    // It is never sent as history.
    const request = mocks.streamMessage.mock.calls[0][0] as {
      messages: Array<{ content: string }>;
    };
    expect(request.messages.map((message) => message.content)).not.toContain(
      'unsent question'
    );
  });
});

