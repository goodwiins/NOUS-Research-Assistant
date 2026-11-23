import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ChatPageMessage } from '@/components/chat/shared/cloudMessageView';
import {
  UNSENT_CHAT_STORAGE_KEY,
  clearUnsentChatStorage,
  discardForeignUnsentChat,
  loadChatDraft,
  loadUnsentTurns,
  saveChatDraft,
  saveUnsentTurns,
} from '@/hooks/chat/unsentChatStorage';

function failed(content: string, runtimeId = content): ChatPageMessage {
  return {
    runtimeId,
    source: 'local-only',
    role: 'user',
    content,
    timestamp: 5,
    sendFailed: { message: 'Not sent.' },
  };
}

beforeEach(() => window.sessionStorage.clear());
afterEach(() => {
  vi.restoreAllMocks();
  window.sessionStorage.clear();
});

describe('unsentChatStorage', () => {
  it('keeps only rejected user sends, with their attachments', () => {
    const attachment = { id: 'att-1', document_id: 'doc-1' };
    saveUnsentTurns('user-1', 'thread-A', [
      {
        runtimeId: 'c1',
        source: 'canonical',
        role: 'assistant',
        content: 'stored by the server',
        timestamp: 1,
      },
      {
        runtimeId: 'o1',
        source: 'optimistic',
        role: 'user',
        content: 'mid-send',
        timestamp: 2,
      },
      { ...failed('rejected'), attachments: [attachment] },
    ]);

    expect(loadUnsentTurns('user-1', 'thread-A')).toEqual([
      { ...failed('rejected'), attachments: [attachment] },
    ]);
  });

  it('keeps a thread’s draft and its unsent sends side by side', () => {
    saveUnsentTurns('user-1', 'thread-A', [failed('rejected')]);
    saveChatDraft('user-1', 'ws-1', 'thread-A', 'next question');

    saveUnsentTurns('user-1', 'thread-A', []);

    expect(loadUnsentTurns('user-1', 'thread-A')).toEqual([]);
    expect(loadChatDraft('user-1', 'ws-1', 'thread-A')).toBe('next question');
  });

  it('keeps the new-chat draft apart from every thread', () => {
    saveChatDraft('user-1', 'ws-1', null, 'fresh idea');

    expect(loadChatDraft('user-1', 'ws-1', null)).toBe('fresh idea');
    expect(loadChatDraft('user-1', 'ws-1', 'thread-A')).toBeUndefined();
  });

  it('removes the record once nothing is left in it', () => {
    saveChatDraft('user-1', 'ws-1', 'thread-A', 'typing');
    saveChatDraft('user-1', 'ws-1', 'thread-A', '');

    expect(window.sessionStorage.getItem(UNSENT_CHAT_STORAGE_KEY)).toBeNull();
  });

  it('replaces another account’s record instead of mixing them', () => {
    saveChatDraft('user-1', 'ws-1', 'thread-A', 'user one');
    saveChatDraft('user-2', 'ws-1', 'thread-B', 'user two');

    expect(loadChatDraft('user-1', 'ws-1', 'thread-A')).toBeUndefined();
    expect(loadChatDraft('user-2', 'ws-1', 'thread-B')).toBe('user two');
  });

  it('keeps drafts per workspace', () => {
    saveChatDraft('user-1', 'ws-1', null, 'in one');

    expect(loadChatDraft('user-1', 'ws-2', null)).toBeUndefined();
    expect(loadChatDraft('user-1', null, null)).toBeUndefined();
  });

  it('caps the record at the 20 most recent slots', () => {
    const now = vi.spyOn(Date, 'now');
    for (let index = 0; index < 21; index += 1) {
      now.mockReturnValue(1_000 + index);
      saveChatDraft('user-1', 'ws-1', `thread-${index}`, `draft ${index}`);
    }

    expect(loadChatDraft('user-1', 'ws-1', 'thread-0')).toBeUndefined();
    expect(loadChatDraft('user-1', 'ws-1', 'thread-1')).toBe('draft 1');
    expect(loadChatDraft('user-1', 'ws-1', 'thread-20')).toBe('draft 20');
  });

  it.each([
    ['not JSON', '{oops'],
    ['the wrong version', JSON.stringify({ version: 2, ownerUserId: 'user-1', threads: {} })],
    [
      'a malformed turn',
      JSON.stringify({
        version: 1,
        ownerUserId: 'user-1',
        threads: { 'thread-A': { updatedAt: 1, unsent: [{ content: 'x' }] } },
      }),
    ],
    [
      'an attachment without ids',
      JSON.stringify({
        version: 1,
        ownerUserId: 'user-1',
        threads: {
          'thread:thread-A': {
            updatedAt: 1,
            unsent: [
              {
                runtimeId: 'r1',
                content: 'x',
                timestamp: 1,
                sendFailed: { message: 'Not sent.' },
                attachments: [{ display_name: 'paper.pdf' }],
              },
            ],
          },
        },
      }),
    ],
    [
      'an attachment with a valid id but a bad document_id',
      JSON.stringify({
        version: 1,
        ownerUserId: 'user-1',
        threads: {
          'thread:thread-A': {
            updatedAt: 1,
            unsent: [
              {
                runtimeId: 'r1',
                content: 'x',
                timestamp: 1,
                sendFailed: { message: 'Not sent.' },
                attachments: [{ id: 'att-1', document_id: 42 }],
              },
            ],
          },
        },
      }),
    ],
    [
      'an attachment with a non-string label',
      JSON.stringify({
        version: 1,
        ownerUserId: 'user-1',
        threads: {
          'thread:thread-A': {
            updatedAt: 1,
            unsent: [
              {
                runtimeId: 'r1',
                content: 'x',
                timestamp: 1,
                sendFailed: { message: 'Not sent.' },
                attachments: [
                  { id: 'a', document_id: 'd', thumbnail_url: { href: 'x' } },
                ],
              },
            ],
          },
        },
      }),
    ],
    [
      'an attachment that is not an object',
      JSON.stringify({
        version: 1,
        ownerUserId: 'user-1',
        threads: {
          'thread:thread-A': {
            updatedAt: 1,
            unsent: [
              {
                runtimeId: 'r1',
                content: 'x',
                timestamp: 1,
                sendFailed: { message: 'Not sent.' },
                attachments: ['att-1'],
              },
            ],
          },
        },
      }),
    ],
  ])('drops a record that is %s instead of restoring it', (_label, raw) => {
    window.sessionStorage.setItem(UNSENT_CHAT_STORAGE_KEY, raw);

    expect(loadUnsentTurns('user-1', 'thread-A')).toEqual([]);
    expect(window.sessionStorage.getItem(UNSENT_CHAT_STORAGE_KEY)).toBeNull();
  });

  it('discards another account’s record and keeps the same account’s', () => {
    saveChatDraft('user-1', 'ws-1', 'thread-A', 'mine');

    discardForeignUnsentChat('user-1');
    expect(loadChatDraft('user-1', 'ws-1', 'thread-A')).toBe('mine');

    discardForeignUnsentChat('user-2');
    expect(window.sessionStorage.getItem(UNSENT_CHAT_STORAGE_KEY)).toBeNull();
  });

  it('clears everything on sign-out', () => {
    saveUnsentTurns('user-1', 'thread-A', [failed('rejected')]);
    saveChatDraft('user-1', 'ws-1', null, 'typing');

    clearUnsentChatStorage();

    expect(window.sessionStorage.getItem(UNSENT_CHAT_STORAGE_KEY)).toBeNull();
  });

  it('keeps chat working when storage refuses writes', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('full', 'QuotaExceededError');
    });

    expect(() => saveChatDraft('user-1', 'ws-1', 'thread-A', 'typing')).not.toThrow();
    expect(loadChatDraft('user-1', 'ws-1', 'thread-A')).toBeUndefined();
  });
});
