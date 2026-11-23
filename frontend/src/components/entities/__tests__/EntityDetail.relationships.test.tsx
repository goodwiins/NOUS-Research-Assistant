/**
 * EntityDetail relationship list (GOO-794): it refetches when the parent
 * bumps relationshipsVersion, and a response for an entity or version no
 * longer shown never replaces the current list.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Mock } from 'vitest';
import { render, screen, within } from '@/test/test-utils';
import { entityService } from '@/services/entityService';
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
const entity = (id: string): Entity => ({ id, name: id, type: 'GENE' }) as Entity;
const edge = (source: string, target: string): GraphEdge =>
  ({ id: `${source}-${target}`, source, target, type: 'ENCODES' }) as GraphEdge;

function deferred<T>(): { promise: Promise<T>; resolve: (v: T) => void } {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

const relationshipsPanel = async (
  user: ReturnType<typeof render>['user']
): Promise<HTMLElement> => {
  await user.click(screen.getByRole('tab', { name: /relationships/i }));
  return screen.getByRole('tabpanel');
};

beforeEach(() => {
  getRelationships.mockReset();
  vi.spyOn(console, 'error').mockImplementation(() => {});
});

describe('EntityDetail relationships (GOO-794)', () => {
  it('refetches when relationshipsVersion changes', async () => {
    getRelationships
      .mockResolvedValueOnce([edge('A', 'RAD51')])
      .mockResolvedValueOnce([edge('A', 'RAD51'), edge('A', 'TP53')]);
    const { user, rerender } = render(
      <EntityDetail entity={entity('A')} relationshipsVersion={0} />
    );
    const panel = await relationshipsPanel(user);
    await within(panel).findByText('RAD51');

    rerender(<EntityDetail entity={entity('A')} relationshipsVersion={1} />);

    expect(await within(screen.getByRole('tabpanel')).findByText('TP53')).toBeInTheDocument();
    expect(getRelationships).toHaveBeenCalledTimes(2);
  });

  it("drops a late response for the previous entity", async () => {
    const forA = deferred<GraphEdge[]>();
    getRelationships
      .mockReturnValueOnce(forA.promise)
      .mockResolvedValueOnce([edge('B', 'MYC')]);
    const { user, rerender } = render(<EntityDetail entity={entity('A')} />);
    await relationshipsPanel(user);

    rerender(<EntityDetail entity={entity('B')} />);
    await within(screen.getByRole('tabpanel')).findByText('MYC');
    forA.resolve([edge('A', 'RAD51')]);
    await forA.promise;
    await new Promise((resolve) => setTimeout(resolve, 0));

    const panel = screen.getByRole('tabpanel');
    expect(within(panel).getByText('MYC')).toBeInTheDocument();
    expect(within(panel).queryByText('RAD51')).toBeNull();
  });

  it('shows the loading state until the current request settles, even when it fails', async () => {
    const pending = deferred<GraphEdge[]>();
    getRelationships.mockReturnValueOnce(pending.promise);
    const { user } = render(<EntityDetail entity={entity('A')} />);
    await relationshipsPanel(user);
    expect(screen.getByRole('status', { name: 'Loading relationships' })).toBeInTheDocument();

    getRelationships.mockReset();
    pending.resolve(Promise.reject(new Error('down')) as unknown as GraphEdge[]);

    expect(await screen.findByText('No relationships yet')).toBeInTheDocument();
    expect(screen.queryByRole('status', { name: 'Loading relationships' })).toBeNull();
  });
});
