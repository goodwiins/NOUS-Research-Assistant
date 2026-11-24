/**
 * Page counts for the entities list (GOO-727).
 *
 * With client-side filters on (the `__null__` type, text search, confidence
 * range or a non-default sort), the page fetches every entity and filters in
 * the browser, so the server total counts rows the user cannot see. Both the
 * page count and the "N items" total must then come from the filtered list.
 */
export function entityPagination({
  hasClientFilters,
  filteredCount,
  serverTotal,
  pageSize,
}: {
  hasClientFilters: boolean;
  filteredCount: number;
  serverTotal: number;
  pageSize: number;
}): { totalItems: number; totalPages: number } {
  const totalItems = hasClientFilters ? filteredCount : serverTotal;
  return {
    totalItems,
    totalPages: pageSize > 0 ? Math.ceil(totalItems / pageSize) : 0,
  };
}
