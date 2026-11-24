import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Mock } from 'vitest';
import { renderHook } from '@testing-library/react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import analyticsService from '@/services/analyticsService';
import { useTimeRange } from '@/stores/analyticsStore';
import {
  useComparisonData,
  useExportAnalytics,
  useHistoricalTrends,
  useUsageAnalytics,
} from '../useAnalytics';

vi.mock('@tanstack/react-query', () => ({
  useQuery: vi.fn(),
  useMutation: vi.fn(),
  useQueryClient: vi.fn(),
}));

vi.mock('@/services/analyticsService', () => ({
  __esModule: true,
  default: {
    getUsageAnalytics: vi.fn(),
    getHistoricalTrends: vi.fn(),
    getComparisonData: vi.fn(),
    exportAnalytics: vi.fn(),
  },
}));

vi.mock('@/stores/analyticsStore', () => ({
  useAnalyticsStore: vi.fn(),
  useTimeRange: vi.fn(),
  useAnalyticsFilters: vi.fn(),
}));

type Options = {
  queryKey: unknown[];
  queryFn: () => unknown;
  select: (data: never) => Record<string, unknown>;
  enabled?: boolean;
};

const range = { start: '2026-11-01T00:00:00Z', end: '2026-11-08T00:00:00Z' };
const invalidateQueries = vi.fn();

/** The options the hook handed to useQuery on its last render. */
const lastQuery = (): Options => (useQuery as Mock).mock.calls.at(-1)![0];

beforeEach(() => {
  vi.clearAllMocks();
  (useQuery as Mock).mockReturnValue({ data: undefined });
  (useMutation as Mock).mockImplementation((options) => options);
  (useQueryClient as Mock).mockReturnValue({ invalidateQueries });
  (useTimeRange as unknown as Mock).mockReturnValue(range);
});

describe('useUsageAnalytics select', () => {
  const run = (data: object): Record<string, unknown> => {
    renderHook(() => useUsageAnalytics());
    return lastQuery().select(data as never);
  };

  it('fetches usage for the store time range', () => {
    renderHook(() => useUsageAnalytics());
    expect(lastQuery().queryKey).toEqual(['usage-analytics', range]);
    lastQuery().queryFn();
    expect(analyticsService.getUsageAnalytics).toHaveBeenCalledWith(range);
  });

  it.each([
    ['no behaviour data', undefined, []],
    ['no peak hours', { peak_usage_hours: [] }, []],
    ['some peak hours', { peak_usage_hours: [9, 14] }, [9, 14]],
    [
      'every hour a peak',
      { peak_usage_hours: Array.from({ length: 24 }, (_, h) => h) },
      Array.from({ length: 24 }, (_, h) => h),
    ],
  ])('maps the heatmap from peak hours: %s', (_label, behaviour, peaks) => {
    const out = run({ user_behavior: behaviour, popular_queries: [] });
    const heatmap = out.usageHeatmap as Array<{
      hour: number;
      intensity: number;
      label: string;
    }>;
    expect(heatmap).toHaveLength(24);
    expect(heatmap[13]).toMatchObject({ hour: 13, label: '13:00' });
    expect(heatmap.filter((h) => h.intensity === 1).map((h) => h.hour)).toEqual(peaks);
    expect(heatmap.every((h) => h.intensity === 0 || h.intensity === 1)).toBe(true);
  });

  it('summarises and buckets the popular queries by keyword', () => {
    const out = run({
      user_behavior: {},
      popular_queries: [
        { query: 'How to cite', frequency: 5 },
        { query: 'API limits', frequency: 3 },
        { query: 'code example', frequency: 2 },
        { query: 'best practice', frequency: 1 },
        { query: 'upload error', frequency: 4 },
        { query: 'zebra', frequency: 1 },
      ],
    });
    const patterns = out.queryPatterns as {
      averageQueryLength: number;
      uniqueQueries: number;
      totalQueries: number;
      topCategories: Record<string, Array<{ query: string }>>;
    };
    expect(patterns.uniqueQueries).toBe(6);
    expect(patterns.totalQueries).toBe(16);
    expect(patterns.averageQueryLength).toBe(Math.round((11 + 10 + 12 + 13 + 12 + 5) / 6));
    const names = (key: string): string[] => patterns.topCategories[key]!.map((q) => q.query);
    expect(names('General Inquiry')).toEqual(['How to cite']);
    expect(names('Technical Documentation')).toEqual(['API limits']);
    expect(names('Code Examples')).toEqual(['code example']);
    expect(names('Best Practices')).toEqual(['best practice']);
    expect(names('Troubleshooting')).toEqual(['upload error']);
  });
});

describe('useHistoricalTrends select', () => {
  it('builds the chart and summary from the series', () => {
    renderHook(() => useHistoricalTrends('faithfulness', range));
    const query = lastQuery();
    expect(query.enabled).toBe(true);
    query.queryFn();
    expect(analyticsService.getHistoricalTrends).toHaveBeenCalledWith('faithfulness', range);

    const out = query.select({
      metric_name: 'Faithfulness',
      timestamps: ['a', 'b', 'c'],
      values: [50, 80, 60],
    } as never);
    expect(out.chartData).toMatchObject({
      labels: ['a', 'b', 'c'],
      datasets: [{ label: 'Faithfulness', data: [50, 80, 60] }],
    });
    expect(out.summary).toEqual({ change: 20, average: 63.33, max: 80, min: 50, trend: 'improving' });
  });

  it('is disabled without a metric or with an incomplete range', () => {
    renderHook(() => useHistoricalTrends('', range));
    expect(lastQuery().enabled).toBe(false);
    renderHook(() => useHistoricalTrends('x', { start: '', end: '' } as never));
    expect(lastQuery().enabled).toBe(false);
  });
});

describe('useComparisonData select', () => {
  it('labels periods and names the best one', () => {
    const ranges = [range, range];
    renderHook(() => useComparisonData(ranges));
    const query = lastQuery();
    expect(query.enabled).toBe(true);
    query.queryFn();
    expect(analyticsService.getComparisonData).toHaveBeenCalledWith(ranges);

    const out = query.select({
      time_ranges: [
        { answer_relevancy: 60, faithfulness: 60, contextual_relevancy: 60 },
        { answer_relevancy: 90, faithfulness: 90, contextual_relevancy: 90 },
      ],
      answer_relevancy_comparison: [60, 90],
      faithfulness_comparison: [60, 90],
      contextual_relevancy_comparison: [60, 90],
    } as never);
    expect((out.comparisonChart as { labels: string[] }).labels).toEqual(['Period 1', 'Period 2']);
    expect(out.insights).toEqual([
      {
        type: 'performance',
        title: 'Best Performing Period',
        description:
          'Period 2 showed the best overall performance with an average score of 90.0%',
      },
    ]);
  });

  it('needs at least two periods', () => {
    renderHook(() => useComparisonData([range]));
    expect(lastQuery().enabled).toBe(false);
  });
});

describe('useExportAnalytics', () => {
  it('exports with the chosen format and refreshes usage afterwards', () => {
    const { result } = renderHook(() => useExportAnalytics());
    const mutation = result.current as unknown as {
      mutationFn: (args: object) => unknown;
      onSuccess: () => void;
    };
    const filters = { modalities: [] };
    mutation.mutationFn({ format: 'csv', filters, timeRange: range });
    expect(analyticsService.exportAnalytics).toHaveBeenCalledWith('csv', filters, range);
    expect(invalidateQueries).not.toHaveBeenCalled();
    mutation.onSuccess();
    expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: ['usage-analytics'] });
  });
});
