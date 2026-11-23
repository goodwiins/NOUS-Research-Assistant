import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  useChatComposerActions,
  type UseChatComposerActionsReturn,
} from '@/hooks/chat/useChatComposerActions';
import { makeChatPageMessage } from '@/test/chatMessageFactory';
import { useChatStore } from '@/store/chat-store';
import type { Workspace } from '@/types/workspace';

const toastErrorMock = vi.fn();
vi.mock('react-hot-toast', () => ({
  default: { error: (...args: unknown[]) => toastErrorMock(...args) },
}));

const uploadDocumentMock = vi.fn();
vi.mock('@/services/enhancedDocumentService', () => ({
  enhancedDocumentService: {
    uploadDocument: (...args: unknown[]) => uploadDocumentMock(...args),
  },
}));

const WORKSPACE = { id: 'ws-1' } as unknown as Workspace;

function setup(overrides: Record<string, unknown> = {}): {
  result: { current: UseChatComposerActionsReturn };
  handleSubmit: ReturnType<typeof vi.fn>;
} {
  const handleSubmit = vi.fn();
  const params = {
    workspace: WORKSPACE,
    handleSubmit,
    isBusy: false,
    displayedMessages: [
      makeChatPageMessage({
        id: 'u1',
        role: 'user',
        content: 'first turn',
        timestamp: 1,
      }),
      makeChatPageMessage({
        id: 'a1',
        role: 'assistant',
        content: 'first reply',
        timestamp: 2,
      }),
    ],
    ...overrides,
  };
  const { result } = renderHook(() => useChatComposerActions(params));
  return { result, handleSubmit };
}

describe('useChatComposerActions', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useChatStore.getState().reset();
  });

  it.each(['edit', 'retry'])(
    'creates a sibling for a canonical PDF %s without supersedes',
    async (kind) => {
      useChatStore.setState({
        currentThreadId: 'branch-thread',
        messageFreshness: { 'branch-thread': 'fresh' },
        messagePagination: {
          'branch-thread': {
            hasMore: false,
            loadingOlder: false,
            loadedCount: 2,
            message_branching: true,
            branch_document_id: 'pdf',
            selected_message_id: 'answer',
          },
        },
      });
      const { result, handleSubmit } = setup({
        displayedMessages: [
          makeChatPageMessage({
            id: 'user',
            clientMessageId: 'runtime-user',
            role: 'user',
            content: 'Original',
            parentMessageId: null,
          }),
          makeChatPageMessage({
            id: 'answer',
            role: 'assistant',
            content: 'Answer',
          }),
        ],
      });
      act(() => {
        if (kind === 'edit') result.current.handleEditUserMessage(0, 'Edited');
        else result.current.handleRegenerate(1);
      });
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
      expect(handleSubmit).toHaveBeenCalledWith(
        kind === 'edit' ? 'Edited' : 'Original',
        [],
        undefined,
        undefined,
        undefined,
        {
          parentMessageId: null,
          replacedMessageId: 'user',
          selectedHeadMessageId: 'answer',
        }
      );
    }
  );

  it('does not reinterpret an unhydrated PDF edit as an ordinary follow-up', async () => {
    useChatStore.setState({
      currentThreadId: 'branch-thread',
      messageFreshness: { 'branch-thread': 'fresh' },
      messagePagination: {
        'branch-thread': {
          hasMore: false,
          loadingOlder: false,
          loadedCount: 2,
          message_branching: true,
          branch_document_id: 'pdf',
          selected_message_id: 'answer',
        },
      },
    });
    const { result, handleSubmit } = setup();
    act(() => result.current.handleEditUserMessage(0, 'Edited'));
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(handleSubmit).not.toHaveBeenCalled();
    expect(toastErrorMock).toHaveBeenCalledWith(
      expect.stringContaining('branch')
    );
  });

  it.each(['edit', 'retry'])(
    'keeps the composer draft when a %s waits for an unloaded branch',
    async (kind) => {
      useChatStore.setState({
        currentThreadId: 'branch-thread',
        messageFreshness: { 'branch-thread': 'fresh' },
        messagePagination: {
          'branch-thread': {
            hasMore: false,
            loadingOlder: false,
            loadedCount: 2,
            message_branching: true,
            branch_document_id: 'pdf',
            selected_message_id: 'answer',
          },
        },
      });
      const { result, handleSubmit } = setup();
      act(() => {
        if (kind === 'edit') result.current.handleEditUserMessage(0, 'Edited');
        else result.current.handleRegenerate(1);
      });
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
      expect(handleSubmit).not.toHaveBeenCalled();
      expect(toastErrorMock).toHaveBeenCalledWith(
        expect.stringContaining('branch')
      );
    }
  );

  it('resends an unsent PDF turn as a follow-up on the current branch head (GOO-772)', async () => {
    useChatStore.setState({
      currentThreadId: 'branch-thread',
      messageFreshness: { 'branch-thread': 'fresh' },
      messagePagination: {
        'branch-thread': {
          hasMore: false,
          loadingOlder: false,
          loadedCount: 2,
          message_branching: true,
          branch_document_id: 'pdf',
          selected_message_id: 'answer',
        },
      },
    });
    const history = [
      makeChatPageMessage({
        id: 'user',
        clientMessageId: 'runtime-user',
        role: 'user',
        content: 'Original',
        parentMessageId: null,
      }),
      makeChatPageMessage({ id: 'answer', role: 'assistant', content: 'A' }),
    ];
    // The server never accepted it: no id, no parent, no client id.
    const unsent = {
      ...makeChatPageMessage({ role: 'user', content: 'Unsent' }),
      id: undefined,
      clientMessageId: undefined,
      parentMessageId: undefined,
      source: 'local-only' as const,
      sendFailed: { message: 'Not sent.' },
    };
    const { result, handleSubmit } = setup({
      displayedMessages: [...history, unsent],
    });

    act(() => result.current.handleEditUserMessage(2, 'Unsent'));
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(toastErrorMock).not.toHaveBeenCalled();
    expect(handleSubmit).toHaveBeenCalledWith(
      'Unsent',
      history,
      undefined,
      undefined,
      undefined
    );
  });

  it('retrying an earlier unsent turn keeps the later unsent turns, in order (GOO-772)', async () => {
    const unsentTurn = (
      content: string
    ): ReturnType<typeof makeChatPageMessage> => ({
      ...makeChatPageMessage({ role: 'user', content }),
      id: undefined,
      clientMessageId: undefined,
      parentMessageId: undefined,
      source: 'local-only' as const,
      sendFailed: { message: 'Not sent.' },
    });
    const first = makeChatPageMessage({
      id: 'user',
      clientMessageId: 'runtime-user',
      role: 'user',
      content: 'Original',
    });
    const answer = makeChatPageMessage({
      id: 'answer',
      role: 'assistant',
      content: 'A',
    });
    const unsentOne = unsentTurn('Unsent one');
    const unsentTwo = unsentTurn('Unsent two');
    const later = makeChatPageMessage({
      id: 'later',
      role: 'assistant',
      content: 'Later reply',
    });
    const { result, handleSubmit } = setup({
      displayedMessages: [first, answer, unsentOne, unsentTwo, later],
    });

    act(() => result.current.handleEditUserMessage(2, 'Unsent one'));
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    // The server never stored it, so nothing after it is truncated: the turn
    // goes out on the current head, and the other unsent turn stays.
    expect(handleSubmit).toHaveBeenCalledWith(
      'Unsent one',
      [first, answer, unsentTwo, later],
      undefined,
      undefined,
      undefined
    );
  });

  it('handleRegenerate refuses with a toast while a turn is busy', async () => {
    const { result, handleSubmit } = setup({ isBusy: true });

    act(() => {
      result.current.handleRegenerate(1);
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(handleSubmit).not.toHaveBeenCalled();
    expect(toastErrorMock).toHaveBeenCalledWith(
      'Wait for the current reply to finish before retrying.'
    );
  });

  it('handleRegenerate re-sends the prior user turn', async () => {
    const { result, handleSubmit } = setup();

    act(() => {
      result.current.handleRegenerate(1);
    });
    // handleSubmit is deferred via setTimeout(0) to dodge the stale-closure bug.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(handleSubmit).toHaveBeenCalledWith(
      'first turn',
      [],
      undefined,
      undefined,
      undefined
    );
  });

  it('retryLast regenerates the most recent assistant turn', async () => {
    const { result, handleSubmit } = setup();

    act(() => {
      result.current.retryLast();
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(handleSubmit).toHaveBeenCalledWith(
      'first turn',
      [],
      undefined,
      undefined,
      undefined
    );
  });

  it('handleRegenerate tombstones the replaced turn when it has a client id', async () => {
    const cmid = 'cmid-regen';
    const { result, handleSubmit } = setup({
      displayedMessages: [
        makeChatPageMessage({
          id: 'u1',
          role: 'user',
          content: 'first turn',
          timestamp: 1,
          clientMessageId: cmid,
        }),
        makeChatPageMessage({
          id: 'a1',
          role: 'assistant',
          content: 'first reply',
          timestamp: 2,
        }),
      ],
    });

    act(() => {
      result.current.handleRegenerate(1);
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(handleSubmit).toHaveBeenCalledWith(
      'first turn',
      [],
      cmid,
      undefined,
      undefined
    );
  });

  describe('handleEditUserMessage', () => {
    function setupFour(): ReturnType<typeof setup> {
      return setup({
        displayedMessages: [
          makeChatPageMessage({
            id: 'u1',
            role: 'user',
            content: 'first',
            timestamp: 1,
          }),
          makeChatPageMessage({
            id: 'a1',
            role: 'assistant',
            content: 'reply 1',
            timestamp: 2,
          }),
          makeChatPageMessage({
            id: 'u2',
            role: 'user',
            content: 'second',
            timestamp: 3,
          }),
          makeChatPageMessage({
            id: 'a2',
            role: 'assistant',
            content: 'reply 2',
            timestamp: 4,
          }),
        ],
      });
    }

    it('refuses with a toast while a turn is busy', async () => {
      const { result, handleSubmit } = setup({
        isBusy: true,
        displayedMessages: [
          makeChatPageMessage({
            id: 'u1',
            role: 'user',
            content: 'first',
            timestamp: 1,
          }),
          makeChatPageMessage({
            id: 'a1',
            role: 'assistant',
            content: 'reply 1',
            timestamp: 2,
          }),
          makeChatPageMessage({
            id: 'u2',
            role: 'user',
            content: 'second',
            timestamp: 3,
          }),
          makeChatPageMessage({
            id: 'a2',
            role: 'assistant',
            content: 'reply 2',
            timestamp: 4,
          }),
        ],
      });
      act(() => {
        result.current.handleEditUserMessage(2, 'edited');
      });
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
      expect(handleSubmit).not.toHaveBeenCalled();
      expect(toastErrorMock).toHaveBeenCalledWith(
        'Wait for the current reply to finish before editing.'
      );
    });

    it('is a no-op when the new content is blank', () => {
      const { result, handleSubmit } = setupFour();
      act(() => {
        result.current.handleEditUserMessage(2, '   ');
      });
      expect(handleSubmit).not.toHaveBeenCalled();
    });

    it('re-sends the edited text with history truncated before the edited message', async () => {
      const { result, handleSubmit } = setupFour();

      act(() => {
        result.current.handleEditUserMessage(2, 'edited second turn');
      });
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
      });

      // History passed to handleSubmit is everything BEFORE index 2 (u1, a1).
      expect(handleSubmit).toHaveBeenCalledTimes(1);
      const [, historyOverride] = handleSubmit.mock.calls[0];
      expect(historyOverride).toHaveLength(2);
      expect(
        historyOverride.map((m: { content: string }) => m.content)
      ).toEqual(['first', 'reply 1']);
    });

    it('names the edited turn as superseded when it has a persisted cmid', async () => {
      // Durable edit: the FE truncation alone left the old answer and every
      // later turn in chat_messages + the LangGraph checkpoint (PR #1313).
      const cmid = '11111111-1111-4111-8111-111111111111';
      const { result, handleSubmit } = setup({
        displayedMessages: [
          makeChatPageMessage({
            id: 'u1',
            role: 'user',
            content: 'first',
            timestamp: 1,
          }),
          makeChatPageMessage({
            id: 'a1',
            role: 'assistant',
            content: 'reply 1',
            timestamp: 2,
          }),
          makeChatPageMessage({
            id: 'u2',
            role: 'user',
            content: 'second',
            timestamp: 3,
            clientMessageId: cmid,
          }),
          makeChatPageMessage({
            id: 'a2',
            role: 'assistant',
            content: 'reply 2',
            timestamp: 4,
          }),
        ],
      });

      act(() => {
        result.current.handleEditUserMessage(2, 'edited second turn');
      });
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
      });

      expect(handleSubmit.mock.calls[0][2]).toBe(cmid);
    });

    it('omits the superseded id when the edited turn has no persisted cmid', async () => {
      // A turn that never reached the server (or a legacy row) has nothing for
      // the backend to look up — send the edit without the field rather than
      // guessing an id, and fall back to the old FE-only truncation.
      const { result, handleSubmit } = setupFour();

      act(() => {
        result.current.handleEditUserMessage(2, 'edited second turn');
      });
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
      });

      expect(handleSubmit.mock.calls[0][2]).toBeUndefined();
    });
  });

  it('submit forwards a plain send with no overrides', () => {
    const { result, handleSubmit } = setup();

    act(() => {
      result.current.submit();
    });

    // Content/history/supersedes/attachments all absent: a bare composer send
    // lets handleSubmit read the live input and history itself.
    expect(handleSubmit).toHaveBeenCalledWith(
      undefined,
      undefined,
      undefined,
      undefined
    );
  });

  it('submit forwards the composer attachment ids', () => {
    const { result, handleSubmit } = setup();

    act(() => {
      result.current.submit(['doc-1', 'doc-2']);
    });

    expect(handleSubmit.mock.calls[0][3]).toEqual(['doc-1', 'doc-2']);
  });

  it('handleAttach uploads every file and reports partial failure', async () => {
    uploadDocumentMock
      .mockResolvedValueOnce({ response: { document_id: 'doc-1' } })
      .mockRejectedValueOnce(new Error('boom'));
    const { result } = setup();
    const files = [
      new File(['a'], 'a.pdf'),
      new File(['b'], 'b.pdf'),
    ] as unknown as FileList;

    await act(async () => {
      await result.current.handleAttach(files);
    });

    expect(uploadDocumentMock).toHaveBeenCalledTimes(2);
    expect(toastErrorMock).toHaveBeenCalledWith('Upload failed for b.pdf.');
  });

  it('handleAttach reports the document each upload became', async () => {
    // The id is the whole point of the upload: without it the file lands in
    // the library and nothing links it to the turn the user attached it to.
    uploadDocumentMock
      .mockResolvedValueOnce({ response: { document_id: 'doc-1' } })
      .mockRejectedValueOnce(new Error('boom'));
    const { result } = setup();
    const files = [
      new File(['a'], 'a.pdf'),
      new File(['b'], 'b.pdf'),
    ] as unknown as FileList;

    let outcomes;
    await act(async () => {
      outcomes = await result.current.handleAttach(files);
    });

    expect(outcomes).toEqual([
      { ok: true, documentId: 'doc-1' },
      { ok: false },
    ]);
  });

  it('handleAttach refuses to upload without a workspace', async () => {
    const { result } = setup({ workspace: null });

    await act(async () => {
      await result.current.handleAttach([
        new File(['a'], 'a.pdf'),
      ] as unknown as FileList);
    });

    expect(uploadDocumentMock).not.toHaveBeenCalled();
    expect(toastErrorMock).toHaveBeenCalledWith(
      'No workspace available — attachment was not uploaded.'
    );
  });
});

describe('account lifetime of queued composer actions', () => {
  it.each(['edit', 'regenerate'] as const)(
    'drops a queued %s across account reset',
    async (action) => {
      const { result, handleSubmit } = setup();
      act(() => {
        if (action === 'edit')
          result.current.handleEditUserMessage(0, 'A private revision');
        else result.current.handleRegenerate(1);
        useChatStore.getState().reset();
      });
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
      expect(handleSubmit).not.toHaveBeenCalled();
    }
  );
});
