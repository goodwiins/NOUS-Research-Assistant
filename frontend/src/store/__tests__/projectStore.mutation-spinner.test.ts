/**
 * GOO-754 / GOO-770 follow-up: every settleMutationLoading call site (the
 * success and the failure path of each of the five list mutations) clears
 * its own list spinner, and never clears one that a list fetch of the same
 * kind still owns.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Mocked } from 'vitest';
import { useProjectStore } from '../projectStore';
import { projectService } from '@/services/projectService';
import type {
  Project,
  ProjectDocument,
  ProjectNote,
} from '@/services/projectService';

vi.mock('@/services/projectService', () => ({
  projectService: {
    listProjectDocuments: vi.fn(),
    listProjectNotes: vi.fn(),
    addDocumentToProject: vi.fn(),
    removeDocumentFromProject: vi.fn(),
    createNote: vi.fn(),
    updateNote: vi.fn(),
    deleteNote: vi.fn(),
  },
}));

const svc = projectService as Mocked<typeof projectService>;
const state = (): ReturnType<typeof useProjectStore.getState> =>
  useProjectStore.getState();

function deferred<T>(): {
  promise: Promise<T>;
  resolve: (v: T) => void;
  reject: (e: unknown) => void;
} {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

type Spinner = 'documentsLoading' | 'notesLoading';
interface MockFn {
  mockReturnValueOnce(v: unknown): void;
}

const sites: ReadonlyArray<{
  name: string;
  spinner: Spinner;
  mock: () => MockFn;
  start: () => Promise<unknown>;
  value: unknown;
}> = [
  {
    name: 'addDocument',
    spinner: 'documentsLoading',
    mock: () => svc.addDocumentToProject as never,
    start: () => state().addDocument('P', 'd1'),
    value: { document_id: 'd1' } as ProjectDocument,
  },
  {
    name: 'removeDocument',
    spinner: 'documentsLoading',
    mock: () => svc.removeDocumentFromProject as never,
    start: () => state().removeDocument('P', 'd1'),
    value: undefined,
  },
  {
    name: 'createNote',
    spinner: 'notesLoading',
    mock: () => svc.createNote as never,
    start: () => state().createNote('P', { content: 'x' } as never),
    value: { id: 'n1' } as ProjectNote,
  },
  {
    name: 'updateNote',
    spinner: 'notesLoading',
    mock: () => svc.updateNote as never,
    start: () => state().updateNote('P', 'n1', { content: 'y' } as never),
    value: { id: 'n1' } as ProjectNote,
  },
  {
    name: 'deleteNote',
    spinner: 'notesLoading',
    mock: () => svc.deleteNote as never,
    start: () => state().deleteNote('P', 'n1'),
    value: undefined,
  },
];

const other = (s: Spinner): Spinner =>
  s === 'documentsLoading' ? 'notesLoading' : 'documentsLoading';

/** Start this site's list fetch for P and leave it in flight. */
function startListFetch(spinner: Spinner): {
  done: () => Promise<void>;
} {
  if (spinner === 'documentsLoading') {
    const list = deferred<{ documents: ProjectDocument[] }>();
    svc.listProjectDocuments.mockReturnValueOnce(list.promise as never);
    const fetching = state().fetchProjectDocuments('P');
    return {
      done: async () => {
        list.resolve({ documents: [] });
        await fetching;
      },
    };
  }
  const list = deferred<{ notes: ProjectNote[] }>();
  svc.listProjectNotes.mockReturnValueOnce(list.promise as never);
  const fetching = state().fetchProjectNotes('P');
  return {
    done: async () => {
      list.resolve({ notes: [] });
      await fetching;
    },
  };
}

/** Start the other kind's list fetch, so its spinner is owned elsewhere. */
function startOtherListFetch(spinner: Spinner): { done: () => Promise<void> } {
  return startListFetch(other(spinner));
}

describe.each(['resolves', 'rejects'] as const)(
  'settleMutationLoading when the mutation %s',
  (outcome) => {
    beforeEach(() => {
      vi.clearAllMocks();
      vi.spyOn(console, 'error').mockImplementation(() => undefined);
      state().reset();
      state().setCurrentProject({ id: 'P' } as Project);
    });

    const settle = async (
      call: ReturnType<typeof deferred<unknown>>,
      pending: Promise<unknown>,
      value: unknown
    ): Promise<void> => {
      if (outcome === 'resolves') {
        call.resolve(value);
        await pending;
      } else {
        call.reject(new Error('failed'));
        await expect(pending).rejects.toThrow('failed');
      }
    };

    it.each(sites)(
      '$name clears its spinner when no list fetch owns it',
      async (site) => {
        // The other kind's fetch is in flight: this site must clear its
        // own spinner, and only its own.
        const otherFetch = startOtherListFetch(site.spinner);
        const call = deferred<unknown>();
        site.mock().mockReturnValueOnce(call.promise);
        const pending = site.start();
        expect(state()[site.spinner]).toBe(true);

        await settle(call, pending, site.value);

        expect(state()[site.spinner]).toBe(false);
        expect(state()[other(site.spinner)]).toBe(true);
        await otherFetch.done();
      }
    );

    it.each(sites)(
      "$name leaves its spinner on while a list fetch of its kind owns it",
      async (site) => {
        const call = deferred<unknown>();
        site.mock().mockReturnValueOnce(call.promise);
        const pending = site.start();
        const fetch = startListFetch(site.spinner);

        await settle(call, pending, site.value);
        expect(state()[site.spinner]).toBe(true);

        await fetch.done();
        expect(state()[site.spinner]).toBe(false);
      }
    );
  }
);
