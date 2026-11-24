vi.mock('@/hooks/useBackendCapabilities', () => ({
  useBackendCapabilities: () => ({ draftClaims: false, draftRelease: false }),
}));
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@/test/test-utils';
import { DraftViewer } from '../DraftViewer';
import type { Draft } from '@/services/projectService';

const draft: Draft = {
  id: 'draft-1',
  project_id: 'project-1',
  version: 1,
  title: 'Review',
  content: 'Mortality fell by forty percent across the cohort [Doc 1].',
  themes: [],
  word_count: 9,
  citation_count: 1,
  is_current: true,
  created_at: '2026-09-30T00:00:00Z',
  content_hash: 'a'.repeat(64),
};

function clickCollapsedCursor(): void {
  vi.spyOn(window, 'getSelection').mockReturnValue({
    isCollapsed: true,
    toString: () => '',
  } as unknown as Selection);
  fireEvent.mouseUp(screen.getByText(/Mortality fell/));
}

describe('DraftViewer edit tools (FR-7)', () => {
  afterEach(() => vi.restoreAllMocks());

  it('hides the writer tools when no caller can apply an accepted result', () => {
    render(<DraftViewer draft={draft} />);
    clickCollapsedCursor();
    expect(
      screen.queryByRole('button', { name: 'Close toolbar' })
    ).not.toBeInTheDocument();
  });

  it('offers the writer tools when the caller passes onTextRewrite', () => {
    render(<DraftViewer draft={draft} onTextRewrite={vi.fn()} />);
    clickCollapsedCursor();
    expect(
      screen.getByRole('button', { name: 'Close toolbar' })
    ).toBeInTheDocument();
  });
});
