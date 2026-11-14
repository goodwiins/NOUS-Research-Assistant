/**
 * Resume-on-mount: a stale `running` run in the agent activity store for the
 * displayed thread must trigger agentChatService.resumeStream (from the last
 * seen seq), and a 204/idle result must clear the stale run record.
 */

import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createElement, type ReactNode } from 'react';
import type { ChatPageMessage } from '@/components/chat/shared/cloudMessageView';
import { makeChatPageMessage } from '@/test/chatMessageFactory';

const testQueryClient = new QueryClient({
  defaultOptions: { queries: { retry: false } },
});

function wrapper({ children }: { children: ReactNode }) {
  return createElement(
    QueryClientProvider,
    { client: testQueryClient },
    children
  );
}

// ---- Mocks ----
vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

const resumeStreamMock = vi.fn();
vi.mock('@/services/agentChatService', () => ({
  agentChatService: {
    streamMessage: vi.fn(),
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
import { useArtifactPanelStore } from '@/store/artifactPanelStore';
import { workspaceService } from '@/services/workspaceService';

function makeParams() {
  const activeConversationIdRef = { current: 'thread-A' as string | null };
  return {
    messages: [
      makeChatPageMessage({
        role: 'user',
        content: 'earlier question',
        timestamp: 1,
      }),
    ] as ChatPageMessage[],
    displayedMessages: [
      makeChatPageMessage({
        role: 'user',
        content: 'earlier question',
        timestamp: 1,
      }),
    ] as ChatPageMessage[],
    setMessages: vi.fn(),
    conversations: [],
    setConversations: vi.fn(),
    activeConversationId: 'thread-A',
    setActiveConversationId: vi.fn(),
    activeConversationIdRef,
    dbConversation: null,
    isAuthenticated: true,
    setCurrentThread: vi.fn(),
    addMessageToStore: vi.fn(),
    enableRAG: false,
  };
}

describe('useChatStreaming stream resume on mount', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  beforeEach(() => {
    resumeStreamMock.mockReset();
    useAgentActivityStore.setState({ runs: {}, currentThreadId: null });
    useChatStore.getState().reset();
    useChatStore.setState({
      currentThreadId: 'thread-A',
      isStreaming: false,
    });
    vi.mocked(workspaceService.listMessages).mockResolvedValue({
      messages: [],
      has_more: false,
    } as never);
  });

  it('asks the backend to replay from 0 when this turn holds no content, whatever the saved cursor', async () => {
    useAgentActivityStore.getState().startRun('thread-A', 'Agent', 'task');
    useAgentActivityStore.getState().setStreamSeq('thread-A', 7);
    resumeStreamMock.mockResolvedValue({ status: 'resumed' });

    await act(async () => {
      renderHook(() => useChatStreaming(makeParams()), { wrapper });
    });

    expect(resumeStreamMock).toHaveBeenCalledTimes(1);
    expect(resumeStreamMock.mock.calls[0][0]).toBe('thread-A');
    // Saved cursor was 7, but this resume turn starts with an empty answer:
    // replaying from 7 would rebuild only the tail.
    expect(resumeStreamMock.mock.calls[0][1]).toBe(0);
  });

  // Backend stand-in: a buffered ledger of seq-stamped token frames; replays
  // only frames with seq > afterSeq, like /agent/stream/resume.
  function replayLedger(
    tokens: string[],
    attemptHook?: (afterSeq: number) => void
  ) {
    return (
      _threadId: string,
      afterSeq: number,
      callbacks: {
        onSeq: (seq: number) => void;
        onToken: (content: string) => void;
        onDone: (payload?: unknown) => void;
      }
    ) => {
      attemptHook?.(afterSeq);
      tokens.forEach((token, i) => {
        const seq = i + 1;
        if (seq <= afterSeq) return;
        callbacks.onSeq(seq);
        callbacks.onToken(token);
      });
      callbacks.onDone({
        assistant_message_id: 'resume-assistant-1',
        client_message_id: 'resume-runtime-1',
      });
      return Promise.resolve({ status: 'resumed' });
    };
  }

  function lastCommittedAssistant(setMessages: ReturnType<typeof vi.fn>) {
    const commits = setMessages.mock.calls
      .map(([arg]) => arg)
      .filter((arg): arg is ChatPageMessage[] => Array.isArray(arg));
    const last = commits[commits.length - 1] ?? [];
    return last.filter((m) => m.role === 'assistant').pop();
  }

  it('rebuilds the FULL answer when resuming from a non-zero cursor', async () => {
    // Tokens 1-3 streamed before the page was left; cursor saved at 3. This
    // fresh turn holds none of them, so a replay from the cursor would commit
    // only "tail" and drop the prefix until a reload.
    useAgentActivityStore.getState().startRun('thread-A', 'Agent', 'task');
    useAgentActivityStore.getState().setStreamSeq('thread-A', 3);
    resumeStreamMock.mockImplementation(
      replayLedger(['The ', 'whole ', 'answer ', 'tail'])
    );
    const params = makeParams();

    await act(async () => {
      renderHook(() => useChatStreaming(params), { wrapper });
    });

    expect(resumeStreamMock).toHaveBeenCalledTimes(1);
    expect(lastCommittedAssistant(params.setMessages)?.content).toBe(
      'The whole answer tail'
    );
  });

  it('retries from the latest cursor once this turn has processed frames', async () => {
    useAgentActivityStore.getState().startRun('thread-A', 'Agent', 'task');
    useAgentActivityStore.getState().setStreamSeq('thread-A', 3);
    const ledger = ['a ', 'b ', 'c ', 'd ', 'e'];
    const afterSeqs: number[] = [];
    let attempt = 0;
    resumeStreamMock.mockImplementation(
      (
        threadId: string,
        afterSeq: number,
        callbacks: {
          onSeq: (seq: number) => void;
          onToken: (c: string) => void;
          onDone: (p?: unknown) => void;
        },
        ...rest: unknown[]
      ) => {
        attempt += 1;
        afterSeqs.push(afterSeq);
        if (attempt === 1) {
          // connection drops after two frames, no terminal
          callbacks.onSeq(1);
          callbacks.onToken('a ');
          callbacks.onSeq(2);
          callbacks.onToken('b ');
          return Promise.resolve({ status: 'failed', error: 'dropped' });
        }
        return replayLedger(ledger)(threadId, afterSeq, callbacks, ...rest);
      }
    );
    const params = makeParams();

    await act(async () => {
      renderHook(() => useChatStreaming(params), { wrapper });
    });
    await waitFor(() => expect(resumeStreamMock).toHaveBeenCalledTimes(2), {
      timeout: 10_000,
    });

    expect(afterSeqs).toEqual([0, 2]);
    await waitFor(() =>
      expect(lastCommittedAssistant(params.setMessages)?.content).toBe(
        'a b c d e'
      )
    );
  }, 20_000);

  it('does not commit a partial replay when the buffer trimmed the prefix', async () => {
    useAgentActivityStore.getState().startRun('thread-A', 'Agent', 'task');
    useAgentActivityStore.getState().setStreamSeq('thread-A', 3);
    resumeStreamMock.mockImplementation(
      (
        _t: string,
        _after: number,
        callbacks: {
          onSeq: (seq: number) => void;
          onToken: (c: string) => void;
          onDone: (p?: unknown) => void;
          onReplayGap?: (first: number, expected: number) => void;
        }
      ) => {
        // buffer ring has dropped seq 1-4; replay starts at 5
        callbacks.onReplayGap?.(5, 1);
        callbacks.onSeq(5);
        callbacks.onToken('tail only');
        callbacks.onDone({ assistant_message_id: 'a1' });
        return Promise.resolve({ status: 'resumed' });
      }
    );
    const params = makeParams();

    await act(async () => {
      renderHook(() => useChatStreaming(params), { wrapper });
    });

    const committed = params.setMessages.mock.calls
      .flatMap(([arg]) => (Array.isArray(arg) ? arg : []))
      .filter((m: ChatPageMessage) => m.role === 'assistant');
    expect(committed.map((m: ChatPageMessage) => m.content)).not.toContain(
      'tail only'
    );
  });

  it('retries from the last seq this turn processed even when rAF never flushes the cursor (background tab)', async () => {
    // A hidden tab never runs requestAnimationFrame, so the activity store
    // still holds the previous mount's cursor (7) while this turn has only
    // processed seq 1-2. Retrying from 7 would silently skip seq 3-7.
    vi.stubGlobal('requestAnimationFrame', () => 0);
    vi.stubGlobal('cancelAnimationFrame', () => {});
    useAgentActivityStore.getState().startRun('thread-A', 'Agent', 'task');
    useAgentActivityStore.getState().setStreamSeq('thread-A', 7);
    const afterSeqs: number[] = [];
    let attempt = 0;
    resumeStreamMock.mockImplementation(
      (
        threadId: string,
        afterSeq: number,
        callbacks: {
          onSeq: (seq: number) => void;
          onToken: (c: string) => void;
          onDone: (p?: unknown) => void;
        },
        ...rest: unknown[]
      ) => {
        attempt += 1;
        afterSeqs.push(afterSeq);
        if (attempt === 1) {
          callbacks.onSeq(1);
          callbacks.onToken('a ');
          callbacks.onSeq(2);
          callbacks.onToken('b ');
          return Promise.resolve({ status: 'failed', error: 'dropped' });
        }
        return replayLedger(['a ', 'b ', 'c ', 'd ', 'e ', 'f ', 'g ', 'h'])(
          threadId,
          afterSeq,
          callbacks,
          ...rest
        );
      }
    );
    const params = makeParams();

    await act(async () => {
      renderHook(() => useChatStreaming(params), { wrapper });
    });
    await waitFor(() => expect(resumeStreamMock).toHaveBeenCalledTimes(2), {
      timeout: 10_000,
    });

    expect(afterSeqs).toEqual([0, 2]);
  }, 20_000);

  it('does not re-apply activity side effects for tool frames at or below the saved cursor', async () => {
    const note = JSON.stringify({
      note_id: 'n1',
      project_id: 'p1',
      title: 'Note',
    });
    const store = useAgentActivityStore.getState();
    store.startRun('thread-A', 'Agent', 'task');
    store.setPlan('thread-A', [
      { text: 'first note', tool: 'create_project_note' },
      { text: 'second note', tool: 'create_project_note' },
    ]);
    // Seq 1-2 already ran on the earlier mount: finished tool, first plan
    // item ticked.
    store.pushToolStart('thread-A', 'create_project_note', 'c1');
    store.pushToolEnd('thread-A', 'create_project_note', true, 'c1');
    store.setStreamSeq('thread-A', 2);
    const invalidate = vi.spyOn(testQueryClient, 'invalidateQueries');
    const openArtifact = vi.spyOn(
      useArtifactPanelStore.getState(),
      'openArtifact'
    );
    resumeStreamMock.mockImplementation(
      (
        _t: string,
        afterSeq: number,
        callbacks: {
          onSeq: (seq: number) => void;
          onToolStart: (t: string, a: unknown, id?: string) => void;
          onToolEnd: (t: string, r: string, e: boolean, id?: string) => void;
          onToken: (c: string) => void;
          onDone: (p?: unknown) => void;
        }
      ) => {
        expect(afterSeq).toBe(0);
        callbacks.onSeq(1);
        callbacks.onToolStart('create_project_note', {}, 'c1');
        callbacks.onSeq(2);
        callbacks.onToolEnd('create_project_note', note, false, 'c1');
        callbacks.onSeq(3);
        callbacks.onToken('rest');
        callbacks.onSeq(4);
        callbacks.onDone({ assistant_message_id: 'a1' });
        return Promise.resolve({ status: 'resumed' });
      }
    );
    const params = makeParams();

    await act(async () => {
      renderHook(() => useChatStreaming(params), { wrapper });
    });

    const run = useAgentActivityStore.getState().runs['thread-A'];
    expect(run.steps.filter((st) => st.callId === 'c1')).toHaveLength(1);
    expect(run.plan.map((p) => p.done)).toEqual([true, false]);
    const projectInvalidations = invalidate.mock.calls.filter(
      ([filters]) =>
        Array.isArray(filters?.queryKey) && filters.queryKey[0] === 'project'
    );
    expect(projectInvalidations).toHaveLength(0);
    expect(openArtifact).not.toHaveBeenCalled();
    // The turn's own content still rebuilds from the full replay.
    expect(lastCommittedAssistant(params.setMessages)?.content).toBe('rest');
  });

  it('pins current behaviour for a confirmation continuation: first frame past seq 1 reads as a gap, commit is suppressed and the thread is marked stale for reconcile', async () => {
    // A confirm stream continues seq numbering in a NEW buffer (N+1...), and
    // the client has no state that distinguishes that from a trimmed ring
    // buffer. Known limitation: needs a backend signal (e.g. a buffer-start
    // marker on the frame) to be told apart.
    useAgentActivityStore.getState().startRun('thread-A', 'Agent', 'task');
    useAgentActivityStore.getState().setStreamSeq('thread-A', 7);
    const staleSpy = vi.fn();
    useChatStore.setState({ markMessagesStale: staleSpy });
    resumeStreamMock.mockImplementation(
      (
        _t: string,
        _after: number,
        callbacks: {
          onSeq: (seq: number) => void;
          onToken: (c: string) => void;
          onDone: (p?: unknown) => void;
          onReplayGap?: (first: number, expected: number) => void;
        }
      ) => {
        // agentChatService fires this when first seq (8) > afterSeq (0) + 1.
        callbacks.onReplayGap?.(8, 1);
        callbacks.onSeq(8);
        callbacks.onToken('post-confirm answer');
        callbacks.onDone({ assistant_message_id: 'a1' });
        return Promise.resolve({ status: 'resumed' });
      }
    );
    const params = makeParams();

    await act(async () => {
      renderHook(() => useChatStreaming(params), { wrapper });
    });

    expect(resumeStreamMock.mock.calls[0][1]).toBe(0);
    expect(staleSpy).toHaveBeenCalledWith('thread-A');
    const contents = params.setMessages.mock.calls
      .flatMap(([arg]) => (Array.isArray(arg) ? arg : []))
      .filter((m: ChatPageMessage) => m.role === 'assistant')
      .map((m: ChatPageMessage) => m.content);
    expect(contents).not.toContain('post-confirm answer');
  });

  it('clears the stale run on idle and does not resume twice', async () => {
    useAgentActivityStore.getState().startRun('thread-A', 'Agent', 'task');
    resumeStreamMock.mockResolvedValue({ status: 'idle' });

    let rerender: () => void = () => {};
    await act(async () => {
      ({ rerender } = renderHook(() => useChatStreaming(makeParams()), {
        wrapper,
      }));
    });
    await act(async () => {
      rerender();
    });

    expect(resumeStreamMock).toHaveBeenCalledTimes(1);
    // defaults streamSeq to 0 when never set
    expect(resumeStreamMock.mock.calls[0][1]).toBe(0);
    expect(useAgentActivityStore.getState().runs['thread-A'].state).toBe(
      'done'
    );
  });

  it('keeps a failed resume retryable on the next thread activation', async () => {
    useAgentActivityStore.getState().startRun('thread-A', 'Agent', 'task');
    useAgentActivityStore.getState().startRun('thread-B', 'Agent', 'settled');
    useAgentActivityStore.getState().finishRun('thread-B', 'stopped');
    resumeStreamMock.mockResolvedValue({
      status: 'failed',
      error: 'checkpoint unavailable',
    });

    await act(async () => {
      renderHook(() => useChatStreaming(makeParams()), { wrapper });
    });
    // Each activation retries with backoff before giving up (round-3 H7).
    await waitFor(() => expect(resumeStreamMock).toHaveBeenCalledTimes(3), {
      timeout: 10_000,
    });
    expect(useAgentActivityStore.getState().runs['thread-A'].state).toBe(
      'running'
    );

    act(() => useChatStore.setState({ currentThreadId: 'thread-B' }));
    act(() => useChatStore.setState({ currentThreadId: 'thread-A' }));

    await waitFor(() => expect(resumeStreamMock).toHaveBeenCalledTimes(4), {
      timeout: 10_000,
    });
    expect(
      new Set(resumeStreamMock.mock.calls.map(([threadId]) => threadId))
    ).toEqual(new Set(['thread-A']));
  }, 20_000);

  it('does not resume a stream when there is no run for the thread', async () => {
    // With no run record the hook still asks ONCE whether the thread is parked
    // on a HITL interrupt (a cold reload has no in-memory run either) — but
    // that probe is confirmation-only, from seq 0. It must not turn into a
    // stream resume: no token/done handling, nothing committed.
    resumeStreamMock.mockResolvedValue({ status: 'idle' });
    await act(async () => {
      renderHook(() => useChatStreaming(makeParams()), { wrapper });
    });

    expect(resumeStreamMock).toHaveBeenCalledTimes(1);
    const [threadId, afterSeq, callbacks] = resumeStreamMock.mock.calls[0];
    expect(threadId).toBe('thread-A');
    expect(afterSeq).toBe(0);
    // Confirmation/run discovery plus live-run detectors: any token/tool/status/plan
    // frame means a live run owns the thread, and the probe aborts rather than
    // consuming that run's frames (round-3 L7).
    expect(Object.keys(callbacks as object)).toEqual([
      'onConfirmation',
      'onRunId',
      'onToken',
      'onToolStart',
      'onStatus',
      'onPlan',
    ]);
  });

  it('reconciles the owning thread after a resumed stream completes', async () => {
    useAgentActivityStore.getState().startRun('thread-A', 'Agent', 'task');
    resumeStreamMock.mockImplementation(
      (
        _threadId: string,
        _seq: number,
        callbacks: {
          onToken: (content: string) => void;
          onDone: (payload?: unknown) => void;
        }
      ) => {
        callbacks.onToken('resumed answer');
        callbacks.onDone({
          assistant_message_id: 'resume-assistant-1',
          client_message_id: 'resume-runtime-1',
        });
        return Promise.resolve({ status: 'resumed' });
      }
    );
    const actualRefresh = useChatStore.getState().refreshMessages;
    const refreshSpy = vi.fn(actualRefresh);
    useChatStore.setState({ refreshMessages: refreshSpy });

    await act(async () => {
      renderHook(() => useChatStreaming(makeParams()), { wrapper });
    });

    expect(refreshSpy).toHaveBeenCalledWith(
      'thread-A',
      expect.objectContaining({
        persistedId: 'resume-assistant-1',
        runtimeId: 'resume-runtime-1',
        diagnostic: expect.objectContaining({ terminalReason: 'done' }),
      })
    );
    useChatStore.setState({ refreshMessages: actualRefresh });
  });
});
