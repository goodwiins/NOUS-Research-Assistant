import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Mock } from 'vitest';
import { act, screen, waitFor } from '@testing-library/react';
import { render } from '@/test/test-utils';
import {
  AnalyticsDashboard,
  fetchAnalyticsDashboard,
} from '@/components/analytics/AnalyticsDashboard';
import { api } from '@/services/api-client';
import {
  adminOverviewBody,
  memberOverviewBody,
  organizationTrendsBody,
  searchPerformanceBody,
  searchVolumeChartBody,
} from '@/services/__tests__/fixtures/analyticsBackend';

vi.mock('@/services/api-client', () => ({
  api: { get: vi.fn() },
}));

const auth = vi.hoisted(() => ({
  role: 'user' as 'admin' | 'user',
  isLoading: false,
}));
vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => ({
    user: auth.isLoading ? null : { id: 'me', role: auth.role },
    isLoading: auth.isLoading,
  }),
}));

// Record each chart's props, then render the real chart.
const charts = vi.hoisted(() => ({ props: [] as Array<Record<string, unknown>> }));
vi.mock('@/components/analytics/AnalyticsChart', async (importOriginal) => {
  const actual =
    await importOriginal<typeof import('@/components/analytics/AnalyticsChart')>();
  const Recorded = (
    props: Parameters<typeof actual.AnalyticsChart>[0]
  ): ReturnType<typeof actual.AnalyticsChart> => {
    charts.props.push(props as unknown as Record<string, unknown>);
    return actual.AnalyticsChart(props);
  };
  return { ...actual, AnalyticsChart: Recorded };
});

const get = api.get as Mock;

// backend/src/services/documents/file_service.py: FileService.get_file_stats.
const fileStatsBody = {
  files_by_type: [{ type: 'pdf', count: 3, total_size_mb: 1.5 }],
  processing_stats: [
    { status: 'pending', count: 1 },
    { status: 'completed', count: 2 },
  ],
};
const documentListBody = {
  documents: [],
  pagination: {
    page: 1,
    page_size: 25,
    total: 0,
    total_pages: 0,
    has_next: false,
    has_prev: false,
  },
};

const routesFor = (overviewBody: unknown): Record<string, unknown> => ({
  '/files/stats': fileStatsBody,
  '/documents': documentListBody,
  '/analytics/performance/search-performance': searchPerformanceBody,
  '/analytics/performance/overview': overviewBody,
  '/analytics/performance/charts/search_volume': searchVolumeChartBody,
  '/analytics/behavior/organization/trends': organizationTrendsBody,
});

function route(table: Record<string, unknown>): void {
  get.mockImplementation(async (url: string) => {
    const match = Object.keys(table).find((prefix) => url.startsWith(prefix));
    if (!match) throw new Error(`404 ${url}`);
    return table[match];
  });
}

const requested = (): string[] => get.mock.calls.map(([url]) => String(url));

describe('AnalyticsDashboard (GOO-649)', () => {
  beforeEach(() => {
    get.mockReset();
    charts.props = [];
    auth.role = 'user';
    auth.isLoading = false;
    vi.spyOn(console, 'error').mockImplementation(() => {});
    // Only Date is faked: "today" is 2026-11-23 UTC, so the chart's window is
    // 2026-11-17 through 2026-11-23. Timers stay real for react-query.
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-11-23T05:00:00Z'));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('shows an outage as unavailable, never as zeros', async () => {
    get.mockRejectedValue(new Error('outage'));

    render(<AnalyticsDashboard />);

    expect(
      await screen.findByText(/Some data couldn.t be loaded/)
    ).toBeInTheDocument();
    expect(screen.getAllByText('Unavailable right now').length).toBeGreaterThanOrEqual(3);
    expect(screen.queryByText('0')).not.toBeInTheDocument();
    expect(
      screen.getByText('Search volume is unavailable right now.')
    ).toBeInTheDocument();
  });

  it('renders the backend figures for a member and never asks for admin trends', async () => {
    // On develop a member's /overview has no user_engagement (GOO-633).
    route(routesFor(memberOverviewBody));

    render(<AnalyticsDashboard />);

    // 120 searches over 7 days from /search-performance; 3 indexed documents.
    expect(await screen.findByText('120')).toBeInTheDocument();
    expect(screen.getAllByText('3').length).toBeGreaterThan(0);
    expect(
      screen.getByText('Active-user trends are available to organization admins.')
    ).toBeInTheDocument();
    expect(screen.queryByText(/Some data couldn.t be loaded/)).not.toBeInTheDocument();
    expect(screen.queryByText('Unavailable right now')).not.toBeInTheDocument();
    expect(screen.queryByText('Active users, last 24 hours')).not.toBeInTheDocument();
    expect(requested().some((url) => url.startsWith('/analytics/behavior'))).toBe(false);
    expect(requested()).toContain(
      '/analytics/performance/charts/search_volume?time_range=7d&granularity=day'
    );
  });

  it('shows an untimed average search time as not measured, not as an outage (GOO-768)', async () => {
    route(
      routesFor({
        ...memberOverviewBody,
        search_performance: {
          ...memberOverviewBody.search_performance,
          avg_response_time: null,
        },
      })
    );

    render(<AnalyticsDashboard />);

    expect(await screen.findByText('Not measured yet')).toBeInTheDocument();
    expect(screen.queryByText('Unavailable right now')).not.toBeInTheDocument();
    expect(screen.queryByText(/Some data couldn.t be loaded/)).not.toBeInTheDocument();
  });

  it('plots the 7-day daily search series and keeps its first, midnight-truncated day', async () => {
    route(routesFor(memberOverviewBody));

    const { data } = await fetchAnalyticsDashboard(false);

    expect(requested()).toContain(
      '/analytics/performance/charts/search_volume?time_range=7d&granularity=day'
    );
    expect(data.searchVolume?.map(({ name, value }) => [name, value])).toEqual([
      ['2026-11-17', 3],
      ['2026-11-21', 4],
      ['2026-11-22', 9],
    ]);
  });

  it('shows 7 whole days with no made-up trend and no range selector (8-bucket probe)', async () => {
    // The reviewer's probe: a partial D-7 bucket of 1, then 10 on each of 7 days.
    const days = [16, 17, 18, 19, 20, 21, 22, 23];
    route({
      ...routesFor(memberOverviewBody),
      '/analytics/performance/charts/search_volume': {
        ...searchVolumeChartBody,
        data: days.map((day, i) => ({
          timestamp: `2026-11-${day}T00:00:00+00:00`,
          value: i === 0 ? 1 : 10,
        })),
      },
    });

    const { data } = await fetchAnalyticsDashboard(false);
    expect(data.searchVolume).toHaveLength(7);
    expect(data.searchVolume?.[0].name).toBe('2026-11-17');

    render(<AnalyticsDashboard />);

    const title = await screen.findByText('Searches per day (last 7 days)');
    const card = title.closest('.overflow-hidden') as HTMLElement;
    expect(card).not.toBeNull();
    expect(card.textContent).not.toMatch(/percent|%/);
    expect(
      screen.queryByLabelText(/percent versus the prior period/)
    ).not.toBeInTheDocument();
    expect(
      card.querySelector('[aria-label="Time range"]')
    ).toBeNull();
    // A fixed 7-day window has no prior period and no other range.
    const searchChart = charts.props.find(
      (props) => props.title === 'Searches per day (last 7 days)'
    );
    expect(searchChart).toMatchObject({ showTrend: false, showTimeRange: false });
  });

  it('waits for auth before requesting anything, then loads', async () => {
    auth.isLoading = true;
    route(routesFor(memberOverviewBody));

    const { rerender } = render(<AnalyticsDashboard />);

    // Flush effects and pending microtasks: an enabled query would have called
    // its queryFn by now.
    await act(async () => {
      await Promise.resolve();
    });
    expect(get).not.toHaveBeenCalled();

    auth.isLoading = false;
    rerender(<AnalyticsDashboard />);

    expect(await screen.findByText('120')).toBeInTheDocument();
    expect(requested()).toContain('/analytics/performance/overview?time_range=24h');
  });

  it('titles the search chart as a 7-day daily series', async () => {
    route(routesFor(memberOverviewBody));

    render(<AnalyticsDashboard />);

    expect(
      await screen.findByText('Searches per day (last 7 days)')
    ).toBeInTheDocument();
  });

  it('requests organization trends with days_back for an admin', async () => {
    auth.role = 'admin';
    route(routesFor(adminOverviewBody));

    render(<AnalyticsDashboard />);

    await waitFor(() =>
      expect(requested()).toContain(
        '/analytics/behavior/organization/trends?days_back=30&group_by=day'
      )
    );
    expect(await screen.findByText('120')).toBeInTheDocument();
    expect(
      screen.queryByText('Active-user trends are available to organization admins.')
    ).not.toBeInTheDocument();
    // user_engagement.active_users from the admin-scoped overview.
    expect(
      screen.getByText('Active users, last 24 hours').closest('.rounded-xl')
    ).toHaveTextContent('5');
    expect(screen.queryByText(/Some data couldn.t be loaded/)).not.toBeInTheDocument();
  });
});
