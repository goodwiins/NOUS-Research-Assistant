/**
 * Entities page, GOO-767:
 * - The "Add relationship" action in the entity detail dialog opens the
 *   relationship dialog for that entity. The real Dialog renders here, so the
 *   form is only reachable through the control.
 * - The action is disabled while the relationship types load, when they
 *   failed to load, and for a user who cannot create.
 *
 * Also the two #2308 review gaps:
 * - handleAuthError is read through a ref: a new identity on every render
 *   must not refetch the type lists, and the newest one must be the one called.
 * - A 401 on the entities fetch signs out without a "Failed to fetch" toast.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Mock } from 'vitest';
import type { ReactNode } from 'react';
import { act, render, screen, waitFor } from '@/test/test-utils';
import toast from 'react-hot-toast';
import { entityService } from '@/services/entityService';
import { API_CONFIG, APIErrorClass } from '@/types/api';
import type { Entity } from '@/types/entity';

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  useSearchParams: () => new URLSearchParams(''),
}));

const { auth, permissions, last } = vi.hoisted(() => ({
  // The handleAuthError the next render of useAuth returns. Tests swap it to
  // change its identity, as the real one changes on every auth-store change.
  auth: { handleAuthError: (_error: unknown): void => {} },
  permissions: { canCreate: true },
  last: {} as Record<string, Record<string, unknown>>,
}));

vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => ({
    handleAuthError: auth.handleAuthError,
    isAuthenticated: true,
    isLoading: false,
  }),
}));

vi.mock('@/hooks/useEntityPermissions', () => ({
  useEntityPermissions: () => ({
    canCreate: permissions.canCreate,
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
    deleteEntity: vi.fn(),
    createRelationship: vi.fn(),
  },
}));

const record =
  (name: string) =>
  (props: Record<string, unknown>): null => {
    last[name] = props;
    return null;
  };

vi.mock('@/components/entities/RelationshipForm', () => ({
  RelationshipForm: ({
    sourceEntityId,
    onSubmit,
    onCancel,
  }: {
    sourceEntityId?: string;
    onSubmit: (data: Record<string, unknown>) => Promise<void>;
    onCancel: () => void;
  }) => (
    <div data-testid="relationship-form" data-source={sourceEntityId}>
      <button
        type="button"
        onClick={() =>
          void onSubmit({
            source_entity_id: sourceEntityId,
            target_entity_id: 'e2',
            relationship_type: 'ENCODES',
          })
        }
      >
        Save relationship
      </button>
      <button type="button" onClick={onCancel}>
        Cancel relationship
      </button>
    </div>
  ),
}));
vi.mock('@/components/entities/EntityList', () => ({
  EntityList: (props: Record<string, unknown>) => record('list')(props),
}));
vi.mock('@/components/entities/EntityErrorBoundary', () => ({
  EntityErrorBoundary: ({ children }: { children: ReactNode }): ReactNode => children,
}));
vi.mock('@/components/entities/EntityFilters', () => ({ EntityFilters: (): null => null }));
vi.mock('@/components/entities/EntityForm', () => ({ EntityForm: (): null => null }));
vi.mock('@/components/entities/EntityGraphPanel', () => ({ EntityGraphPanel: (): null => null }));
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
const gene = { id: 'e1', name: 'BRCA1', type: 'GENE' } as Entity;

/** A 401 from our own backend: the only error that ends the session. */
const expired = (): APIErrorClass => {
  const error = new APIErrorClass({
    message: 'Session expired',
    status_code: 401,
    type: 'http_error',
  });
  error.requestUrl = new URL(
    `${API_CONFIG.BASE_URL}/knowledge-graph/x`,
    window.location.origin
  ).toString();
  return error;
};

const addRelationship = (): HTMLElement =>
  screen.getByRole('button', { name: /add relationship/i });

/** Open the detail dialog for `gene` the way the list does. */
const viewGene = async (): Promise<void> => {
  await waitFor(() => expect(last.list).toBeDefined());
  act(() => (last.list.onView as (entity: Entity) => void)(gene));
  await screen.findByText('Entity details');
};

beforeEach(() => {
  for (const key of Object.keys(last)) delete last[key];
  for (const fn of Object.values(service)) fn.mockReset();
  toastError.mockReset();
  (toast.success as unknown as Mock).mockReset();
  permissions.canCreate = true;
  auth.handleAuthError = vi.fn();
  vi.spyOn(console, 'warn').mockImplementation(() => {});
  vi.spyOn(console, 'error').mockImplementation(() => {});
  service.getEntityTypes.mockResolvedValue(['GENE']);
  service.getRelationshipTypes.mockResolvedValue(['ENCODES']);
  service.getAnalytics.mockResolvedValue({ entity_type_counts: {} });
  service.getEntities.mockResolvedValue(emptyPage);
  service.getAllRelationships.mockResolvedValue([]);
});

describe('Add relationship from the entity detail dialog (GOO-767)', () => {
  it('opens the relationship dialog with the viewed entity as the source', async () => {
    const { user } = render(<EntityManagementPage />);
    await viewGene();
    expect(screen.queryByTestId('relationship-form')).toBeNull();

    await waitFor(() => expect(addRelationship()).toBeEnabled());
    await user.click(addRelationship());

    expect(await screen.findByText('New relationship')).toBeInTheDocument();
    expect(screen.getByTestId('relationship-form')).toHaveAttribute('data-source', 'e1');
  });

  it('is disabled while the relationship types are loading', async () => {
    service.getRelationshipTypes.mockReturnValue(new Promise(() => {}));
    render(<EntityManagementPage />);
    await viewGene();

    expect(addRelationship()).toBeDisabled();
    expect(screen.getByText('Loading relationship types…')).toBeInTheDocument();
  });

  it('is disabled when the relationship types failed to load', async () => {
    service.getRelationshipTypes.mockRejectedValue(
      new APIErrorClass({ message: 'HTTP 400', status_code: 400, type: 'http_error' })
    );
    const { user } = render(<EntityManagementPage />);
    await viewGene();

    await waitFor(() =>
      expect(screen.getByText("Couldn't load relationship types")).toBeInTheDocument()
    );
    expect(addRelationship()).toBeDisabled();
    await user.click(addRelationship());
    expect(screen.queryByTestId('relationship-form')).toBeNull();
  });

  it('is disabled for a user who cannot create', async () => {
    permissions.canCreate = false;
    render(<EntityManagementPage />);
    await viewGene();
    await waitFor(() => expect(service.getRelationshipTypes).toHaveBeenCalled());

    // Types loaded, so only the permission keeps it disabled.
    await waitFor(() =>
      expect(screen.queryByText('Loading relationship types…')).toBeNull()
    );
    expect(addRelationship()).toBeDisabled();
  });
});

/** Open the relationship dialog through the real control. */
const openRelationshipDialog = async (
  user: ReturnType<typeof render>['user']
): Promise<void> => {
  await viewGene();
  await waitFor(() => expect(addRelationship()).toBeEnabled());
  await user.click(addRelationship());
  await screen.findByTestId('relationship-form');
};

describe('focus returns to "Add relationship" when the relationship dialog closes (GOO-767, WCAG 2.4.3)', () => {
  it.each([
    ['Escape', async (user: ReturnType<typeof render>['user']) => user.keyboard('{Escape}')],
    ['Cancel', async (user: ReturnType<typeof render>['user']) =>
      user.click(screen.getByRole('button', { name: 'Cancel relationship' }))],
    ['a successful save', async (user: ReturnType<typeof render>['user']) =>
      user.click(screen.getByRole('button', { name: 'Save relationship' }))],
  ])('after %s', async (_how, close) => {
    service.createRelationship.mockResolvedValue({});
    const { user } = render(<EntityManagementPage />);
    await openRelationshipDialog(user);

    await close(user);

    await waitFor(() => expect(screen.queryByTestId('relationship-form')).toBeNull());
    // The detail dialog stayed open underneath, so the trigger still exists.
    expect(screen.getByText('Entity details')).toBeInTheDocument();
    await waitFor(() => expect(document.activeElement).toBe(addRelationship()));
  });
});

describe('a failed relationship create (#2327 review)', () => {
  it('signs out on a 401 without a toast, and keeps the form open', async () => {
    const failure = expired();
    service.createRelationship.mockRejectedValue(failure);
    const { user } = render(<EntityManagementPage />);
    await openRelationshipDialog(user);

    await user.click(screen.getByRole('button', { name: 'Save relationship' }));

    await waitFor(() => expect(auth.handleAuthError).toHaveBeenCalledWith(failure));
    expect(toastError).not.toHaveBeenCalled();
    expect(toast.success).not.toHaveBeenCalled();
    expect(screen.getByTestId('relationship-form')).toBeInTheDocument();
  });

  it('shows the generic error on any other failure, and keeps the form open', async () => {
    service.createRelationship.mockRejectedValue(
      new APIErrorClass({ message: 'neo4j exploded at line 7', status_code: 500, type: 'http_error' })
    );
    const { user } = render(<EntityManagementPage />);
    await openRelationshipDialog(user);

    await user.click(screen.getByRole('button', { name: 'Save relationship' }));

    await waitFor(() => expect(toastError).toHaveBeenCalledWith('Failed to create relationship'));
    expect(auth.handleAuthError).not.toHaveBeenCalled();
    expect(screen.getByTestId('relationship-form')).toBeInTheDocument();
  });
});

describe('handleAuthError is read through a ref (#2308 review)', () => {
  it('loads the type lists once even though handleAuthError changes identity every render', async () => {
    const calls: unknown[] = [];
    // A fresh function per useAuth call, like the real hook across store updates.
    Object.defineProperty(auth, 'handleAuthError', {
      configurable: true,
      get: () => (error: unknown) => calls.push(error),
    });
    try {
      render(<EntityManagementPage />);
      await waitFor(() => expect(last.list).toBeDefined());
      // Past the 250 ms entities debounce. Without the ref, every render makes
      // new fetch callbacks: the type lists refetch, and the debounced
      // entities fetch is reset before it can fire.
      await new Promise((resolve) => setTimeout(resolve, 600));

      expect(service.getEntityTypes).toHaveBeenCalledTimes(1);
      expect(service.getRelationshipTypes).toHaveBeenCalledTimes(1);
      expect(service.getEntities).toHaveBeenCalledTimes(1);
    } finally {
      Object.defineProperty(auth, 'handleAuthError', {
        configurable: true,
        writable: true,
        value: vi.fn(),
      });
    }
  });

  it('calls the newest handleAuthError, not the one from the first render', async () => {
    const first = vi.fn();
    const newest = vi.fn();
    auth.handleAuthError = first;
    const { rerender } = render(<EntityManagementPage />);
    await waitFor(() => expect(last.list).toBeDefined());

    auth.handleAuthError = newest;
    rerender(<EntityManagementPage />);
    const failure = expired();
    service.deleteEntity.mockRejectedValue(failure);
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    await act(async () => {
      await (last.list.onDelete as (id: string) => Promise<void>)('e1');
    });

    expect(newest).toHaveBeenCalledWith(failure);
    expect(first).not.toHaveBeenCalled();
  });
});

describe('a 401 on the entities fetch (#2308 review)', () => {
  it('signs out without a "Failed to fetch entities" toast', async () => {
    const failure = expired();
    service.getEntities.mockRejectedValue(failure);

    render(<EntityManagementPage />);

    await waitFor(() => expect(auth.handleAuthError).toHaveBeenCalledWith(failure));
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(toastError).not.toHaveBeenCalled();
  });
});
