import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Mock } from 'vitest';
import { api } from '@/services/api-client';
import analyticsService from '@/services/analyticsService';
import {
  countInFlightDocuments,
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

  it('getPerformanceMetrics rejects instead of returning mock services (FL2C-1)', async () => {
    get.mockRejectedValue(new Error('404'));
    await expect(analyticsService.getPerformanceMetrics()).rejects.toThrow('404');
  });

  it('getPerformanceMetrics returns the response body as-is (FL2C-1)', async () => {
    const body = { metrics: {}, services: [], alerts: [] };
    get.mockResolvedValue(body);
    await expect(analyticsService.getPerformanceMetrics()).resolves.toBe(body);
  });

  it.each([
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
  beforeEach(() => {
    get.mockReset();
  });

  it('search volume comes from /charts/search_volume and reads `data`', async () => {
    route({
      '/analytics/performance/charts/search_volume': searchVolumeChartBody,
    });

    const points = await performanceApi.getSearchVolumeTrend();

    expect(get).toHaveBeenCalledWith(
      '/analytics/performance/charts/search_volume?time_range=30d&granularity=day'
    );
    expect(points).toEqual([
      { name: '2026-11-21', date: '2026-11-21T00:00:00', value: 4 },
      { name: '2026-11-22', date: '2026-11-22T00:00:00', value: 9 },
    ]);
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
    expect(points.map((p) => p.value)).toEqual([2, 4]);
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
