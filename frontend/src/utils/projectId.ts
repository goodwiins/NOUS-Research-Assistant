const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * Canonical form of a project id. The backend returns UUIDs in lowercase,
 * but a URL can carry the same id in any case (GOO-778), so ids are compared
 * in lowercase. Anything that is not a UUID is returned unchanged.
 */
export function normalizeProjectId(id: string): string {
  return UUID_PATTERN.test(id) ? id.toLowerCase() : id;
}

/** True when both ids name the same project. */
export function sameProjectId(a: string, b: string): boolean {
  return normalizeProjectId(a) === normalizeProjectId(b);
}
