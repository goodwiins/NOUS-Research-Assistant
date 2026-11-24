import { AlertTriangle } from 'lucide-react';

import { EntityGraph } from '@/components/entities/EntityGraph';
import { Button } from '@/components/ui/button';
import type { Entity, GraphEdge } from '@/types/entity';

interface EntityGraphPanelProps {
  loading: boolean;
  /** The relationships request failed: show that, never an empty graph. */
  failed: boolean;
  onRetry: () => void;
  entities: Entity[];
  relationships: GraphEdge[];
  onEntityClick: (entity: Entity) => void;
}

/**
 * Graph tab body. A failed load is its own state (GOO-749): an empty graph
 * would tell the user they have no relationships when the request failed.
 */
export function EntityGraphPanel({
  loading,
  failed,
  onRetry,
  entities,
  relationships,
  onEntityClick,
}: EntityGraphPanelProps): JSX.Element {
  if (loading) {
    return (
      <div role="status" aria-label="Loading graph data" className="space-y-4">
        <div className="flex items-center justify-between">
          <div className="h-4 w-40 rounded bg-muted animate-pulse" />
          <div className="h-4 w-24 rounded bg-muted animate-pulse" />
        </div>
        <div className="h-[600px] rounded-lg border border-border bg-muted/30 animate-pulse" />
        <p className="text-sm text-muted-foreground">
          Loading connected nodes and relationships…
        </p>
      </div>
    );
  }

  if (failed) {
    return (
      <div
        role="alert"
        className="flex h-[600px] flex-col items-center justify-center gap-3 text-center"
      >
        <AlertTriangle aria-hidden="true" className="h-6 w-6 text-destructive" />
        <p className="text-sm font-medium text-foreground">
          Couldn&apos;t load the graph
        </p>
        <p className="max-w-sm text-sm text-muted-foreground">
          The relationships request failed, so the graph can&apos;t be shown.
        </p>
        <Button variant="outline" size="sm" onClick={onRetry}>
          Try again
        </Button>
      </div>
    );
  }

  return (
    <EntityGraph
      entities={entities}
      relationships={relationships}
      onEntityClick={onEntityClick}
      height={600}
    />
  );
}
