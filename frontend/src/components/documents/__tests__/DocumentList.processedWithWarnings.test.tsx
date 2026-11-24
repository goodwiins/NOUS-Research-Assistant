/**
 * GOO-765: the document list shows "Processed with warnings" for a document
 * the backend marks `processing_partial`, and only for that document.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook, screen, within } from '@testing-library/react';
import { render } from '@/test/test-utils';
import {
  DocumentList,
  type Document,
} from '../../../../app/(dashboard)/documents/components/DocumentList';
import { useDocuments } from '@/hooks/useDocuments';

const mockGet = vi.fn();
const mockHandleAuthError = vi.fn();

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn() }),
}));
vi.mock('@/services/api-client', () => ({
  api: {
    get: (...args: unknown[]) => mockGet(...args),
    post: vi.fn(),
    delete: vi.fn(),
  },
}));
vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => ({
    isAuthenticated: true,
    isLoading: false,
    handleAuthError: mockHandleAuthError,
  }),
}));

afterEach(() => {
  mockGet.mockReset();
});

const BADGE = 'Processed with warnings';

function doc(
  id: string,
  processing_status: string,
  processing_partial?: boolean
): Document {
  return {
    id,
    title: `Doc ${id}`,
    filename: `${id}.pdf`,
    file_type: 'pdf',
    file_size: 1024,
    upload_timestamp: '2026-11-01T12:00:00Z',
    processing_status,
    ...(processing_partial === undefined ? {} : { processing_partial }),
  };
}

function renderList(documents: Document[]): ReturnType<typeof render> {
  return render(
    <DocumentList
      documents={documents}
      loading={false}
      selectedDocuments={new Set()}
      onSelect={vi.fn()}
      onSelectAll={vi.fn()}
      onDelete={vi.fn()}
      onRetry={vi.fn()}
    />
  );
}

/** The row that holds a document's title. */
function row(title: string): HTMLElement {
  const heading = screen.getByRole('heading', { name: title });
  const el = heading.closest('div.group');
  if (!(el instanceof HTMLElement)) throw new Error(`no row for ${title}`);
  return el;
}

describe('DocumentList "Processed with warnings" badge', () => {
  it('shows the badge in place of Indexed when processing_partial is true', () => {
    renderList([doc('a', 'indexed', true)]);

    const r = row('Doc a');
    const badge = within(r).getByText(BADGE);
    expect(within(r).queryByText('Indexed')).not.toBeInTheDocument();
    // Accessible label: the visible text plus the screen-reader detail.
    expect(badge).toHaveTextContent(
      "Processed with warnings: searchable, but some processing steps didn't finish"
    );
    expect(badge).toHaveAttribute(
      'title',
      "searchable, but some processing steps didn't finish"
    );
    // Theme-safe accent token, not text-helios (1.84:1 on white).
    expect(badge.className).toContain('text-(--nous-fg-accent-safe)');
    expect(badge.className).not.toContain('helios');
    // Indexed, so no retry.
    expect(
      within(r).queryByRole('button', { name: 'Retry processing' })
    ).not.toBeInTheDocument();
  });

  it.each([
    ['false', false],
    ['absent', undefined],
  ])('shows Indexed and no badge when processing_partial is %s', (_, flag) => {
    renderList([doc('a', 'indexed', flag)]);

    const r = row('Doc a');
    expect(within(r).getByText('Indexed')).toBeInTheDocument();
    expect(within(r).queryByText(BADGE)).not.toBeInTheDocument();
  });

  it.each([
    ['failed', 'Failed'],
    ['processing', 'Processing'],
    ['queued', 'Queued'],
  ])(
    'never shows the badge on a %s document, even with the flag set',
    (status, label) => {
      renderList([doc('a', status, true)]);

      const r = row('Doc a');
      expect(within(r).getByText(label)).toBeInTheDocument();
      expect(within(r).queryByText(BADGE)).not.toBeInTheDocument();
    }
  );

  it('keeps Retry on a failed document with the flag set', () => {
    renderList([doc('a', 'failed', true)]);

    expect(
      within(row('Doc a')).getByRole('button', { name: 'Retry processing' })
    ).toBeInTheDocument();
  });

  it('marks only the partial row in a mixed list', () => {
    renderList([
      doc('a', 'indexed', false),
      doc('b', 'indexed', true),
      doc('c', 'indexed'),
    ]);

    expect(screen.getAllByText(BADGE)).toHaveLength(1);
    expect(within(row('Doc b')).getByText(BADGE)).toBeInTheDocument();
    expect(within(row('Doc a')).getByText('Indexed')).toBeInTheDocument();
    expect(within(row('Doc c')).getByText('Indexed')).toBeInTheDocument();
  });
});

describe('useDocuments carries processing_partial from the list API', () => {
  it.each([
    [true, true],
    [false, false],
    [undefined, false],
    ['true', false],
    [1, false],
  ])('backend %s -> %s', async (backend, expected) => {
    mockGet.mockResolvedValue({
      documents: [
        {
          id: 'doc-1',
          title: 'Doc',
          filename: 'doc.pdf',
          document_type: 'pdf',
          file_size_bytes: 10,
          processing_status: 'indexed',
          created_at: '2026-11-01T12:00:00Z',
          ...(backend === undefined ? {} : { processing_partial: backend }),
        },
      ],
      pagination: {
        page: 1,
        page_size: 20,
        total: 1,
        total_pages: 1,
        has_next: false,
        has_prev: false,
      },
    });

    const { result } = renderHook(() => useDocuments({ autoFetch: false }));
    await act(async () => {
      await result.current.fetchDocuments();
    });

    expect(result.current.documents).toHaveLength(1);
    expect(result.current.documents[0].processing_partial).toBe(expected);
  });
});
