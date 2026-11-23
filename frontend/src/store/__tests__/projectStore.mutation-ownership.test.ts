/**
 * GOO-754: a mutation started on project A whose response arrives after the
 * user switched to project B must not change B's lists, error or spinners.
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
    getProject: vi.fn(),
    listProjectDocuments: vi.fn(),
    listProjectNotes: vi.fn(),
    getProjectBibliography: vi.fn(),
    addDocumentToProject: vi.fn(),
    removeDocumentFromProject: vi.fn(),
    createNote: vi.fn(),
    updateNote: vi.fn(),
    deleteNote: vi.fn(),
    toggleNotePin: vi.fn(),
  },
}));

const svc = projectService as Mocked<typeof projectService>;
const project = (id: string): Project =>
  ({ id, name: `Project ${id}` }) as Project;
const doc = (id: string): ProjectDocument =>
  ({ document_id: id }) as ProjectDocument;
const note = (id: string): ProjectNote => ({ id }) as ProjectNote;
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

/**
 * Project B is current with one document and one note on screen. Pass the
 * ids A uses to give B an item with the same id, so an A mutation that
 * leaked into B's list would visibly change it.
 */
async function onProjectB(docId = 'b-doc', noteId = 'b-note'): Promise<void> {
  state().setCurrentProject(project('A'));
  state().setCurrentProject(project('B'));
  svc.listProjectDocuments.mockResolvedValueOnce({
    documents: [doc(docId)],
  } as never);
  svc.listProjectNotes.mockResolvedValueOnce({
    notes: [note(noteId)],
  } as never);
  await state().fetchProjectDocuments('B');
  await state().fetchProjectNotes('B');
}

describe('projectStore mutations after a project switch (GOO-754)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    state().reset();
  });

  it("addDocument for A resolving on B does not add A's document to B's list", async () => {
    state().setCurrentProject(project('A'));
    const added = deferred<ProjectDocument>();
    svc.addDocumentToProject.mockReturnValueOnce(added.promise);
    const pending = state().addDocument('A', 'a-doc');

    await onProjectB();
    added.resolve(doc('a-doc'));
    await pending;

    expect(state().projectDocuments.map((d) => d.document_id)).toEqual([
      'b-doc',
    ]);
    expect(state().documentsLoading).toBe(false);
  });

  it("createNote for A resolving on B does not add A's note, but still returns it", async () => {
    state().setCurrentProject(project('A'));
    const created = deferred<ProjectNote>();
    svc.createNote.mockReturnValueOnce(created.promise);
    const pending = state().createNote('A', { content: 'x' } as never);

    await onProjectB();
    created.resolve(note('a-note'));

    await expect(pending).resolves.toEqual(note('a-note'));
    expect(state().projectNotes.map((n) => n.id)).toEqual(['b-note']);
    expect(state().notesLoading).toBe(false);
  });

  it("a failing A mutation does not set an error on B's view, and still rejects", async () => {
    state().setCurrentProject(project('A'));
    const added = deferred<ProjectDocument>();
    const created = deferred<ProjectNote>();
    svc.addDocumentToProject.mockReturnValueOnce(added.promise);
    svc.createNote.mockReturnValueOnce(created.promise);
    const addPending = state().addDocument('A', 'a-doc');
    const notePending = state().createNote('A', { content: 'x' } as never);

    await onProjectB();
    added.reject(new Error('add failed'));
    created.reject(new Error('create failed'));

    await expect(addPending).rejects.toThrow('add failed');
    await expect(notePending).rejects.toThrow('create failed');
    expect(state().error).toBeNull();
  });

  it("an A mutation settling does not clear B's in-flight documents spinner", async () => {
    state().setCurrentProject(project('A'));
    const added = deferred<ProjectDocument>();
    svc.addDocumentToProject.mockReturnValueOnce(added.promise);
    const pending = state().addDocument('A', 'a-doc');

    state().setCurrentProject(project('B'));
    const list = deferred<{ documents: ProjectDocument[] }>();
    svc.listProjectDocuments.mockReturnValueOnce(list.promise as never);
    const fetching = state().fetchProjectDocuments('B');

    added.resolve(doc('a-doc'));
    await pending;
    expect(state().documentsLoading).toBe(true);

    list.resolve({ documents: [doc('b-doc')] });
    await fetching;
    expect(state().documentsLoading).toBe(false);
    expect(state().projectDocuments.map((d) => d.document_id)).toEqual([
      'b-doc',
    ]);
  });

  it('a mutation for the current project still commits and reports errors', async () => {
    await onProjectB();
    svc.addDocumentToProject.mockResolvedValueOnce(doc('b-doc-2'));
    await state().addDocument('B', 'b-doc-2');
    expect(state().projectDocuments.map((d) => d.document_id)).toEqual([
      'b-doc',
      'b-doc-2',
    ]);
    expect(state().documentsLoading).toBe(false);

    svc.createNote.mockRejectedValueOnce(new Error('nope'));
    await expect(
      state().createNote('B', { content: 'x' } as never)
    ).rejects.toThrow();
    expect(state().error).toBe('nope');
    expect(state().notesLoading).toBe(false);
  });

  it('a mutation with no known project (nothing loaded yet) commits as before', async () => {
    svc.createNote.mockResolvedValueOnce(note('n1'));
    await state().createNote('A', { content: 'x' } as never);
    expect(state().projectNotes.map((n) => n.id)).toEqual(['n1']);
  });

  it("clears the bibliography's shared spinner at the switch, not when the stale response settles", async () => {
    state().setCurrentProject(project('A'));
    const bib = deferred<unknown>();
    svc.getProjectBibliography.mockReturnValueOnce(bib.promise as never);
    const pending = state().fetchBibliography('A');
    expect(state().loading).toBe(true);

    state().setCurrentProject(project('B'));
    expect(state().loading).toBe(false);

    bib.resolve({ content: 'bib-A' });
    await pending;
    expect(state().bibliography).toBeNull();
  });

  describe('id-keyed mutations for A, settling on B where the same id exists', () => {
    const SHARED = 'shared';
    const changed = { id: SHARED, content: 'from A' } as unknown as ProjectNote;

    const cases = [
      {
        name: 'removeDocument',
        mock: (): typeof svc.removeDocumentFromProject =>
          svc.removeDocumentFromProject,
        start: (): Promise<unknown> => state().removeDocument('A', SHARED),
        value: undefined,
      },
      {
        name: 'updateNote',
        mock: (): typeof svc.updateNote => svc.updateNote,
        start: (): Promise<unknown> =>
          state().updateNote('A', SHARED, { content: 'from A' } as never),
        value: changed,
      },
      {
        name: 'deleteNote',
        mock: (): typeof svc.deleteNote => svc.deleteNote,
        start: (): Promise<unknown> => state().deleteNote('A', SHARED),
        value: undefined,
      },
      {
        name: 'toggleNotePin',
        mock: (): typeof svc.toggleNotePin => svc.toggleNotePin,
        start: (): Promise<unknown> => state().toggleNotePin('A', SHARED),
        value: changed,
      },
    ] as const;

    const bIsUntouched = (): void => {
      expect(state().projectDocuments).toEqual([doc(SHARED)]);
      expect(state().projectNotes).toEqual([note(SHARED)]);
    };

    it.each(cases)("$name success leaves B's same-id item alone", async (c) => {
      state().setCurrentProject(project('A'));
      const call = deferred<unknown>();
      (
        c.mock() as unknown as { mockReturnValueOnce(v: unknown): void }
      ).mockReturnValueOnce(call.promise);
      const pending = c.start();

      await onProjectB(SHARED, SHARED);
      call.resolve(c.value);
      await pending;

      bIsUntouched();
      expect(state().documentsLoading).toBe(false);
      expect(state().notesLoading).toBe(false);
    });

    it.each(cases)(
      '$name failure sets no error on B, and still rejects',
      async (c) => {
        state().setCurrentProject(project('A'));
        const call = deferred<unknown>();
        (
          c.mock() as unknown as { mockReturnValueOnce(v: unknown): void }
        ).mockReturnValueOnce(call.promise);
        const pending = c.start();

        await onProjectB(SHARED, SHARED);
        call.reject(new Error(`${c.name} failed`));

        await expect(pending).rejects.toThrow(`${c.name} failed`);
        expect(state().error).toBeNull();
        bIsUntouched();
      }
    );

    it.each(cases)(
      '$name for the project on screen still commits and reports errors',
      async (c) => {
        await onProjectB(SHARED, SHARED);
        const forB = {
          ...c,
          start: (): Promise<unknown> => {
            switch (c.name) {
              case 'removeDocument':
                return state().removeDocument('B', SHARED);
              case 'updateNote':
                return state().updateNote('B', SHARED, {
                  content: 'from A',
                } as never);
              case 'deleteNote':
                return state().deleteNote('B', SHARED);
              default:
                return state().toggleNotePin('B', SHARED);
            }
          },
        };
        const mock = c.mock() as unknown as {
          mockResolvedValueOnce(v: unknown): void;
          mockRejectedValueOnce(e: unknown): void;
        };
        mock.mockResolvedValueOnce(c.value);
        await forB.start();
        if (c.name === 'removeDocument')
          expect(state().projectDocuments).toEqual([]);
        else if (c.name === 'deleteNote')
          expect(state().projectNotes).toEqual([]);
        else expect(state().projectNotes).toEqual([changed]);

        mock.mockRejectedValueOnce(new Error('nope'));
        await expect(forB.start()).rejects.toThrow('nope');
        expect(state().error).toBe('nope');
      }
    );
  });

  it('with no list loaded yet, the current project decides', async () => {
    // B is current but none of its lists has been fetched: an A note must
    // not appear in it, while a B note does.
    state().setCurrentProject(project('B'));
    svc.createNote.mockResolvedValueOnce(note('a-note'));
    await state().createNote('A', { content: 'x' } as never);
    expect(state().projectNotes).toEqual([]);

    svc.createNote.mockResolvedValueOnce(note('b-note'));
    await state().createNote('B', { content: 'x' } as never);
    expect(state().projectNotes.map((n) => n.id)).toEqual(['b-note']);
  });

  it("while fetchProject(B) is pending, the lists on screen are B's: they decide, not currentProject", async () => {
    // currentProject is still A, but B's lists are already committed.
    state().setCurrentProject(project('A'));
    svc.listProjectDocuments.mockResolvedValueOnce({
      documents: [doc('b-doc')],
    } as never);
    await state().fetchProjectDocuments('B');
    const fetchingB = deferred<Project>();
    svc.getProject.mockReturnValueOnce(fetchingB.promise);
    const projectPending = state().fetchProject('B');
    expect(state().currentProject?.id).toBe('A');

    // A mutation for B lands in B's list ...
    svc.addDocumentToProject.mockResolvedValueOnce(doc('b-doc-2'));
    await state().addDocument('B', 'b-doc-2');
    // ... and one for A, the stale current project, does not.
    svc.addDocumentToProject.mockResolvedValueOnce(doc('a-doc'));
    await state().addDocument('A', 'a-doc');

    expect(state().projectDocuments.map((d) => d.document_id)).toEqual([
      'b-doc',
      'b-doc-2',
    ]);

    fetchingB.resolve(project('B'));
    await projectPending;
    expect(state().projectDocuments.map((d) => d.document_id)).toEqual([
      'b-doc',
      'b-doc-2',
    ]);
  });
});
