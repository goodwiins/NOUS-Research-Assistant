import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Mock } from 'vitest';
import { api } from '@/services/api-client';
import analyticsService from '@/services/analyticsService';
import {
  performanceApi,
  searchAnalyticsApi,
  userBehaviorApi,
} from '@/services/documentAnalyticsApi';

vi.mock('@/services/api-client', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    download: vi.fn(),
  },
}));

const get = api.get as Mock;

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

  it('combined search analytics leaves missing per-query fields null, never random (FL2C-2)', async () => {
    const random = vi.spyOn(Math, 'random');
    get.mockResolvedValue({
      total_searches: 4,
      avg_response_time: 12,
      success_rate: 1,
      top_queries: [{ query: 'graphs', count: 4 }],
      search_types: [{ type: 'hybrid', count: 4 }],
    });

    const result = await searchAnalyticsApi.getCombinedSearchAnalytics();

    expect(result.topQueries[0]).toMatchObject({
      query: 'graphs',
      results: null,
      clickRate: null,
      type: null,
      lastSearched: null,
    });
    expect(random).not.toHaveBeenCalled();
    random.mockRestore();
  });

  it('combined search analytics rejects when every source fails (FL2C-2)', async () => {
    get.mockRejectedValue(new Error('down'));
    await expect(searchAnalyticsApi.getCombinedSearchAnalytics()).rejects.toThrow('down');
  });

  it.each([
    ['getOrganizationTrends', () => userBehaviorApi.getOrganizationTrends(30)],
    ['getOrganizationUserSummary', () => userBehaviorApi.getOrganizationUserSummary()],
    ['getDashboardOverview', () => performanceApi.getDashboardOverview()],
    ['getSystemHealth', () => performanceApi.getSystemHealth()],
    ['getUserEngagement', () => performanceApi.getUserEngagement()],
    ['getRealtimeMetrics', () => performanceApi.getRealtimeMetrics()],
    ['getTrendData', () => performanceApi.getTrendData(30)],
  ])('%s rejects instead of turning a failure into zeros (FL2C-2)', async (_name, call) => {
    get.mockRejectedValue(new Error('outage'));
    await expect(call()).rejects.toThrow('outage');
  });
});
