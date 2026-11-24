'use client';

import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query';

import { entityService } from '@/services/entityService';
import type { GraphEdge } from '@/types/entity';

export const entityRelationshipQueryKeys = {
  all: ['entity-relationships'] as const,
  forEntity: (entityId: string): readonly ['entity-relationships', string] =>
    [...entityRelationshipQueryKeys.all, entityId] as const,
};

/**
 * Refetch every entity's relationship list. A relationship has two ends, so a
 * create or delete changes the list of both entities; only lists on screen
 * refetch, the rest are marked stale.
 */
export function invalidateEntityRelationships(
  queryClient: QueryClient
): Promise<void> {
  return queryClient.invalidateQueries({
    queryKey: entityRelationshipQueryKeys.all,
  });
}

/**
 * Relationships of one entity, keyed by its id. Switching entity switches the
 * key, so the previous entity's list is never shown for the new one and a late
 * response for it is cached under its own key only.
 */
export function useEntityRelationships(
  entityId: string | undefined
): UseQueryResult<GraphEdge[], Error> {
  return useQuery({
    queryKey: entityRelationshipQueryKeys.forEntity(entityId ?? ''),
    queryFn: () => entityService.getEntityRelationships(entityId!),
    enabled: Boolean(entityId),
    retry: false,
  });
}

/** Delete a relationship, then refetch the relationship lists. */
export function useDeleteEntityRelationship(): UseMutationResult<
  void,
  Error,
  string
> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (relationshipId: string) =>
      entityService.deleteRelationship(relationshipId),
    onSuccess: () => invalidateEntityRelationships(queryClient),
  });
}
