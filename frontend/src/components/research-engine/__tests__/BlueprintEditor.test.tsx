import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BlueprintEditor } from '../BlueprintEditor';
import { StepCard } from '../StepCard';
import {
  createBlueprint,
  getBlueprint,
  getCapabilities,
  getProject,
  getTemplateDetail,
  listTemplates,
  startRun,
} from '@/services/researchEngineService';
import {
  researchProtocolService,
  type ResearchQuestionResponse,
} from '@/services/researchProtocolService';
import { APIErrorClass } from '@/types/api';

vi.mock('next/navigation', () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock('@/services/researchEngineService', () => ({
  getProject: vi.fn(),
  getBlueprint: vi.fn(),
  listTemplates: vi.fn(),
  getTemplateDetail: vi.fn(),
  getCapabilities: vi.fn(),
  createBlueprint: vi.fn(),
  startRun: vi.fn(),
}));
vi.mock('@/services/researchProtocolService', () => ({
  researchProtocolService: {
    listQuestions: vi.fn(),
    createQuestion: vi.fn(),
    createQuestionVersion: vi.fn(),
    listProtocols: vi.fn(),
    createProtocol: vi.fn(),
    createProtocolVersion: vi.fn(),
  },
}));

const TEMPLATE_STEPS = [
  'search',
  'screen',
  'extract',
  'synthesize',
  'verify',
  'export',
].map((type) => ({
  type,
  name: `${type} step`,
  parameters: {},
  mode: 'deterministic' as const,
  temperature: 0,
}));

const TEMPLATE_DETAIL = {
  slug: 'daily_research_brief',
  name: 'Daily Research Brief',
  description: 'A bounded brief',
  template_source: 'daily_research_brief',
  contract_version: 1,
  parameters: {
    contract_version: 1,
    research_question: '',
    inclusion_criteria: [],
    exclusion_criteria: [],
    providers: ['openalex', 'crossref'],
    limit_per_provider: 25,
    notes: '',
  },
  constraints: {
    providers: { min: 1, max: 4 },
    limit_per_provider: { min: 1, max: 50 },
  },
  coverage: { exhaustive: false },
  steps: TEMPLATE_STEPS,
};

const CAPABILITIES = [
  {
    id: 'openalex',
    label: 'OpenAlex',
    daily_brief_eligible: true,
    available: true,
    features: { full_text: false, date_filter: false, cursor: true },
  },
  {
    id: 'crossref',
    label: 'Crossref',
    daily_brief_eligible: true,
    available: true,
    features: { full_text: false, date_filter: false, cursor: false },
  },
];

function renderEditor(
  projectId = 'project-1',
  approvedProtocolVersionId: string | null = 'protocol-version-2'
): ReturnType<typeof render> {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <BlueprintEditor
        projectId={projectId}
        approvedProtocolVersionId={approvedProtocolVersionId ?? undefined}
      />
    </QueryClientProvider>
  );
}

describe('BlueprintEditor loading', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(getProject).mockResolvedValue({
      id: 'project-1',
      name: 'Research',
      blueprint_id: 'blueprint-1',
    });
    vi.mocked(getBlueprint).mockResolvedValue({
      id: 'blueprint-1',
      name: 'Paper discovery',
      steps: [],
      parameters: {},
    });
    vi.mocked(listTemplates).mockResolvedValue([]);
    vi.mocked(getTemplateDetail).mockResolvedValue(TEMPLATE_DETAIL);
    vi.mocked(getCapabilities).mockResolvedValue(CAPABILITIES);
  });

  it('loads the saved blueprint after the request completes', async () => {
    renderEditor();
    expect(screen.getByText('Loading project')).toBeInTheDocument();
    expect(
      await screen.findByRole('textbox', { name: 'Blueprint name' })
    ).toHaveValue('Paper discovery');
    expect(screen.queryByText('Loading project')).not.toBeInTheDocument();
  });

  it('recovers from a failed load through Retry', async () => {
    vi.mocked(getProject).mockRejectedValueOnce(
      new Error('Network unavailable')
    );
    renderEditor();
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Failed to load project.'
    );
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(screen.getByText('Loading project')).toBeInTheDocument();
    expect(
      await screen.findByRole('textbox', { name: 'Blueprint name' })
    ).toHaveValue('Paper discovery');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('fetches full template detail before applying and saves its template source', async () => {
    vi.mocked(getProject).mockResolvedValue({
      id: 'project-1',
      name: 'Research',
    });
    vi.mocked(listTemplates).mockResolvedValue([
      {
        slug: 'daily_research_brief',
        name: 'Daily Research Brief',
        description: 'A bounded brief',
        step_count: 6,
      },
    ]);
    let resolveDetail: (value: typeof TEMPLATE_DETAIL) => void = () =>
      undefined;
    vi.mocked(getTemplateDetail).mockReturnValue(
      new Promise((resolve) => {
        resolveDetail = resolve;
      })
    );
    vi.mocked(createBlueprint).mockResolvedValue({
      id: 'blueprint-1',
      ...TEMPLATE_DETAIL,
      project_id: 'project-1',
      version: 1,
      is_immutable: false,
      created_at: '2026-09-28T00:00:00Z',
      updated_at: '2026-09-28T00:00:00Z',
    });

    renderEditor();
    fireEvent.click(
      await screen.findByRole('button', { name: /Daily Research Brief/i })
    );
    expect(getTemplateDetail).toHaveBeenCalledWith('daily_research_brief');
    expect(
      screen.queryByRole('textbox', { name: 'Blueprint name' })
    ).not.toBeInTheDocument();

    resolveDetail(TEMPLATE_DETAIL);
    expect(
      await screen.findByRole('textbox', { name: 'Blueprint name' })
    ).toHaveValue('Daily Research Brief');
    expect(screen.getByText('Steps (6)')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(createBlueprint).toHaveBeenCalledWith(
      'project-1',
      expect.objectContaining({
        template_source: 'daily_research_brief',
        steps: TEMPLATE_STEPS,
      })
    );
  });

  it('clears template_source when the user creates a custom topology', async () => {
    vi.mocked(getBlueprint).mockResolvedValue({
      id: 'blueprint-1',
      name: 'Daily Research Brief',
      template_source: 'daily_research_brief',
      steps: TEMPLATE_STEPS,
      parameters: TEMPLATE_DETAIL.parameters,
    });
    vi.mocked(createBlueprint).mockResolvedValue({
      id: 'blueprint-custom',
      name: 'Daily Research Brief',
      steps: [...TEMPLATE_STEPS, TEMPLATE_STEPS[0]],
      parameters: TEMPLATE_DETAIL.parameters,
    });

    renderEditor();
    expect(await screen.findByText('Steps (6)')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Add step' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    const request = vi.mocked(createBlueprint).mock.calls[0][1];
    expect(request).not.toHaveProperty('template_source');
    expect(request.steps).toHaveLength(7);
  });

  it('requires an exact scope confirmation before starting a Daily Brief', async () => {
    const parameters = {
      ...TEMPLATE_DETAIL.parameters,
      research_question: 'What changed?',
      inclusion_criteria: ['Peer reviewed'],
    };
    vi.mocked(getBlueprint).mockResolvedValue({
      id: 'blueprint-1',
      name: 'Daily Research Brief',
      template_source: 'daily_research_brief',
      steps: TEMPLATE_STEPS,
      parameters,
    });
    vi.mocked(startRun).mockResolvedValue({ id: 'run-1' });

    renderEditor();
    const start = await screen.findByRole('button', { name: 'Start run' });
    expect(start).toBeDisabled();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Confirm scope' }));
    expect(start).toBeEnabled();
    fireEvent.click(start);

    expect(startRun).toHaveBeenCalledWith('blueprint-1', {
      protocol_version_id: 'protocol-version-2',
      parameters_override: {},
      scope_confirmation: {
        confirmed: true,
        research_question: 'What changed?',
        inclusion_criteria: ['Peer reviewed'],
        exclusion_criteria: [],
        providers: ['openalex', 'crossref'],
        limit_per_provider: 25,
        notes: '',
      },
    });
  });

  it('keeps an edited Daily Brief draft from starting the old blueprint until save succeeds', async () => {
    const parameters = {
      ...TEMPLATE_DETAIL.parameters,
      research_question: 'What changed?',
      inclusion_criteria: ['Peer reviewed'],
    };
    vi.mocked(getBlueprint).mockResolvedValue({
      id: 'blueprint-1',
      name: 'Daily Research Brief',
      template_source: 'daily_research_brief',
      steps: TEMPLATE_STEPS,
      parameters,
    });
    vi.mocked(createBlueprint)
      .mockRejectedValueOnce(new Error('Save unavailable'))
      .mockResolvedValueOnce({
        id: 'blueprint-custom',
        name: 'Daily Research Brief',
        steps: [
          ...TEMPLATE_STEPS,
          {
            type: 'search',
            name: '',
            description: '',
            parameters: {},
            mode: 'deterministic',
            temperature: 0,
          },
        ],
        parameters,
      });
    vi.mocked(startRun).mockResolvedValue({ id: 'run-custom' });

    renderEditor();
    const start = await screen.findByRole('button', { name: 'Start run' });
    fireEvent.click(screen.getByRole('checkbox', { name: 'Confirm scope' }));
    expect(start).toBeEnabled();

    fireEvent.click(screen.getByRole('button', { name: 'Add step' }));
    expect(start).toBeDisabled();
    fireEvent.click(start);
    expect(startRun).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Failed to save blueprint.'
    );
    expect(start).toBeDisabled();
    expect(startRun).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(start).toBeEnabled());
    expect(createBlueprint).toHaveBeenLastCalledWith(
      'project-1',
      expect.objectContaining({
        steps: expect.arrayContaining([
          expect.objectContaining({ type: 'search', name: '' }),
        ]),
      })
    );
    expect(
      vi.mocked(createBlueprint).mock.calls.at(-1)?.[1]
    ).not.toHaveProperty('template_source');

    fireEvent.click(start);
    expect(startRun).toHaveBeenCalledWith('blueprint-custom', {
      protocol_version_id: 'protocol-version-2',
      parameters_override: {},
    });
  });

  it('requires an approved protocol before starting a saved blueprint', async () => {
    vi.mocked(getBlueprint).mockResolvedValue({
      id: 'blueprint-1',
      name: 'Paper discovery',
      steps: [TEMPLATE_STEPS[0]],
      parameters: { topic: 'Saved method' },
    });
    vi.mocked(startRun).mockResolvedValue({ id: 'run-1' });

    const rendered = renderEditor('project-1', null);
    const start = await screen.findByRole('button', { name: 'Start run' });
    expect(start).toBeDisabled();
    expect(
      screen.getByText('Approve a protocol version before starting a run.')
    ).toBeInTheDocument();

    rendered.rerender(
      <QueryClientProvider
        client={
          new QueryClient({
            defaultOptions: { queries: { retry: false } },
          })
        }
      >
        <BlueprintEditor
          projectId="project-1"
          approvedProtocolVersionId="protocol-version-2"
        />
      </QueryClientProvider>
    );
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }));
    expect(startRun).toHaveBeenCalledWith('blueprint-1', {
      protocol_version_id: 'protocol-version-2',
      parameters_override: {},
    });
  });

  it('keeps a newer topology dirty when an older save resolves', async () => {
    vi.mocked(getBlueprint).mockResolvedValue({
      id: 'blueprint-1',
      name: 'Custom brief',
      steps: TEMPLATE_STEPS,
      parameters: {},
    });
    const staleResponse = {
      id: 'blueprint-stale',
      name: 'Custom brief',
      steps: [
        ...TEMPLATE_STEPS,
        {
          type: 'search',
          name: '',
          description: '',
          parameters: {},
          mode: 'deterministic' as const,
          temperature: 0,
        },
      ],
      parameters: {},
    };
    let resolveSave: (value: typeof staleResponse) => void = () => undefined;
    vi.mocked(createBlueprint).mockReturnValue(
      new Promise((resolve) => {
        resolveSave = resolve;
      })
    );

    renderEditor();
    const start = await screen.findByRole('button', { name: 'Start run' });
    expect(start).toBeEnabled();

    fireEvent.click(screen.getByRole('button', { name: 'Add step' }));
    expect(screen.getByText('Steps (7)')).toBeInTheDocument();
    expect(start).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(
      await screen.findByRole('button', { name: 'Saving' })
    ).toBeDisabled();
    expect(vi.mocked(createBlueprint).mock.calls[0][1].steps).toHaveLength(7);

    fireEvent.click(screen.getByRole('button', { name: 'Add step' }));
    expect(screen.getByText('Steps (8)')).toBeInTheDocument();

    await act(async () => resolveSave(staleResponse));
    await screen.findByRole('button', { name: 'Save' });
    expect(start).toBeDisabled();
    expect(screen.queryByText('blueprint-stale')).not.toBeInTheDocument();
    fireEvent.click(start);
    expect(startRun).not.toHaveBeenCalled();
  });
});

const BRIEF_PARAMETERS = {
  ...TEMPLATE_DETAIL.parameters,
  research_question: 'What changed?',
  inclusion_criteria: ['Peer reviewed'],
};

const SAVED_BRIEF = {
  id: 'blueprint-1',
  name: 'Daily Research Brief',
  template_source: 'daily_research_brief',
  steps: TEMPLATE_STEPS,
  parameters: BRIEF_PARAMETERS,
};

const EXPECTED_SNAPSHOT = {
  eligibility: {
    inclusion_criteria: ['Peer reviewed'],
    exclusion_criteria: [],
  },
  sources_search: {
    providers: ['openalex', 'crossref'],
    limit_per_provider: 25,
  },
  selection: { method: 'daily_research_brief', exhaustive: false },
  extraction: { method: 'daily_research_brief' },
  appraisal_synthesis: { method: 'daily_research_brief' },
  outcomes: { method: 'daily_research_brief' },
  reviewer_mode: { mode: 'single' },
};

function questionRecord(
  text: string,
  versionId = 'question-version-1'
): ResearchQuestionResponse {
  const version = {
    id: versionId,
    question_id: 'question-1',
    version: 1,
    parent_version_id: null,
    question: text,
    hypothesis: null,
    scope: null,
    framework: {},
    content_hash: 'q'.repeat(64),
    author_user_id: 'author-1',
    created_at: '2026-11-20T00:00:00Z',
  };
  return {
    id: 'question-1',
    project_id: 'project-1',
    current_version_id: versionId,
    current_version: version,
    versions: [version],
    created_at: '2026-11-20T00:00:00Z',
  };
}

const BRIEF_PROTOCOL = {
  id: 'protocol-1',
  project_id: 'project-1',
  name: 'Daily Research Brief protocol',
  current_draft_version_id: 'protocol-version-1',
  current_approved_version_id: null,
  versions: [
    {
      id: 'protocol-version-1',
      question_version_id: 'question-version-1',
      blueprint_id: 'blueprint-1',
    },
  ],
};

function savedBriefResponse(
  id: string
): Awaited<ReturnType<typeof createBlueprint>> {
  return {
    ...SAVED_BRIEF,
    id,
    project_id: 'project-1',
    version: 1,
    is_immutable: false,
    created_at: '2026-11-26T00:00:00Z',
    updated_at: '2026-11-26T00:00:00Z',
  };
}

describe('BlueprintEditor Daily Research Brief protocol flow', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(getProject).mockResolvedValue({
      id: 'project-1',
      name: 'Research',
      blueprint_id: 'blueprint-1',
    });
    vi.mocked(getBlueprint).mockResolvedValue(SAVED_BRIEF);
    vi.mocked(listTemplates).mockResolvedValue([
      {
        slug: 'daily_research_brief',
        name: 'Daily Research Brief',
        description: 'A bounded brief',
        step_count: 6,
      },
    ]);
    vi.mocked(getTemplateDetail).mockResolvedValue(TEMPLATE_DETAIL);
    vi.mocked(getCapabilities).mockResolvedValue(CAPABILITIES);
  });

  it('creates the question and protocol draft from the first brief save', async () => {
    vi.mocked(getProject).mockResolvedValue({
      id: 'project-1',
      name: 'Research',
    });
    vi.mocked(createBlueprint).mockResolvedValue(
      savedBriefResponse('blueprint-new')
    );
    vi.mocked(researchProtocolService.listProtocols).mockResolvedValue([]);
    vi.mocked(researchProtocolService.createQuestion).mockResolvedValue(
      questionRecord('What changed?', 'question-version-new')
    );
    vi.mocked(researchProtocolService.createProtocol).mockResolvedValue(
      BRIEF_PROTOCOL as never
    );

    renderEditor('project-1', null);
    fireEvent.click(
      await screen.findByRole('button', { name: /Daily Research Brief/i })
    );
    fireEvent.change(
      await screen.findByRole('textbox', { name: 'Research question' }),
      { target: { value: '  What changed?  ' } }
    );
    fireEvent.change(screen.getByRole('textbox', { name: 'What to include' }), {
      target: { value: 'Peer reviewed' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() =>
      expect(researchProtocolService.createProtocol).toHaveBeenCalled()
    );
    expect(vi.mocked(createBlueprint).mock.calls[0][1].parameters).toEqual(
      expect.objectContaining({ research_question: '  What changed?  ' })
    );
    expect(researchProtocolService.createQuestion).toHaveBeenCalledWith(
      'project-1',
      { question: 'What changed?', framework: {} }
    );
    expect(researchProtocolService.createProtocol).toHaveBeenCalledWith(
      'project-1',
      {
        name: 'Daily Research Brief protocol',
        question_version_id: 'question-version-new',
        blueprint_id: 'blueprint-new',
        snapshot: EXPECTED_SNAPSHOT,
      }
    );
    const order = [
      vi.mocked(createBlueprint).mock.invocationCallOrder[0],
      vi.mocked(researchProtocolService.createQuestion).mock
        .invocationCallOrder[0],
      vi.mocked(researchProtocolService.createProtocol).mock
        .invocationCallOrder[0],
    ];
    expect(order).toEqual([...order].sort((a, b) => a - b));
    expect(
      researchProtocolService.createProtocolVersion
    ).not.toHaveBeenCalled();
    expect(
      await screen.findByText(
        "An independent supervisor must approve this brief's protocol before it can start."
      )
    ).toBeInTheDocument();
    expect(
      screen.queryByText('Approve a protocol version before starting a run.')
    ).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('amends the brief protocol on re-save without a new question version when the question is unchanged', async () => {
    vi.mocked(createBlueprint).mockResolvedValue(
      savedBriefResponse('blueprint-2')
    );
    vi.mocked(researchProtocolService.listProtocols).mockResolvedValue([
      BRIEF_PROTOCOL as never,
    ]);
    vi.mocked(researchProtocolService.listQuestions).mockResolvedValue([
      questionRecord('What changed?'),
    ]);
    vi.mocked(researchProtocolService.createProtocolVersion).mockResolvedValue(
      BRIEF_PROTOCOL as never
    );

    renderEditor();
    fireEvent.click(await screen.findByRole('button', { name: 'Save' }));

    await waitFor(() =>
      expect(researchProtocolService.createProtocolVersion).toHaveBeenCalled()
    );
    expect(researchProtocolService.createProtocolVersion).toHaveBeenCalledWith(
      'protocol-1',
      {
        question_version_id: 'question-version-1',
        blueprint_id: 'blueprint-2',
        snapshot: EXPECTED_SNAPSHOT,
        parent_version_id: 'protocol-version-1',
        amendment_reason:
          'Daily Research Brief setup changed and was saved again.',
      }
    );
    expect(researchProtocolService.createQuestion).not.toHaveBeenCalled();
    expect(
      researchProtocolService.createQuestionVersion
    ).not.toHaveBeenCalled();
    expect(researchProtocolService.createProtocol).not.toHaveBeenCalled();
  });

  it('creates a question version before amending when the brief question changed', async () => {
    vi.mocked(createBlueprint).mockResolvedValue(
      savedBriefResponse('blueprint-2')
    );
    vi.mocked(researchProtocolService.listProtocols).mockResolvedValue([
      BRIEF_PROTOCOL as never,
    ]);
    vi.mocked(researchProtocolService.listQuestions).mockResolvedValue([
      questionRecord('What changed?'),
    ]);
    vi.mocked(researchProtocolService.createQuestionVersion).mockResolvedValue(
      questionRecord('What changed this week?', 'question-version-2')
    );
    vi.mocked(researchProtocolService.createProtocolVersion).mockResolvedValue(
      BRIEF_PROTOCOL as never
    );

    renderEditor();
    fireEvent.change(
      await screen.findByRole('textbox', { name: 'Research question' }),
      { target: { value: 'What changed this week? ' } }
    );
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() =>
      expect(researchProtocolService.createProtocolVersion).toHaveBeenCalled()
    );
    expect(researchProtocolService.createQuestionVersion).toHaveBeenCalledWith(
      'question-1',
      {
        question: 'What changed this week?',
        framework: {},
        parent_version_id: 'question-version-1',
      }
    );
    expect(researchProtocolService.createProtocolVersion).toHaveBeenCalledWith(
      'protocol-1',
      expect.objectContaining({
        question_version_id: 'question-version-2',
        blueprint_id: 'blueprint-2',
        parent_version_id: 'protocol-version-1',
      })
    );
    expect(researchProtocolService.createQuestion).not.toHaveBeenCalled();
  });

  it('shows the brief waiting message for a saved brief without an approved protocol', async () => {
    renderEditor('project-1', null);
    expect(
      await screen.findByText(
        "An independent supervisor must approve this brief's protocol before it can start."
      )
    ).toBeInTheDocument();
    expect(
      screen.queryByText('Approve a protocol version before starting a run.')
    ).not.toBeInTheDocument();
  });

  it('marks a saved brief unsaved when a setup field changes after save', async () => {
    renderEditor();
    const start = await screen.findByRole('button', { name: 'Start run' });
    const confirm = screen.getByRole('checkbox', { name: 'Confirm scope' });
    fireEvent.click(confirm);
    expect(start).toBeEnabled();

    fireEvent.change(
      screen.getByRole('textbox', { name: 'Research question' }),
      {
        target: { value: 'What changed this week?' },
      }
    );
    fireEvent.click(confirm);

    expect(confirm).toBeChecked();
    expect(start).toBeDisabled();
    expect(
      screen.getByText('Save your changes before starting a run.')
    ).toBeInTheDocument();
    fireEvent.click(start);
    expect(startRun).not.toHaveBeenCalled();
  });

  it('keeps an unedited saved brief startable when only the scope confirmation toggles', async () => {
    renderEditor();
    const start = await screen.findByRole('button', { name: 'Start run' });
    const confirm = screen.getByRole('checkbox', { name: 'Confirm scope' });
    fireEvent.click(confirm);
    fireEvent.click(confirm);
    expect(start).toBeDisabled();
    fireEvent.click(confirm);

    expect(start).toBeEnabled();
    expect(
      screen.queryByText('Save your changes before starting a run.')
    ).not.toBeInTheDocument();
  });

  it('shows the server reason when starting a run is rejected', async () => {
    vi.mocked(startRun).mockRejectedValue(
      new APIErrorClass({
        message:
          'Scope confirmation does not match effective Daily Brief parameters',
        status_code: 422,
        type: 'http_error',
      })
    );

    renderEditor();
    const start = await screen.findByRole('button', { name: 'Start run' });
    fireEvent.click(screen.getByRole('checkbox', { name: 'Confirm scope' }));
    fireEvent.click(start);

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(
      'Scope confirmation does not match effective Daily Brief parameters'
    );
    expect(alert).not.toHaveTextContent('Failed to start run.');
  });

  it('reports a failed protocol step and retries Save without a duplicate question', async () => {
    vi.mocked(createBlueprint)
      .mockResolvedValueOnce(savedBriefResponse('blueprint-2'))
      .mockResolvedValueOnce(savedBriefResponse('blueprint-3'));
    vi.mocked(researchProtocolService.listProtocols).mockResolvedValue([]);
    vi.mocked(researchProtocolService.createQuestion).mockResolvedValue(
      questionRecord('What changed?', 'question-version-new')
    );
    vi.mocked(researchProtocolService.createProtocol)
      .mockRejectedValueOnce({})
      .mockResolvedValueOnce(BRIEF_PROTOCOL as never);

    renderEditor();
    fireEvent.click(await screen.findByRole('button', { name: 'Save' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The brief was saved, but its protocol draft could not be created.'
    );

    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() =>
      expect(researchProtocolService.createProtocol).toHaveBeenCalledTimes(2)
    );
    expect(researchProtocolService.createQuestion).toHaveBeenCalledTimes(1);
    expect(
      vi.mocked(researchProtocolService.createProtocol).mock.calls[1][1]
    ).toEqual(
      expect.objectContaining({
        question_version_id: 'question-version-new',
        blueprint_id: 'blueprint-3',
      })
    );
    await waitFor(() =>
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    );
  });

  it('confirms the exact saved question so a padded brief question can start', async () => {
    vi.mocked(getBlueprint).mockResolvedValue({
      ...SAVED_BRIEF,
      parameters: {
        ...BRIEF_PARAMETERS,
        research_question: '  What changed?  ',
      },
    });
    vi.mocked(startRun).mockResolvedValue({ id: 'run-1' });

    renderEditor();
    const start = await screen.findByRole('button', { name: 'Start run' });
    fireEvent.click(screen.getByRole('checkbox', { name: 'Confirm scope' }));
    fireEvent.click(start);

    expect(startRun).toHaveBeenCalledWith(
      'blueprint-1',
      expect.objectContaining({
        scope_confirmation: expect.objectContaining({
          research_question: '  What changed?  ',
        }),
      })
    );
  });

  it('creates the protocol draft for a saved brief even when newer edits keep the editor unsaved', async () => {
    let resolveSave: (
      value: Awaited<ReturnType<typeof createBlueprint>>
    ) => void = () => undefined;
    vi.mocked(createBlueprint).mockReturnValue(
      new Promise((resolve) => {
        resolveSave = resolve;
      })
    );
    vi.mocked(researchProtocolService.listProtocols).mockResolvedValue([]);
    vi.mocked(researchProtocolService.createQuestion).mockResolvedValue(
      questionRecord('What changed?', 'question-version-new')
    );
    vi.mocked(researchProtocolService.createProtocol).mockResolvedValue(
      BRIEF_PROTOCOL as never
    );

    renderEditor();
    fireEvent.click(await screen.findByRole('button', { name: 'Save' }));
    fireEvent.change(screen.getByRole('textbox', { name: 'Notes' }), {
      target: { value: 'Added while saving' },
    });
    await act(async () => resolveSave(savedBriefResponse('blueprint-2')));

    await waitFor(() =>
      expect(researchProtocolService.createProtocol).toHaveBeenCalled()
    );
    expect(researchProtocolService.createProtocol).toHaveBeenCalledWith(
      'project-1',
      expect.objectContaining({
        blueprint_id: 'blueprint-2',
        snapshot: EXPECTED_SNAPSHOT,
      })
    );
    expect(screen.getByRole('button', { name: 'Start run' })).toBeDisabled();
    expect(
      screen.getByText('Save your changes before starting a run.')
    ).toBeInTheDocument();
  });

  it('retries only the protocol step from the alert after a protocol failure', async () => {
    vi.mocked(createBlueprint).mockResolvedValue(
      savedBriefResponse('blueprint-2')
    );
    vi.mocked(researchProtocolService.listProtocols).mockResolvedValue([]);
    vi.mocked(researchProtocolService.createQuestion).mockResolvedValue(
      questionRecord('What changed?', 'question-version-new')
    );
    vi.mocked(researchProtocolService.createProtocol)
      .mockRejectedValueOnce(
        new APIErrorClass({
          message: 'Protocol service unavailable',
          status_code: 503,
          type: 'http_error',
        })
      )
      .mockResolvedValueOnce(BRIEF_PROTOCOL as never);

    renderEditor();
    fireEvent.click(await screen.findByRole('button', { name: 'Save' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Protocol service unavailable'
    );

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() =>
      expect(researchProtocolService.createProtocol).toHaveBeenCalledTimes(2)
    );
    expect(
      vi.mocked(researchProtocolService.createProtocol).mock.calls[1][1]
    ).toEqual(
      expect.objectContaining({
        question_version_id: 'question-version-new',
        blueprint_id: 'blueprint-2',
      })
    );
    expect(createBlueprint).toHaveBeenCalledTimes(1);
    expect(researchProtocolService.createQuestion).toHaveBeenCalledTimes(1);
    expect(getBlueprint).toHaveBeenCalledTimes(1);
    await waitFor(() =>
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    );
  });

  it('makes no protocol calls when a non-brief blueprint is saved', async () => {
    vi.mocked(getBlueprint).mockResolvedValue({
      id: 'blueprint-1',
      name: 'Paper discovery',
      steps: [TEMPLATE_STEPS[0]],
      parameters: { topic: 'Saved method', research_question: 'Unrelated' },
    });
    vi.mocked(createBlueprint).mockResolvedValue({
      id: 'blueprint-2',
      name: 'Paper discovery',
      steps: [TEMPLATE_STEPS[0]],
      parameters: { topic: 'Saved method', research_question: 'Unrelated' },
    });

    renderEditor();
    fireEvent.click(await screen.findByRole('button', { name: 'Save' }));
    await waitFor(() => expect(createBlueprint).toHaveBeenCalled());
    await screen.findByRole('button', { name: 'Save' });
    expect(researchProtocolService.listProtocols).not.toHaveBeenCalled();
    expect(researchProtocolService.createQuestion).not.toHaveBeenCalled();
    expect(researchProtocolService.createProtocol).not.toHaveBeenCalled();
  });
});

describe('StepCard contract', () => {
  it('offers exactly the six backend-supported step types', () => {
    render(
      <StepCard
        step={TEMPLATE_STEPS[0]}
        index={0}
        totalSteps={1}
        onChange={vi.fn()}
        onMoveUp={vi.fn()}
        onMoveDown={vi.fn()}
        onRemove={vi.fn()}
      />
    );
    fireEvent.click(screen.getByRole('button', { name: /search step/i }));
    const typeSelect = screen.getByRole('combobox', { name: 'Step type' });
    expect(
      within(typeSelect)
        .getAllByRole('option')
        .map((option) => option.getAttribute('value'))
    ).toEqual([
      'search',
      'screen',
      'extract',
      'synthesize',
      'verify',
      'export',
    ]);
  });
});
