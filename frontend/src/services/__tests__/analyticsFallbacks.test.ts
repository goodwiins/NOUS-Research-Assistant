import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Mock } from 'vitest';
import { api } from '@/services/api-client';
import {
  countInFlightDocuments,
  documentAnalyticsApi,
  parseBackendTimestamp,
  performanceApi,
  searchAnalyticsApi,
  userBehaviorApi,
} from '@/services/documentAnalyticsApi';
import {
  adminOverviewBody,
  memberOverviewBody,
  operatorOverviewBody,
  organizationTrendsBody,
  searchAnalyticsBody,
  searchPerformanceBody,
  searchVolumeChartBody,
} from './fixtures/analyticsBackend';

vi.mock('@/services/api-client', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    download: vi.fn(),
  },
}));

const get = api.get as Mock;

/**
 * Run the enclosing describe block in `zone`. CI runners are UTC, where a
 * local-time bug gives the same answer as the UTC code, so tests that guard
 * against local-time parsing or day cuts pin a non-UTC zone (Node applies a
 * runtime `process.env.TZ` change to Date).
 */
function pinTimeZone(zone: string): void {
  let previous: string | undefined;
  beforeAll(() => {
    previous = process.env.TZ;
    process.env.TZ = zone;
  });
  afterAll(() => {
    if (previous === undefined) delete process.env.TZ;
    else process.env.TZ = previous;
  });
}

/** "Now" for the windowed chart: D is 2026-11-23 UTC, so D-6 is 11-17. */
const NOW = new Date('2026-11-23T05:00:00Z');

/** Answer each request by path prefix; anything unlisted fails like a 404. */
function route(table: Record<string, unknown>): void {
  get.mockImplementation(async (url: string) => {
    const match = Object.keys(table).find((prefix) => url.startsWith(prefix));
    if (!match) throw new Error(`404 ${url}`);
    const body = table[match];
    if (body instanceof Error) throw body;
    return body;
  });
}

describe('analytics services never fabricate data', () => {
  beforeEach(() => {
    get.mockReset();
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  it.each([
    ['getFileStats', () => documentAnalyticsApi.getFileStats()],
    ['getDashboardOverview', () => performanceApi.getDashboardOverview()],
    ['getSearchVolumeTrend', () => performanceApi.getSearchVolumeTrend()],
    [
      'getOrganizationActiveUserTrend',
      () => userBehaviorApi.getOrganizationActiveUserTrend(30),
    ],
    [
      'getCombinedSearchAnalytics',
      () => searchAnalyticsApi.getCombinedSearchAnalytics(),
    ],
  ])('%s rejects instead of turning a failure into zeros (FL2C-2)', async (_name, call) => {
    get.mockRejectedValue(new Error('outage'));
    await expect(call()).rejects.toThrow('outage');
  });
});

describe('an unmeasured search time stays null, never 0 (GOO-745)', () => {
  beforeEach(() => {
    get.mockReset();
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  it('overview: a null avg_response_time maps to null', async () => {
    route({
      '/analytics/performance/overview': {
        ...memberOverviewBody,
        search_performance: {
          ...memberOverviewBody.search_performance,
          avg_response_time: null,
        },
      },
    });

    const overview = await performanceApi.getDashboardOverview();

    expect(overview.avgSearchTimeMs).toBeNull();
    expect(overview.totalSearches).toBe(37);
  });

  it('combined: a null performance average stays null and does not fall back', async () => {
    route({
      '/analytics/performance/search-performance': {
        ...searchPerformanceBody,
        avg_response_time: null,
        top_queries: [{ query: 'q', count: 2, avg_response_time: null }],
      },
      '/search/analytics': searchAnalyticsBody,
    });

    const combined = await searchAnalyticsApi.getCombinedSearchAnalytics();

    expect(combined.avgResponseTimeMs).toBeNull();
    expect(combined.topQueries[0].avgResponseTimeMs).toBeNull();
    expect(combined.totalSearches).toBe(searchPerformanceBody.total_searches);
    expect(get).toHaveBeenCalledTimes(1);
  });

  it.each([
    ['missing', undefined],
    ['a string', '12'],
    ['NaN', Number.NaN],
  ])('still rejects an avg_response_time that is %s', async (_label, value) => {
    route({
      '/analytics/performance/overview': {
        ...memberOverviewBody,
        search_performance: {
          ...memberOverviewBody.search_performance,
          avg_response_time: value,
        },
      },
    });
    await expect(performanceApi.getDashboardOverview()).rejects.toThrow(
      /avg_response_time/
    );
  });
});

describe('getDashboardOverview reads the nested backend shape (GOO-649)', () => {
  beforeEach(() => {
    get.mockReset();
  });

  // The three shapes `_scope_overview_for` sends on develop (GOO-633).
  it('member: reads search_performance and reports engagement as absent', async () => {
    route({ '/analytics/performance/overview': memberOverviewBody });

    const overview = await performanceApi.getDashboardOverview();

    expect(get).toHaveBeenCalledWith(
      '/analytics/performance/overview?time_range=24h'
    );
    expect(overview).toEqual({
      timeRange: '24h',
      totalSearches: 37,
      avgSearchTimeMs: 212.4,
      engagement: null,
    });
  });

  it('org admin: also maps user_engagement', async () => {
    route({ '/analytics/performance/overview': adminOverviewBody });

    const overview = await performanceApi.getDashboardOverview();

    expect(overview).toEqual({
      timeRange: '24h',
      totalSearches: 37,
      avgSearchTimeMs: 212.4,
      engagement: { activeUsers: 5, searchSessions: 11 },
    });
  });

  it('platform operator: maps the same figures and never passes host metrics through', async () => {
    route({ '/analytics/performance/overview': operatorOverviewBody });

    const overview = await performanceApi.getDashboardOverview();

    expect(overview.engagement).toEqual({ activeUsers: 5, searchSessions: 11 });
    expect(JSON.stringify(overview)).not.toMatch(/cpu|memory|disk|uptime|network/i);
  });

  it('rejects a flat body instead of reading zeros from it', async () => {
    // The shape the old getter assumed; the backend never sends it.
    route({
      '/analytics/performance/overview': {
        total_users: 0,
        total_searches: 0,
        total_sessions: 0,
      },
    });

    await expect(performanceApi.getDashboardOverview()).rejects.toThrow(
      /search_performance/
    );
  });

  it('rejects a member body whose search_performance is malformed', async () => {
    route({
      '/analytics/performance/overview': {
        ...memberOverviewBody,
        search_performance: { avg_response_time: 1 },
      },
    });

    await expect(performanceApi.getDashboardOverview()).rejects.toThrow(
      /total_searches/
    );
  });

  it.each([
    ['a string', 'n/a'],
    ['null', null],
    ['an array', []],
  ])('rejects user_engagement that is %s, not the section', async (_kind, value) => {
    route({
      '/analytics/performance/overview': {
        ...memberOverviewBody,
        user_engagement: value,
      },
    });

    await expect(performanceApi.getDashboardOverview()).rejects.toThrow(
      /user_engagement/
    );
  });

  it('rejects user_engagement without total_sessions instead of reading 0', async () => {
    route({
      '/analytics/performance/overview': {
        ...adminOverviewBody,
        user_engagement: { ...adminOverviewBody.user_engagement, total_sessions: undefined },
      },
    });

    await expect(performanceApi.getDashboardOverview()).rejects.toThrow(
      /total_sessions/
    );
  });

  it('rejects a present but malformed user_engagement', async () => {
    route({
      '/analytics/performance/overview': {
        ...adminOverviewBody,
        user_engagement: { total_sessions: 11 },
      },
    });

    await expect(performanceApi.getDashboardOverview()).rejects.toThrow(
      /active_users/
    );
  });
});

describe('trend getters call valid routes with the right parameters (GOO-649)', () => {
  // Behind UTC: a period read as local time, or labelled with the local date,
  // lands on the previous day here.
  pinTimeZone('America/Los_Angeles');

  it('the pin is in effect: midnight UTC is the previous local day', () => {
    expect(new Date('2026-11-21T00:00:00Z').getDate()).toBe(20);
  });

  beforeEach(() => {
    get.mockReset();
  });

  it('search volume asks for the 7-day range, the only one the producer buckets by day', async () => {
    route({
      '/analytics/performance/charts/search_volume': searchVolumeChartBody,
    });

    const points = await performanceApi.getSearchVolumeTrend(NOW);

    expect(get).toHaveBeenCalledWith(
      '/analytics/performance/charts/search_volume?time_range=7d&granularity=day'
    );
    expect(points).toEqual([
      { name: '2026-11-17', date: '2026-11-17T00:00:00.000Z', value: 3 },
      { name: '2026-11-21', date: '2026-11-21T00:00:00.000Z', value: 4 },
      { name: '2026-11-22', date: '2026-11-22T00:00:00.000Z', value: 9 },
    ]);
  });

  it('drops the partial D-7 bucket the utcnow()-7d cutoff lets in (8 buckets)', async () => {
    // An active org: 8 daily buckets, the first only partly inside the window.
    const days = [16, 17, 18, 19, 20, 21, 22, 23];
    route({
      '/analytics/performance/charts/search_volume': {
        ...searchVolumeChartBody,
        data: days.map((day, i) => ({
          timestamp: `2026-11-${day}T00:00:00+00:00`,
          value: i === 0 ? 1 : 10,
        })),
      },
    });

    const points = await performanceApi.getSearchVolumeTrend(NOW);

    expect(points.map((p) => p.name)).toEqual([
      '2026-11-17',
      '2026-11-18',
      '2026-11-19',
      '2026-11-20',
      '2026-11-21',
      '2026-11-22',
      '2026-11-23',
    ]);
    expect(points.every((p) => p.value === 10)).toBe(true);
  });

  it('keeps a sparse series as-is: days without searches stay absent', async () => {
    route({
      '/analytics/performance/charts/search_volume': {
        ...searchVolumeChartBody,
        data: [
          { timestamp: '2026-11-19T00:00:00+00:00', value: 2.0 },
          { timestamp: '2026-11-23T00:00:00+00:00', value: 5.0 },
        ],
      },
    });

    const points = await performanceApi.getSearchVolumeTrend(NOW);

    expect(points.map((p) => [p.name, p.value])).toEqual([
      ['2026-11-19', 2],
      ['2026-11-23', 5],
    ]);
  });

  it('cuts on the UTC day, not the local one, just after midnight UTC', async () => {
    route({
      '/analytics/performance/charts/search_volume': {
        ...searchVolumeChartBody,
        data: [
          { timestamp: '2026-11-16T00:00:00+00:00', value: 1.0 },
          { timestamp: '2026-11-17T00:00:00+00:00', value: 3.0 },
        ],
      },
    });

    const points = await performanceApi.getSearchVolumeTrend(
      new Date('2026-11-23T00:30:00Z')
    );

    expect(points.map((p) => p.name)).toEqual(['2026-11-17']);
  });

  it('at exactly midnight UTC, the D-7 bucket is still out (cut is D-6 midnight, not now-7d)', async () => {
    route({
      '/analytics/performance/charts/search_volume': {
        ...searchVolumeChartBody,
        data: [
          { timestamp: '2026-11-16T00:00:00+00:00', value: 1.0 },
          { timestamp: '2026-11-17T00:00:00+00:00', value: 3.0 },
        ],
      },
    });

    const points = await performanceApi.getSearchVolumeTrend(
      new Date('2026-11-23T00:00:00Z')
    );

    expect(points.map((p) => p.name)).toEqual(['2026-11-17']);
  });

  // CI runners are UTC, where a local-day cut equals the UTC one. Pin a zone
  // ahead of UTC so a local-day cut gives a different answer on every machine.
  describe('with the viewer in Europe/Paris', () => {
    pinTimeZone('Europe/Paris');

    it('the pin is in effect: 23:30Z is already the next local day', () => {
      expect(new Date('2026-11-23T23:30:00Z').getDate()).toBe(24);
    });

    it('late in the UTC day the window is still D-6..D, whatever the local date', async () => {
      // 23:30Z is already the 24th in Europe/Paris: a local-day cut would drop 11-17.
      route({
        '/analytics/performance/charts/search_volume': {
          ...searchVolumeChartBody,
          data: [
            { timestamp: '2026-11-17T00:00:00+00:00', value: 3.0 },
            { timestamp: '2026-11-23T00:00:00+00:00', value: 5.0 },
          ],
        },
      });

      const points = await performanceApi.getSearchVolumeTrend(
        new Date('2026-11-23T23:30:00Z')
      );

      expect(points.map((p) => p.name)).toEqual(['2026-11-17', '2026-11-23']);
    });
  });

  it('rejects a chart point without a usable timestamp', async () => {
    route({
      '/analytics/performance/charts/search_volume': {
        ...searchVolumeChartBody,
        data: [{ timestamp: 'yesterday', value: 1 }],
      },
    });
    await expect(performanceApi.getSearchVolumeTrend()).rejects.toThrow(/period/);
  });

  it('rejects a chart body without a data array', async () => {
    route({ '/analytics/performance/charts/search_volume': [] });
    await expect(performanceApi.getSearchVolumeTrend()).rejects.toThrow(/data/);
  });

  it('organization trends send days_back and read engagement_trends', async () => {
    route({ '/analytics/behavior/organization/trends': organizationTrendsBody });

    const points = await userBehaviorApi.getOrganizationActiveUserTrend(30);

    expect(get).toHaveBeenCalledWith(
      '/analytics/behavior/organization/trends?days_back=30&group_by=day'
    );
    expect(points).toEqual([
      { name: '2026-11-21', date: '2026-11-21T00:00:00.000Z', value: 2 },
      { name: '2026-11-22', date: '2026-11-22T00:00:00.000Z', value: 4 },
    ]);
  });
});

// Every expectation here must hold whatever the runner's zone is; the block
// pins one behind UTC so a local-time parse fails even on a UTC runner.
describe('parseBackendTimestamp reads backend periods as UTC (GOO-649 F2)', () => {
  pinTimeZone('America/Los_Angeles');

  it.each([
    ['isoformat with offset', '2026-11-21T00:00:00+00:00', '2026-11-21T00:00:00.000Z'],
    ['str() with offset', '2026-11-21 00:00:00+00:00', '2026-11-21T00:00:00.000Z'],
    ['naive isoformat is UTC', '2026-11-21T00:00:00', '2026-11-21T00:00:00.000Z'],
    ['naive str() is UTC', '2026-11-21 00:00:00', '2026-11-21T00:00:00.000Z'],
    ['Z suffix', '2026-11-21T00:00:00Z', '2026-11-21T00:00:00.000Z'],
    ['a non-UTC offset is honoured', '2026-11-21T00:00:00+01:00', '2026-11-20T23:00:00.000Z'],
  ])('%s', (_case, raw, expected) => {
    expect(parseBackendTimestamp(raw).toISOString()).toBe(expected);
  });

  it.each([[''], ['soon'], [null], [42]])('rejects %j', (raw) => {
    expect(() => parseBackendTimestamp(raw)).toThrow(/period/);
  });
});

describe('getFileStats rejects a body that would read as 0 (GOO-649 F4)', () => {
  beforeEach(() => {
    get.mockReset();
  });

  it('returns a well-formed body', async () => {
    const body = {
      files_by_type: [{ type: 'pdf', count: 2, total_size_mb: 1 }],
      processing_stats: [{ status: 'pending', count: 1 }],
    };
    route({ '/files/stats': body });
    await expect(documentAnalyticsApi.getFileStats()).resolves.toBe(body);
  });

  it.each([
    ['without processing_stats', { files_by_type: [] }, /processing_stats/],
    ['without files_by_type', { processing_stats: [] }, /files_by_type/],
    [
      'with a non-numeric processing count',
      { files_by_type: [], processing_stats: [{ status: 'pending' }] },
      /processing_stats.count/,
    ],
    [
      'with a non-numeric type count',
      { files_by_type: [{ type: 'pdf', count: '2' }], processing_stats: [] },
      /files_by_type.count/,
    ],
  ])('%s', async (_case, body, error) => {
    route({ '/files/stats': body });
    await expect(documentAnalyticsApi.getFileStats()).rejects.toThrow(error);
  });
});

describe('combined search analytics reads each source’s real keys (GOO-649)', () => {
  beforeEach(() => {
    get.mockReset();
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  it('maps /search-performance without inventing per-query values', async () => {
    const random = vi.spyOn(Math, 'random');
    route({ '/analytics/performance/search-performance': searchPerformanceBody });

    const result = await searchAnalyticsApi.getCombinedSearchAnalytics();

    expect(get).toHaveBeenCalledWith(
      '/analytics/performance/search-performance?time_range=7d'
    );
    expect(result).toEqual({
      topQueries: [
        { id: 'search_1', query: 'transformers', count: 14, avgResponseTimeMs: 199.2 },
        { id: 'search_2', query: 'diffusion', count: 6, avgResponseTimeMs: 250 },
      ],
      searchTypes: [
        { name: 'Hybrid', value: 100 },
        { name: 'Keyword', value: 20 },
      ],
      totalSearches: 120,
      avgResponseTimeMs: 230.6,
    });
    expect(random).not.toHaveBeenCalled();
    random.mockRestore();
  });

  it('falls back to /search/analytics and reads its SearchAnalytics keys', async () => {
    route({
      '/analytics/performance/search-performance': new Error('500'),
      '/search/analytics': searchAnalyticsBody,
    });

    const result = await searchAnalyticsApi.getCombinedSearchAnalytics();

    expect(get).toHaveBeenCalledWith('/search/analytics?days=7');
    expect(result).toEqual({
      topQueries: [
        { id: 'search_1', query: 'protein folding', count: 8, avgResponseTimeMs: null },
      ],
      searchTypes: [
        { name: 'Hybrid', value: 30 },
        { name: 'Unknown', value: 12 },
      ],
      totalSearches: 42,
      avgResponseTimeMs: 310,
    });
  });

  it('rejects when the fallback lacks its keys instead of reporting 0 searches', async () => {
    route({
      '/analytics/performance/search-performance': new Error('500'),
      // The keys the old fallback read; the backend never sends them.
      '/search/analytics': {
        total_searches: 3,
        top_queries: [],
        search_types: [],
        avg_response_time_ms: 1,
      },
    });

    await expect(searchAnalyticsApi.getCombinedSearchAnalytics()).rejects.toThrow(
      /most_common_queries/
    );
  });
});

describe('countInFlightDocuments', () => {
  it('counts the raw stored statuses still being worked on', () => {
    expect(
      countInFlightDocuments([
        { status: 'pending', count: 2 },
        { status: 'processing', count: 3 },
        { status: 'retrying', count: 1 },
        { status: 'completed', count: 40 },
        { status: 'failed', count: 5 },
      ])
    ).toBe(6);
  });
});
