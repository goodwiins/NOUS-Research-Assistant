/**
 * Project Store - Zustand state management for Research Projects
 */

import { create } from 'zustand';
import { captureAccountSession } from '@/lib/account-session';
import { getAppQueryClient } from '@/lib/query-client';
import { projectService } from '@/services/projectService';
import type {
  Project,
  ProjectCreate,
  ProjectUpdate,
  ProjectDocument,
  ProjectNote,
  ProjectNoteCreate,
  ProjectNoteUpdate,
  ProjectBibliography,
} from '@/services/projectService';

function getErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof Error) {
    return error.message;
  }
  if (typeof error === 'string') {
    return error;
  }
  return fallback;
}

// In-flight dedup for fetchProject: /chat's layout effect (and the project
// page) re-invoke it on render churn before the first GET settles, queueing
// parallel identical /projects/{id} requests (Sentry JAVASCRIPT-NEXTJS-3Q).
// Callers awaiting the same id share one request.
let inflightProjectFetch: { id: string; promise: Promise<void> } | null = null;

// Identity for the "owner" of currentProject/projectDocuments/projectNotes.
// inflightProjectFetch above only dedups the NETWORK call for a repeated
// same-id request; it does nothing for a slow fetchProject('A') resolving
// after fetchProject('B') already committed — that would overwrite B's
// state with A's. Module scope (outside Zustand), unique token objects, same
// pattern as pipelineStore's pipelineRequestToken. A dedup joiner (returns
// inflightProjectFetch.promise) never installs its own token — it's sharing
// the original caller's in-flight request and that caller already owns it.
let projectFetchToken: object | null = null;

// Ownership for the per-project lists (GOO-666 FL2E-4). Each list fetch
// installs its own owner object for its project; a response commits only if
// it still owns its kind, so a slow response for project A can no longer
// overwrite project B's documents, notes or bibliography. A newer request of
// the same kind supersedes an older one, and switching to another project
// drops every owner (in flight or committed) that belongs to a different
// project. Module scope, same pattern as projectFetchToken.
type ListKind = 'documents' | 'notes' | 'bibliography';
interface ListOwner {
  readonly projectId: string;
}
const listRequest: Record<ListKind, ListOwner | null> = {
  documents: null,
  notes: null,
  bibliography: null,
};
// Which project the committed list currently shown belongs to.
const listShownFor: Record<ListKind, string | null> = {
  documents: null,
  notes: null,
  bibliography: null,
};

function claimList(kind: ListKind, projectId: string): ListOwner {
  const owner: ListOwner = { projectId };
  listRequest[kind] = owner;
  return owner;
}

const ownsList = (kind: ListKind, owner: ListOwner): boolean =>
  listRequest[kind] === owner;

function commitList(kind: ListKind, owner: ListOwner): void {
  listRequest[kind] = null;
  listShownFor[kind] = owner.projectId;
}

/**
 * State to apply when the current project becomes `projectId`: drop every
 * in-flight list request and committed list that belongs to another project
 * (and clear its spinner), keep the ones that already belong to this one.
 */
function releaseListsNotOwnedBy(
  projectId: string | null,
  previousProjectId: string | null
): Partial<
  Pick<
    ProjectState,
    | 'projectDocuments'
    | 'projectNotes'
    | 'bibliography'
    | 'documentsLoading'
    | 'notesLoading'
  >
> {
  const patch: Partial<
    Pick<
      ProjectState,
      | 'projectDocuments'
      | 'projectNotes'
      | 'bibliography'
      | 'documentsLoading'
      | 'notesLoading'
    >
  > = {};
  const foreign = (id: string | null | undefined): boolean =>
    id != null && id !== projectId;
  if (foreign(listRequest.documents?.projectId)) {
    listRequest.documents = null;
    patch.documentsLoading = false;
  }
  if (foreign(listRequest.notes?.projectId)) {
    listRequest.notes = null;
    patch.notesLoading = false;
  }
  if (foreign(listRequest.bibliography?.projectId)) {
    listRequest.bibliography = null;
  }
  // A list with no recorded owner (set by a mutation, or before any fetch)
  // is taken to belong to the previous current project, as before.
  const shownFor = (kind: ListKind) => listShownFor[kind] ?? previousProjectId;
  if (foreign(shownFor('documents'))) {
    listShownFor.documents = null;
    patch.projectDocuments = [];
  }
  if (foreign(shownFor('notes'))) {
    listShownFor.notes = null;
    patch.projectNotes = [];
  }
  if (foreign(shownFor('bibliography'))) {
    listShownFor.bibliography = null;
    patch.bibliography = null;
  }
  return patch;
}

function resetListOwnership(): void {
  for (const kind of ['documents', 'notes', 'bibliography'] as const) {
    listRequest[kind] = null;
    listShownFor[kind] = null;
  }
}

// Dual-cache reconciliation (docs/engineering/frontend.md, "Legacy
// server-state stores"): the context rail caches the same project documents/
// notes via useProjectWorkingFolders under ['project', id, 'documents'|
// 'notes'] with a 5-minute staleTime. Every store-side mutation must
// invalidate the Query copy, or the rail serves stale lists until staleTime
// expires. (Reads don't invalidate — a refetch doesn't change server state.)
function invalidateProjectQueries(
  projectId: string,
  scope: 'documents' | 'notes'
): void {
  void getAppQueryClient()?.invalidateQueries({
    queryKey: ['project', projectId, scope],
  });
}

interface ProjectState {
  // State
  projects: Project[];
  currentProject: Project | null;
  projectDocuments: ProjectDocument[];
  projectNotes: ProjectNote[];
  bibliography: ProjectBibliography | null;
  loading: boolean;
  documentsLoading: boolean;
  notesLoading: boolean;
  mutating: boolean;
  error: string | null;
  total: number;

  // Project Actions
  fetchProjects: (
    params?: {
      workspace_id?: string;
      skip?: number;
      limit?: number;
      search?: string;
      project_status?: 'active' | 'paused' | 'completed' | 'archived';
      project_type?: 'research' | 'literature_review' | 'thesis' | 'paper';
      tag?: string;
    },
    options?: { signal?: AbortSignal }
  ) => Promise<void>;
  fetchProject: (projectId: string) => Promise<void>;
  createProject: (data: ProjectCreate) => Promise<Project>;
  updateProject: (projectId: string, data: ProjectUpdate) => Promise<void>;
  deleteProject: (projectId: string) => Promise<void>;
  setCurrentProject: (project: Project | null) => void;

  // Document Actions
  fetchProjectDocuments: (projectId: string) => Promise<void>;
  addDocument: (projectId: string, documentId: string) => Promise<void>;
  removeDocument: (projectId: string, documentId: string) => Promise<void>;

  // Note Actions
  fetchProjectNotes: (projectId: string, pinnedOnly?: boolean) => Promise<void>;
  createNote: (
    projectId: string,
    data: ProjectNoteCreate
  ) => Promise<ProjectNote>;
  updateNote: (
    projectId: string,
    noteId: string,
    data: ProjectNoteUpdate
  ) => Promise<void>;
  deleteNote: (projectId: string, noteId: string) => Promise<void>;
  toggleNotePin: (projectId: string, noteId: string) => Promise<void>;

  // Bibliography Actions
  fetchBibliography: (
    projectId: string,
    format?: 'bibtex' | 'ieee' | 'apa' | 'mla'
  ) => Promise<void>;
  downloadBibliography: (
    projectId: string,
    format?: 'bibtex' | 'ieee' | 'apa' | 'mla'
  ) => Promise<void>;

  // Utility
  clearError: () => void;
  reset: () => void;
}

export const useProjectStore = create<ProjectState>((set, get) => ({
  // Initial state
  projects: [],
  currentProject: null,
  projectDocuments: [],
  projectNotes: [],
  bibliography: null,
  loading: false,
  mutating: false,
  documentsLoading: false,
  notesLoading: false,
  error: null,
  total: 0,

  // =========================================================================
  // Project Actions
  // =========================================================================

  fetchProjects: async (params, options) => {
    const isCurrentAccount = captureAccountSession();
    set({ loading: true, error: null });
    try {
      const response = await projectService.listProjects(params, options);
      if (!isCurrentAccount()) return;
      set({
        projects: response.projects,
        total: response.total,
        loading: false,
      });
    } catch (error: unknown) {
      if (!isCurrentAccount()) return;
      // Aborted requests (e.g., workspace switched or component unmounted
      // before the previous fetch settled) must still clear `loading` — the
      // common case is that a superseding fetch has already set it back to
      // true, but on unmount nothing replaces us and the spinner would
      // otherwise stick in the global store until the next visit.
      if (
        (error instanceof Error && error.name === 'AbortError') ||
        (error instanceof Error && error.name === 'CanceledError')
      ) {
        set({ loading: false });
        return;
      }
      console.error('[ProjectStore] Failed to fetch projects:', error);
      set({
        error: getErrorMessage(error, 'Failed to fetch projects'),
        loading: false,
      });
    }
  },

  fetchProject: async (projectId) => {
    const isCurrentAccount = captureAccountSession();
    if (inflightProjectFetch?.id === projectId) {
      return inflightProjectFetch.promise;
    }
    const requestToken = {};
    projectFetchToken = requestToken;
    set({ loading: true, error: null });
    const promise = (async () => {
      try {
        const project = await projectService.getProject(projectId);
        if (!isCurrentAccount()) return;
        if (projectFetchToken !== requestToken) return; // superseded
        // Drop lists (and in-flight list requests) that belong to another
        // project; keep any that already belong to this one.
        set({
          currentProject: project,
          ...releaseListsNotOwnedBy(
            projectId,
            get().currentProject?.id ?? null
          ),
          loading: false,
        });
      } catch (error: unknown) {
        if (!isCurrentAccount()) return;
        if (projectFetchToken !== requestToken) return; // superseded
        console.error('[ProjectStore] Failed to fetch project:', error);
        set({
          error: getErrorMessage(error, 'Failed to fetch project'),
          loading: false,
        });
      } finally {
        if (
          projectFetchToken === requestToken &&
          inflightProjectFetch?.id === projectId
        ) {
          inflightProjectFetch = null;
        }
      }
    })();
    inflightProjectFetch = { id: projectId, promise };
    return promise;
  },

  createProject: async (data) => {
    const isCurrentAccount = captureAccountSession();
    set({ mutating: true, error: null });
    try {
      const project = await projectService.createProject(data);
      if (!isCurrentAccount())
        throw new DOMException('Account changed', 'AbortError');
      set((state) => ({
        projects: [project, ...state.projects],
        total: state.total + 1,
        mutating: false,
      }));
      return project;
    } catch (error: unknown) {
      if (!isCurrentAccount()) throw error;
      console.error('[ProjectStore] Failed to create project:', error);
      set({
        error: getErrorMessage(error, 'Failed to create project'),
        mutating: false,
      });
      throw error;
    }
  },

  updateProject: async (projectId, data) => {
    const isCurrentAccount = captureAccountSession();
    set({ mutating: true, error: null });
    try {
      const updated = await projectService.updateProject(projectId, data);
      if (!isCurrentAccount()) return;
      set((state) => ({
        projects: state.projects.map((p) => (p.id === projectId ? updated : p)),
        currentProject:
          state.currentProject?.id === projectId
            ? updated
            : state.currentProject,
        mutating: false,
      }));
    } catch (error: unknown) {
      if (!isCurrentAccount()) return;
      console.error('[ProjectStore] Failed to update project:', error);
      set({
        error: getErrorMessage(error, 'Failed to update project'),
        mutating: false,
      });
      throw error;
    }
  },

  deleteProject: async (projectId) => {
    const isCurrentAccount = captureAccountSession();
    set({ mutating: true, error: null });
    try {
      await projectService.deleteProject(projectId);
      if (!isCurrentAccount()) return;
      set((state) => ({
        projects: state.projects.filter((p) => p.id !== projectId),
        currentProject:
          state.currentProject?.id === projectId ? null : state.currentProject,
        total: state.total - 1,
        mutating: false,
      }));
    } catch (error: unknown) {
      if (!isCurrentAccount()) return;
      console.error('[ProjectStore] Failed to delete project:', error);
      set({
        error: getErrorMessage(error, 'Failed to delete project'),
        mutating: false,
      });
      throw error;
    }
  },

  setCurrentProject: (project) => {
    const prev = get().currentProject;
    if (project && prev?.id !== project.id) {
      // Switching project: drop lists that belong to another one.
      set({
        currentProject: project,
        ...releaseListsNotOwnedBy(project.id, prev?.id ?? null),
      });
    } else {
      set({ currentProject: project });
    }
  },

  // =========================================================================
  // Document Actions
  // =========================================================================

  fetchProjectDocuments: async (projectId) => {
    const isCurrentAccount = captureAccountSession();
    const owner = claimList('documents', projectId);
    set({ documentsLoading: true, error: null });
    try {
      const response = await projectService.listProjectDocuments(projectId);
      if (!isCurrentAccount()) return;
      if (!ownsList('documents', owner)) return; // superseded or switched away
      commitList('documents', owner);
      set({
        projectDocuments: response.documents,
        documentsLoading: false,
      });
    } catch (error: unknown) {
      if (!isCurrentAccount()) return;
      if (!ownsList('documents', owner)) return;
      listRequest.documents = null;
      console.error('[ProjectStore] Failed to fetch documents:', error);
      set({
        error: getErrorMessage(error, 'Failed to fetch documents'),
        documentsLoading: false,
      });
    }
  },

  addDocument: async (projectId, documentId) => {
    const isCurrentAccount = captureAccountSession();
    set({ documentsLoading: true, error: null });
    try {
      const doc = await projectService.addDocumentToProject(
        projectId,
        documentId
      );
      if (!isCurrentAccount()) return;
      set((state) => ({
        projectDocuments: [...state.projectDocuments, doc],
        documentsLoading: false,
      }));
      invalidateProjectQueries(projectId, 'documents');
    } catch (error: unknown) {
      if (!isCurrentAccount()) return;
      console.error('[ProjectStore] Failed to add document:', error);
      set({
        error: getErrorMessage(error, 'Failed to add document'),
        documentsLoading: false,
      });
      throw error;
    }
  },

  removeDocument: async (projectId, documentId) => {
    const isCurrentAccount = captureAccountSession();
    set({ documentsLoading: true, error: null });
    try {
      await projectService.removeDocumentFromProject(projectId, documentId);
      if (!isCurrentAccount()) return;
      set((state) => ({
        projectDocuments: state.projectDocuments.filter(
          (d) => d.document_id !== documentId
        ),
        documentsLoading: false,
      }));
      invalidateProjectQueries(projectId, 'documents');
    } catch (error: unknown) {
      if (!isCurrentAccount()) return;
      console.error('[ProjectStore] Failed to remove document:', error);
      set({
        error: getErrorMessage(error, 'Failed to remove document'),
        documentsLoading: false,
      });
      throw error;
    }
  },

  // =========================================================================
  // Note Actions
  // =========================================================================

  fetchProjectNotes: async (projectId, pinnedOnly = false) => {
    const isCurrentAccount = captureAccountSession();
    const owner = claimList('notes', projectId);
    set({ notesLoading: true, error: null });
    try {
      const response = await projectService.listProjectNotes(projectId, {
        pinned_only: pinnedOnly,
      });
      if (!isCurrentAccount()) return;
      if (!ownsList('notes', owner)) return; // superseded or switched away
      commitList('notes', owner);
      set({
        projectNotes: response.notes,
        notesLoading: false,
      });
    } catch (error: unknown) {
      if (!isCurrentAccount()) return;
      if (!ownsList('notes', owner)) return;
      listRequest.notes = null;
      console.error('[ProjectStore] Failed to fetch notes:', error);
      set({
        error: getErrorMessage(error, 'Failed to fetch notes'),
        notesLoading: false,
      });
    }
  },

  createNote: async (projectId, data) => {
    const isCurrentAccount = captureAccountSession();
    set({ notesLoading: true, error: null });
    try {
      const note = await projectService.createNote(projectId, data);
      if (!isCurrentAccount())
        throw new DOMException('Account changed', 'AbortError');
      set((state) => ({
        projectNotes: [note, ...state.projectNotes],
        notesLoading: false,
      }));
      invalidateProjectQueries(projectId, 'notes');
      return note;
    } catch (error: unknown) {
      if (!isCurrentAccount()) throw error;
      console.error('[ProjectStore] Failed to create note:', error);
      set({
        error: getErrorMessage(error, 'Failed to create note'),
        notesLoading: false,
      });
      throw error;
    }
  },

  updateNote: async (projectId, noteId, data) => {
    const isCurrentAccount = captureAccountSession();
    set({ notesLoading: true, error: null });
    try {
      const updated = await projectService.updateNote(projectId, noteId, data);
      if (!isCurrentAccount()) return;
      set((state) => ({
        projectNotes: state.projectNotes.map((n) =>
          n.id === noteId ? updated : n
        ),
        notesLoading: false,
      }));
      invalidateProjectQueries(projectId, 'notes');
    } catch (error: unknown) {
      if (!isCurrentAccount()) return;
      console.error('[ProjectStore] Failed to update note:', error);
      set({
        error: getErrorMessage(error, 'Failed to update note'),
        notesLoading: false,
      });
      throw error;
    }
  },

  deleteNote: async (projectId, noteId) => {
    const isCurrentAccount = captureAccountSession();
    set({ notesLoading: true, error: null });
    try {
      await projectService.deleteNote(projectId, noteId);
      if (!isCurrentAccount()) return;
      set((state) => ({
        projectNotes: state.projectNotes.filter((n) => n.id !== noteId),
        notesLoading: false,
      }));
      invalidateProjectQueries(projectId, 'notes');
    } catch (error: unknown) {
      if (!isCurrentAccount()) return;
      console.error('[ProjectStore] Failed to delete note:', error);
      set({
        error: getErrorMessage(error, 'Failed to delete note'),
        notesLoading: false,
      });
      throw error;
    }
  },

  toggleNotePin: async (projectId, noteId) => {
    const isCurrentAccount = captureAccountSession();
    try {
      const updated = await projectService.toggleNotePin(projectId, noteId);
      if (!isCurrentAccount()) return;
      set((state) => ({
        projectNotes: state.projectNotes.map((n) =>
          n.id === noteId ? updated : n
        ),
      }));
      invalidateProjectQueries(projectId, 'notes');
    } catch (error: unknown) {
      if (!isCurrentAccount()) return;
      console.error('[ProjectStore] Failed to toggle pin:', error);
      set({
        error: getErrorMessage(error, 'Failed to toggle pin'),
      });
      throw error;
    }
  },

  // =========================================================================
  // Bibliography Actions
  // =========================================================================

  fetchBibliography: async (projectId, format = 'bibtex') => {
    const isCurrentAccount = captureAccountSession();
    const owner = claimList('bibliography', projectId);
    set({ loading: true, error: null });
    // A dropped bibliography request still owns the shared `loading` flag
    // unless a project fetch has taken it over.
    const releaseLoading = () => {
      if (!listRequest.bibliography && !inflightProjectFetch) {
        set({ loading: false });
      }
    };
    try {
      const bibliography = await projectService.getProjectBibliography(
        projectId,
        format
      );
      if (!isCurrentAccount()) return;
      if (!ownsList('bibliography', owner)) return releaseLoading();
      commitList('bibliography', owner);
      set({ bibliography, loading: false });
    } catch (error: unknown) {
      if (!isCurrentAccount()) return;
      if (!ownsList('bibliography', owner)) return releaseLoading();
      listRequest.bibliography = null;
      console.error('[ProjectStore] Failed to fetch bibliography:', error);
      set({
        error: getErrorMessage(error, 'Failed to fetch bibliography'),
        loading: false,
      });
    }
  },

  downloadBibliography: async (projectId, format = 'bibtex') => {
    const isCurrentAccount = captureAccountSession();
    try {
      await projectService.downloadBibliography(projectId, format);
      if (!isCurrentAccount()) return;
    } catch (error: unknown) {
      if (!isCurrentAccount()) return;
      console.error('[ProjectStore] Failed to download bibliography:', error);
      set({
        error: getErrorMessage(error, 'Failed to download bibliography'),
      });
      throw error;
    }
  },

  // =========================================================================
  // Utility
  // =========================================================================

  clearError: () => {
    set({ error: null });
  },

  reset: () => {
    projectFetchToken = null;
    inflightProjectFetch = null;
    resetListOwnership();
    set({
      projects: [],
      currentProject: null,
      projectDocuments: [],
      projectNotes: [],
      bibliography: null,
      loading: false,
      mutating: false,
      documentsLoading: false,
      notesLoading: false,
      error: null,
      total: 0,
    });
  },
}));

export default useProjectStore;
