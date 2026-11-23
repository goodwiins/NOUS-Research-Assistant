import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Mock } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { render } from '@/test/test-utils';
import DashboardPage from '../../../../app/(dashboard)/dashboard/page';
import { api } from '@/services/api-client';
import { searchPerformanceBody } from '@/services/__tests__/fixtures/analyticsBackend';

vi.mock('@/services/api-client', () => ({
  api: { get: vi.fn() },
}));
vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => ({
    isAuthenticated: true,
    isLoading: false,
    user: { id: 'me', email: 'me@example.test', role: 'user' },
  }),
}));
vi.mock('@/hooks/useDocuments', () => ({
  useDocuments: () => ({
    pagination: { total: 7 },
    loading: false,
    error: null,
  }),
}));
vi.mock('@/lib/analytics', () => ({
  getAnalytics: () => ({ trackPageView: vi.fn(), trackFeatureUsage: vi.fn() }),
}));
vi.mock('@/components/dashboard/KeyboardShortcuts', () => ({
  KeyboardShortcuts: () => null,
}));
vi.mock('@/components/dashboard/QuickSearch', () => ({
  QuickSearch: () => null,
}));

const get = api.get as Mock;

function statCard(label: string): HTMLElement {
  const card = screen.getByText(label).closest('.rounded-xl');
  if (!(card instanceof HTMLElement)) throw new Error(`no card for ${label}`);
  return card;
}

describe('DashboardPage stat cards (GOO-649)', () => {
  beforeEach(() => {
    get.mockReset();
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  it('shows failed sources as unavailable instead of 0', async () => {
    get.mockRejectedValue(new Error('outage'));

    render(<DashboardPage />);

    await waitFor(() =>
      expect(statCard('Processing')).toHaveTextContent('Unavailable right now')
    );
    expect(statCard('Searches, last 7 days')).toHaveTextContent('—');
    expect(statCard('Searches, last 7 days')).not.toHaveTextContent('0');
    expect(statCard('Documents')).toHaveTextContent('7');
    expect(
      screen.getByText('Document types are unavailable right now.')
    ).toBeInTheDocument();
  });

  it('renders the backend search and processing figures', async () => {
    get.mockImplementation(async (url: string) => {
      if (url.startsWith('/analytics/performance/search-performance')) {
        return searchPerformanceBody;
      }
      if (url === '/files/stats') {
        return {
          files_by_type: [{ type: 'pdf', count: 7, total_size_mb: 2 }],
          processing_stats: [
            { status: 'retrying', count: 2 },
            { status: 'completed', count: 5 },
          ],
        };
      }
      throw new Error(`404 ${url}`);
    });

    render(<DashboardPage />);

    await waitFor(() =>
      expect(statCard('Searches, last 7 days')).toHaveTextContent('120')
    );
    expect(statCard('Avg search time (ms), last 7 days')).toHaveTextContent('231');
    expect(statCard('Processing')).toHaveTextContent('2');
    // No organization-wide conversation count exists, so no card claims one.
    expect(screen.queryByText('Conversations')).not.toBeInTheDocument();
  });

  it('shows service health as not available yet, with no request and no Retry', async () => {
    get.mockRejectedValue(new Error('outage'));

    render(<DashboardPage />);

    expect(
      await screen.findByText("Service health isn't available yet.")
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();
    expect(get.mock.calls.map(([url]) => url)).not.toContain('/analytics/performance');
  });
});
