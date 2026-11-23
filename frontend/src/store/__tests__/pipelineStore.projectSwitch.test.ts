/**
 * GOO-762: after a project switch, nothing of the previous project's pipeline
 * may show under the new project, and no step mutation may build its update
 * from it.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/services/scispaceService', () => ({
  getPipeline: vi.fn(),
  updatePipeline: vi.fn(),
  resetPipeline: vi.fn(),
}));

import { usePipelineStore } from '@/store/pipelineStore';
import * as scispaceService from '@/services/scispaceService';
import type { PipelineState } from '@/types/scispace';

const getPipeline = scispaceService.getPipeline as ReturnType<typeof vi.fn>;
const updatePipeline = scispaceService.updatePipeline as ReturnType<
  typeof vi.fn
>;
const resetPipeline = scispaceService.resetPipeline as ReturnType<typeof vi.fn>;

const makePipeline = (projectId: string, step = 0): PipelineState => ({
  id: `pipeline-${projectId}`,
  project_id: projectId,
  current_step: step,
  completed_steps: [],
  skipped_steps: [],
  step_data: {},
  invalidated_steps: [],
  created_at: null,
  updated_at: null,
});

function deferred<T>(): { promise: Promise<T>; resolve: (v: T) => void } {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

const state = (): ReturnType<typeof usePipelineStore.getState> =>
  usePipelineStore.getState();

/** A at step 1 (Extract), the one skippable step, so skipStep would act. */
async function loadA(): Promise<void> {
  getPipeline.mockResolvedValueOnce(makePipeline('A', 1));
  await state().fetchPipeline('A');
  expect(state().pipeline?.project_id).toBe('A');
}

describe('pipelineStore project switch (GOO-762)', () => {
  beforeEach(() => {
    usePipelineStore.getState().reset();
    getPipeline.mockReset();
    updatePipeline.mockReset();
    resetPipeline.mockReset();
  });

  it("A loads, then B fails: nothing of A's pipeline shows under B", async () => {
    await loadA();
    getPipeline.mockRejectedValueOnce(new Error('HTTP 503'));

    await state().fetchPipeline('B');

    expect(state().pipeline).toBeNull();
    expect(state().error).toBe('HTTP 503');
    expect(state().loading).toBe(false);
  });

  it("A's pipeline is gone as soon as B's load starts", async () => {
    await loadA();
    const pending = deferred<PipelineState>();
    getPipeline.mockReturnValueOnce(pending.promise);

    const load = state().fetchPipeline('B');

    expect(state().pipeline).toBeNull();
    expect(state().loading).toBe(true);
    pending.resolve(makePipeline('B'));
    await load;
    expect(state().pipeline?.project_id).toBe('B');
  });

  it('reloading the same project keeps its pipeline on screen', async () => {
    await loadA();
    const pending = deferred<PipelineState>();
    getPipeline.mockReturnValueOnce(pending.promise);

    const load = state().fetchPipeline('A');

    expect(state().pipeline?.project_id).toBe('A');
    pending.resolve(makePipeline('A', 3));
    await load;
    expect(state().pipeline?.current_step).toBe(3);
  });

  it('resetPipeline for another project also drops the previous pipeline at once', async () => {
    await loadA();
    const pending = deferred<PipelineState>();
    resetPipeline.mockReturnValueOnce(pending.promise);

    const reset = state().resetPipeline('B');

    expect(state().pipeline).toBeNull();
    pending.resolve(makePipeline('B'));
    await reset;
    expect(state().pipeline?.project_id).toBe('B');
  });

  it('a load response for another project is dropped', async () => {
    getPipeline.mockResolvedValueOnce(makePipeline('A'));

    await state().fetchPipeline('B');

    expect(state().pipeline).toBeNull();
  });

  it.each([
    ['advanceStep', () => state().advanceStep('B')],
    ['skipStep', () => state().skipStep('B')],
    ['goToStep', () => state().goToStep('B', 1)],
    ['updateStepData', () => state().updateStepData('B', 1, { x: 1 })],
  ])(
    "%s for another project is refused while A's pipeline is loaded",
    async (_name, mutate) => {
      // B's load failed, but suppose a stale A pipeline were still in the store.
      await loadA();
      usePipelineStore.setState({ error: null });

      await mutate();

      expect(updatePipeline).not.toHaveBeenCalled();
      expect(state().pipeline?.project_id).toBe('A');
    }
  );

  it('a mutation for the loaded project still goes through', async () => {
    await loadA();
    updatePipeline.mockResolvedValueOnce(makePipeline('A', 2));

    await state().advanceStep('A');

    expect(updatePipeline).toHaveBeenCalledWith(
      'A',
      expect.objectContaining({ current_step: 2 })
    );
    expect(state().pipeline?.current_step).toBe(2);
  });

  it('after A loads and B fails, a step edit on B sends nothing built from A', async () => {
    await loadA();
    getPipeline.mockRejectedValueOnce(new Error('HTTP 503'));
    await state().fetchPipeline('B');

    await state().advanceStep('B');
    await state().goToStep('B', 1);

    expect(updatePipeline).not.toHaveBeenCalled();
  });
});
