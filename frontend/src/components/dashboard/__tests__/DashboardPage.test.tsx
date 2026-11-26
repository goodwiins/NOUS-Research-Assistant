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
const session = vi.hoisted(() => ({ signedIn: true, isLoading: false }));
vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => ({
    isAuthenticated: session.signedIn,
    isLoading: session.isLoading,
    user: session.signedIn
      ? { id: 'me', email: 'me@example.test', role: 'user' }
      : null,
  }),
}));
const docs = vi.hoisted(() => ({
  state: { total: 7, hasLoaded: true, error: null as string | null },
}));
vi.mock('@/hooks/useDocuments', () => ({
  useDocuments: () => ({
    pagination: { total: docs.state.total },
    loading: false,
    hasLoaded: docs.state.hasLoaded,
    error: docs.state.error,
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

/** The big figure on a stat card, without its label. */
function statValue(label: string): string | null {
  return statCard(label).querySelector('.tabular-nums')?.textContent ?? null;
}

describe('DashboardPage stat cards (GOO-649)', () => {
  beforeEach(() => {
    get.mockReset();
    docs.state = { total: 7, hasLoaded: true, error: null };
    session.signedIn = true;
    session.isLoading = false;
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

  it('shows an unmeasured average search time as not measured, not 0 (GOO-745, GOO-768)', async () => {
    get.mockImplementation(async (url: string) => {
      if (url.startsWith('/analytics/performance/search-performance')) {
        // The backend sends null when no search in the window was timed.
        return { ...searchPerformanceBody, avg_response_time: null };
      }
      throw new Error(`404 ${url}`);
    });

    render(<DashboardPage />);

    // The source itself is fine: its total still shows.
    await waitFor(() =>
      expect(statCard('Searches, last 7 days')).toHaveTextContent('120')
    );
    const avg = statCard('Avg search time (ms), last 7 days');
    expect(statValue('Avg search time (ms), last 7 days')).toBe('—');
    // Not measured is not an outage (GOO-768).
    expect(avg).toHaveTextContent('Not measured yet');
    expect(avg).not.toHaveTextContent('Unavailable right now');
  });

  it('shows a measured 0 ms average search time as 0, not "—" (GOO-768)', async () => {
    get.mockImplementation(async (url: string) => {
      if (url.startsWith('/analytics/performance/search-performance')) {
        return { ...searchPerformanceBody, avg_response_time: 0 };
      }
      throw new Error(`404 ${url}`);
    });

    render(<DashboardPage />);

    await waitFor(() =>
      expect(statCard('Searches, last 7 days')).toHaveTextContent('120')
    );
    expect(statValue('Avg search time (ms), last 7 days')).toBe('0');
    expect(statCard('Avg search time (ms), last 7 days')).not.toHaveTextContent(
      'Not measured yet'
    );
  });

  it('Documents waits for the first response instead of showing the default 0 (F3)', async () => {
    // useDocuments' state before any fetch: loading false, total 0.
    docs.state = { total: 0, hasLoaded: false, error: null };
    get.mockRejectedValue(new Error('outage'));

    render(<DashboardPage />);

    expect(statCard('Documents')).toHaveTextContent('loading');
    expect(statCard('Documents')).not.toHaveTextContent('0');
  });

  it('Documents shows unavailable when the document request fails (F3)', async () => {
    docs.state = { total: 0, hasLoaded: false, error: 'Request failed' };
    get.mockRejectedValue(new Error('outage'));

    render(<DashboardPage />);

    expect(statCard('Documents')).toHaveTextContent('Unavailable right now');
    expect(statCard('Documents')).not.toHaveTextContent('0');
  });

  it('Processing is unavailable when /files/stats omits processing_stats (F4)', async () => {
    get.mockImplementation(async (url: string) => {
      if (url === '/files/stats') return { files_by_type: [] };
      throw new Error(`404 ${url}`);
    });

    render(<DashboardPage />);

    await waitFor(() =>
      expect(statCard('Processing')).toHaveTextContent('Unavailable right now')
    );
    expect(statCard('Processing')).not.toHaveTextContent('0');
  });

  it('a signed-out user sees unavailable cards, not an endless loading state', async () => {
    session.signedIn = false;
    // useDocuments does not fetch when signed out: loading false, no error.
    docs.state = { total: 0, hasLoaded: false, error: null };

    render(<DashboardPage />);

    for (const label of [
      'Documents',
      'Searches, last 7 days',
      'Avg search time (ms), last 7 days',
      'Processing',
    ]) {
      expect(statCard(label)).toHaveTextContent('Unavailable right now');
      expect(statCard(label)).not.toHaveTextContent('loading');
    }
    expect(
      screen.getByText('Document types are unavailable right now.')
    ).toBeInTheDocument();
    expect(get).not.toHaveBeenCalled();
  });

  it('still shows loading while auth itself is loading', () => {
    session.signedIn = false;
    session.isLoading = true;
    docs.state = { total: 0, hasLoaded: false, error: null };

    render(<DashboardPage />);

    expect(statCard('Documents')).toHaveTextContent('loading');
    expect(statCard('Documents')).not.toHaveTextContent('Unavailable');
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
