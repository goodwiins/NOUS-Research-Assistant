/**
 * Pipeline Store - Zustand state management for Research Pipeline wizard
 */

import { create } from 'zustand';
import type { PipelineState, UpdatePipelineRequest } from '@/types/scispace';
import { PIPELINE_STEPS } from '@/types/scispace';
import * as scispaceService from '@/services/scispaceService';
import { sameProjectId } from '@/utils/projectId';
import { PIPELINE_ERRORS, type PipelineErrorMessage } from './pipelineErrors';

export { PIPELINE_ERRORS } from './pipelineErrors';

interface PipelineStore {
  pipeline: PipelineState | null;
  loading: boolean;
  /** A fixed safe message (see pipelineErrors.ts), never raw error text. */
  error: string | null;

  fetchPipeline: (projectId: string) => Promise<void>;
  advanceStep: (projectId: string) => Promise<void>;
  skipStep: (projectId: string) => Promise<void>;
  goToStep: (projectId: string, step: number) => Promise<void>;
  updateStepData: (
    projectId: string,
    step: number,
    data: Record<string, unknown>
  ) => Promise<void>;
  resetPipeline: (projectId: string) => Promise<void>;
  clearError: () => void;
  reset: () => void;
}

// Identity for the in-flight pipeline fetch. Module scope (outside the store)
// — unique token objects, not counters; see chat/slices/threadSlice.ts for
// the full rationale. Boundary: this guards CROSS-slot supersession (a fetch/
// reset for another project invalidates in-flight work). Two overlapping
// mutations for the SAME project both pass — last response wins; no
// per-mutation ordering (ledger RS-B2, accepted).
let pipelineRequestToken: object | null = null;

/**
 * GOO-762: the loaded pipeline belongs to one project. A step mutation for
 * another project must not build its update from it (the update would carry
 * the old project's steps), so it is refused. Ids are compared in canonical
 * form (GOO-778): a URL may carry an uppercase UUID that the backend returns
 * in lowercase.
 */
function pipelineFor(
  pipeline: PipelineState | null,
  projectId: string
): PipelineState | null {
  return pipeline && sameProjectId(pipeline.project_id, projectId)
    ? pipeline
    : null;
}

/**
 * Loading another project drops the previous project's pipeline at once, so
 * nothing of it shows (or is edited) under the new project while it loads,
 * nor after its load fails. A reload of the same project keeps it on screen.
 */
function startLoad(
  current: PipelineState | null,
  projectId: string
): Pick<PipelineStore, 'pipeline' | 'loading' | 'error'> {
  return {
    pipeline: pipelineFor(current, projectId),
    loading: true,
    error: null,
  };
}

/** Logs the detail; the store only ever exposes the fixed message. */
function logFailure(action: string, detail: unknown): void {
  console.error(`[pipelineStore] ${action} failed`, detail);
}

function foreignResponse(): Error {
  return new Error('pipeline response belongs to another project');
}

export const usePipelineStore = create<PipelineStore>((set, get) => {
  /**
   * fetch and reset own `loading` and install a new request token. Every
   * outcome of the current request ends loading (GOO-778): a response for
   * another project is an error with Retry, never a spinner that never stops.
   */
  const load = async (
    action: string,
    projectId: string,
    request: (projectId: string) => Promise<PipelineState>,
    failure: PipelineErrorMessage
  ): Promise<void> => {
    const requestToken = {};
    pipelineRequestToken = requestToken;
    set(startLoad(get().pipeline, projectId));
    try {
      const pipeline = await request(projectId);
      if (pipelineRequestToken !== requestToken) return; // superseded
      if (!sameProjectId(pipeline.project_id, projectId)) {
        logFailure(action, foreignResponse());
        set({ error: failure, loading: false });
        return;
      }
      set({ pipeline, loading: false });
    } catch (err) {
      if (pipelineRequestToken !== requestToken) return; // superseded
      logFailure(action, err);
      set({ error: failure, loading: false });
    }
  };

  /**
   * Step mutations only snapshot the token: they don't own the slot, the
   * last fetch/reset does. A refused mutation and a response for another
   * project are reported, never dropped silently (GOO-778).
   */
  const mutate = async (
    action: string,
    projectId: string,
    buildUpdate: (pipeline: PipelineState) => UpdatePipelineRequest | null,
    failure: PipelineErrorMessage
  ): Promise<void> => {
    const requestToken = pipelineRequestToken;
    const pipeline = pipelineFor(get().pipeline, projectId);
    if (!pipeline) {
      set({ error: PIPELINE_ERRORS.refused });
      return;
    }
    const update = buildUpdate(pipeline);
    if (!update) return;

    try {
      const updated = await scispaceService.updatePipeline(projectId, update);
      if (pipelineRequestToken !== requestToken) return; // superseded
      if (!sameProjectId(updated.project_id, projectId)) {
        logFailure(action, foreignResponse());
        set({ error: failure });
        return;
      }
      set({ pipeline: updated });
    } catch (err) {
      if (pipelineRequestToken !== requestToken) return; // superseded
      logFailure(action, err);
      set({ error: failure });
    }
  };

  return {
    pipeline: null,
    loading: false,
    error: null,

    fetchPipeline: (projectId: string) =>
      load(
        'fetchPipeline',
        projectId,
        scispaceService.getPipeline,
        PIPELINE_ERRORS.load
      ),

    advanceStep: (projectId: string) =>
      mutate(
        'advanceStep',
        projectId,
        (pipeline) => {
          const currentStep = pipeline.current_step;
          return {
            current_step: Math.min(currentStep + 1, PIPELINE_STEPS.length - 1),
            completed_steps: [
              ...new Set([...pipeline.completed_steps, currentStep]),
            ],
            // Remove from invalidated if re-completing
            invalidated_steps: pipeline.invalidated_steps.filter(
              (s) => s !== currentStep
            ),
          };
        },
        PIPELINE_ERRORS.advance
      ),

    skipStep: (projectId: string) =>
      mutate(
        'skipStep',
        projectId,
        (pipeline) => {
          const currentStep = pipeline.current_step;
          if (!PIPELINE_STEPS[currentStep]?.skippable) return null;
          return {
            current_step: Math.min(currentStep + 1, PIPELINE_STEPS.length - 1),
            skipped_steps: [
              ...new Set([...pipeline.skipped_steps, currentStep]),
            ],
          };
        },
        PIPELINE_ERRORS.skip
      ),

    goToStep: (projectId: string, step: number) =>
      mutate(
        'goToStep',
        projectId,
        () => ({ current_step: step }),
        PIPELINE_ERRORS.goTo
      ),

    updateStepData: (
      projectId: string,
      step: number,
      data: Record<string, unknown>
    ) =>
      mutate(
        'updateStepData',
        projectId,
        () => ({ step_data: { [String(step)]: data } }),
        PIPELINE_ERRORS.updateData
      ),

    resetPipeline: (projectId: string) =>
      load(
        'resetPipeline',
        projectId,
        scispaceService.resetPipeline,
        PIPELINE_ERRORS.reset
      ),

    clearError: () => set({ error: null }),
    reset: () => {
      pipelineRequestToken = {};
      set({ pipeline: null, loading: false, error: null });
    },
  };
});
