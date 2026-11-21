import { describe, expect, it } from 'vitest';
import { render } from '@/test/test-utils';
import { EntityGraph } from '@/components/entities/EntityGraph';
import type { Entity } from '@/types/entity';

const entities = [
  { id: 'e1', name: 'Aspirin', type: 'DRUG', confidence: 0.9 },
  { id: 'e2', name: 'Headache', type: 'CONDITION', confidence: 0.8 },
] as unknown as Entity[];

describe('EntityGraph (FR-4)', () => {
  it('renders with bundled d3 and never injects a CDN script', () => {
    const { container } = render(
      <EntityGraph
        entities={entities}
        relationships={[
          { source: 'e1', target: 'e2', type: 'TREATS', weight: 0.7 },
        ] as never}
      />
    );

    expect(document.querySelector('script[src*="d3js.org"]')).toBeNull();
    const svg = container.querySelector('svg circle');
    expect(svg).not.toBeNull();
    expect(container.textContent).toContain('Aspirin');
  });
});
