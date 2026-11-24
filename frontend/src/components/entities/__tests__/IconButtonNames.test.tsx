import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Mock } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

import { EntityForm } from '@/components/entities/EntityForm';
import { EntityMergeTool } from '@/components/entities/EntityMergeTool';
import { RelationshipForm } from '@/components/entities/RelationshipForm';
import { entityService } from '@/services/entityService';
import type { Entity, GraphEdge } from '@/types/entity';

class MockResizeObserver {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}
(global as typeof global & { ResizeObserver?: typeof MockResizeObserver }).ResizeObserver =
  MockResizeObserver;

vi.mock('@/hooks/useEntityPermissions', () => ({
  useEntityPermissions: () => ({
    canCreate: true,
    canEdit: true,
    canDelete: true,
    canBulkEdit: true,
    isAdmin: true,
  }),
}));

vi.mock('@/services/entityService', () => ({
  entityService: {
    getEntities: vi.fn(),
    searchEntities: vi.fn(),
    createMergeJob: vi.fn(),
    getProcessingJob: vi.fn(),
  },
}));

vi.mock('react-hot-toast', () => ({
  __esModule: true,
  default: { success: vi.fn(), error: vi.fn() },
}));

beforeEach(() => {
  vi.clearAllMocks();
});

describe('entity icon-only button names', () => {
  it('EntityForm names alias and metadata-field remove buttons', () => {
    const entity = {
      id: 'e1',
      name: 'Ada',
      type: 'PERSON',
      confidence: 0.9,
      metadata: {
        aliases: ['Countess'],
        properties: { born: '1815' },
      },
    } as unknown as Entity;
    render(<EntityForm entity={entity} onSubmit={vi.fn()} onCancel={vi.fn()} />);

    expect(
      screen.getByRole('button', { name: 'Remove alias Countess' })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Remove metadata field born' })
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add alias' })).toBeInTheDocument();
  });

  it('RelationshipForm names clear-target and remove-evidence buttons', async () => {
    (entityService.searchEntities as Mock).mockResolvedValue([
      { id: 't1', name: 'Babbage', type: 'PERSON' },
    ]);
    const initial = {
      source: 's1',
      evidence: ['Letter, 1843'],
    } as unknown as Partial<GraphEdge>;
    render(
      <RelationshipForm
        sourceEntityId="s1"
        initialData={initial}
        types={{ status: 'loaded', types: ['KNOWS'] }}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
      />
    );

    expect(
      screen.getByRole('button', { name: 'Remove evidence 1' })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Add evidence' })
    ).toBeInTheDocument();

    fireEvent.change(screen.getByPlaceholderText(/search/i), {
      target: { value: 'Bab' },
    });
    fireEvent.click(await screen.findByText('Babbage'));
    expect(
      screen.getByRole('button', { name: 'Clear target entity Babbage' })
    ).toBeInTheDocument();
  });

  it('EntityMergeTool names the error dismiss button', async () => {
    (entityService.getEntities as Mock).mockRejectedValue(new Error('boom'));
    render(<EntityMergeTool />);
    fireEvent.click(screen.getByRole('button', { name: /find_duplicates/i }));
    await waitFor(() =>
      expect(
        screen.getByRole('button', { name: 'Dismiss error' })
      ).toBeInTheDocument()
    );
  });
});
