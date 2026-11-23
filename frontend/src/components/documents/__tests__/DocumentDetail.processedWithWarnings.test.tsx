import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Mock } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { render } from '@/test/test-utils';
import DocumentDetailPage from '../../../../app/(dashboard)/documents/[id]/page';
import { api } from '@/services/api-client';
import { getProcessingWarnings } from '@/utils/documentProcessingWarnings';

vi.mock('next/navigation', () => ({
  useParams: () => ({ id: 'doc-1' }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn() }),
}));
vi.mock('@/stores/authStore', () => ({
  useAuthStore: () => ({ isAuthenticated: true }),
}));
vi.mock('@/services/api-client', () => ({
  api: { get: vi.fn(), post: vi.fn(), delete: vi.fn() },
}));
vi.mock('@/services/citationService', () => ({
  citationService: {
    extractCitations: vi.fn(async () => ({})),
    getCitationsForDocument: vi.fn(async () => []),
  },
}));
vi.mock('@/services/scispaceService', () => ({
  getIntegrityScore: vi.fn(async () => null),
  extractTables: vi.fn(async () => ({ tables: [] })),
}));
vi.mock('@/components/documents/ProcessingStatus', () => ({
  ProcessingStatus: () => <div>live processing status</div>,
}));
vi.mock('@/components/documents/IntegrityBadge', () => ({
  IntegrityBadge: () => null,
}));
vi.mock('@/components/documents/IntegrityDetail', () => ({
  IntegrityDetail: () => null,
}));
vi.mock('@/components/documents/DocumentOverviewTab', () => ({
  DocumentOverviewTab: () => null,
}));
vi.mock('@/components/documents/DocumentMetadataTab', () => ({
  DocumentMetadataTab: () => null,
}));
vi.mock('@/components/documents/DocumentPreviewTab', () => ({
  DocumentPreviewTab: () => null,
}));
vi.mock('@/components/documents/DocumentTablesTab', () => ({
  DocumentTablesTab: () => null,
}));

const get = api.get as Mock;
const post = api.post as Mock;

const WARNINGS = [
  'Image OCR failed; images were not indexed.',
  'Table extraction failed; tables were not indexed.',
];

function detail(
  status: string,
  metadata: Record<string, unknown> | null
): Record<string, unknown> {
  return {
    id: 'doc-1',
    filename: 'paper.pdf',
    title: 'Paper',
    document_type: 'pdf',
    processing_status: status,
    processing_completed_at: '2026-11-23T08:00:00Z',
    file_size_bytes: 1024,
    created_at: '2026-11-23T07:00:00Z',
    metadata,
  };
}

async function statusSection(): Promise<HTMLElement> {
  return screen.findByRole('region', { name: 'Processing status' });
}

describe('getProcessingWarnings (GOO-744)', () => {
  it('is clean without metadata or without the partial flag', () => {
    expect(getProcessingWarnings(null)).toEqual({
      partial: false,
      warnings: [],
    });
    expect(getProcessingWarnings(undefined)).toEqual({
      partial: false,
      warnings: [],
    });
    // A clean retry writes partial false and an empty list.
    expect(
      getProcessingWarnings({
        processing_partial: false,
        processing_warnings: [],
      })
    ).toEqual({ partial: false, warnings: [] });
    // Warnings on a failed run are not "processed with warnings".
    expect(
      getProcessingWarnings({
        processing_warnings: ['x'],
        processing_partial: false,
      })
    ).toEqual({ partial: false, warnings: [] });
  });

  it('only a literal true marks a document partial', () => {
    expect(getProcessingWarnings({ processing_partial: 'true' }).partial).toBe(
      false
    );
    expect(getProcessingWarnings({ processing_partial: 1 }).partial).toBe(
      false
    );
  });

  it('keeps the string warnings and drops anything else', () => {
    expect(
      getProcessingWarnings({
        processing_partial: true,
        processing_warnings: ['a', 3, null, '  ', 'b'],
      })
    ).toEqual({ partial: true, warnings: ['a', 'b'] });
  });

  it('stays partial when the warnings are missing or malformed', () => {
    expect(getProcessingWarnings({ processing_partial: true })).toEqual({
      partial: true,
      warnings: [],
    });
    expect(
      getProcessingWarnings({
        processing_partial: true,
        processing_warnings: 'x',
      })
    ).toEqual({ partial: true, warnings: [] });
  });
});

describe('document detail: processed with warnings (GOO-744)', () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  it('shows the warnings state and each message instead of "Indexed"', async () => {
    get.mockResolvedValue(
      detail('indexed', {
        processing_partial: true,
        processing_warnings: WARNINGS,
      })
    );

    render(<DocumentDetailPage />);

    const section = await statusSection();
    expect(
      within(section).getByText('Processed with warnings')
    ).toBeInTheDocument();
    const list = within(section).getByRole('list', {
      name: 'Processing warnings',
    });
    expect(
      within(list)
        .getAllByRole('listitem')
        .map((li) => li.textContent)
    ).toEqual(WARNINGS);
    expect(screen.queryByText('Indexed')).not.toBeInTheDocument();
    expect(screen.queryByText('ready for retrieval')).not.toBeInTheDocument();
  });

  it('a clean indexed document still shows "Indexed" and no warnings', async () => {
    get.mockResolvedValue(
      detail('indexed', { processing_partial: false, processing_warnings: [] })
    );

    render(<DocumentDetailPage />);

    expect(await screen.findByText('Indexed')).toBeInTheDocument();
    expect(
      screen.queryByText('Processed with warnings')
    ).not.toBeInTheDocument();
  });

  it('says the details are missing when partial carries no readable warning', async () => {
    get.mockResolvedValue(detail('indexed', { processing_partial: true }));

    render(<DocumentDetailPage />);

    const section = await statusSection();
    expect(
      within(section).getByText('Processed with warnings')
    ).toBeInTheDocument();
    expect(
      within(section).getByText("What didn't finish wasn't recorded.")
    ).toBeInTheDocument();
  });

  it('Reprocess forces a re-run of the indexed document, then reloads it', async () => {
    get.mockResolvedValue(
      detail('indexed', {
        processing_partial: true,
        processing_warnings: WARNINGS,
      })
    );
    post.mockResolvedValue({});

    render(<DocumentDetailPage />);

    const section = await statusSection();
    fireEvent.click(within(section).getByRole('button', { name: 'Reprocess' }));

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        '/documents/doc-1/reprocess?force_reprocess=true'
      )
    );
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
  });

  it('retrying a failed document does not force', async () => {
    get.mockResolvedValue(detail('failed', null));
    post.mockResolvedValue({});

    render(<DocumentDetailPage />);

    fireEvent.click(
      await screen.findByRole('button', { name: /Retry processing/ })
    );

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith('/documents/doc-1/reprocess')
    );
  });
});
