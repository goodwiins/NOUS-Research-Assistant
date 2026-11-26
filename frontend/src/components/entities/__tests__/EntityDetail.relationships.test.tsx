/**
 * EntityDetail relationship list (GOO-794, GOO-796): the list comes from the
 * useEntityRelationships query, keyed by entity id. It shows loading, error
 * and success states, refetches when the query is invalidated (a save or a
 * delete), and never shows the previous entity's list for a new entity.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Mock } from 'vitest';
import type { ReactNode } from 'react';
import { QueryClientProvider, type QueryClient } from '@tanstack/react-query';
import {
  act,
  createTestQueryClient,
  render,
  screen,
  waitFor,
  within,
} from '@/test/test-utils';
import { entityService } from '@/services/entityService';
import { invalidateEntityRelationships } from '@/hooks/useEntityRelationships';
import type { Entity, GraphEdge } from '@/types/entity';
import { EntityDetail } from '../EntityDetail';

vi.mock('@/hooks/useEntityPermissions', () => ({
  useEntityPermissions: () => ({ canCreate: true, canEdit: true, canDelete: true }),
}));
vi.mock('@/services/entityService', () => ({
  entityService: {
    getEntityRelationships: vi.fn(),
    deleteRelationship: vi.fn(),
  },
}));
vi.mock('../NeighborhoodExplorer', () => ({ NeighborhoodExplorer: (): null => null }));

const getRelationships = entityService.getEntityRelationships as unknown as Mock;
const deleteRelationship = entityService.deleteRelationship as unknown as Mock;
const entity = (id: string): Entity => ({ id, name: id, type: 'GENE' }) as Entity;
const edge = (source: string, target: string): GraphEdge =>
  ({ id: `${source}-${target}`, source, target, type: 'ENCODES' }) as GraphEdge;

function deferred<T>(): {
  promise: Promise<T>;
  resolve: (v: T) => void;
  reject: (e: unknown) => void;
} {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

let queryClient: QueryClient;
// One client for the whole test, so a rerender keeps the cache (the shared
// test wrapper builds a new client on every render).
const Wrapper = ({ children }: { children: ReactNode }): React.ReactElement => (
  <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
);
const renderDetail = (ui: React.ReactElement): ReturnType<typeof render> =>
  render(ui, { wrapper: Wrapper });

const relationshipsPanel = async (
  user: ReturnType<typeof render>['user']
): Promise<HTMLElement> => {
  await user.click(screen.getByRole('tab', { name: /relationships/i }));
  return screen.getByRole('tabpanel');
};
const loadingStatus = (): HTMLElement | null =>
  screen.queryByRole('status', { name: 'Loading relationships' });

beforeEach(() => {
  queryClient = createTestQueryClient();
  getRelationships.mockReset();
  deleteRelationship.mockReset();
  vi.spyOn(console, 'error').mockImplementation(() => {});
});

describe('EntityDetail relationships', () => {
  it('shows the loading state until the request settles, then the relationships', async () => {
    const pending = deferred<GraphEdge[]>();
    getRelationships.mockReturnValueOnce(pending.promise);
    const { user } = renderDetail(<EntityDetail entity={entity('A')} />);
    const panel = await relationshipsPanel(user);
    expect(loadingStatus()).toBeInTheDocument();
    expect(within(panel).queryByText('No relationships yet')).toBeNull();

    pending.resolve([edge('A', 'RAD51')]);

    expect(await within(panel).findByText('RAD51')).toBeInTheDocument();
    expect(loadingStatus()).toBeNull();
    expect(getRelationships).toHaveBeenCalledWith('A');
  });

  it('shows the empty state when the entity has no relationships', async () => {
    getRelationships.mockResolvedValueOnce([]);
    const { user } = renderDetail(<EntityDetail entity={entity('A')} />);
    const panel = await relationshipsPanel(user);

    expect(await within(panel).findByText('No relationships yet')).toBeInTheDocument();
    expect(within(panel).queryByRole('alert')).toBeNull();
  });

  it('shows an error state with a retry when the fetch fails, and retry loads the list', async () => {
    getRelationships
      .mockRejectedValueOnce(new Error('down'))
      .mockResolvedValueOnce([edge('A', 'RAD51')]);
    const { user } = renderDetail(<EntityDetail entity={entity('A')} />);
    const panel = await relationshipsPanel(user);

    const alert = await within(panel).findByRole('alert');
    expect(alert).toHaveTextContent("Couldn't load relationships");
    expect(loadingStatus()).toBeNull();
    expect(within(panel).queryByText('No relationships yet')).toBeNull();

    await user.click(within(alert).getByRole('button', { name: 'Try again' }));

    expect(await within(panel).findByText('RAD51')).toBeInTheDocument();
    expect(within(panel).queryByRole('alert')).toBeNull();
    expect(getRelationships).toHaveBeenCalledTimes(2);
  });

  it('refetches when the relationship queries are invalidated (a saved relationship)', async () => {
    getRelationships
      .mockResolvedValueOnce([edge('A', 'RAD51')])
      .mockResolvedValueOnce([edge('A', 'RAD51'), edge('A', 'TP53')]);
    const { user } = renderDetail(<EntityDetail entity={entity('A')} />);
    const panel = await relationshipsPanel(user);
    await within(panel).findByText('RAD51');

    await act(() => invalidateEntityRelationships(queryClient));

    expect(await within(panel).findByText('TP53')).toBeInTheDocument();
    expect(getRelationships).toHaveBeenCalledTimes(2);
  });

  it('switching entity fetches the new entity and never shows the previous list', async () => {
    const forB = deferred<GraphEdge[]>();
    getRelationships
      .mockResolvedValueOnce([edge('A', 'RAD51')])
      .mockReturnValueOnce(forB.promise);
    const { user, rerender } = renderDetail(<EntityDetail entity={entity('A')} />);
    const panel = await relationshipsPanel(user);
    await within(panel).findByText('RAD51');

    rerender(<EntityDetail entity={entity('B')} />);

    // B is loading: A's rows are gone, not shown as B's.
    expect(loadingStatus()).toBeInTheDocument();
    expect(within(screen.getByRole('tabpanel')).queryByText('RAD51')).toBeNull();
    expect(getRelationships).toHaveBeenLastCalledWith('B');

    forB.resolve([edge('B', 'MYC')]);
    const panelB = screen.getByRole('tabpanel');
    expect(await within(panelB).findByText('MYC')).toBeInTheDocument();
    expect(within(panelB).queryByText('RAD51')).toBeNull();
  });

  it('drops a late response for the previous entity', async () => {
    const forA = deferred<GraphEdge[]>();
    getRelationships
      .mockReturnValueOnce(forA.promise)
      .mockResolvedValueOnce([edge('B', 'MYC')]);
    const { user, rerender } = renderDetail(<EntityDetail entity={entity('A')} />);
    await relationshipsPanel(user);

    rerender(<EntityDetail entity={entity('B')} />);
    await within(screen.getByRole('tabpanel')).findByText('MYC');
    await act(async () => {
      forA.resolve([edge('A', 'RAD51')]);
      await forA.promise;
    });

    const panel = screen.getByRole('tabpanel');
    expect(within(panel).getByText('MYC')).toBeInTheDocument();
    expect(within(panel).queryByText('RAD51')).toBeNull();
  });

  it('a delete refetches the list through the query and closes the dialog', async () => {
    let stored = [edge('A', 'RAD51'), edge('A', 'TP53')];
    getRelationships.mockImplementation(async () => stored);
    deleteRelationship.mockImplementation(async (id: string) => {
      stored = stored.filter((rel) => rel.id !== id);
    });
    const { user } = renderDetail(<EntityDetail entity={entity('A')} />);
    const panel = await relationshipsPanel(user);
    await within(panel).findByText('TP53');

    const row = within(panel).getByText('TP53').closest('tr') as HTMLElement;
    await user.click(within(row).getByRole('button', { name: 'Delete relationship' }));
    await user.click(await screen.findByRole('button', { name: 'Delete' }));

    expect(deleteRelationship).toHaveBeenCalledWith('A-TP53');
    await waitFor(() => expect(within(panel).queryByText('TP53')).toBeNull());
    expect(within(panel).getByText('RAD51')).toBeInTheDocument();
    expect(getRelationships).toHaveBeenCalledTimes(2);
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull());
  });

  it('a failed delete keeps the list, does not refetch, and leaves the dialog open', async () => {
    getRelationships.mockResolvedValue([edge('A', 'RAD51')]);
    deleteRelationship.mockRejectedValue(new Error('500'));
    const { user } = renderDetail(<EntityDetail entity={entity('A')} />);
    const panel = await relationshipsPanel(user);
    await within(panel).findByText('RAD51');

    await user.click(within(panel).getByRole('button', { name: 'Delete relationship' }));
    await user.click(await screen.findByRole('button', { name: 'Delete' }));

    await waitFor(() => expect(deleteRelationship).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Delete' })).toBeEnabled()
    );
    expect(screen.getByRole('alertdialog')).toBeInTheDocument();
    expect(getRelationships).toHaveBeenCalledTimes(1);
    expect(within(panel).getByText('RAD51')).toBeInTheDocument();
  });
});
