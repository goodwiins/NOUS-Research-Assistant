/**
 * GOO-666 FL2E-4: a slow documents/notes/bibliography response for the
 * previous project (or an older request) must not overwrite the current
 * project's list.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Mocked } from 'vitest';
import { useProjectStore } from '../projectStore';
import { projectService } from '@/services/projectService';
import type {
  Project,
  ProjectBibliography,
  ProjectDocument,
  ProjectNote,
} from '@/services/projectService';

vi.mock('@/services/projectService', () => ({
  projectService: {
    getProject: vi.fn(),
    listProjectDocuments: vi.fn(),
    listProjectNotes: vi.fn(),
    getProjectBibliography: vi.fn(),
  },
}));

const svc = projectService as Mocked<typeof projectService>;

const project = (id: string): Project =>
  ({ id, name: `Project ${id}` }) as Project;
const docs = (id: string): { documents: ProjectDocument[] } => ({
  documents: [{ document_id: `doc-${id}` } as ProjectDocument],
});
const notes = (id: string): { notes: ProjectNote[] } => ({
  notes: [{ id: `note-${id}` } as ProjectNote],
});
const bib = (id: string): ProjectBibliography =>
  ({ project_id: id, content: `bib-${id}` }) as unknown as ProjectBibliography;

/** A promise the test settles by hand. */
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

const state = (): ReturnType<typeof useProjectStore.getState> =>
  useProjectStore.getState();
type Kind = 'documents' | 'notes' | 'bibliography';

const KINDS: Array<{
  kind: Kind;
  mock: () => ReturnType<typeof vi.fn>;
  fetch: (id: string) => Promise<void>;
  payload: (id: string) => unknown;
  shown: () => string | undefined;
  loading: () => boolean;
}> = [
  {
    kind: 'documents',
    mock: () => svc.listProjectDocuments as unknown as ReturnType<typeof vi.fn>,
    fetch: (id) => state().fetchProjectDocuments(id),
    payload: docs,
    shown: () => state().projectDocuments[0]?.document_id,
    loading: () => state().documentsLoading,
  },
  {
    kind: 'notes',
    mock: () => svc.listProjectNotes as unknown as ReturnType<typeof vi.fn>,
    fetch: (id) => state().fetchProjectNotes(id),
    payload: notes,
    shown: () => state().projectNotes[0]?.id,
    loading: () => state().notesLoading,
  },
  {
    kind: 'bibliography',
    mock: () =>
      svc.getProjectBibliography as unknown as ReturnType<typeof vi.fn>,
    fetch: (id) => state().fetchBibliography(id),
    payload: bib,
    shown: () =>
      (state().bibliography as unknown as { content?: string } | null)?.content,
    loading: () => state().loading,
  },
];

const label = (kind: Kind, id: string): string =>
  ({ documents: `doc-${id}`, notes: `note-${id}`, bibliography: `bib-${id}` })[
    kind
  ];

describe('projectStore list ownership (FL2E-4)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    state().reset();
  });

  describe.each(KINDS)(
    '$kind',
    ({ kind, mock, fetch, payload, shown, loading }) => {
      it('an older request resolving last does not overwrite the newer one', async () => {
        const a = deferred<unknown>();
        const b = deferred<unknown>();
        mock().mockReturnValueOnce(a.promise).mockReturnValueOnce(b.promise);

        const first = fetch('A');
        const second = fetch('B');
        b.resolve(payload('B'));
        await second;
        a.resolve(payload('A'));
        await first;

        expect(shown()).toBe(label(kind, 'B'));
        expect(loading()).toBe(false);
      });

      it('a response for the previous project is dropped after a switch, and its spinner cleared', async () => {
        state().setCurrentProject(project('A'));
        const a = deferred<unknown>();
        mock().mockReturnValueOnce(a.promise);

        const pending = fetch('A');
        state().setCurrentProject(project('B'));
        a.resolve(payload('A'));
        await pending;

        expect(shown()).toBeUndefined();
        expect(loading()).toBe(false);
      });

      it('a failing stale request does not report an error over the current one', async () => {
        const a = deferred<unknown>();
        mock()
          .mockReturnValueOnce(a.promise)
          .mockResolvedValueOnce(payload('B'));

        const first = fetch('A');
        await fetch('B');
        a.reject(new Error('boom'));
        await first;

        expect(shown()).toBe(label(kind, 'B'));
        expect(state().error).toBeNull();
      });
    }
  );

  it('keeps lists that already belong to the project fetchProject commits', async () => {
    svc.listProjectDocuments.mockResolvedValueOnce(docs('B') as never);
    svc.listProjectNotes.mockResolvedValueOnce(notes('B') as never);
    state().setCurrentProject(project('A'));
    const p = deferred<Project>();
    svc.getProject.mockReturnValueOnce(p.promise);

    // The project page starts all three at once on navigation.
    const pending = state().fetchProject('B');
    await state().fetchProjectDocuments('B');
    await state().fetchProjectNotes('B');
    p.resolve(project('B'));
    await pending;

    expect(state().currentProject?.id).toBe('B');
    expect(state().projectDocuments[0]?.document_id).toBe('doc-B');
    expect(state().projectNotes[0]?.id).toBe('note-B');
  });

  it("clears the previous project's lists when fetchProject commits a different project", async () => {
    svc.listProjectDocuments.mockResolvedValueOnce(docs('A') as never);
    state().setCurrentProject(project('A'));
    await state().fetchProjectDocuments('A');
    svc.getProject.mockResolvedValueOnce(project('B'));

    await state().fetchProject('B');

    expect(state().projectDocuments).toEqual([]);
  });

  it('a switch clears lists that were set without a tracked fetch', () => {
    state().setCurrentProject(project('A'));
    useProjectStore.setState({
      projectDocuments: docs('A').documents,
      projectNotes: notes('A').notes,
    });

    state().setCurrentProject(project('B'));

    expect(state().projectDocuments).toEqual([]);
    expect(state().projectNotes).toEqual([]);
  });

  it('refetching the same project keeps its lists', async () => {
    state().setCurrentProject(project('A'));
    useProjectStore.setState({ projectDocuments: docs('A').documents });
    svc.getProject.mockResolvedValueOnce(project('A'));

    await state().fetchProject('A');

    expect(state().projectDocuments[0]?.document_id).toBe('doc-A');
  });
});
