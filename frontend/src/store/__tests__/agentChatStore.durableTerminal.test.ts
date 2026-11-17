/**
 * GOO-601: the widget's durable-run polls must stop on every terminal
 * Trigger.dev status. They used to exit only on COMPLETED-with-output, FAILED
 * and CRASHED, so a CANCELED, TIMED_OUT or EXPIRED run (or one that completed
 * with no output) kept the composer locked for the full ten-minute budget and
 * then reported a timeout.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { durableRunOutcome, useAgentChatStore } from '@/store/agentChatStore';
// sendMessage imports the service lazily; load it up front so the first test's
// poll timer is scheduled before the fake clock advances.
import '@/services/agentChatService';

const serviceMocks = vi.hoisted(() => ({
  listThreads: vi.fn(async () => ({ threads: [] })),
  getThreadMessages: vi.fn(async () => ({ messages: [] })),
  streamMessage: vi.fn(async () => {}),
  startDurableRun: vi.fn(async () => ({ runId: 'run-stub' })),
  getDurableRunStatus: vi.fn(),
  streamConfirm: vi.fn(async () => {}),
  confirmAction: vi.fn(async () => {}),
  completeDurableConfirmation: vi.fn(async () => {}),
  pollJob: vi.fn(async () => ({ status: 'pending' })),
}));

vi.mock('@/services/agentChatService', async (importOriginal) => {
  const actual =
    await importOriginal<typeof import('@/services/agentChatService')>();
  return {
    isTerminalJobStatus: actual.isTerminalJobStatus,
    agentChatService: serviceMocks,
  };
});

function runStatus(
  status: string,
  output: unknown = null
): {
  runId: string;
  status: string;
  metadata: Record<string, unknown>;
  output: unknown;
  error: null;
} {
  return { runId: 'run-stub', status, metadata: {}, output, error: null };
}

describe('durableRunOutcome', () => {
  it('classifies every terminal Trigger.dev status', () => {
    expect(durableRunOutcome('COMPLETED')).toBe('completed');
    for (const status of [
      'CANCELED',
      'FAILED',
      'CRASHED',
      'INTERRUPTED',
      'SYSTEM_FAILURE',
      'EXPIRED',
      'TIMED_OUT',
    ]) {
      expect(durableRunOutcome(status)).toBe('failed');
    }
  });

  it('keeps polling on live statuses', () => {
    for (const status of [
      'PENDING_VERSION',
      'QUEUED',
      'DEQUEUED',
      'EXECUTING',
      'WAITING',
      'DELAYED',
      undefined,
    ]) {
      expect(durableRunOutcome(status)).toBeNull();
    }
  });
});

describe('agentChatStore durable polls stop on terminal statuses', () => {
  beforeEach(() => {
    useAgentChatStore.getState().reset();
    vi.clearAllMocks();
    serviceMocks.getThreadMessages.mockResolvedValue({ messages: [] });
    serviceMocks.startDurableRun.mockResolvedValue({ runId: 'run-stub' });
    // SSE fails before any frame, so sendMessage falls back to the durable run.
    serviceMocks.streamMessage.mockRejectedValue(new Error('sse down'));
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  async function sendAndPoll(
    status: string,
    output: unknown = null
  ): Promise<void> {
    serviceMocks.getDurableRunStatus.mockResolvedValue(
      runStatus(status, output)
    );
    useAgentChatStore.getState().setInputValue('hello');
    const p = useAgentChatStore.getState().sendMessage();
    // A couple of poll ticks: well short of the 200 x 3 s budget.
    await vi.advanceTimersByTimeAsync(3 * 3000);
    return p;
  }

  it.each(['CANCELED', 'TIMED_OUT', 'EXPIRED', 'SYSTEM_FAILURE'])(
    'settles the turn as an error on %s',
    async (status) => {
      await sendAndPoll(status);
      const state = useAgentChatStore.getState();
      expect(state.isStreaming).toBe(false);
      expect(serviceMocks.getDurableRunStatus).toHaveBeenCalledOnce();
      const last = state.messages[state.messages.length - 1];
      expect(last.role).toBe('assistant');
      expect(last.isError).toBe(true);
      expect(last.isStreaming).toBe(false);
      expect(last.content).not.toContain('timed out');
    }
  );

  it('settles the turn when the run completed with no output', async () => {
    await sendAndPoll('COMPLETED', null);
    const state = useAgentChatStore.getState();
    expect(state.isStreaming).toBe(false);
    expect(serviceMocks.getDurableRunStatus).toHaveBeenCalledOnce();
    const last = state.messages[state.messages.length - 1];
    expect(last.isStreaming).toBe(false);
    expect(last.isError).toBeFalsy();
  });

  it('releases the confirm poll on a canceled run', async () => {
    useAgentChatStore.setState({
      activeThreadId: 'thread-A',
      messages: [
        {
          id: 'a-1',
          role: 'assistant',
          content: 'Waiting for your confirmation...',
          timestamp: new Date(),
        },
      ],
      pendingConfirmations: {
        'thread-A': {
          threadId: 'thread-A',
          assistantMessageId: 'a-1',
          jobId: 'run-42',
          origin: 'durable',
          waitTokenId: 'wait-7',
          tools: [{ name: 'ingest_arxiv', args: {} }],
          message: 'Confirm?',
        },
      },
    });
    serviceMocks.streamConfirm.mockRejectedValueOnce(new Error('sse down'));
    serviceMocks.getDurableRunStatus.mockResolvedValue(runStatus('CANCELED'));

    const p = useAgentChatStore.getState().confirmAction('thread-A', true);
    await vi.advanceTimersByTimeAsync(3 * 3000);
    await p;

    const state = useAgentChatStore.getState();
    expect(serviceMocks.getDurableRunStatus).toHaveBeenCalledOnce();
    expect(state.isConfirming).toBe(false);
    expect(state.isStreaming).toBe(false);
    expect(state.messages[0].content).toBe('This request was cancelled.');
  });
});
