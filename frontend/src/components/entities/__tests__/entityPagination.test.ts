/**
 * GOO-727: with the client-side `__null__` filter (or any client filter) on,
 * the Pagination total must be the filtered count, not the server total.
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

import { entityPagination } from '../entityPagination';

describe('entityPagination', () => {
  it('uses the filtered count when client-side filters are on', () => {
    expect(
      entityPagination({
        hasClientFilters: true,
        filteredCount: 7,
        serverTotal: 240,
        pageSize: 5,
      })
    ).toEqual({ totalItems: 7, totalPages: 2 });
  });

  it('uses the server total when the server does the filtering', () => {
    expect(
      entityPagination({
        hasClientFilters: false,
        filteredCount: 20,
        serverTotal: 240,
        pageSize: 20,
      })
    ).toEqual({ totalItems: 240, totalPages: 12 });
  });

  it('never divides by zero', () => {
    expect(
      entityPagination({
        hasClientFilters: true,
        filteredCount: 3,
        serverTotal: 3,
        pageSize: 0,
      }).totalPages
    ).toBe(0);
  });

  it('is what the entities page passes to Pagination', () => {
    const page = readFileSync(
      resolve(__dirname, '../../../../app/(dashboard)/entities/page.tsx'),
      'utf8'
    );
    const pagination = /<Pagination\b[\s\S]*?\/>/.exec(page)?.[0] ?? '';
    expect(pagination).toContain('totalPages={totalPages}');
    expect(pagination).toContain('totalItems={paginationTotal}');
    expect(page).toMatch(
      /\{ totalItems: paginationTotal, totalPages \} = entityPagination\(/
    );
  });
});
