/**
 * FL2C-1 (GOO-649): the legacy analytics getters have no mock fallback. Each
 * one hands back the response body untouched and rethrows a failure, so a
 * caller renders "unavailable" instead of fabricated numbers.
 *
 * This pins that contract, not the URLs: several of these routes are not
 * mounted on the backend, and a test must not make them look supported.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Mock } from 'vitest';
import { api } from '@/services/api-client';
import analyticsService from '@/services/analyticsService';
import type { AnalyticsFilters, TimeRange } from '@/types';

vi.mock('@/services/api-client', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    download: vi.fn(),
  },
}));

const mocked = api as unknown as Record<'get' | 'post' | 'patch' | 'download', Mock>;

const preset = '7d' as unknown as TimeRange;
const custom = {
  start: '2026-11-01',
  end: '2026-11-07',
} as unknown as TimeRange;
const filters = {} as AnalyticsFilters;

type Case = [
  name: string,
  method: 'get' | 'post' | 'patch' | 'download',
  call: () => Promise<unknown>,
];

// Getters whose resolved value is the response body.
const returning: Case[] = [
  ['getQualityMetrics', 'get', () => analyticsService.getQualityMetrics()],
  ['getUserBehaviorStats', 'get', () => analyticsService.getUserBehaviorStats()],
  ['getRecommendations', 'get', () => analyticsService.getRecommendations()],
  [
    'getRAGTriadMetrics (preset)',
    'get',
    () => analyticsService.getRAGTriadMetrics(preset),
  ],
  [
    'getRAGTriadMetrics (custom)',
    'get',
    () => analyticsService.getRAGTriadMetrics(custom),
  ],
  [
    'getPerformanceAnalytics',
    'post',
    () => analyticsService.getPerformanceAnalytics(filters, preset),
  ],
  [
    'getUsageAnalytics (preset)',
    'get',
    () => analyticsService.getUsageAnalytics(preset),
  ],
  [
    'getUsageAnalytics (custom)',
    'get',
    () => analyticsService.getUsageAnalytics(custom),
  ],
  ['getRealTimeMetrics', 'get', () => analyticsService.getRealTimeMetrics()],
  [
    'getHistoricalTrends',
    'get',
    () => analyticsService.getHistoricalTrends('latency', custom),
  ],
  [
    'getComparisonData',
    'post',
    () => analyticsService.getComparisonData([preset, custom]),
  ],
];

// Mutations that resolve to nothing.
const voidCalls: Case[] = [
  [
    'updateRecommendationStatus',
    'patch',
    () => analyticsService.updateRecommendationStatus('r1', 'implemented' as never),
  ],
  [
    'voteOnRecommendation',
    'post',
    () => analyticsService.voteOnRecommendation('r1', 'up'),
  ],
  [
    'exportAnalytics',
    'download',
    () => analyticsService.exportAnalytics('csv', filters, preset),
  ],
];

describe('legacy analytics getters never fall back to mock data (FL2C-1)', () => {
  beforeEach(() => {
    for (const fn of Object.values(mocked)) fn.mockReset();
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  it.each(returning)('%s returns the response body as-is', async (_name, method, call) => {
    const body = { from: 'backend' };
    mocked[method].mockResolvedValue(body);

    await expect(call()).resolves.toBe(body);
    expect(mocked[method]).toHaveBeenCalledTimes(1);
  });

  it.each([...returning, ...voidCalls])(
    '%s rethrows a failure instead of inventing a value',
    async (_name, method, call) => {
      const failure = new Error('outage');
      mocked[method].mockRejectedValue(failure);

      await expect(call()).rejects.toBe(failure);
    }
  );

  it.each(voidCalls)('%s resolves once the request succeeds', async (_name, method, call) => {
    mocked[method].mockResolvedValue(undefined);

    await expect(call()).resolves.toBeUndefined();
    expect(mocked[method]).toHaveBeenCalledTimes(1);
  });
});
