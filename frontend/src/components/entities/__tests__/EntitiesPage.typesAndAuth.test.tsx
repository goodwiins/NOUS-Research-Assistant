/**
 * Entities page:
 * - GOO-756: the filter panel and the relationship form get the graph's own
 *   type lists, as loading / failed / loaded, never a built-in list.
 * - GOO-760: the three type/analytics requests settle separately, a failure
 *   names the list that failed, and every entity call's error goes through
 *   handleAuthError so an expired session signs the user out.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Mock } from 'vitest';
import type { ReactNode } from 'react';
import { act, render, waitFor } from '@/test/test-utils';
import toast from 'react-hot-toast';
import { entityService } from '@/services/entityService';
import { API_CONFIG, APIErrorClass } from '@/types/api';
import type { TypeList } from '@/components/entities/typeList';

let search = '';
vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  useSearchParams: () => new URLSearchParams(search),
}));

vi.mock('@/hooks/useEntityPermissions', () => ({
  useEntityPermissions: () => ({ canCreate: true, canEdit: true, canDelete: true }),
}));

/** The props the page passed to a stubbed child, as far as these tests read them. */
interface Recorded {
  types?: TypeList;
  typeCounts?: Record<string, number>;
  onDelete?: (id: string) => Promise<void>;
  onEdit?: (entity: unknown) => void;
  onSubmit?: (data: Record<string, unknown>) => Promise<void>;
}

const { handleAuthError, last, recorder } = vi.hoisted(() => {
  const last: Record<string, Recorded> = {};
  return {
    // Stable like the real useCallback, or the fetch callbacks change every render.
    handleAuthError: vi.fn(),
    last,
    recorder:
      (name: string) =>
      (props: Recorded): null => {
        last[name] = props;
        return null;
      },
  };
});
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
    deleteEntity: vi.fn(),
    updateEntity: vi.fn(),
    createEntity: vi.fn(),
    createRelationship: vi.fn(),
  },
}));

// Dialogs render their content inline so the forms inside them are reachable.
vi.mock('@/components/ui/dialog', () => {
  const Pass = ({ children }: { children?: ReactNode }): ReactNode => children;
  return {
    Dialog: Pass,
    DialogContent: Pass,
    DialogHeader: Pass,
    DialogTitle: Pass,
  };
});

vi.mock('@/components/entities/EntityFilters', () => ({ EntityFilters: recorder('filters') }));
vi.mock('@/components/entities/RelationshipForm', () => ({
  RelationshipForm: recorder('relationshipForm'),
}));
vi.mock('@/components/entities/EntityList', () => ({ EntityList: recorder('list') }));
vi.mock('@/components/entities/EntityForm', () => ({ EntityForm: recorder('entityForm') }));
vi.mock('@/components/entities/EntityGraphPanel', () => ({
  EntityGraphPanel: recorder('graph'),
}));
vi.mock('@/components/entities/EntityErrorBoundary', () => ({
  EntityErrorBoundary: ({ children }: { children: ReactNode }): ReactNode => children,
}));
vi.mock('@/components/entities/EntityDetail', () => ({ EntityDetail: (): null => null }));
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
const toastError = toast.error as unknown as Mock;
const emptyPage = { entities: [], total: 0, limit: 20, offset: 0, has_more: false };
const apiError = (status_code: number, message = `HTTP ${status_code}`): APIErrorClass =>
  new APIErrorClass({ message, status_code, type: 'http_error' });

const typesOf = (name: string): TypeList => last[name]?.types;

beforeEach(() => {
  search = '';
  for (const key of Object.keys(last)) delete last[key];
  for (const fn of Object.values(service)) fn.mockReset();
  handleAuthError.mockReset();
  toastError.mockReset();
  vi.spyOn(console, 'warn').mockImplementation(() => {});
  vi.spyOn(console, 'error').mockImplementation(() => {});
  service.getEntityTypes.mockResolvedValue(['GENE', 'PROTEIN']);
  service.getRelationshipTypes.mockResolvedValue(['ENCODES']);
  service.getAnalytics.mockResolvedValue({ entity_type_counts: { GENE: 3 } });
  service.getEntities.mockResolvedValue(emptyPage);
  service.getAllRelationships.mockResolvedValue([]);
});

describe('type lists reach the filters and the relationship form (GOO-756)', () => {
  it('starts both in the loading state, before the requests settle', () => {
    service.getEntityTypes.mockReturnValue(new Promise(() => {}));
    service.getRelationshipTypes.mockReturnValue(new Promise(() => {}));

    render(<EntityManagementPage />);

    expect(typesOf('filters')).toEqual({ status: 'loading' });
    expect(typesOf('relationshipForm')).toEqual({ status: 'loading' });
  });

  it('passes the loaded lists and counts down', async () => {
    render(<EntityManagementPage />);

    await waitFor(() =>
      expect(typesOf('filters')).toEqual({ status: 'loaded', types: ['GENE', 'PROTEIN'] })
    );
    expect(typesOf('relationshipForm')).toEqual({ status: 'loaded', types: ['ENCODES'] });
    expect(last.filters.typeCounts).toEqual({ GENE: 3 });
    expect(toastError).not.toHaveBeenCalled();
  });
});

describe('one failed list does not drop the others (GOO-760)', () => {
  it('keeps entity types and counts when relationship types fail, and names the failure', async () => {
    service.getRelationshipTypes.mockRejectedValue(apiError(400));

    render(<EntityManagementPage />);

    await waitFor(() => expect(typesOf('relationshipForm')).toEqual({ status: 'failed' }));
    expect(typesOf('filters')).toEqual({ status: 'loaded', types: ['GENE', 'PROTEIN'] });
    expect(last.filters.typeCounts).toEqual({ GENE: 3 });
    expect(toastError).toHaveBeenCalledWith("Couldn't load relationship types");
  });

  it('marks entity types failed and keeps the relationship types', async () => {
    service.getEntityTypes.mockRejectedValue(apiError(400));

    render(<EntityManagementPage />);

    await waitFor(() => expect(typesOf('filters')).toEqual({ status: 'failed' }));
    expect(typesOf('relationshipForm')).toEqual({ status: 'loaded', types: ['ENCODES'] });
    expect(toastError).toHaveBeenCalledWith("Couldn't load entity types");
  });

  it('names every list that failed in one message', async () => {
    service.getEntityTypes.mockRejectedValue(apiError(400));
    service.getAnalytics.mockRejectedValue(apiError(400));

    render(<EntityManagementPage />);

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("Couldn't load entity types, entity counts")
    );
    expect(typesOf('relationshipForm')).toEqual({ status: 'loaded', types: ['ENCODES'] });
    expect(last.filters.typeCounts).toEqual({});
  });

  it('shows the unavailable banner instead of a toast when the graph is down', async () => {
    service.getEntityTypes.mockRejectedValue(apiError(503, 'Service Unavailable'));

    const { findByText } = render(<EntityManagementPage />);

    expect(await findByText('Knowledge graph unavailable')).toBeInTheDocument();
    expect(typesOf('filters')).toEqual({ status: 'failed' });
    expect(toastError).not.toHaveBeenCalledWith(expect.stringContaining('entity types'));
  });
});

describe('every entity call routes its error through handleAuthError (GOO-760)', () => {
  // A 401 from our own backend: the only error that ends the session.
  const expired = (): APIErrorClass => {
    const error = apiError(401, 'Session expired');
    error.requestUrl = new URL(
      `${API_CONFIG.BASE_URL}/knowledge-graph/x`,
      window.location.origin
    ).toString();
    return error;
  };

  it.each([
    ['getEntityTypes', 'getEntityTypes'],
    ['getRelationshipTypes', 'getRelationshipTypes'],
    ['getAnalytics', 'getAnalytics'],
    ['getEntities', 'getEntities'],
  ])('a 401 from %s', async (_name, method) => {
    const failure = expired();
    service[method].mockRejectedValue(failure);

    render(<EntityManagementPage />);

    await waitFor(() => expect(handleAuthError).toHaveBeenCalledWith(failure));
  });

  it('a 401 from getAllRelationships on the graph tab', async () => {
    search = 'tab=graph';
    const failure = expired();
    service.getAllRelationships.mockRejectedValue(failure);

    render(<EntityManagementPage />);

    await waitFor(() => expect(handleAuthError).toHaveBeenCalledWith(failure));
  });

  it('a 401 from deleting an entity', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const failure = expired();
    service.deleteEntity.mockRejectedValue(failure);
    render(<EntityManagementPage />);
    await waitFor(() => expect(last.list).toBeDefined());

    await act(async () => {
      await last.list.onDelete!('e1');
    });

    expect(handleAuthError).toHaveBeenCalledWith(failure);
  });

  it('a 401 from updating an entity', async () => {
    const failure = expired();
    service.updateEntity.mockRejectedValue(failure);
    render(<EntityManagementPage />);
    await waitFor(() => expect(last.list).toBeDefined());

    act(() => last.list.onEdit!({ id: 'e1', name: 'x', type: 'GENE' }));
    await act(async () => {
      await last.entityForm.onSubmit!({ name: 'y' });
    });

    await waitFor(() => expect(handleAuthError).toHaveBeenCalledWith(failure));
  });

  it('a 401 from creating an entity', async () => {
    const failure = expired();
    service.createEntity.mockRejectedValue(failure);
    render(<EntityManagementPage />);
    await waitFor(() => expect(last.entityForm).toBeDefined());

    await act(async () => {
      await last.entityForm.onSubmit!({ name: 'x', type: 'GENE' });
    });

    expect(handleAuthError).toHaveBeenCalledWith(failure);
  });

  it('a 401 from creating a relationship', async () => {
    const failure = expired();
    service.createRelationship.mockRejectedValue(failure);
    render(<EntityManagementPage />);
    await waitFor(() => expect(last.relationshipForm).toBeDefined());

    await act(async () => {
      await last.relationshipForm.onSubmit!({
        source_entity_id: 'a',
        target_entity_id: 'b',
        relationship_type: 'ENCODES',
      });
    });

    expect(handleAuthError).toHaveBeenCalledWith(failure);
  });
});
