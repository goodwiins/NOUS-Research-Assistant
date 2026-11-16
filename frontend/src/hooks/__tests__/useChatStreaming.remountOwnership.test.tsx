/**
 * GOO-602: stream ownership was tracked per hook instance while the streaming
 * slice it resets is global. Leave /chat mid-stream and return at once: the
 * old instance's late cleanup must not wipe the new instance's live bubble.
 *
 * Mutation check: in useChatStreaming.ts make the claim per instance again
 * (a `useRef(0)` in place of the module-level streamSliceOwner) and the final
 * isStreaming assertion fails.
 */

import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createElement, type ReactNode } from 'react';
import type { ChatPageMessage } from '@/components/chat/shared/cloudMessageView';

const testQueryClient = new QueryClient({
  defaultOptions: { queries: { retry: false } },
});

function wrapper({ children }: { children: ReactNode }): ReactNode {
  return createElement(
    QueryClientProvider,
    { client: testQueryClient },
    children
  );
}

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

const streamMessageMock = vi.fn();
const resumeStreamMock = vi.fn();
vi.mock('@/services/agentChatService', () => ({
  agentChatService: {
    streamMessage: (...args: unknown[]) => streamMessageMock(...args),
    streamConfirm: vi.fn(),
    resumeStream: (...args: unknown[]) => resumeStreamMock(...args),
  },
}));

vi.mock('@/services/workspaceService', () => ({
  workspaceService: {
    createThread: vi.fn(),
    createMessage: vi.fn().mockResolvedValue({ id: 'db-msg-1' }),
    listMessages: vi.fn(),
  },
}));

vi.mock('react-hot-toast', () => ({
  default: { error: vi.fn(), success: vi.fn() },
}));

import { useChatStreaming } from '@/hooks/chat/useChatStreaming';
import { useAgentActivityStore } from '@/stores/agentActivityStore';
import { useChatStore } from '@/store/chat-store';
import { workspaceService } from '@/services/workspaceService';

function makeParams(): Parameters<typeof useChatStreaming>[0] {
  return {
    messages: [] as ChatPageMessage[],
    displayedMessages: [] as ChatPageMessage[],
    setMessages: vi.fn(),
    conversations: [],
    setConversations: vi.fn(),
    activeConversationId: 'thread-A',
    setActiveConversationId: vi.fn(),
    activeConversationIdRef: { current: 'thread-A' as string | null },
    dbConversation: null,
    setCurrentThread: vi.fn(),
    enableRAG: false,
  };
}

type StreamCallbacks = {
  onToken?: (content: string) => void;
  onSeq?: (seq: number) => void;
};

describe('useChatStreaming stream ownership across a remount', () => {
  beforeEach(() => {
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
      cb(0);
      return 1;
    });
    vi.stubGlobal('cancelAnimationFrame', () => {});
    streamMessageMock.mockReset();
    resumeStreamMock.mockReset();
    useAgentActivityStore.setState({ runs: {}, currentThreadId: null });
    useChatStore.getState().reset();
    useChatStore.setState({ currentThreadId: 'thread-A', isStreaming: false });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("keeps the new instance's live slice when the old instance settles late", async () => {
    // The old instance's terminal reconcile is held so it settles after the
    // new instance has claimed the slice.
    let releaseReconcile!: () => void;
    const reconcileHeld = new Promise<void>((resolve) => {
      releaseReconcile = resolve;
    });
    vi.mocked(workspaceService.listMessages).mockImplementation((async () => {
      await reconcileHeld;
      return { messages: [], has_more: false };
    }) as never);

    streamMessageMock.mockImplementation(
      (_req: unknown, cb: StreamCallbacks, signal: AbortSignal) => {
        cb.onSeq?.(1);
        cb.onToken?.('old partial');
        // The fetch ends when the unmount aborts it.
        return new Promise<void>((resolve) => {
          signal.addEventListener('abort', () => resolve());
        });
      }
    );
    // Before the remount, resumeStream is only the cold-load confirmation
    // probe: nothing is parked.
    let remounted = false;
    resumeStreamMock.mockImplementation(
      (
        _t: string,
        _after: number,
        cb: StreamCallbacks,
        signal: AbortSignal
      ) => {
        if (!remounted) return Promise.resolve({ status: 'idle' });
        cb.onSeq?.(1);
        cb.onToken?.('new live');
        return new Promise((resolve) => {
          signal.addEventListener('abort', () =>
            resolve({ status: 'aborted' })
          );
        });
      }
    );

    const first = renderHook(() => useChatStreaming(makeParams()), {
      wrapper,
    });
    await act(async () => {
      void first.result.current.handleSubmit('hello');
      await new Promise((r) => setTimeout(r, 10));
    });
    await waitFor(() => expect(streamMessageMock).toHaveBeenCalled());

    // Leave /chat mid-stream and come straight back.
    first.unmount();
    await act(async () => {
      await new Promise((r) => setTimeout(r, 10));
    });
    remounted = true;
    renderHook(() => useChatStreaming(makeParams()), { wrapper });
    await waitFor(() =>
      expect(useChatStore.getState().streamingContent).toContain('new live')
    );

    // Now the old instance's reconcile settles and its finally runs.
    await act(async () => {
      releaseReconcile();
      await new Promise((r) => setTimeout(r, 20));
    });

    expect(useChatStore.getState().isStreaming).toBe(true);
    expect(useChatStore.getState().streamingContent).toContain('new live');
  });
});
