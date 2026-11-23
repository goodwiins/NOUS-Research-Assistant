import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Mock } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
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

const auth = vi.hoisted(() => ({ role: 'user' as 'admin' | 'user' }));
vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => ({ user: { id: 'me', role: auth.role }, isLoading: false }),
}));

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
    auth.role = 'user';
    vi.spyOn(console, 'error').mockImplementation(() => {});
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

  it('plots the 7-day daily search series and keeps its first, midnight-truncated day', async () => {
    route(routesFor(memberOverviewBody));

    const { data } = await fetchAnalyticsDashboard(false);

    expect(requested()).toContain(
      '/analytics/performance/charts/search_volume?time_range=7d&granularity=day'
    );
    // No `date`, so AnalyticsChart's own now-7d cutoff cannot drop 2026-11-17.
    expect(data.searchVolume).toEqual([
      { name: '2026-11-17', value: 3 },
      { name: '2026-11-21', value: 4 },
      { name: '2026-11-22', value: 9 },
    ]);
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
