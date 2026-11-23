/**
 * GOO-666 FL2D-1 / FL2D-2: durable (Trigger.dev) confirmations.
 *
 * FL2D-1: confirmAction tried `/stream/confirm` first for every confirmation.
 * For a durable one it sent the Trigger run id as `thread_id`, which the
 * backend rejects with 422 (StreamConfirmRequest.thread_id is a strict UUID).
 * The real `streamConfirm` reports HTTP failures through `onError` and
 * resolves; it does not throw. So the catch that led to the durable branch
 * never ran and the wait token was never completed. The mocks here report
 * the failure the same way the service does.
 *
 * FL2D-2: one failed status poll ended a ten-minute durable poll.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  MAX_DURABLE_POLL_FAILURES,
  useAgentChatStore,
} from '@/store/agentChatStore';
import type { AgentStreamCallbacks } from '@/services/agentChatService';
import '@/services/agentChatService';

const serviceMocks = vi.hoisted(() => ({
  listThreads: vi.fn(async () => ({ threads: [] })),
  getThreadMessages: vi.fn(async () => ({ messages: [] })),
  streamMessage: vi.fn(async () => {}),
  startDurableRun: vi.fn(async () => ({ runId: 'run-stub' })),
  getDurableRunStatus: vi.fn(),
  streamConfirm: vi.fn(),
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

/** What the real streamConfirm does on a 422: onError, then resolve. */
async function streamConfirmRejects422(
  _request: unknown,
  callbacks: AgentStreamCallbacks
): Promise<void> {
  callbacks.onError?.(
    'Stream confirm failed (422): thread_id: Input should be a valid UUID',
    'validation'
  );
}

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
  return { runId: 'run-42', status, metadata: {}, output, error: null };
}

function parkDurableConfirmation(waitTokenId: string | null = 'wait-7'): void {
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
        jobId: 'run_cmabc123',
        origin: 'durable',
        approvalId: 'a'.repeat(64),
        ...(waitTokenId ? { waitTokenId } : {}),
        tools: [{ name: 'ingest_arxiv', args: {} }],
        message: 'Confirm?',
      },
    },
  });
}

describe('durable confirmations take the durable path (FL2D-1)', () => {
  beforeEach(() => {
    useAgentChatStore.getState().reset();
    vi.clearAllMocks();
    serviceMocks.streamConfirm.mockImplementation(streamConfirmRejects422);
    vi.useFakeTimers();
  });
  afterEach(() => vi.useRealTimers());

  it('completes the wait token and settles from the run output', async () => {
    parkDurableConfirmation();
    serviceMocks.getDurableRunStatus.mockResolvedValue(
      runStatus('COMPLETED', { result: { message: 'Ingested 1 paper.' } })
    );

    const p = useAgentChatStore.getState().confirmAction('thread-A', true);
    await vi.advanceTimersByTimeAsync(2 * 3000);
    await p;

    expect(serviceMocks.streamConfirm).not.toHaveBeenCalled();
    expect(serviceMocks.completeDurableConfirmation).toHaveBeenCalledWith(
      'run_cmabc123',
      'wait-7',
      true
    );
    expect(serviceMocks.confirmAction).not.toHaveBeenCalled();
    const state = useAgentChatStore.getState();
    expect(state.messages[0].content).toBe('Ingested 1 paper.');
    expect(state.pendingConfirmations['thread-A']).toBeUndefined();
    expect(state.isConfirming).toBe(false);
    expect(state.isStreaming).toBe(false);
  });

  it('keeps the card when a durable confirmation has no wait token', async () => {
    parkDurableConfirmation(null);

    const p = useAgentChatStore.getState().confirmAction('thread-A', true);
    await vi.advanceTimersByTimeAsync(3000);
    await p;

    expect(serviceMocks.streamConfirm).not.toHaveBeenCalled();
    expect(serviceMocks.completeDurableConfirmation).not.toHaveBeenCalled();
    // /agent/confirm/{job_id} does not know Trigger run ids.
    expect(serviceMocks.confirmAction).not.toHaveBeenCalled();
    const state = useAgentChatStore.getState();
    expect(state.pendingConfirmations['thread-A']?.jobId).toBe('run_cmabc123');
    expect(state.isConfirming).toBe(false);
  });

  it('still tries the stream first for an SSE confirmation', async () => {
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
          jobId: 'thread-A',
          origin: 'sse',
          approvalId: 'a'.repeat(64),
          tools: [],
          message: 'Confirm?',
        },
      },
    });
    serviceMocks.streamConfirm.mockImplementation(
      async (_r: unknown, cb: AgentStreamCallbacks) => cb.onDone?.()
    );

    await useAgentChatStore.getState().confirmAction('thread-A', true);

    expect(serviceMocks.streamConfirm).toHaveBeenCalledOnce();
    expect(serviceMocks.completeDurableConfirmation).not.toHaveBeenCalled();
  });
});

describe('a durable card clicked before its wait token lands (GOO-748)', () => {
  const STILL_PREPARING = 'Still preparing this step — try again in a moment.';

  function awaiting(waitTokenId?: string): ReturnType<typeof runStatus> {
    return {
      ...runStatus('WAITING'),
      metadata: {
        status: 'awaiting_confirmation',
        ...(waitTokenId ? { waitTokenId } : {}),
      },
    };
  }

  beforeEach(() => {
    useAgentChatStore.getState().reset();
    vi.clearAllMocks();
    serviceMocks.getDurableRunStatus.mockReset();
    vi.useFakeTimers();
  });
  afterEach(() => vi.useRealTimers());

  it('re-reads the run and completes a token that arrived late', async () => {
    parkDurableConfirmation(null);
    serviceMocks.getDurableRunStatus
      .mockResolvedValueOnce(awaiting('wait-late'))
      .mockResolvedValue(
        runStatus('COMPLETED', { result: { message: 'Ingested 1 paper.' } })
      );

    const p = useAgentChatStore.getState().confirmAction('thread-A', false);
    await vi.advanceTimersByTimeAsync(2 * 3000);
    await p;

    expect(serviceMocks.completeDurableConfirmation).toHaveBeenCalledWith(
      'run_cmabc123',
      'wait-late',
      false
    );
    const state = useAgentChatStore.getState();
    expect(state.messages[0].content).toBe('Ingested 1 paper.');
    expect(state.pendingConfirmations['thread-A']).toBeUndefined();
  });

  it.each([
    ['the run still has no token', () => Promise.resolve(awaiting())],
    [
      'the run has moved on',
      () =>
        Promise.resolve({
          ...runStatus('EXECUTING'),
          metadata: { status: 'running', waitTokenId: 'wait-old' },
        }),
    ],
    [
      'the status read fails',
      () => Promise.reject(new Error('Failed to get run status: 502')),
    ],
  ])('keeps the card with a visible message when %s', async (_label, read) => {
    parkDurableConfirmation(null);
    serviceMocks.getDurableRunStatus.mockImplementation(read);

    const p = useAgentChatStore.getState().confirmAction('thread-A', true);
    await vi.advanceTimersByTimeAsync(3000);
    await p;

    expect(serviceMocks.getDurableRunStatus).toHaveBeenCalledWith(
      'run_cmabc123'
    );
    expect(serviceMocks.completeDurableConfirmation).not.toHaveBeenCalled();
    expect(serviceMocks.confirmAction).not.toHaveBeenCalled();
    const state = useAgentChatStore.getState();
    expect(state.pendingConfirmations['thread-A']?.jobId).toBe('run_cmabc123');
    expect(state.messages[0].content).toBe(STILL_PREPARING);
    expect(state.isConfirming).toBe(false);
    expect(state.isStreaming).toBe(false);
  });
});

describe('durable polls survive a dropped request (FL2D-2)', () => {
  beforeEach(() => {
    useAgentChatStore.getState().reset();
    vi.clearAllMocks();
    serviceMocks.streamConfirm.mockImplementation(streamConfirmRejects422);
    serviceMocks.getThreadMessages.mockResolvedValue({ messages: [] });
    serviceMocks.startDurableRun.mockResolvedValue({ runId: 'run-stub' });
    serviceMocks.streamMessage.mockRejectedValue(new Error('sse down'));
    vi.useFakeTimers();
  });
  afterEach(() => vi.useRealTimers());

  it('confirm poll keeps going after one failed poll', async () => {
    parkDurableConfirmation();
    serviceMocks.getDurableRunStatus
      .mockRejectedValueOnce(new Error('Failed to fetch'))
      .mockResolvedValue(
        runStatus('COMPLETED', { result: { message: 'Done after a blip.' } })
      );

    const p = useAgentChatStore.getState().confirmAction('thread-A', true);
    await vi.advanceTimersByTimeAsync(3 * 3000);
    await p;

    expect(serviceMocks.getDurableRunStatus).toHaveBeenCalledTimes(2);
    expect(useAgentChatStore.getState().messages[0].content).toBe(
      'Done after a blip.'
    );
    expect(useAgentChatStore.getState().isConfirming).toBe(false);
  });

  it('confirm poll counts only consecutive failures', async () => {
    parkDurableConfirmation();
    const blip = new Error('Failed to fetch');
    const live = runStatus('EXECUTING');
    serviceMocks.getDurableRunStatus
      .mockRejectedValueOnce(blip)
      .mockResolvedValueOnce(live)
      .mockRejectedValueOnce(blip)
      .mockResolvedValueOnce(live)
      .mockRejectedValueOnce(blip)
      .mockResolvedValue(
        runStatus('COMPLETED', { result: { message: 'Survived blips.' } })
      );

    const p = useAgentChatStore.getState().confirmAction('thread-A', true);
    await vi.advanceTimersByTimeAsync(8 * 3000);
    await p;

    expect(serviceMocks.getDurableRunStatus).toHaveBeenCalledTimes(6);
    expect(useAgentChatStore.getState().messages[0].content).toBe(
      'Survived blips.'
    );
  });

  it('confirm poll gives up after repeated failures without restoring the card', async () => {
    parkDurableConfirmation();
    serviceMocks.getDurableRunStatus.mockRejectedValue(
      new Error('Failed to fetch')
    );

    const p = useAgentChatStore.getState().confirmAction('thread-A', true);
    await vi.advanceTimersByTimeAsync((MAX_DURABLE_POLL_FAILURES + 2) * 3000);
    await p;

    expect(serviceMocks.getDurableRunStatus).toHaveBeenCalledTimes(
      MAX_DURABLE_POLL_FAILURES
    );
    const state = useAgentChatStore.getState();
    expect(state.isConfirming).toBe(false);
    expect(state.isStreaming).toBe(false);
    // The wait token was consumed, so Approve must not come back.
    expect(state.pendingConfirmations['thread-A']).toBeUndefined();
    expect(state.messages[0].content).toMatch(/may still be running/);
  });

  it('send poll keeps going after one failed poll', async () => {
    serviceMocks.getDurableRunStatus
      .mockRejectedValueOnce(new Error('Failed to fetch'))
      .mockResolvedValue(runStatus('COMPLETED', null));

    useAgentChatStore.getState().setInputValue('hello');
    const p = useAgentChatStore.getState().sendMessage();
    await vi.advanceTimersByTimeAsync(3 * 3000);
    await p;

    expect(serviceMocks.getDurableRunStatus).toHaveBeenCalledTimes(2);
    const state = useAgentChatStore.getState();
    const last = state.messages[state.messages.length - 1];
    expect(state.isStreaming).toBe(false);
    expect(last.isError).toBeFalsy();
  });

  it('send poll counts only consecutive failures', async () => {
    const blip = new Error('Failed to fetch');
    const live = runStatus('EXECUTING');
    serviceMocks.getDurableRunStatus
      .mockRejectedValueOnce(blip)
      .mockResolvedValueOnce(live)
      .mockRejectedValueOnce(blip)
      .mockResolvedValueOnce(live)
      .mockRejectedValueOnce(blip)
      .mockResolvedValue(runStatus('COMPLETED', null));

    useAgentChatStore.getState().setInputValue('hello');
    const p = useAgentChatStore.getState().sendMessage();
    await vi.advanceTimersByTimeAsync(8 * 3000);
    await p;

    expect(serviceMocks.getDurableRunStatus).toHaveBeenCalledTimes(6);
    const state = useAgentChatStore.getState();
    expect(state.messages[state.messages.length - 1].content).not.toMatch(
      /may still be processing/
    );
  });

  it('send poll gives up after repeated failures with the timeout note', async () => {
    serviceMocks.getDurableRunStatus.mockRejectedValue(
      new Error('Failed to fetch')
    );

    useAgentChatStore.getState().setInputValue('hello');
    const p = useAgentChatStore.getState().sendMessage();
    await vi.advanceTimersByTimeAsync((MAX_DURABLE_POLL_FAILURES + 2) * 3000);
    await p;

    expect(serviceMocks.getDurableRunStatus).toHaveBeenCalledTimes(
      MAX_DURABLE_POLL_FAILURES
    );
    const state = useAgentChatStore.getState();
    const last = state.messages[state.messages.length - 1];
    expect(state.isStreaming).toBe(false);
    expect(last.content).toMatch(/may still be processing/);
  });
});
