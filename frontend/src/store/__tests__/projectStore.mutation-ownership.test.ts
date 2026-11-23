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

/** Project B is current with one document and one note on screen. */
async function onProjectB(): Promise<void> {
  state().setCurrentProject(project('A'));
  state().setCurrentProject(project('B'));
  svc.listProjectDocuments.mockResolvedValueOnce({
    documents: [doc('b-doc')],
  } as never);
  svc.listProjectNotes.mockResolvedValueOnce({
    notes: [note('b-note')],
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
});
