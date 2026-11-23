/**
 * Entities page, GOO-794: the entity detail dialog stays open while the
 * relationship dialog is used (GOO-767), so its relationship list must show
 * a saved relationship without reopening, and must not change after a failed
 * save. The real EntityDetail and both real dialogs render here; only the
 * service, the form and unrelated panels are stubbed.
 *
 * Also: the detail view has exactly one "Add relationship" control, the
 * page's working one. The detail card's own button had no handler.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Mock } from 'vitest';
import type { ReactNode } from 'react';
import { act, render, screen, waitFor, within } from '@/test/test-utils';
import toast from 'react-hot-toast';
import { entityService } from '@/services/entityService';
import { APIErrorClass } from '@/types/api';
import type { Entity, GraphEdge } from '@/types/entity';

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  useSearchParams: () => new URLSearchParams(''),
}));

const { last } = vi.hoisted(() => ({
  last: {} as Record<string, Record<string, unknown>>,
}));

vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => ({
    handleAuthError: vi.fn(),
    isAuthenticated: true,
    isLoading: false,
  }),
}));

vi.mock('@/hooks/useEntityPermissions', () => ({
  useEntityPermissions: () => ({
    canCreate: true,
    canEdit: true,
    canDelete: true,
  }),
}));

vi.mock('react-hot-toast', () => ({
  default: { error: vi.fn(), success: vi.fn() },
}));

vi.mock('@/services/entityService', () => ({
  entityService: {
    getEntityTypes: vi.fn(),
    getRelationshipTypes: vi.fn(),
    getAnalytics: vi.fn(),
    getEntities: vi.fn(),
    getAllRelationships: vi.fn(),
    getEntityRelationships: vi.fn(),
    deleteRelationship: vi.fn(),
    createRelationship: vi.fn(),
  },
}));

vi.mock('@/components/entities/RelationshipForm', () => ({
  RelationshipForm: ({
    sourceEntityId,
    onSubmit,
  }: {
    sourceEntityId?: string;
    onSubmit: (data: Record<string, unknown>) => Promise<void>;
  }) => (
    <button
      type="button"
      onClick={() =>
        void onSubmit({
          source_entity_id: sourceEntityId,
          target_entity_id: 'TP53',
          relationship_type: 'ENCODES',
        })
      }
    >
      Save relationship
    </button>
  ),
}));
vi.mock('@/components/entities/EntityList', () => ({
  EntityList: (props: Record<string, unknown>): null => {
    last.list = props;
    return null;
  },
}));
vi.mock('@/components/entities/EntityErrorBoundary', () => ({
  EntityErrorBoundary: ({ children }: { children: ReactNode }): ReactNode =>
    children,
}));
vi.mock('@/components/entities/EntityFilters', () => ({ EntityFilters: (): null => null }));
vi.mock('@/components/entities/EntityForm', () => ({ EntityForm: (): null => null }));
vi.mock('@/components/entities/EntityGraphPanel', () => ({ EntityGraphPanel: (): null => null }));
vi.mock('@/components/entities/Pagination', () => ({ Pagination: (): null => null }));
vi.mock('@/components/entities/PathFinder', () => ({ PathFinder: (): null => null }));
vi.mock('@/components/entities/NeighborhoodExplorer', () => ({ NeighborhoodExplorer: (): null => null }));
vi.mock('@/components/entities/GraphAnalyticsDashboard', () => ({ GraphAnalyticsDashboard: (): null => null }));
vi.mock('@/components/entities/EntitySearch', () => ({ EntitySearch: (): null => null }));
vi.mock('@/components/entities/BulkOperations', () => ({ BulkOperations: (): null => null }));
vi.mock('@/components/entities/KeyboardShortcutsDialog', () => ({ KeyboardShortcutsDialog: (): null => null }));
vi.mock('@/components/entities/DocumentEntityExtractor', () => ({ DocumentEntityExtractor: (): null => null }));
vi.mock('@/components/entities/EntityMergeTool', () => ({ EntityMergeTool: (): null => null }));
vi.mock('@/components/entities/GraphHealthMonitor', () => ({ GraphHealthMonitor: (): null => null }));

import EntityManagementPage from '../../../../app/(dashboard)/entities/page';

const service = entityService as unknown as Record<string, Mock>;
const emptyPage = { entities: [], total: 0, limit: 20, offset: 0, has_more: false };
const gene = { id: 'BRCA1', name: 'BRCA1', type: 'GENE' } as Entity;
const edge = (target: string): GraphEdge =>
  ({ id: `BRCA1-${target}`, source: 'BRCA1', target, type: 'ENCODES' }) as GraphEdge;

const detailDialog = (): HTMLElement =>
  screen.getByRole('dialog', { name: 'Entity details' });

/** Open the detail dialog for `gene` and show its relationships tab. */
const viewRelationships = async (
  user: ReturnType<typeof render>['user']
): Promise<void> => {
  await waitFor(() => expect(last.list).toBeDefined());
  act(() => (last.list.onView as (entity: Entity) => void)(gene));
  await screen.findByRole('dialog', { name: 'Entity details' });
  await user.click(within(detailDialog()).getByRole('tab', { name: /relationships/i }));
};

const saveFromDetail = async (
  user: ReturnType<typeof render>['user']
): Promise<void> => {
  const add = within(detailDialog()).getByRole('button', { name: /add relationship/i });
  await waitFor(() => expect(add).toBeEnabled());
  await user.click(add);
  await user.click(await screen.findByRole('button', { name: 'Save relationship' }));
};

beforeEach(() => {
  for (const key of Object.keys(last)) delete last[key];
  for (const fn of Object.values(service)) fn.mockReset();
  (toast.error as unknown as Mock).mockReset();
  (toast.success as unknown as Mock).mockReset();
  vi.spyOn(console, 'error').mockImplementation(() => {});
  vi.spyOn(console, 'warn').mockImplementation(() => {});
  service.getEntityTypes.mockResolvedValue(['GENE']);
  service.getRelationshipTypes.mockResolvedValue(['ENCODES']);
  service.getAnalytics.mockResolvedValue({ entity_type_counts: {} });
  service.getEntities.mockResolvedValue(emptyPage);
  service.getAllRelationships.mockResolvedValue([]);
});

describe('the open entity detail dialog after a relationship save (GOO-794)', () => {
  it('shows the saved relationship without reopening', async () => {
    let stored: GraphEdge[] = [edge('RAD51')];
    service.getEntityRelationships.mockImplementation(async () => stored);
    service.createRelationship.mockImplementation(async () => {
      stored = [...stored, edge('TP53')];
      return stored.at(-1);
    });
    const { user } = render(<EntityManagementPage />);
    await viewRelationships(user);
    expect(await within(detailDialog()).findByText('RAD51')).toBeInTheDocument();
    expect(within(detailDialog()).queryByText('TP53')).toBeNull();

    await saveFromDetail(user);

    expect(await within(detailDialog()).findByText('TP53')).toBeInTheDocument();
    expect(within(detailDialog()).getByText('RAD51')).toBeInTheDocument();
  });

  it('leaves the list unchanged, and does not refetch it, when the save fails', async () => {
    service.getEntityRelationships.mockResolvedValue([edge('RAD51')]);
    service.createRelationship.mockRejectedValue(
      new APIErrorClass({ message: 'HTTP 500', status_code: 500, type: 'http_error' })
    );
    const { user } = render(<EntityManagementPage />);
    await viewRelationships(user);
    await within(detailDialog()).findByText('RAD51');
    const fetchesBefore = service.getEntityRelationships.mock.calls.length;

    await saveFromDetail(user);

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('Failed to create relationship')
    );
    // The form stays open on a failure; close it to look at the list.
    await user.keyboard('{Escape}');
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Save relationship' })).toBeNull()
    );
    expect(service.getEntityRelationships).toHaveBeenCalledTimes(fetchesBefore);
    expect(within(detailDialog()).getByText('RAD51')).toBeInTheDocument();
    expect(within(detailDialog()).queryByText('TP53')).toBeNull();
  });

  it('has one "Add relationship" control, and it opens the relationship dialog', async () => {
    service.getEntityRelationships.mockResolvedValue([]);
    const { user } = render(<EntityManagementPage />);
    await viewRelationships(user);

    const adds = within(detailDialog()).getAllByRole('button', {
      name: /add relationship/i,
    });
    expect(adds).toHaveLength(1);
    await waitFor(() => expect(adds[0]).toBeEnabled());
    await user.click(adds[0]);
    expect(
      await screen.findByRole('button', { name: 'Save relationship' })
    ).toBeInTheDocument();
  });
});
