/**
 * projectService request contract (GOO-742).
 *
 * The service is the frontend's only client for the project, draft, claim,
 * peer-review, release, statement and deposit routes. Each case pins what a
 * call sends (method, exact path and query encoding, body or download name)
 * and what it hands back (the response body untouched, or nothing for a
 * delete/download), and that a failed request rejects with the same error.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Mock } from 'vitest';
import { api } from '@/services/api-client';
import { projectService } from '@/services/projectService';

vi.mock('@/services/api-client', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
    download: vi.fn(),
    request: vi.fn(),
  },
}));

type Method = 'get' | 'post' | 'patch' | 'delete' | 'download';
const mocked = api as unknown as Record<Method | 'put' | 'request', Mock>;
const METHODS: Method[] = ['get', 'post', 'patch', 'delete', 'download'];

interface Case {
  name: string;
  call: () => Promise<unknown>;
  method: Method;
  /** The exact arguments the api client must receive. */
  args: unknown[];
  /** True when the call resolves to the response body, false for void. */
  returnsBody: boolean;
}

const data = { field: 'value' };
const abortSignal = new AbortController().signal;

const cases: Case[] = [
  // Projects
  {
    name: 'listProjects drops undefined params and passes the abort signal',
    call: () =>
      projectService.listProjects(
        { workspace_id: 'w1', skip: 0, limit: 20, search: undefined, tag: 'a b' },
        { signal: abortSignal }
      ),
    method: 'get',
    args: ['/projects?workspace_id=w1&skip=0&limit=20&tag=a+b', { signal: abortSignal }],
    returnsBody: true,
  },
  {
    name: 'listProjects without params has no query string',
    call: () => projectService.listProjects(),
    method: 'get',
    args: ['/projects', { signal: undefined }],
    returnsBody: true,
  },
  {
    name: 'createProject',
    call: () => projectService.createProject(data as never),
    method: 'post',
    args: ['/projects', data],
    returnsBody: true,
  },
  {
    name: 'getProject',
    call: () => projectService.getProject('p1'),
    method: 'get',
    args: ['/projects/p1'],
    returnsBody: true,
  },
  {
    name: 'updateProject',
    call: () => projectService.updateProject('p1', data as never),
    method: 'patch',
    args: ['/projects/p1', data],
    returnsBody: true,
  },
  {
    name: 'deleteProject',
    call: () => projectService.deleteProject('p1'),
    method: 'delete',
    args: ['/projects/p1'],
    returnsBody: false,
  },
  // Project documents
  {
    name: 'listProjectDocuments keeps only defined params',
    call: () => projectService.listProjectDocuments('p1', { skip: 5, limit: undefined }),
    method: 'get',
    args: ['/projects/p1/documents?skip=5'],
    returnsBody: true,
  },
  {
    name: 'listProjectDocuments without params',
    call: () => projectService.listProjectDocuments('p1'),
    method: 'get',
    args: ['/projects/p1/documents'],
    returnsBody: true,
  },
  {
    name: 'addDocumentToProject sends the document id as a query param',
    call: () => projectService.addDocumentToProject('p1', 'd1'),
    method: 'post',
    args: ['/projects/p1/documents?document_id=d1'],
    returnsBody: true,
  },
  {
    name: 'removeDocumentFromProject',
    call: () => projectService.removeDocumentFromProject('p1', 'd1'),
    method: 'delete',
    args: ['/projects/p1/documents/d1'],
    returnsBody: false,
  },
  // Notes and memories
  {
    name: 'listProjectNotes serialises booleans',
    call: () => projectService.listProjectNotes('p1', { pinned_only: true }),
    method: 'get',
    args: ['/projects/p1/notes?pinned_only=true'],
    returnsBody: true,
  },
  {
    name: 'listProjectNotes without params',
    call: () => projectService.listProjectNotes('p1'),
    method: 'get',
    args: ['/projects/p1/notes'],
    returnsBody: true,
  },
  {
    name: 'createNote',
    call: () => projectService.createNote('p1', data as never),
    method: 'post',
    args: ['/projects/p1/notes', data],
    returnsBody: true,
  },
  {
    name: 'getNote',
    call: () => projectService.getNote('p1', 'n1'),
    method: 'get',
    args: ['/projects/p1/notes/n1'],
    returnsBody: true,
  },
  {
    name: 'updateNote',
    call: () => projectService.updateNote('p1', 'n1', data as never),
    method: 'patch',
    args: ['/projects/p1/notes/n1', data],
    returnsBody: true,
  },
  {
    name: 'deleteNote',
    call: () => projectService.deleteNote('p1', 'n1'),
    method: 'delete',
    args: ['/projects/p1/notes/n1'],
    returnsBody: false,
  },
  {
    name: 'toggleNotePin',
    call: () => projectService.toggleNotePin('p1', 'n1'),
    method: 'post',
    args: ['/projects/p1/notes/n1/pin'],
    returnsBody: true,
  },
  {
    name: 'listMemories',
    call: () => projectService.listMemories('p1'),
    method: 'get',
    args: ['/projects/p1/memories'],
    returnsBody: true,
  },
  {
    name: 'createMemory defaults the source to manual',
    call: () => projectService.createMemory('p1', 'a fact'),
    method: 'post',
    args: ['/projects/p1/memories', { content: 'a fact', source: 'manual' }],
    returnsBody: true,
  },
  {
    name: 'createMemory passes an explicit source',
    call: () => projectService.createMemory('p1', 'a fact', 'agent'),
    method: 'post',
    args: ['/projects/p1/memories', { content: 'a fact', source: 'agent' }],
    returnsBody: true,
  },
  {
    name: 'deleteMemory',
    call: () => projectService.deleteMemory('p1', 'm1'),
    method: 'delete',
    args: ['/projects/p1/memories/m1'],
    returnsBody: false,
  },
  // Bibliography
  {
    name: 'getProjectBibliography defaults to bibtex',
    call: () => projectService.getProjectBibliography('p1'),
    method: 'get',
    args: ['/projects/p1/bibliography?format=bibtex'],
    returnsBody: true,
  },
  {
    name: 'getProjectBibliography with a style',
    call: () => projectService.getProjectBibliography('p1', 'apa'),
    method: 'get',
    args: ['/projects/p1/bibliography?format=apa'],
    returnsBody: true,
  },
  // Drafts
  {
    name: 'generateDraft repeats themes and document ids and sends every option',
    call: () =>
      projectService.generateDraft('p1', ['a', 'b'], {
        documentIds: ['d1', 'd2'],
        style: 'technical',
        maxSections: 3,
        includeAbstract: false,
      }),
    method: 'post',
    args: [
      '/projects/p1/drafts?themes=a&themes=b&document_ids=d1&document_ids=d2&style=technical&max_sections=3&include_abstract=false',
    ],
    returnsBody: true,
  },
  {
    name: 'generateDraft with themes only',
    call: () => projectService.generateDraft('p1', ['x']),
    method: 'post',
    args: ['/projects/p1/drafts?themes=x'],
    returnsBody: true,
  },
  {
    name: 'listDrafts maps camelCase options to snake_case params',
    call: () => projectService.listDrafts('p1', { includeContent: false, limit: 10 }),
    method: 'get',
    args: ['/projects/p1/drafts?include_content=false&limit=10'],
    returnsBody: true,
  },
  {
    name: 'listDrafts without options',
    call: () => projectService.listDrafts('p1'),
    method: 'get',
    args: ['/projects/p1/drafts'],
    returnsBody: true,
  },
  {
    name: 'listDraftReviews',
    call: () => projectService.listDraftReviews('p1'),
    method: 'get',
    args: ['/projects/p1/drafts/reviews'],
    returnsBody: true,
  },
  {
    name: 'getCurrentDraft',
    call: () => projectService.getCurrentDraft('p1'),
    method: 'get',
    args: ['/projects/p1/drafts/current'],
    returnsBody: true,
  },
  {
    name: 'getDraft',
    call: () => projectService.getDraft('p1', 'd1'),
    method: 'get',
    args: ['/projects/p1/drafts/d1'],
    returnsBody: true,
  },
  {
    name: 'deleteDraft',
    call: () => projectService.deleteDraft('p1', 'd1'),
    method: 'delete',
    args: ['/projects/p1/drafts/d1'],
    returnsBody: false,
  },
  {
    name: 'getDraftCitations',
    call: () => projectService.getDraftCitations('p1', 'd1'),
    method: 'get',
    args: ['/projects/p1/drafts/d1/citations'],
    returnsBody: true,
  },
  {
    name: 'compareDrafts',
    call: () => projectService.compareDrafts('p1', 1, 2),
    method: 'get',
    args: ['/projects/p1/drafts/compare?version_a=1&version_b=2'],
    returnsBody: true,
  },
  {
    name: 'diffDrafts',
    call: () => projectService.diffDrafts('p1', 'a', 'b'),
    method: 'get',
    args: ['/projects/p1/drafts/diff?from_draft_id=a&to_draft_id=b'],
    returnsBody: true,
  },
  {
    name: 'getDraftRelease',
    call: () => projectService.getDraftRelease('p1', 'd1', 3),
    method: 'get',
    args: ['/projects/p1/drafts/d1/versions/3/release'],
    returnsBody: true,
  },
  {
    name: 'promoteDraft',
    call: () => projectService.promoteDraft('p1', 'd1', 3, data as never),
    method: 'post',
    args: ['/projects/p1/drafts/d1/versions/3/promote', data],
    returnsBody: true,
  },
  {
    name: 'getGenerationStatus without a task',
    call: () => projectService.getGenerationStatus('p1'),
    method: 'get',
    args: ['/projects/p1/drafts/status'],
    returnsBody: true,
  },
  {
    name: 'getGenerationStatus for a task',
    call: () => projectService.getGenerationStatus('p1', 't1'),
    method: 'get',
    args: ['/projects/p1/drafts/status?task_id=t1'],
    returnsBody: true,
  },
  {
    name: 'cancelGeneration without a task',
    call: () => projectService.cancelGeneration('p1'),
    method: 'post',
    args: ['/projects/p1/drafts/cancel'],
    returnsBody: false,
  },
  {
    name: 'cancelGeneration for a task',
    call: () => projectService.cancelGeneration('p1', 't1'),
    method: 'post',
    args: ['/projects/p1/drafts/cancel?task_id=t1'],
    returnsBody: false,
  },
  // Claims
  {
    name: 'listClaims encodes the draft id',
    call: () => projectService.listClaims('p1', 'd 1'),
    method: 'get',
    args: ['/projects/p1/claims?draft_id=d+1'],
    returnsBody: true,
  },
  {
    name: 'createClaim',
    call: () => projectService.createClaim('p1', data as never),
    method: 'post',
    args: ['/projects/p1/claims', data],
    returnsBody: true,
  },
  {
    name: 'linkClaimEvidence',
    call: () => projectService.linkClaimEvidence('p1', 'c1', data as never),
    method: 'post',
    args: ['/projects/p1/claims/c1/links', data],
    returnsBody: true,
  },
  {
    name: 'observeClaimLink sends the caller idempotency key',
    call: () => projectService.observeClaimLink('p1', 'c1', 'l1', 'key-1'),
    method: 'post',
    args: ['/projects/p1/claims/c1/links/l1/observations', { idempotency_key: 'key-1' }],
    returnsBody: true,
  },
  {
    name: 'assessClaim',
    call: () => projectService.assessClaim('p1', 'c1', data as never),
    method: 'post',
    args: ['/projects/p1/claims/c1/assessments', data],
    returnsBody: true,
  },
  {
    name: 'downloadClaimsExport lets the server name the file',
    call: () => projectService.downloadClaimsExport('p1', 'd1'),
    method: 'download',
    args: ['/projects/p1/claims/export?draft_id=d1'],
    returnsBody: false,
  },
  // Peer review
  {
    name: 'listPeerReviewRounds',
    call: () => projectService.listPeerReviewRounds('p1'),
    method: 'get',
    args: ['/projects/p1/peer-review/rounds'],
    returnsBody: true,
  },
  {
    name: 'getPeerReviewRound without a target draft',
    call: () => projectService.getPeerReviewRound('p1', 'r1'),
    method: 'get',
    args: ['/projects/p1/peer-review/rounds/r1'],
    returnsBody: true,
  },
  {
    name: 'getPeerReviewRound against a target draft',
    call: () => projectService.getPeerReviewRound('p1', 'r1', 'd1'),
    method: 'get',
    args: ['/projects/p1/peer-review/rounds/r1?target_draft_id=d1'],
    returnsBody: true,
  },
  {
    name: 'respondToPeerReviewComment',
    call: () => projectService.respondToPeerReviewComment('p1', 'c1', data as never),
    method: 'post',
    args: ['/projects/p1/peer-review/comments/c1/responses', data],
    returnsBody: true,
  },
  {
    name: 'decidePeerReviewComment',
    call: () => projectService.decidePeerReviewComment('p1', 'c1', data as never),
    method: 'post',
    args: ['/projects/p1/peer-review/comments/c1/decisions', data],
    returnsBody: true,
  },
  {
    name: 'downloadPeerReviewExport',
    call: () => projectService.downloadPeerReviewExport('p1', 'r1', 'json'),
    method: 'download',
    args: ['/projects/p1/peer-review/rounds/r1/export?format=json'],
    returnsBody: false,
  },
  // Manuscript releases
  {
    name: 'createCandidateRelease',
    call: () => projectService.createCandidateRelease('p1', data as never),
    method: 'post',
    args: ['/projects/p1/manuscript-releases', data],
    returnsBody: true,
  },
  {
    name: 'listManuscriptReleases',
    call: () => projectService.listManuscriptReleases('p1'),
    method: 'get',
    args: ['/projects/p1/manuscript-releases'],
    returnsBody: true,
  },
  {
    name: 'promoteManuscriptRelease',
    call: () => projectService.promoteManuscriptRelease('p1', 'r1', data as never),
    method: 'post',
    args: ['/projects/p1/manuscript-releases/r1/promote', data],
    returnsBody: true,
  },
  {
    name: 'downloadManuscriptPackage defaults to the identified package',
    call: () => projectService.downloadManuscriptPackage('p1', 'r1'),
    method: 'download',
    args: [
      '/projects/p1/manuscript-releases/r1/package?variant=identified',
      'manuscript-release-r1.zip',
    ],
    returnsBody: false,
  },
  {
    name: 'downloadManuscriptPackage names the anonymized variant',
    call: () => projectService.downloadManuscriptPackage('p1', 'r1', 'anonymized'),
    method: 'download',
    args: [
      '/projects/p1/manuscript-releases/r1/package?variant=anonymized',
      'manuscript-release-r1-anonymized.zip',
    ],
    returnsBody: false,
  },
  {
    name: 'verifyManuscriptRelease',
    call: () => projectService.verifyManuscriptRelease('p1', 'r1'),
    method: 'get',
    args: ['/projects/p1/manuscript-releases/r1/verify'],
    returnsBody: true,
  },
  {
    name: 'listVenueChecks',
    call: () => projectService.listVenueChecks('p1', 'r1'),
    method: 'get',
    args: ['/projects/p1/manuscript-releases/r1/venue-checks'],
    returnsBody: true,
  },
  // Statements and ORCID
  {
    name: 'listStatements',
    call: () => projectService.listStatements('p1'),
    method: 'get',
    args: ['/projects/p1/statements'],
    returnsBody: true,
  },
  {
    name: 'createStatementSet',
    call: () => projectService.createStatementSet('p1', data as never),
    method: 'post',
    args: ['/projects/p1/statements', data],
    returnsBody: true,
  },
  {
    name: 'approveStatementSet',
    call: () => projectService.approveStatementSet('p1', 's1', data as never),
    method: 'post',
    args: ['/projects/p1/statements/s1/approvals', data],
    returnsBody: true,
  },
  {
    name: 'orcidStart',
    call: () => projectService.orcidStart(),
    method: 'get',
    args: ['/auth/orcid/start'],
    returnsBody: true,
  },
  {
    name: 'completeOrcid forwards code and state and drops empty values',
    call: () => projectService.completeOrcid({ code: 'c1', state: 's1', error: null }),
    method: 'get',
    args: ['/auth/orcid/callback?code=c1&state=s1'],
    returnsBody: true,
  },
  {
    name: 'completeOrcid forwards a provider error',
    call: () => projectService.completeOrcid({ code: '', error: 'access_denied' }),
    method: 'get',
    args: ['/auth/orcid/callback?error=access_denied'],
    returnsBody: true,
  },
  // Deposits
  {
    name: 'listDeposits',
    call: () => projectService.listDeposits('p1'),
    method: 'get',
    args: ['/projects/p1/deposits'],
    returnsBody: true,
  },
  {
    name: 'approveDeposit',
    call: () => projectService.approveDeposit('p1', data as never),
    method: 'post',
    args: ['/projects/p1/deposits/approvals', data],
    returnsBody: true,
  },
  {
    name: 'revokeDepositApproval',
    call: () => projectService.revokeDepositApproval('p1', 'a1', data as never),
    method: 'post',
    args: ['/projects/p1/deposits/approvals/a1/revoke', data],
    returnsBody: true,
  },
  {
    name: 'requestDeposit',
    call: () => projectService.requestDeposit('p1', data as never),
    method: 'post',
    args: ['/projects/p1/deposits', data],
    returnsBody: true,
  },
  {
    name: 'requeueDeposit sends an empty body',
    call: () => projectService.requeueDeposit('p1', 'o1'),
    method: 'post',
    args: ['/projects/p1/deposits/o1/requeue', {}],
    returnsBody: true,
  },
];

describe('projectService request contract (GOO-742)', () => {
  beforeEach(() => {
    for (const fn of Object.values(mocked)) fn.mockReset();
  });

  it.each(cases)('$name', async ({ call, method, args, returnsBody }) => {
    const body = { from: 'backend' };
    mocked[method].mockResolvedValue(body);

    const result = await call();

    expect(mocked[method]).toHaveBeenCalledTimes(1);
    expect(mocked[method]).toHaveBeenCalledWith(...args);
    for (const other of METHODS.filter((m) => m !== method)) {
      expect(mocked[other]).not.toHaveBeenCalled();
    }
    if (returnsBody) expect(result).toBe(body);
    else expect(result).toBeUndefined();
  });

  it.each(cases)('$name rejects with the request error', async ({ call, method }) => {
    const failure = new Error('outage');
    mocked[method].mockRejectedValue(failure);

    await expect(call()).rejects.toBe(failure);
  });
});

describe('projectService.runVenueCheck (GOO-742)', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('posts a fresh idempotency key for every run', async () => {
    for (const fn of Object.values(mocked)) fn.mockReset();
    const body = { status: 'queued' };
    mocked.post.mockResolvedValue(body);
    vi.spyOn(crypto, 'randomUUID')
      .mockReturnValueOnce('00000000-0000-4000-8000-000000000001')
      .mockReturnValueOnce('00000000-0000-4000-8000-000000000002');

    await expect(projectService.runVenueCheck('p1', 'r1')).resolves.toBe(body);
    await projectService.runVenueCheck('p1', 'r1');

    expect(mocked.post).toHaveBeenNthCalledWith(
      1,
      '/projects/p1/manuscript-releases/r1/venue-checks',
      { idempotency_key: '00000000-0000-4000-8000-000000000001' }
    );
    expect(mocked.post).toHaveBeenNthCalledWith(
      2,
      '/projects/p1/manuscript-releases/r1/venue-checks',
      { idempotency_key: '00000000-0000-4000-8000-000000000002' }
    );
  });
});
