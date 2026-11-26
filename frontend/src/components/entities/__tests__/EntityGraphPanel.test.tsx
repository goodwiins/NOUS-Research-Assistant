/**
 * GOO-749: a failed relationships load shows an error with a retry, never an
 * empty graph that would read as "no relationships".
 */
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@/test/test-utils';
import { EntityGraphPanel } from '@/components/entities/EntityGraphPanel';
import type { Entity, GraphEdge } from '@/types/entity';

const entities = [
  { id: 'e1', name: 'Aspirin', type: 'DRUG', confidence: 0.9 },
  { id: 'e2', name: 'Headache', type: 'CONDITION', confidence: 0.8 },
] as unknown as Entity[];
const relationships = [
  { source: 'e1', target: 'e2', type: 'TREATS', weight: 0.7 },
] as unknown as GraphEdge[];

const renderPanel = (
  props: Partial<React.ComponentProps<typeof EntityGraphPanel>> = {}
): ReturnType<typeof render> & { onRetry: ReturnType<typeof vi.fn> } => {
  const onRetry = vi.fn();
  const view = render(
    <EntityGraphPanel
      loading={false}
      failed={false}
      onRetry={onRetry}
      entities={entities}
      relationships={relationships}
      onEntityClick={vi.fn()}
      {...props}
    />
  );
  return { ...view, onRetry };
};

describe('EntityGraphPanel (GOO-749)', () => {
  it('shows the failure and no graph when the load failed', () => {
    const { container } = renderPanel({ failed: true, entities: [], relationships: [] });

    expect(screen.getByRole('alert')).toHaveTextContent("Couldn't load the graph");
    expect(container.querySelector('svg circle')).toBeNull();
  });

  it('retries from the error state', () => {
    const { onRetry } = renderPanel({ failed: true });

    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));

    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it('shows the loading state while a load (or retry) is running', () => {
    renderPanel({ loading: true, failed: true });

    expect(screen.getByRole('status', { name: 'Loading graph data' })).toBeInTheDocument();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('draws the graph after a successful load', () => {
    const { container } = renderPanel();

    expect(screen.queryByRole('alert')).toBeNull();
    expect(container.querySelector('svg circle')).not.toBeNull();
    expect(container.textContent).toContain('Aspirin');
  });
});
