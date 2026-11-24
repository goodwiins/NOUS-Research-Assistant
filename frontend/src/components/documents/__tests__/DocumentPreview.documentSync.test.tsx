/**
 * DocumentPreview keeps a local copy of the document for the optimistic
 * retry update. That copy must follow the `document` prop, or the embedded
 * ProcessingStatus keeps showing the previous document.
 */

import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { DocumentPreview } from '../DocumentPreview';
import type { Document } from '@/types';

vi.mock('@/services/api-client', () => ({
  api: { get: vi.fn() },
}));

function makeDocument(overrides: Partial<Document> = {}): Document {
  return {
    id: 'doc-1',
    user_id: 'user-1',
    organization_id: 'org-1',
    title: 'Test Document',
    filename: 'test.pdf',
    file_type: 'pdf',
    processing_status: 'failed',
    upload_timestamp: '2026-01-01T00:00:00Z',
    metadata: {},
    ...overrides,
  } as Document;
}

describe('DocumentPreview', () => {
  it('shows the new document in the processing status when the prop changes', () => {
    const { rerender } = render(
      <DocumentPreview
        document={makeDocument({ id: 'doc-a', processing_error: 'Error A' })}
        isOpen
        onClose={() => {}}
      />
    );
    expect(screen.getByText('• Error A')).toBeInTheDocument();

    rerender(
      <DocumentPreview
        document={makeDocument({ id: 'doc-b', processing_error: 'Error B' })}
        isOpen
        onClose={() => {}}
      />
    );

    expect(screen.getByText('• Error B')).toBeInTheDocument();
    expect(screen.queryByText('• Error A')).not.toBeInTheDocument();
  });
});
