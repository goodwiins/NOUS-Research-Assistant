// A Codex resume names the run its cursor came from. When another tab has
// started a newer run on the same thread, the backend serves that newer run
// from seq 0 (external_resume_cursor ignores a cursor recorded for another
// run). The client adopts the new run id from the accepted frame, so the old
// run's cursor must not come with it: otherwise a failed first attempt
// retries with after=<old cursor> against the NEW run id, which the backend
// honours, and the new run's early frames are skipped. Frames of the new run
// are also all new to this client, so none of them may be treated as an
// already-handled replay.
// Command: pnpm --dir frontend exec vitest run
// src/hooks/chat/__tests__/useChatStreaming.codexRunSwitch.test.tsx
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createElement, type ReactElement, type ReactNode } from 'react';
import type { ChatPageMessage } from '@/components/chat/shared/cloudMessageView';
import type { UseChatStreamingParams } from '@/hooks/chat/useChatStreaming';
import { useChatStore } from '@/store/chat-store';
import { useAgentActivityStore } from '@/stores/agentActivityStore';

function wrapper({ children }: { children: ReactNode }): ReactElement {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return createElement(QueryClientProvider, { client }, children);
}

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
    cancelPendingConfirmation: vi.fn(),
  },
}));

vi.mock('@/services/workspaceService', () => ({
  workspaceService: {
    createThread: vi.fn(),
    listMessages: vi.fn().mockResolvedValue({ messages: [], has_more: false }),
  },
}));

const harnessConnection = {
  executionProvider: 'codex',
  deviceId: 'device-1',
  workspaceId: 'workspace-1',
  devices: [],
  workspaces: [],
  pendingRequests: [],
  connectionState: 'idle',
  statusLabel: 'Codex ready',
  disabledReason: null,
  canSend: true,
  runId: null,
  loadApproval: vi.fn(),
  receive: vi.fn(),
  markConnectionLost: vi.fn(),
};
vi.mock('@/hooks/chat/useHarnessConnection', () => ({
  useHarnessConnection: () => harnessConnection,
}));

function makeParams(): UseChatStreamingParams {
  return {
    messages: [] as ChatPageMessage[],
    displayedMessages: [] as ChatPageMessage[],
    setMessages: vi.fn(),
    conversations: [],
    setConversations: vi.fn(),
    dbConversation: null,
    enableRAG: false,
  };
}

type ReplayCallbacks = {
  onSeq?: (seq: number) => void;
  onRunId?: (runId: string) => void;
  onStatus?: (phase: string, detail?: string) => void;
  onToolStart?: (tool: string, args: unknown, callId?: string) => void;
  onToolEnd?: (
    tool: string,
    result: string,
    isError: boolean,
    callId?: string
  ) => void;
  onDone?: (payload?: { assistant_message_id?: string | null }) => void;
};

describe('useChatStreaming Codex resume after another tab started a newer run', () => {
  beforeEach(() => {
    resumeStreamMock.mockReset();
    useAgentActivityStore.setState({ runs: {}, currentThreadId: null });
    useChatStore.setState({
      currentThreadId: 'thread-A',
      isStreaming: false,
      streamingThreadId: null,
    });
  });

  it('drops the old run cursor, so the new run is neither skipped on retry nor treated as a replay', async () => {
    // This client last saw run-1 at seq 8, then lost its stream.
    const store = useAgentActivityStore.getState();
    store.startRun('thread-A', 'Local Codex', 'task');
    store.setRunId('thread-A', 'run-1');
    store.setPlan('thread-A', [{ text: 'search', tool: 'search_arxiv' }]);
    store.setStreamSeq('thread-A', 8);

    resumeStreamMock.mockImplementation(
      async (_threadId: string, _after: number, cb: ReplayCallbacks) => {
        if (resumeStreamMock.mock.calls.length === 1) {
          // The backend serves the newer run-2 from its start: accepted
          // frame, then a finished tool. Then the transport drops.
          cb.onSeq?.(1);
          cb.onRunId?.('run-2');
          cb.onStatus?.('accepted');
          cb.onSeq?.(2);
          cb.onToolStart?.('search_arxiv', {}, 'c1');
          cb.onSeq?.(3);
          cb.onToolEnd?.('search_arxiv', '[]', false, 'c1');
          return { status: 'failed', error: 'network' };
        }
        cb.onSeq?.(4);
        cb.onDone?.({ assistant_message_id: 'assistant-1' });
        return { status: 'resumed' };
      }
    );

    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    renderHook(() => useChatStreaming(makeParams()), { wrapper });

    await waitFor(() => expect(resumeStreamMock).toHaveBeenCalledTimes(2), {
      timeout: 4_000,
    });
    // First attempt: run-1's cursor, pinned to run-1.
    expect(resumeStreamMock.mock.calls[0][1]).toBe(8);
    expect(resumeStreamMock.mock.calls[0][4]).toBe('run-1');
    // Retry: pinned to run-2, so the cursor must be run-2's own (seq 3), not
    // run-1's 8, which would skip run-2's frames 4-8.
    expect(resumeStreamMock.mock.calls[1][4]).toBe('run-2');
    expect(resumeStreamMock.mock.calls[1][1]).toBe(3);

    // run-2's tool frames were new to this client: the step and the plan
    // tick both landed even though their seqs sit below run-1's old cursor.
    const run = useAgentActivityStore.getState().runs['thread-A'];
    expect(run.runId).toBe('run-2');
    expect(run.steps.filter((st) => st.callId === 'c1')).toHaveLength(1);
    expect(run.plan.map((p) => p.done)).toEqual([true]);
  });
});
