/**
 * GOO-778: a pipeline response the store refuses must never leave the
 * spinner running. Ids are compared in canonical (lowercase UUID) form, a
 * mismatch for the current request ends in an error, a refused step mutation
 * says so, and the error text is a fixed safe message, never the raw error.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/services/scispaceService', () => ({
  getPipeline: vi.fn(),
  updatePipeline: vi.fn(),
  resetPipeline: vi.fn(),
}));

import { PIPELINE_ERRORS, usePipelineStore } from '@/store/pipelineStore';
import * as scispaceService from '@/services/scispaceService';
import type { PipelineState } from '@/types/scispace';

const getPipeline = scispaceService.getPipeline as ReturnType<typeof vi.fn>;
const updatePipeline = scispaceService.updatePipeline as ReturnType<
  typeof vi.fn
>;
const resetPipeline = scispaceService.resetPipeline as ReturnType<typeof vi.fn>;

const LOWER = '0f8e2a1c-3b4d-4e5f-8a9b-0c1d2e3f4a5b';
const UPPER = LOWER.toUpperCase();
const OTHER = '11111111-2222-4333-8444-555555555555';

const makePipeline = (projectId: string, step = 1): PipelineState => ({
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

const state = (): ReturnType<typeof usePipelineStore.getState> =>
  usePipelineStore.getState();

describe('pipelineStore id mismatch (GOO-778)', () => {
  let consoleError: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    usePipelineStore.getState().reset();
    getPipeline.mockReset();
    updatePipeline.mockReset();
    resetPipeline.mockReset();
    consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  afterEach(() => {
    consoleError.mockRestore();
  });

  it('loads when the URL id is uppercase and the backend answers in lowercase', async () => {
    getPipeline.mockResolvedValueOnce(makePipeline(LOWER));

    await state().fetchPipeline(UPPER);

    expect(state().loading).toBe(false);
    expect(state().error).toBeNull();
    expect(state().pipeline?.project_id).toBe(LOWER);
  });

  it('a step mutation under the uppercase id goes through and applies', async () => {
    getPipeline.mockResolvedValueOnce(makePipeline(LOWER, 1));
    await state().fetchPipeline(UPPER);
    updatePipeline.mockResolvedValueOnce(makePipeline(LOWER, 2));

    await state().advanceStep(UPPER);

    expect(updatePipeline).toHaveBeenCalledTimes(1);
    expect(state().pipeline?.current_step).toBe(2);
    expect(state().error).toBeNull();
  });

  it('a resetPipeline under the uppercase id ends loading with the pipeline', async () => {
    resetPipeline.mockResolvedValueOnce(makePipeline(LOWER, 0));

    await state().resetPipeline(UPPER);

    expect(state().loading).toBe(false);
    expect(state().pipeline?.project_id).toBe(LOWER);
  });

  it('a load response for another project ends loading with the load error', async () => {
    getPipeline.mockResolvedValueOnce(makePipeline(OTHER));

    await state().fetchPipeline(LOWER);

    expect(state().loading).toBe(false);
    expect(state().pipeline).toBeNull();
    expect(state().error).toBe(PIPELINE_ERRORS.load);
  });

  it('a reset response for another project ends loading with the reset error', async () => {
    resetPipeline.mockResolvedValueOnce(makePipeline(OTHER));

    await state().resetPipeline(LOWER);

    expect(state().loading).toBe(false);
    expect(state().pipeline).toBeNull();
    expect(state().error).toBe(PIPELINE_ERRORS.reset);
  });

  it('a mutation response for another project is not applied and says so', async () => {
    getPipeline.mockResolvedValueOnce(makePipeline(LOWER, 1));
    await state().fetchPipeline(LOWER);
    updatePipeline.mockResolvedValueOnce(makePipeline(OTHER, 4));

    await state().goToStep(LOWER, 4);

    expect(state().pipeline?.project_id).toBe(LOWER);
    expect(state().pipeline?.current_step).toBe(1);
    expect(state().error).toBe(PIPELINE_ERRORS.goTo);
  });

  it.each([
    ['advanceStep', () => state().advanceStep(OTHER)],
    ['skipStep', () => state().skipStep(OTHER)],
    ['goToStep', () => state().goToStep(OTHER, 1)],
    ['updateStepData', () => state().updateStepData(OTHER, 1, { x: 1 })],
  ])('a refused %s is visible, not silent', async (_name, mutate) => {
    getPipeline.mockResolvedValueOnce(makePipeline(LOWER, 1));
    await state().fetchPipeline(LOWER);

    await mutate();

    expect(updatePipeline).not.toHaveBeenCalled();
    expect(state().error).toBe(PIPELINE_ERRORS.refused);
  });

  it.each([
    [
      'fetchPipeline',
      () => {
        getPipeline.mockRejectedValueOnce(new Error('HTTP 500: pg secret'));
        return state().fetchPipeline(LOWER);
      },
      PIPELINE_ERRORS.load,
    ],
    [
      'resetPipeline',
      () => {
        resetPipeline.mockRejectedValueOnce(new Error('HTTP 500: pg secret'));
        return state().resetPipeline(LOWER);
      },
      PIPELINE_ERRORS.reset,
    ],
  ])(
    '%s failure stores the fixed message and logs the detail',
    async (_name, run, expected) => {
      await run();

      expect(state().error).toBe(expected);
      expect(state().loading).toBe(false);
      expect(consoleError).toHaveBeenCalledWith(
        expect.any(String),
        expect.objectContaining({ message: 'HTTP 500: pg secret' })
      );
    }
  );

  it.each([
    ['advanceStep', () => state().advanceStep(LOWER), PIPELINE_ERRORS.advance],
    ['skipStep', () => state().skipStep(LOWER), PIPELINE_ERRORS.skip],
    ['goToStep', () => state().goToStep(LOWER, 2), PIPELINE_ERRORS.goTo],
    [
      'updateStepData',
      () => state().updateStepData(LOWER, 1, { x: 1 }),
      PIPELINE_ERRORS.updateData,
    ],
  ])(
    '%s failure stores the fixed message, not the raw error',
    async (_name, mutate, expected) => {
      getPipeline.mockResolvedValueOnce(makePipeline(LOWER, 1));
      await state().fetchPipeline(LOWER);
      updatePipeline.mockRejectedValueOnce(new Error('HTTP 500: pg secret'));

      await mutate();

      expect(state().error).toBe(expected);
      expect(consoleError).toHaveBeenCalledWith(
        expect.any(String),
        expect.objectContaining({ message: 'HTTP 500: pg secret' })
      );
    }
  );
});
