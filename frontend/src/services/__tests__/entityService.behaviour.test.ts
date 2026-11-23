/**
 * entityService behaviour (GOO-742): the retry policy for idempotent GETs,
 * the relationship mapping, how failures are classified and logged, and the
 * request contract of the remaining calls.
 *
 * GOO-749: relationships and the type lists propagate every failure. There
 * is no empty list or built-in type list to fall back to, because either
 * would look like real data from the user's graph.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Mock } from 'vitest';
import { api } from '@/services/api-client';
import { entityService } from '@/services/entityService';
import { APIErrorClass } from '@/types/api';

vi.mock('@/services/api-client', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
  },
}));

type Method = 'get' | 'post' | 'put' | 'delete';
const mocked = api as unknown as Record<Method, Mock>;
const METHODS: Method[] = ['get', 'post', 'put', 'delete'];

const apiError = (
  status_code: number,
  extra: { message?: string; silent?: boolean } = {}
): APIErrorClass =>
  new APIErrorClass({
    message: extra.message ?? `HTTP ${status_code}`,
    status_code,
    type: 'http_error',
    silent: extra.silent,
  });

let warn: ReturnType<typeof vi.spyOn>;
let error: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  for (const fn of Object.values(mocked)) fn.mockReset();
  warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
  error = vi.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('retry policy for idempotent reads', () => {
  it('retries a 5xx once after a 1 s backoff and returns the retry body', async () => {
    vi.useFakeTimers();
    const body = [{ id: 'e2' }];
    mocked.get.mockRejectedValueOnce(apiError(503)).mockResolvedValueOnce(body);

    const pending = entityService.getRelatedEntities('e1');
    await vi.advanceTimersByTimeAsync(999);
    expect(mocked.get).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);

    await expect(pending).resolves.toBe(body);
    expect(mocked.get).toHaveBeenCalledTimes(2);
    expect(mocked.get).toHaveBeenCalledWith(
      '/knowledge-graph/entities/e1/related?max_depth=2&min_strength=0.1&limit=50'
    );
  });

  it('retries a 429 rate limit', async () => {
    vi.useFakeTimers();
    const body = { entities: [], relationships: [] };
    mocked.get.mockRejectedValueOnce(apiError(429)).mockResolvedValueOnce(body);

    const pending = entityService.getNeighborhood('e1', 3, 0.2, 10);
    await vi.advanceTimersByTimeAsync(1000);

    await expect(pending).resolves.toBe(body);
    expect(mocked.get).toHaveBeenCalledTimes(2);
    expect(mocked.get).toHaveBeenCalledWith(
      '/knowledge-graph/entities/e1/neighborhood?max_depth=3&min_strength=0.2&limit=10'
    );
  });

  it('retries a network error that is not an API error', async () => {
    vi.useFakeTimers();
    const body = { total_entities: 4 };
    mocked.get.mockRejectedValueOnce(new TypeError('fetch failed')).mockResolvedValueOnce(body);

    const pending = entityService.getAnalytics();
    await vi.advanceTimersByTimeAsync(1000);

    await expect(pending).resolves.toBe(body);
    expect(mocked.get).toHaveBeenCalledWith('/knowledge-graph/analytics');
  });

  it.each([400, 404, 409])('never retries a %i client error', async (status) => {
    const failure = apiError(status);
    mocked.get.mockRejectedValue(failure);

    await expect(entityService.findPaths('a', 'b')).rejects.toBe(failure);
    expect(mocked.get).toHaveBeenCalledTimes(1);
    expect(mocked.get).toHaveBeenCalledWith(
      '/knowledge-graph/paths/a/b?max_depth=3&min_strength=0.1'
    );
  });

  it('gives up after one retry and rejects with the last error', async () => {
    vi.useFakeTimers();
    const first = apiError(500, { message: 'first' });
    const last = apiError(502, { message: 'last' });
    mocked.get.mockRejectedValueOnce(first).mockRejectedValueOnce(last);

    const pending = entityService.getAnalytics();
    const assertion = expect(pending).rejects.toBe(last);
    await vi.advanceTimersByTimeAsync(1000);

    await assertion;
    expect(mocked.get).toHaveBeenCalledTimes(2);
  });

  it.each([
    ['createEntity', () => entityService.createEntity({ name: 'x' }), 'post'],
    [
      'createRelationship',
      () =>
        entityService.createRelationship({
          source_entity_id: 'a',
          target_entity_id: 'b',
          relationship_type: 'KNOWS',
        }),
      'post',
    ],
    ['updateEntity', () => entityService.updateEntity('e1', { name: 'y' }), 'put'],
  ] as const)('%s is not retried on a 5xx (non-idempotent)', async (_name, call, method) => {
    const failure = apiError(503);
    mocked[method].mockRejectedValue(failure);

    await expect(call()).rejects.toBe(failure);
    expect(mocked[method]).toHaveBeenCalledTimes(1);
  });
});

describe('getAllRelationships', () => {
  it('maps the backend fields onto GraphEdge', async () => {
    mocked.get.mockResolvedValue([
      {
        id: 'r1',
        source_entity_id: 'a',
        target_entity_id: 'b',
        relationship_type: 'KNOWS',
        strength: 0.7,
        confidence_score: 0.9,
        context: 'ctx',
        metadata: { k: 1 },
      },
    ]);

    const edges = await entityService.getAllRelationships(10, 20);

    expect(mocked.get).toHaveBeenCalledWith('/knowledge-graph/relationships?limit=10&offset=20');
    expect(edges).toEqual([
      {
        id: 'r1',
        source: 'a',
        target: 'b',
        type: 'KNOWS',
        weight: 0.7,
        strength: 0.7,
        confidence: 0.9,
        context: 'ctx',
        metadata: { k: 1 },
      },
    ]);
  });

  it('uses the default page and treats a null body as no relationships', async () => {
    mocked.get.mockResolvedValue(null);

    await expect(entityService.getAllRelationships()).resolves.toEqual([]);
    expect(mocked.get).toHaveBeenCalledWith('/knowledge-graph/relationships?limit=200&offset=0');
  });

  it.each([
    ['a 401', apiError(401)],
    ['a 403', apiError(403)],
    ['a 500', apiError(500)],
    ['a 503', apiError(503)],
    ['a network error', new TypeError('fetch failed')],
  ])('rejects with %s instead of returning no relationships', async (_case, failure) => {
    mocked.get.mockRejectedValue(failure);

    await expect(entityService.getAllRelationships()).rejects.toBe(failure);
    expect(mocked.get).toHaveBeenCalledTimes(1);
    // The caller reports it; the service does not log a second copy.
    expect(warn).not.toHaveBeenCalled();
    expect(error).not.toHaveBeenCalled();
  });
});

describe('failure logging: expected failures warn, others error', () => {
  it.each([
    ['a 401', apiError(401)],
    ['a 403', apiError(403)],
    ['a 503', apiError(503)],
    ['a silent error', apiError(500, { silent: true })],
    ['a "Service Unavailable" message', apiError(500, { message: 'Service Unavailable' })],
    ['a circuit-breaker message', apiError(500, { message: 'Circuit breaker open' })],
  ])('%s is logged as a warning', async (_case, failure) => {
    mocked.get.mockRejectedValue(failure);

    await expect(entityService.getVisualizationData('e1')).rejects.toBe(failure);

    expect(warn).toHaveBeenCalledWith('Error fetching visualization data:', failure);
    expect(error).not.toHaveBeenCalled();
  });

  it.each([
    ['a 500', apiError(500)],
    ['a plain Error', new Error('boom')],
  ])('%s is logged as an error', async (_case, failure) => {
    mocked.get.mockRejectedValue(failure);

    await expect(entityService.getVisualizationData('e1')).rejects.toBe(failure);

    expect(error).toHaveBeenCalledWith('Error fetching visualization data:', failure);
    expect(warn).not.toHaveBeenCalled();
  });
});

describe('type lists', () => {
  it('getEntityTypes returns the backend list', async () => {
    mocked.get.mockResolvedValue(['PERSON', 'GENE']);

    await expect(entityService.getEntityTypes()).resolves.toEqual(['PERSON', 'GENE']);
    expect(mocked.get).toHaveBeenCalledWith('/knowledge-graph/entity-types');
  });

  it('getEntityTypes retries a 5xx once, then rejects with no built-in list', async () => {
    vi.useFakeTimers();
    const first = apiError(500, { message: 'first' });
    const last = apiError(503, { message: 'last' });
    mocked.get.mockRejectedValueOnce(first).mockRejectedValueOnce(last);

    const pending = entityService.getEntityTypes();
    const assertion = expect(pending).rejects.toBe(last);
    await vi.advanceTimersByTimeAsync(1000);

    await assertion;
    expect(mocked.get).toHaveBeenCalledTimes(2);
    expect(mocked.get).toHaveBeenNthCalledWith(2, '/knowledge-graph/entity-types');
  });

  it('getEntityTypes rejects a 403 at once', async () => {
    const failure = apiError(403);
    mocked.get.mockRejectedValue(failure);

    await expect(entityService.getEntityTypes()).rejects.toBe(failure);
    expect(mocked.get).toHaveBeenCalledTimes(1);
  });

  it('getRelationshipTypes returns the backend list', async () => {
    mocked.get.mockResolvedValue(['KNOWS']);

    await expect(entityService.getRelationshipTypes()).resolves.toEqual(['KNOWS']);
    expect(mocked.get).toHaveBeenCalledWith('/knowledge-graph/relationship-types');
  });

  it('getRelationshipTypes rejects without retrying or inventing a list', async () => {
    const failure = apiError(503);
    mocked.get.mockRejectedValue(failure);

    await expect(entityService.getRelationshipTypes()).rejects.toBe(failure);
    expect(mocked.get).toHaveBeenCalledTimes(1);
  });
});

describe('calls that log and rethrow', () => {
  it.each([
    [
      'getVisualizationData',
      () => entityService.getVisualizationData('e1'),
      'get',
      ['/knowledge-graph/visualization/e1?depth=2&max_nodes=50'],
      'Error fetching visualization data:',
    ],
    [
      'searchGraph',
      () => entityService.searchGraph({ query: 'q', max_depth: 2 }),
      'post',
      ['/knowledge-graph/search', { query: 'q', max_depth: 2 }],
      'Error performing graph search:',
    ],
    [
      'batchCreate',
      () => entityService.batchCreate({ entities: [{ name: 'x' }], upsert: true }),
      'post',
      ['/knowledge-graph/batch', { entities: [{ name: 'x' }], upsert: true }],
      'Error in batch create:',
    ],
  ] as const)('%s', async (_name, call, method, args, context) => {
    const body = { ok: true };
    mocked[method].mockResolvedValueOnce(body);
    await expect(call()).resolves.toBe(body);
    expect(mocked[method]).toHaveBeenCalledWith(...args);

    const failure = new Error('boom');
    mocked[method].mockRejectedValueOnce(failure);
    await expect(call()).rejects.toBe(failure);
    expect(error).toHaveBeenCalledWith(context, failure);
  });
});

describe('request contract', () => {
  const cases: Array<{
    name: string;
    call: () => Promise<unknown>;
    method: Method;
    args: unknown[];
    returnsBody: boolean;
  }> = [
    {
      name: 'getEntity',
      call: () => entityService.getEntity('e1'),
      method: 'get',
      args: ['/knowledge-graph/entities/e1'],
      returnsBody: true,
    },
    {
      name: 'createEntity',
      call: () => entityService.createEntity({ name: 'x' }),
      method: 'post',
      args: ['/knowledge-graph/entities', { name: 'x' }],
      returnsBody: true,
    },
    {
      name: 'updateEntity uses PUT',
      call: () => entityService.updateEntity('e1', { name: 'y' }),
      method: 'put',
      args: ['/knowledge-graph/entities/e1', { name: 'y' }],
      returnsBody: true,
    },
    {
      name: 'deleteEntity',
      call: () => entityService.deleteEntity('e1'),
      method: 'delete',
      args: ['/knowledge-graph/entities/e1'],
      returnsBody: false,
    },
    {
      name: 'getEntityRelationships with the default limit only',
      call: () => entityService.getEntityRelationships('e1'),
      method: 'get',
      args: ['/knowledge-graph/entities/e1/relationships?limit=50'],
      returnsBody: true,
    },
    {
      name: 'getEntityRelationships filtered by type',
      call: () => entityService.getEntityRelationships('e1', 'KNOWS', 5),
      method: 'get',
      args: ['/knowledge-graph/entities/e1/relationships?relationship_type=KNOWS&limit=5'],
      returnsBody: true,
    },
    {
      name: 'createRelationship',
      call: () =>
        entityService.createRelationship({
          source_entity_id: 'a',
          target_entity_id: 'b',
          relationship_type: 'KNOWS',
        }),
      method: 'post',
      args: [
        '/knowledge-graph/relationships',
        { source_entity_id: 'a', target_entity_id: 'b', relationship_type: 'KNOWS' },
      ],
      returnsBody: true,
    },
    {
      name: 'deleteRelationship',
      call: () => entityService.deleteRelationship('r1'),
      method: 'delete',
      args: ['/knowledge-graph/relationships/r1'],
      returnsBody: false,
    },
    {
      name: 'createMergeJob wraps the groups',
      call: () =>
        entityService.createMergeJob([
          { entities: [{ id: 'a', name: 'A' }], suggested_primary: 'a' },
        ]),
      method: 'post',
      args: [
        '/knowledge-graph/merge-jobs',
        { groups: [{ entities: [{ id: 'a', name: 'A' }], suggested_primary: 'a' }] },
      ],
      returnsBody: true,
    },
    {
      name: 'createExtractionJob sends document_ids',
      call: () => entityService.createExtractionJob(['d1', 'd2']),
      method: 'post',
      args: ['/knowledge-graph/extraction-jobs', { document_ids: ['d1', 'd2'] }],
      returnsBody: true,
    },
    {
      name: 'getProcessingJob reads the processing route',
      call: () => entityService.getProcessingJob('j1'),
      method: 'get',
      args: ['/processing/jobs/j1'],
      returnsBody: true,
    },
  ];

  it.each(cases)('$name', async ({ call, method, args, returnsBody }) => {
    const body = { from: 'backend' };
    mocked[method].mockResolvedValue(body);

    const result = await call();

    expect(mocked[method]).toHaveBeenCalledTimes(1);
    expect(mocked[method]).toHaveBeenCalledWith(...args);
    for (const other of METHODS.filter((m) => m !== method)) {
      expect(mocked[other]).not.toHaveBeenCalled();
    }
    if (returnsBody) expect(result).toBe(body);
    else expect(result).toBeUndefined();
  });

  it.each(cases)('$name rejects with the request error', async ({ call, method }) => {
    const failure = apiError(404);
    mocked[method].mockRejectedValue(failure);

    await expect(call()).rejects.toBe(failure);
  });
});
