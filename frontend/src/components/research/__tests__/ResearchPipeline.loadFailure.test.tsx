/**
 * GOO-666 FL2E-3: when the first pipeline fetch fails, ResearchPipeline
 * shows an error with a retry instead of a spinner that never stops. Uses
 * the real pipelineStore; only the network call is mocked.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@/test/test-utils';
import { ResearchPipeline } from '../ResearchPipeline';
import { PIPELINE_ERRORS, usePipelineStore } from '@/store/pipelineStore';
import * as scispaceService from '@/services/scispaceService';

vi.mock('@/services/scispaceService', () => ({
  getPipeline: vi.fn(),
  updatePipeline: vi.fn(),
}));

vi.mock('@/store/projectStore', () => ({
  useProjectStore: vi.fn((selector) =>
    selector({
      projectDocuments: [],
      documentsLoading: false,
      removeDocument: vi.fn(),
    })
  ),
}));

vi.mock('../PipelineStepper', () => ({
  PipelineStepper: () => <div>stepper</div>,
}));
vi.mock('../steps/CollectStep', () => ({
  CollectStep: () => <div>collect-step</div>,
}));
vi.mock('../steps/ExtractStep', () => ({
  ExtractStep: () => <div>extract-step</div>,
}));
vi.mock('../steps/CiteStep', () => ({ CiteStep: () => <div>cite-step</div> }));
vi.mock('../steps/DraftStep', () => ({
  DraftStep: () => <div>draft-step</div>,
}));
vi.mock('../steps/ExportStep', () => ({
  ExportStep: () => <div>export-step</div>,
}));

const getPipeline = scispaceService.getPipeline as unknown as ReturnType<
  typeof vi.fn
>;
const pipeline = {
  // The store drops a response for another project (GOO-762).
  project_id: 'p1',
  current_step: 0,
  completed_steps: [],
  skipped_steps: [],
  invalidated_steps: [],
};

describe('ResearchPipeline first-load failure', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    usePipelineStore.getState().reset();
    // The store logs failure detail (GOO-778); keep the test output quiet.
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  it('shows an error with Retry instead of spinning forever, and Retry loads the pipeline', async () => {
    getPipeline.mockRejectedValueOnce(new Error('HTTP 503 upstream'));
    render(<ResearchPipeline projectId="p1" />);

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent("Couldn't load the research pipeline.");
    // A stable message, not the raw error text.
    expect(alert).not.toHaveTextContent('503');
    expect(document.querySelector('.animate-spin')).toBeNull();

    getPipeline.mockResolvedValueOnce(pipeline);
    fireEvent.click(screen.getByRole('button', { name: /retry/i }));

    expect(await screen.findByText('collect-step')).toBeInTheDocument();
    expect(getPipeline).toHaveBeenCalledTimes(2);
    expect(getPipeline).toHaveBeenLastCalledWith('p1');
    // The retry cleared the failure, not just hid it behind the pipeline.
    expect(screen.queryByRole('alert')).toBeNull();
    expect(usePipelineStore.getState().error).toBeNull();
  });

  it('shows the spinner again while a retry is in flight', async () => {
    getPipeline.mockRejectedValueOnce(new Error('boom'));
    render(<ResearchPipeline projectId="p1" />);
    await screen.findByRole('alert');

    let resolve!: (value: typeof pipeline) => void;
    getPipeline.mockReturnValueOnce(
      new Promise((r) => {
        resolve = r;
      })
    );
    fireEvent.click(screen.getByRole('button', { name: /retry/i }));

    await waitFor(() =>
      expect(document.querySelector('.animate-spin')).not.toBeNull()
    );
    expect(screen.queryByRole('alert')).toBeNull();
    resolve(pipeline);
    expect(await screen.findByText('collect-step')).toBeInTheDocument();
  });

  it('a first-load response for another project ends in the error with Retry, not a spinner (GOO-778)', async () => {
    getPipeline.mockResolvedValueOnce({ ...pipeline, project_id: 'other' });
    render(<ResearchPipeline projectId="p1" />);

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent("Couldn't load the research pipeline.");
    expect(document.querySelector('.animate-spin')).toBeNull();

    getPipeline.mockResolvedValueOnce(pipeline);
    fireEvent.click(screen.getByRole('button', { name: /retry/i }));
    expect(await screen.findByText('collect-step')).toBeInTheDocument();
  });

  it('loads under an uppercase UUID project id when the backend answers in lowercase (GOO-778)', async () => {
    const id = '0f8e2a1c-3b4d-4e5f-8a9b-0c1d2e3f4a5b';
    getPipeline.mockResolvedValueOnce({ ...pipeline, project_id: id });
    render(<ResearchPipeline projectId={id.toUpperCase()} />);

    expect(await screen.findByText('collect-step')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('a refused step change shows feedback in the banner (GOO-778)', async () => {
    getPipeline.mockResolvedValueOnce(pipeline);
    render(<ResearchPipeline projectId="p1" />);
    await screen.findByText('collect-step');

    await act(async () => {
      await usePipelineStore.getState().advanceStep('other');
    });

    expect(await screen.findByRole('alert')).toHaveTextContent(
      PIPELINE_ERRORS.refused
    );
    expect(screen.getByText('collect-step')).toBeInTheDocument();
  });

  it('the banner never shows raw error text (GOO-778)', async () => {
    getPipeline.mockResolvedValueOnce(pipeline);
    render(<ResearchPipeline projectId="p1" />);
    await screen.findByText('collect-step');

    act(() => {
      usePipelineStore.setState({ error: 'HTTP 500: pg secret' });
    });

    const alert = await screen.findByRole('alert');
    expect(alert).not.toHaveTextContent('pg secret');
    expect(alert).toHaveTextContent(PIPELINE_ERRORS.generic);
  });

  it('a failed reload of the loaded project offers Retry in the banner (GOO-778)', async () => {
    getPipeline.mockResolvedValueOnce(pipeline);
    render(<ResearchPipeline projectId="p1" />);
    await screen.findByText('collect-step');

    getPipeline.mockRejectedValueOnce(new Error('HTTP 503'));
    await act(async () => {
      await usePipelineStore.getState().fetchPipeline('p1');
    });

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(PIPELINE_ERRORS.load);
    getPipeline.mockResolvedValueOnce(pipeline);
    fireEvent.click(screen.getByRole('button', { name: /retry/i }));
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
    expect(getPipeline).toHaveBeenCalledTimes(3);
  });
});
