/**
 * Shared helpers for presenting entity types in the knowledge-graph UI.
 *
 * Entity type *values* are stored as machine constants (e.g. `JOB_TITLE`).
 * These helpers keep the display human and on-brand without changing the
 * underlying value used for filtering, sorting, or persistence.
 */

/**
 * Humanize a stored entity-type value for display.
 * `JOB_TITLE` -> `Job title`, `PERSON` -> `Person`, `__null__` -> `Unknown type`.
 */
export const formatEntityType = (type?: string | null): string => {
  if (!type) return 'Unknown type';
  if (type === '__null__') return 'Unknown type';
  const spaced = type.replace(/_/g, ' ').trim().toLowerCase();
  if (!spaced) return 'Unknown type';
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
};

/**
 * One neutral, tokenized badge style for every entity type.
 * Meaning is carried by the type label itself, not by color, so the dense
 * table stays calm and scannable (no rainbow hue map).
 */
export const entityTypeBadgeClass =
  'border-border bg-muted text-muted-foreground font-normal';

/**
 * UI-only sentinel for the "Unknown type" filter. The backend
 * `entity_types: List[EntityType]` query rejects it (422), so it must never
 * be sent to the server (GOO-651).
 */
export const NULL_ENTITY_TYPE = '__null__';

/**
 * The entity types the server can filter by, or `undefined` for "no server
 * type filter". "Unknown type" cannot be expressed server-side, and
 * narrowing by the other selected types would drop the untyped entities it
 * asks for, so selecting it means fetching unfiltered and filtering on the
 * client instead.
 */
export function serverEntityTypeFilter<T extends string>(
  selectedTypes: readonly T[]
): T[] | undefined {
  if (selectedTypes.length === 0) return undefined;
  if (selectedTypes.some((type) => type === NULL_ENTITY_TYPE)) return undefined;
  return [...selectedTypes];
}

/** True when the type filter has to run on the client (see above). */
export function needsClientEntityTypeFilter(
  selectedTypes: readonly string[]
): boolean {
  return selectedTypes.includes(NULL_ENTITY_TYPE);
}
