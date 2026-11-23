import { createRef } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@/test/test-utils';
import { DocumentTablesTab } from '../DocumentTablesTab';

describe('DocumentTablesTab (FR-8)', () => {
  it('does not offer crop extract until it can target a rendered PDF page', () => {
    render(
      <DocumentTablesTab
        isPdf
        isIndexed
        tables={[]}
        tablesLoading={false}
        regionResult={null}
        cropActive={false}
        tablesContainerRef={createRef<HTMLDivElement>()}
        documentId="doc-1"
        onFetchTables={vi.fn()}
        onSetTables={vi.fn()}
        onSetRegionResult={vi.fn()}
        onSetCropActive={vi.fn()}
      />
    );

    expect(
      screen.queryByRole('button', { name: /crop extract/i })
    ).not.toBeInTheDocument();
  });
});
