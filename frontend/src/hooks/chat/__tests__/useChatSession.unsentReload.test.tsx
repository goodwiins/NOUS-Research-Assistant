/**
 * GOO-788: a send the server rejected (GOO-772) lives only in the local
 * overlay. A reload used to drop the bubble and its text, because the chat
 * store persists only selection ids. The overlay's rejected sends now mirror
 * to sessionStorage per thread and come back on load; sign-out removes them.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, renderHook } from '@testing-library/react';
import type { ChatPageMessage } from '@/components/chat/shared/cloudMessageView';
import { makeChatPageMessage } from '@/test/chatMessageFactory';
import { useChatStore } from '@/store/chat-store';
import { useAuthStore } from '@/stores/authStore';
import { UNSENT_CHAT_STORAGE_KEY } from '@/hooks/chat/unsentChatStorage';

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock('react-hot-toast', () => ({
  default: { error: vi.fn(), success: vi.fn() },
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
    listWorkspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    createWorkspace: vi.fn(),
    listThreads: vi.fn().mockResolvedValue({ threads: [], total: 0 }),
    listMessages: vi.fn().mockResolvedValue({ messages: [], has_more: false }),
    createThread: vi.fn(),
    createMessage: vi.fn(),
  },
}));

import { useChatSession } from '@/hooks/chat/useChatSession';

const NOT_SENT = 'The server rejected your message, so it wasn’t sent. Edit it or retry.';

function overlayWithUnsentSend(): ChatPageMessage[] {
  return [
    makeChatPageMessage({
      id: 'a1',
      role: 'assistant',
      content: 'done answer',
      timestamp: 1,
      source: 'canonical',
    }),
    {
      ...makeChatPageMessage({
        role: 'user',
        content: 'my rejected question',
        timestamp: 2,
      }),
      source: 'local-only',
      sendFailed: { message: NOT_SENT },
    },
  ];
}

function signIn(userId: string | null): void {
  useAuthStore.setState({
    user: userId ? ({ id: userId } as never) : null,
    isAuthenticated: false,
  } as never);
}

/** Everything in memory goes; the tab's sessionStorage stays. */
function reload(threadId: string): void {
  cleanup();
  useChatStore.getState().reset();
  useChatStore.setState({ currentThreadId: threadId });
}

async function leaveUnsentSendOn(threadId: string): Promise<void> {
  useChatStore.setState({ currentThreadId: threadId });
  const { result } = renderHook(() => useChatSession());
  await act(async () => {
    result.current.setMessages(overlayWithUnsentSend());
  });
}

function unsent(
  messages: ChatPageMessage[]
): Array<Pick<ChatPageMessage, 'role' | 'content' | 'source' | 'sendFailed'>> {
  return messages.map(({ role, content, source, sendFailed }) => ({
    role,
    content,
    source,
    sendFailed,
  }));
}

const RESTORED = [
  {
    role: 'user',
    content: 'my rejected question',
    source: 'local-only',
    sendFailed: { message: NOT_SENT },
  },
];

beforeEach(() => {
  window.sessionStorage.clear();
  useChatStore.getState().reset();
  signIn('user-1');
});

afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
});

describe('an unsent send survives a reload (GOO-788)', () => {
  it('restores the bubble and its text for the same thread', async () => {
    await leaveUnsentSendOn('thread-A');

    reload('thread-A');
    const { result } = renderHook(() => useChatSession());

    expect(unsent(result.current.messages)).toEqual(RESTORED);
  });

  it('restores it when the profile loads after the thread', async () => {
    await leaveUnsentSendOn('thread-A');

    reload('thread-A');
    signIn(null);
    const { result } = renderHook(() => useChatSession());
    expect(result.current.messages).toEqual([]);

    await act(async () => {
      signIn('user-1');
    });

    expect(unsent(result.current.messages)).toEqual(RESTORED);
  });

  it('restores it only on its own thread', async () => {
    await leaveUnsentSendOn('thread-A');

    reload('thread-B');
    const { result } = renderHook(() => useChatSession());

    expect(result.current.messages).toEqual([]);
  });

  it('never shows it to another account', async () => {
    await leaveUnsentSendOn('thread-A');

    reload('thread-A');
    signIn('user-2');
    const { result } = renderHook(() => useChatSession());

    expect(result.current.messages).toEqual([]);
  });

  it('forgets it once the bubble is gone (retried or edited)', async () => {
    useChatStore.setState({ currentThreadId: 'thread-A' });
    const { result } = renderHook(() => useChatSession());
    await act(async () => {
      result.current.setMessages(overlayWithUnsentSend());
    });
    await act(async () => {
      result.current.setMessages([]);
    });

    reload('thread-A');
    const reloaded = renderHook(() => useChatSession());

    expect(reloaded.result.current.messages).toEqual([]);
  });

  it('is cleared by sign-out', async () => {
    await leaveUnsentSendOn('thread-A');
    expect(window.sessionStorage.getItem(UNSENT_CHAT_STORAGE_KEY)).not.toBeNull();

    cleanup();
    await act(async () => {
      await useAuthStore.getState().signOut();
    });

    expect(window.sessionStorage.getItem(UNSENT_CHAT_STORAGE_KEY)).toBeNull();
    // The same account signing back in finds nothing either.
    reload('thread-A');
    signIn('user-1');
    const { result } = renderHook(() => useChatSession());
    expect(result.current.messages).toEqual([]);
  });
});
