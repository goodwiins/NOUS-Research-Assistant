/**
 * GOO-651 (FL2A-1): the knowledge-graph entity endpoints declare
 * `entity_types: List[EntityType]` as a query parameter. FastAPI reads that
 * from repeated keys; one comma-joined value is a single invalid enum and
 * 422s the whole request. The "Unknown type" sentinel must never be sent.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';

const mockGet = vi.fn();
vi.mock('@/services/api-client', () => ({
  api: { get: (...args: unknown[]) => mockGet(...args) },
}));

import { entityService } from '../entityService';
import {
  NULL_ENTITY_TYPE,
  needsClientEntityTypeFilter,
  serverEntityTypeFilter,
} from '@/components/entities/entityType';

const query = (url: string): URLSearchParams =>
  new URLSearchParams(url.split('?')[1] ?? '');

beforeEach(() => {
  mockGet.mockReset();
  mockGet.mockResolvedValue({ entities: [], total: 0, has_more: false });
});

describe('entityService entity_types query', () => {
  it('getEntities sends one repeated key per type', async () => {
    await entityService.getEntities(50, 0, ['PERSON', 'ORGANIZATION'] as never);
    const params = query(mockGet.mock.calls[0][0]);
    expect(params.getAll('entity_types')).toEqual(['PERSON', 'ORGANIZATION']);
    expect(params.get('limit')).toBe('50');
    expect(params.get('offset')).toBe('0');
  });

  it('getEntities omits entity_types when none are selected', async () => {
    await entityService.getEntities(50, 0, [] as never, true);
    const params = query(mockGet.mock.calls[0][0]);
    expect(params.has('entity_types')).toBe(false);
    expect(params.get('connected_only')).toBe('true');
  });

  it('searchEntities sends one repeated key per type', async () => {
    mockGet.mockResolvedValue([]);
    await entityService.searchEntities('ada', ['PERSON', 'CONCEPT'], 5);
    const params = query(mockGet.mock.calls[0][0]);
    expect(params.getAll('entity_types')).toEqual(['PERSON', 'CONCEPT']);
    expect(params.get('query')).toBe('ada');
    expect(params.get('limit')).toBe('5');
  });

  it('searchEntities omits entity_types when none are given', async () => {
    mockGet.mockResolvedValue([]);
    await entityService.searchEntities('ada');
    expect(query(mockGet.mock.calls[0][0]).has('entity_types')).toBe(false);
  });
});

describe('server entity-type filter', () => {
  it('passes plain selections through to the server', () => {
    expect(serverEntityTypeFilter(['PERSON', 'CONCEPT'])).toEqual([
      'PERSON',
      'CONCEPT',
    ]);
    expect(needsClientEntityTypeFilter(['PERSON'])).toBe(false);
  });

  it('sends no server type filter when nothing is selected', () => {
    expect(serverEntityTypeFilter([])).toBeUndefined();
  });

  it.each([[[NULL_ENTITY_TYPE]], [[NULL_ENTITY_TYPE, 'PERSON']]])(
    'never sends the Unknown-type sentinel (%j); filters on the client',
    (selected) => {
      expect(serverEntityTypeFilter(selected)).toBeUndefined();
      expect(needsClientEntityTypeFilter(selected)).toBe(true);
    }
  );
});
