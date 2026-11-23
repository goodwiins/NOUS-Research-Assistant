/**
 * workspaceService request contract for the member, conversation, thread,
 * bulk-thread, message and collection calls (GOO-742).
 *
 * `workspaceService.contract.test.ts` pins the list/create calls; this file
 * covers the rest. Each case pins the method, exact path and body, that the
 * response body comes back untouched (nothing for a delete), and that a
 * failed request rejects with the same error.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Mock } from 'vitest';
import { api } from '@/services/api-client';
import { workspaceService } from '@/services/workspaceService';

vi.mock('@/services/api-client', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
    request: vi.fn(),
  },
}));

type Method = 'get' | 'post' | 'patch' | 'delete' | 'request';
const mocked = api as unknown as Record<Method, Mock>;
const METHODS: Method[] = ['get', 'post', 'patch', 'delete', 'request'];

interface Case {
  name: string;
  call: () => Promise<unknown>;
  method: Method;
  args: unknown[];
  returnsBody: boolean;
}

const data = { field: 'value' };
const ids = ['t1', 't2'];
const jsonDelete = (body: unknown): Record<string, unknown> => ({
  method: 'DELETE',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
});

const cases: Case[] = [
  // Workspaces and members
  {
    name: 'deleteWorkspace',
    call: () => workspaceService.deleteWorkspace('w1'),
    method: 'delete',
    args: ['/api/v2/workspaces/w1'],
    returnsBody: false,
  },
  {
    name: 'addWorkspaceMember',
    call: () => workspaceService.addWorkspaceMember('w1', data as never),
    method: 'post',
    args: ['/api/v2/workspaces/w1/members', data],
    returnsBody: true,
  },
  {
    name: 'updateWorkspaceMember',
    call: () => workspaceService.updateWorkspaceMember('w1', 'u1', data as never),
    method: 'patch',
    args: ['/api/v2/workspaces/w1/members/u1', data],
    returnsBody: true,
  },
  {
    name: 'removeWorkspaceMember',
    call: () => workspaceService.removeWorkspaceMember('w1', 'u1'),
    method: 'delete',
    args: ['/api/v2/workspaces/w1/members/u1'],
    returnsBody: false,
  },
  // Conversations
  {
    name: 'getConversation',
    call: () => workspaceService.getConversation('c1'),
    method: 'get',
    args: ['/api/v2/conversations/c1'],
    returnsBody: true,
  },
  {
    name: 'updateConversation',
    call: () => workspaceService.updateConversation('c1', data as never),
    method: 'patch',
    args: ['/api/v2/conversations/c1', data],
    returnsBody: true,
  },
  {
    name: 'deleteConversation',
    call: () => workspaceService.deleteConversation('c1'),
    method: 'delete',
    args: ['/api/v2/conversations/c1'],
    returnsBody: false,
  },
  // Threads
  {
    name: 'selectMessageBranch encodes both ids for the same-origin proxy',
    call: () => workspaceService.selectMessageBranch('t/1', 'm 1'),
    method: 'post',
    args: ['/api/assistant/v2/t%2F1/messages/m%201/select', {}],
    returnsBody: false,
  },
  {
    name: 'updateThread',
    call: () => workspaceService.updateThread('t1', data as never),
    method: 'patch',
    args: ['/api/v2/threads/t1', data],
    returnsBody: true,
  },
  {
    name: 'deleteThread',
    call: () => workspaceService.deleteThread('t1'),
    method: 'delete',
    args: ['/api/v2/threads/t1'],
    returnsBody: false,
  },
  {
    name: 'regenerateThreadSummary',
    call: () => workspaceService.regenerateThreadSummary('t1'),
    method: 'post',
    args: ['/api/v2/threads/t1/summarize'],
    returnsBody: true,
  },
  // Bulk thread operations
  {
    name: 'bulkResolveThreads',
    call: () => workspaceService.bulkResolveThreads(ids),
    method: 'post',
    args: ['/api/v2/threads/bulk/resolve', { thread_ids: ids }],
    returnsBody: true,
  },
  {
    name: 'bulkArchiveThreads',
    call: () => workspaceService.bulkArchiveThreads(ids),
    method: 'post',
    args: ['/api/v2/threads/bulk/archive', { thread_ids: ids }],
    returnsBody: true,
  },
  {
    name: 'bulkSummarizeThreads',
    call: () => workspaceService.bulkSummarizeThreads(ids),
    method: 'post',
    args: ['/api/v2/threads/bulk/summarize', { thread_ids: ids }],
    returnsBody: true,
  },
  {
    name: 'bulkDeleteThreads sends the ids as a JSON DELETE body',
    call: () => workspaceService.bulkDeleteThreads(ids),
    method: 'request',
    args: ['/api/v2/threads/bulk', jsonDelete({ thread_ids: ids })],
    returnsBody: true,
  },
  // Messages
  {
    name: 'getMessage',
    call: () => workspaceService.getMessage('m1'),
    method: 'get',
    args: ['/api/v2/messages/m1'],
    returnsBody: true,
  },
  {
    name: 'updateMessage',
    call: () => workspaceService.updateMessage('m1', data as never),
    method: 'patch',
    args: ['/api/v2/messages/m1', data],
    returnsBody: true,
  },
  {
    name: 'deleteMessage',
    call: () => workspaceService.deleteMessage('m1'),
    method: 'delete',
    args: ['/api/v2/messages/m1'],
    returnsBody: false,
  },
  // Collections
  {
    name: 'listCollections with paging',
    call: () => workspaceService.listCollections('w1', { page: 2, limit: 5 }),
    method: 'get',
    args: ['/api/v2/workspaces/w1/collections?page=2&limit=5'],
    returnsBody: true,
  },
  {
    name: 'listCollections without options has no query string',
    call: () => workspaceService.listCollections('w1'),
    method: 'get',
    args: ['/api/v2/workspaces/w1/collections'],
    returnsBody: true,
  },
  {
    name: 'createCollection',
    call: () => workspaceService.createCollection(data as never),
    method: 'post',
    args: ['/api/v2/collections', data],
    returnsBody: true,
  },
  {
    name: 'getCollection',
    call: () => workspaceService.getCollection('col1'),
    method: 'get',
    args: ['/api/v2/collections/col1'],
    returnsBody: true,
  },
  {
    name: 'updateCollection',
    call: () => workspaceService.updateCollection('col1', data as never),
    method: 'patch',
    args: ['/api/v2/collections/col1', data],
    returnsBody: true,
  },
  {
    name: 'deleteCollection',
    call: () => workspaceService.deleteCollection('col1'),
    method: 'delete',
    args: ['/api/v2/collections/col1'],
    returnsBody: false,
  },
  {
    name: 'addDocumentsToCollection',
    call: () => workspaceService.addDocumentsToCollection('col1', ['d1', 'd2']),
    method: 'post',
    args: ['/api/v2/collections/col1/documents', { document_ids: ['d1', 'd2'] }],
    returnsBody: true,
  },
  {
    name: 'removeDocumentsFromCollection sends the ids as a JSON DELETE body',
    call: () => workspaceService.removeDocumentsFromCollection('col1', ['d1']),
    method: 'request',
    args: ['/api/v2/collections/col1/documents', jsonDelete({ document_ids: ['d1'] })],
    returnsBody: false,
  },
];

describe('workspaceService request contract (GOO-742)', () => {
  beforeEach(() => {
    for (const fn of Object.values(mocked)) fn.mockReset();
  });

  it.each(cases)('$name', async ({ call, method, args, returnsBody }) => {
    const body = { from: 'backend' };
    mocked[method].mockResolvedValue(body);

    const result = await call();

    expect(mocked[method]).toHaveBeenCalledTimes(1);
    expect(mocked[method]).toHaveBeenCalledWith(...args);
    for (const other of METHODS.filter((m) => m !== method)) {
      expect(mocked[other]).not.toHaveBeenCalled();
    }
    if (returnsBody) expect(result).toBe(body);
    else expect(result).toBeUndefined();
  });

  it.each(cases)('$name rejects with the request error', async ({ call, method }) => {
    const failure = new Error('outage');
    mocked[method].mockRejectedValue(failure);

    await expect(call()).rejects.toBe(failure);
  });
});
