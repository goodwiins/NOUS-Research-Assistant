/**
 * GOO-760 review: an expired session signs out exactly once, and the page
 * stops fetching.
 *
 * Uses the real AuthProvider and auth store. Sign-out resets the account
 * session, AuthProvider is keyed on it, and the page remounts. Before the
 * isAuthenticated gate, the remounted page refetched, got another 401 and
 * signed out again in an endless loop. A mocked useAuth cannot show that.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Mock } from 'vitest';
import type { ReactNode } from 'react';
import { render, waitFor } from '@/test/test-utils';
import toast from 'react-hot-toast';
import { AuthProvider } from '@/hooks/useAuth';
import { useAuthStore } from '@/stores/authStore';
import { entityService } from '@/services/entityService';
import { API_CONFIG, APIErrorClass } from '@/types/api';

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  useSearchParams: () => new URLSearchParams('tab=graph'),
}));

vi.mock('@/lib/supabase/client', () => ({
  createClient: () => ({
    auth: {
      signOut: async () => ({ error: null }),
      onAuthStateChange: () => ({ data: { subscription: { unsubscribe: () => {} } } }),
    },
  }),
}));

vi.mock('@/hooks/useEntityPermissions', () => ({
  useEntityPermissions: () => ({ canCreate: true, canEdit: true, canDelete: true }),
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

vi.mock('@/components/entities/EntityErrorBoundary', () => ({
  EntityErrorBoundary: ({ children }: { children: ReactNode }): ReactNode => children,
}));
vi.mock('@/components/entities/EntityFilters', () => ({ EntityFilters: (): null => null }));
vi.mock('@/components/entities/EntityList', () => ({ EntityList: (): null => null }));
vi.mock('@/components/entities/EntityDetail', () => ({ EntityDetail: (): null => null }));
vi.mock('@/components/entities/EntityForm', () => ({ EntityForm: (): null => null }));
vi.mock('@/components/entities/EntityGraphPanel', () => ({ EntityGraphPanel: (): null => null }));
vi.mock('@/components/entities/RelationshipForm', () => ({ RelationshipForm: (): null => null }));
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

import EntityManagementPage from '../../../../app/(dashboard)/entities/page';

const service = entityService as unknown as Record<string, Mock>;
const backendUrl = new URL(`${API_CONFIG.BASE_URL}/knowledge-graph/x`, window.location.origin).toString();

/** A 401 from our own backend, after a short network delay. */
const expiredSession = (): Promise<never> =>
  new Promise((_, reject) =>
    setTimeout(() => {
      const error = new APIErrorClass({
        message: 'Not authenticated',
        status_code: 401,
        type: 'auth_error',
      });
      error.requestUrl = backendUrl;
      reject(error);
    }, 10)
  );

const totalCalls = (): number =>
  Object.values(service).reduce((sum, fn) => sum + fn.mock.calls.length, 0);

const original = useAuthStore.getState();
let signOut: Mock;

beforeEach(() => {
  for (const fn of Object.values(service)) {
    fn.mockReset();
    fn.mockImplementation(expiredSession);
  }
  vi.spyOn(console, 'warn').mockImplementation(() => {});
  vi.spyOn(console, 'error').mockImplementation(() => {});
  signOut = vi.fn(original.signOut);
  useAuthStore.setState({
    initialize: vi.fn(async () => {}),
    signOut,
    user: { id: 'A' } as never,
    isAuthenticated: true,
    isLoading: false,
  });
});

afterEach(() => {
  useAuthStore.setState({ initialize: original.initialize, signOut: original.signOut });
  vi.restoreAllMocks();
});

describe('entities page on an expired session (GOO-760)', () => {
  it('signs out once when every knowledge-graph call returns 401, then stops fetching', async () => {
    render(
      <AuthProvider>
        <EntityManagementPage />
      </AuthProvider>
    );

    await waitFor(() => expect(signOut).toHaveBeenCalled());
    await waitFor(() => expect(useAuthStore.getState().isAuthenticated).toBe(false));
    const callsAtSignOut = totalCalls();

    // Long enough for the remounted page to refetch (its first entities
    // fetch waits 250 ms) and for any repeat 401 to come back.
    await new Promise((resolve) => setTimeout(resolve, 800));

    expect(signOut).toHaveBeenCalledTimes(1);
    expect(totalCalls()).toBe(callsAtSignOut);
    // Being signed out is not a load failure: no "Couldn't load" toast.
    expect(toast.error).not.toHaveBeenCalled();
  });

  it('does not fetch at all while signed out', async () => {
    useAuthStore.setState({ user: null, isAuthenticated: false });

    render(
      <AuthProvider>
        <EntityManagementPage />
      </AuthProvider>
    );
    await new Promise((resolve) => setTimeout(resolve, 400));

    expect(totalCalls()).toBe(0);
    expect(signOut).not.toHaveBeenCalled();
  });

  it('does not fetch while the session is still loading', async () => {
    useAuthStore.setState({ isLoading: true });

    render(
      <AuthProvider>
        <EntityManagementPage />
      </AuthProvider>
    );
    await new Promise((resolve) => setTimeout(resolve, 400));

    expect(totalCalls()).toBe(0);
  });
});
