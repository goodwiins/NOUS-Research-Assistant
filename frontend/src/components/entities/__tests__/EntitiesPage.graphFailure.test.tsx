/**
 * GOO-749: on the entities page, a failed relationships load puts the graph
 * tab into its error state (never an empty graph), and Try again reloads it.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Mock } from 'vitest';
import { act, render, waitFor } from '@/test/test-utils';
import { entityService } from '@/services/entityService';
import { APIErrorClass } from '@/types/api';

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  useSearchParams: () => new URLSearchParams('tab=graph'),
}));

vi.mock('@/hooks/useEntityPermissions', () => ({
  useEntityPermissions: () => ({ canCreate: true, canEdit: true, canDelete: true }),
}));

// Stable like the real useCallback, or the fetch callbacks change every render.
const { handleAuthError } = vi.hoisted(() => ({ handleAuthError: vi.fn() }));
vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => ({ handleAuthError, isAuthenticated: true, isLoading: false }),
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
  },
}));

// The graph wiring is under test, not the other panels: stub them.
vi.mock('@/components/entities/EntityErrorBoundary', () => ({
  EntityErrorBoundary: ({ children }: { children: React.ReactNode }): React.ReactNode =>
    children,
}));
vi.mock('@/components/entities/EntityList', () => ({ EntityList: (): null => null }));
vi.mock('@/components/entities/EntityDetail', () => ({ EntityDetail: (): null => null }));
vi.mock('@/components/entities/EntityFilters', () => ({ EntityFilters: (): null => null }));
vi.mock('@/components/entities/Pagination', () => ({ Pagination: (): null => null }));
vi.mock('@/components/entities/PathFinder', () => ({ PathFinder: (): null => null }));
vi.mock('@/components/entities/NeighborhoodExplorer', () => ({
  NeighborhoodExplorer: (): null => null,
}));
vi.mock('@/components/entities/GraphAnalyticsDashboard', () => ({
  GraphAnalyticsDashboard: (): null => null,
}));
vi.mock('@/components/entities/EntitySearch', () => ({ EntitySearch: (): null => null }));
vi.mock('@/components/entities/BulkOperations', () => ({ BulkOperations: (): null => null }));

type PanelProps = {
  loading: boolean;
  failed: boolean;
  onRetry: () => void;
  relationships: unknown[];
};
const panelProps: PanelProps[] = [];
vi.mock('@/components/entities/EntityGraphPanel', () => ({
  EntityGraphPanel: (props: PanelProps): null => {
    panelProps.push(props);
    return null;
  },
}));

import EntityManagementPage from '../../../../app/(dashboard)/entities/page';

const service = entityService as unknown as Record<keyof typeof entityService, Mock>;
const lastPanel = (): PanelProps => panelProps[panelProps.length - 1];
const emptyPage = { entities: [], total: 0, limit: 200, offset: 0, has_more: false };

describe('entities page graph tab (GOO-749)', () => {
  beforeEach(() => {
    panelProps.length = 0;
    for (const fn of Object.values(service)) fn.mockReset();
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    vi.spyOn(console, 'error').mockImplementation(() => {});
    service.getEntityTypes.mockResolvedValue(['PERSON']);
    service.getRelationshipTypes.mockResolvedValue(['KNOWS']);
    service.getAnalytics.mockResolvedValue({ entity_type_counts: {} });
    service.getEntities.mockResolvedValue(emptyPage);
  });

  it('shows the error state when relationships fail, then recovers on retry', async () => {
    service.getAllRelationships
      .mockRejectedValueOnce(
        new APIErrorClass({ message: 'HTTP 403', status_code: 403, type: 'http_error' })
      )
      .mockResolvedValueOnce([{ id: 'r1', source: 'a', target: 'b', type: 'KNOWS' }]);

    render(<EntityManagementPage />);

    await waitFor(() => expect(lastPanel()).toMatchObject({ loading: false, failed: true }));
    expect(lastPanel().relationships).toEqual([]);
    expect(service.getAllRelationships).toHaveBeenCalledTimes(1);

    await act(async () => {
      lastPanel().onRetry();
    });

    await waitFor(() => expect(lastPanel()).toMatchObject({ loading: false, failed: false }));
    expect(lastPanel().relationships).toHaveLength(1);
    expect(service.getAllRelationships).toHaveBeenCalledTimes(2);
  });

  it('clears relationships that loaded earlier when a later load fails (GOO-760)', async () => {
    service.getAllRelationships
      .mockResolvedValueOnce([{ id: 'r1', source: 'a', target: 'b', type: 'KNOWS' }])
      .mockRejectedValueOnce(
        new APIErrorClass({ message: 'HTTP 500', status_code: 500, type: 'http_error' })
      );

    render(<EntityManagementPage />);
    await waitFor(() => expect(lastPanel().relationships).toHaveLength(1));

    await act(async () => {
      lastPanel().onRetry();
    });

    await waitFor(() => expect(lastPanel()).toMatchObject({ loading: false, failed: true }));
    expect(lastPanel().relationships).toEqual([]);
  });

  it('is not in the error state after a successful load with no relationships', async () => {
    service.getAllRelationships.mockResolvedValue([]);

    render(<EntityManagementPage />);

    await waitFor(() => expect(service.getAllRelationships).toHaveBeenCalled());
    await waitFor(() => expect(lastPanel()).toMatchObject({ loading: false }));
    expect(lastPanel().failed).toBe(false);
  });
});
