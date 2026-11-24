/**
 * GOO-788: the composer draft survives a reload of the tab, per thread, and
 * goes away on a successful send and on sign-out. The in-memory per-context
 * drafts (GOO-751) were lost on every reload.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, renderHook } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createElement, type ReactElement, type ReactNode } from 'react';
import type { ChatPageMessage } from '@/components/chat/shared/cloudMessageView';
import type { UseChatStreamingReturn } from '@/hooks/chat/useChatStreaming';
import { useChatStore } from '@/store/chat-store';
import { useAgentActivityStore } from '@/stores/agentActivityStore';
import { useAuthStore } from '@/stores/authStore';

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

vi.mock('@/lib/supabase/client', () => ({
  createClient: () => ({
    auth: {
      onAuthStateChange: vi.fn(),
      signOut: vi.fn().mockResolvedValue({ error: null }),
    },
  }),
}));

vi.mock('@/services/workspaceService', () => ({
  clearWorkspaceServiceCache: vi.fn(),
  workspaceService: {
    createThread: vi.fn(),
    createMessage: vi.fn().mockResolvedValue({ id: 'db-msg-1' }),
    listMessages: vi.fn().mockResolvedValue({ messages: [], has_more: false }),
  },
}));

function wrapper({ children }: { children: ReactNode }): ReactElement {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return createElement(QueryClientProvider, { client }, children);
}

async function renderStreaming(): Promise<{
  result: { current: UseChatStreamingReturn };
}> {
  const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
  const params = {
    messages: [] as ChatPageMessage[],
    displayedMessages: [] as ChatPageMessage[],
    setMessages: vi.fn(),
    conversations: [],
    setConversations: vi.fn(),
    dbConversation: null,
    enableRAG: false,
  };
  return renderHook(() => useChatStreaming(params), { wrapper });
}

function selectThread(threadId: string): void {
  useChatStore.setState({
    currentThreadId: threadId,
    isStreaming: false,
    messages: { [threadId]: [] },
    messagePagination: {
      [threadId]: {
        hasMore: false,
        loadingOlder: false,
        loadedCount: 0,
        message_branching: false,
        branch_document_id: null,
        selected_message_id: null,
      },
    },
    messageFreshness: { [threadId]: 'fresh' },
  });
}

function signIn(userId: string): void {
  useAuthStore.setState({ user: { id: userId } as never });
}

/** Everything in memory goes; the tab's sessionStorage stays. */
function reload(threadId: string): void {
  cleanup();
  useChatStore.getState().reset();
  selectThread(threadId);
}

/** Type, then let the composer clear itself the way ChatInput does just
 * before the runtime calls handleSubmit with the text. */
async function send(
  result: { current: UseChatStreamingReturn },
  text: string
): Promise<void> {
  act(() => result.current.setInput(text));
  act(() => result.current.setInput(''));
  await act(async () => {
    await result.current.handleSubmit(text);
  });
}

beforeEach(() => {
  window.sessionStorage.clear();
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
  selectThread('thread-A');
  useAgentActivityStore.setState({ runs: {} });
  signIn('user-1');
});

afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
});

describe('the composer draft survives a reload (GOO-788)', () => {
  it('restores the draft on its own thread', async () => {
    const { result } = await renderStreaming();
    act(() => result.current.setInput('half-typed question'));

    reload('thread-A');
    const reloaded = await renderStreaming();

    expect(reloaded.result.current.input).toBe('half-typed question');
  });

  it('keeps drafts per thread', async () => {
    const { result } = await renderStreaming();
    act(() => result.current.setInput('draft for A'));

    reload('thread-B');
    const reloaded = await renderStreaming();

    expect(reloaded.result.current.input).toBe('');
  });

  it('never restores another account’s draft', async () => {
    const { result } = await renderStreaming();
    act(() => result.current.setInput('user one draft'));

    reload('thread-A');
    signIn('user-2');
    const reloaded = await renderStreaming();

    expect(reloaded.result.current.input).toBe('');
  });

  it('drops the draft once it is sent', async () => {
    const { result } = await renderStreaming();
    await send(result, 'my question');
    expect(mocks.streamMessage).toHaveBeenCalledTimes(1);

    reload('thread-A');
    const reloaded = await renderStreaming();

    expect(reloaded.result.current.input).toBe('');
  });

  it('keeps the draft when the server rejects the send', async () => {
    mocks.streamMessage.mockImplementation(
      (
        _req: unknown,
        cb: {
          onError?: (e: string, c?: string, l?: string, s?: number) => void;
        }
      ) => {
        cb.onError?.('Stream failed (500): down', undefined, undefined, 500);
        return Promise.resolve();
      }
    );
    const { result } = await renderStreaming();
    await send(result, 'my question');
    expect(mocks.toastError).toHaveBeenCalled();

    reload('thread-A');
    const reloaded = await renderStreaming();

    expect(reloaded.result.current.input).toBe('my question');
  });

  it('is cleared by sign-out', async () => {
    const { result } = await renderStreaming();
    act(() => result.current.setInput('private draft'));

    cleanup();
    await act(async () => {
      await useAuthStore.getState().signOut();
    });
    reload('thread-A');
    signIn('user-1');
    const reloaded = await renderStreaming();

    expect(reloaded.result.current.input).toBe('');
  });
});
