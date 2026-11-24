import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Mock } from 'vitest';
import {
  act,
  fireEvent,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { render } from '@/test/test-utils';
import DocumentDetailPage from '../../../../app/(dashboard)/documents/[id]/page';
import { api } from '@/services/api-client';
import { getProcessingWarnings } from '@/utils/documentProcessingWarnings';
import { APIErrorClass } from '@/types';

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

  it('offers no Reprocess on a document processed with warnings', async () => {
    // A forced reprocess re-runs ingestion, which neither retries the failed
    // steps nor clears the warnings (GOO-744 review).
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
    expect(
      screen.queryByRole('button', { name: /reprocess|retry/i })
    ).toBeNull();
  });

  it.each(['queued', 'processing', 'failed'])(
    'a %s document never shows the warnings state, even with the flag left over',
    async (status) => {
      get.mockResolvedValue(
        detail(status, {
          processing_partial: true,
          processing_warnings: WARNINGS,
        })
      );

      render(<DocumentDetailPage />);

      expect(
        await screen.findByText('live processing status')
      ).toBeInTheDocument();
      expect(screen.queryByText('Processed with warnings')).toBeNull();
      expect(screen.queryByText(WARNINGS[0])).toBeNull();
    }
  );

  it('retrying a failed document posts a plain reprocess, then reloads it', async () => {
    get.mockResolvedValue(detail('failed', null));
    post.mockResolvedValue({});

    render(<DocumentDetailPage />);

    fireEvent.click(
      await screen.findByRole('button', { name: /Retry processing/ })
    );

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith('/documents/doc-1/reprocess')
    );
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
  });

  it('a second click while the retry is in flight sends nothing more', async () => {
    get.mockResolvedValue(detail('failed', null));
    let finish!: () => void;
    post.mockReturnValueOnce(
      new Promise<void>((resolve) => {
        finish = resolve;
      })
    );

    render(<DocumentDetailPage />);

    const retry = await screen.findByRole('button', {
      name: /Retry processing/,
    });
    // Two clicks in one tick: both land before React re-renders the button
    // as disabled, so only the in-flight guard can stop the second.
    act(() => {
      retry.click();
      retry.click();
    });
    await waitFor(() => expect(retry).toBeDisabled());
    // Later clicks hit the disabled button.
    fireEvent.click(retry);
    expect(post).toHaveBeenCalledTimes(1);

    finish();
    // The reload renders the page afresh; its Retry is usable again.
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    expect(
      await screen.findByRole('button', { name: /Retry processing/ })
    ).not.toBeDisabled();
    expect(post).toHaveBeenCalledTimes(1);
  });

  it.each([
    [409, 'Processing is already running for this document.'],
    [403, 'Only the uploader or an admin can reprocess this document.'],
    [500, "Couldn't start processing. Try again in a moment."],
  ])('a refused retry (%i) is shown to the user', async (status, message) => {
    get.mockResolvedValue(detail('failed', null));
    post.mockRejectedValueOnce(
      new APIErrorClass({
        message: 'raw backend detail',
        status_code: status,
        type: 'http_error',
      })
    );

    render(<DocumentDetailPage />);

    fireEvent.click(
      await screen.findByRole('button', { name: /Retry processing/ })
    );

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(message);
    expect(alert).not.toHaveTextContent('raw backend detail');
    // The button is usable again for another try.
    expect(
      screen.getByRole('button', { name: /Retry processing/ })
    ).not.toBeDisabled();
  });

  it('a network failure without a status gets the generic message', async () => {
    get.mockResolvedValue(detail('failed', null));
    post.mockRejectedValueOnce(new TypeError('Failed to fetch'));

    render(<DocumentDetailPage />);

    fireEvent.click(
      await screen.findByRole('button', { name: /Retry processing/ })
    );

    expect(await screen.findByRole('alert')).toHaveTextContent(
      "Couldn't start processing. Try again in a moment."
    );
  });

  it('a new retry clears the previous error', async () => {
    get.mockResolvedValue(detail('failed', null));
    post
      .mockRejectedValueOnce(
        new APIErrorClass({
          message: 'x',
          status_code: 409,
          type: 'http_error',
        })
      )
      .mockResolvedValueOnce({});

    render(<DocumentDetailPage />);

    const retry = await screen.findByRole('button', {
      name: /Retry processing/,
    });
    fireEvent.click(retry);
    await screen.findByRole('alert');
    fireEvent.click(retry);

    // Wait for the reload to render the page again, then check.
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    await screen.findByRole('button', { name: /Retry processing/ });
    expect(screen.queryByRole('alert')).toBeNull();
  });
});
