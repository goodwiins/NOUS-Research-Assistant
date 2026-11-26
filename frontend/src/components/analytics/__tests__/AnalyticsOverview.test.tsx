/**
 * GOO-768: a brand-new org sees "No activity yet", a value that was never
 * measured reads "Not measured yet" (not like an outage), and a measured 0
 * is shown as 0.
 */
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import {
  AnalyticsOverview,
  type AnalyticsOverviewData,
} from '@/components/analytics/AnalyticsOverview';
import { NOT_MEASURED } from '@/utils/analyticsFigure';

const card = (title: string): HTMLElement => {
  const heading = screen.getByText(title);
  const root = heading.closest('.rounded-xl');
  if (!(root instanceof HTMLElement)) throw new Error(`no card for ${title}`);
  return root;
};

/** The big figure on a metric card, without its title or note. */
const figure = (title: string): string | null =>
  card(title).querySelector('.tabular-nums')?.textContent ?? null;

const overview = (
  overrides: Partial<AnalyticsOverviewData>
): AnalyticsOverviewData => ({
  documentsIndexed: 3,
  searches24h: 12,
  avgSearchTimeMs24h: 140,
  ...overrides,
});

describe('AnalyticsOverview (GOO-768)', () => {
  it('shows a new org, with zero counts and no measured average, as no activity', () => {
    render(
      <AnalyticsOverview
        data={overview({
          documentsIndexed: 0,
          searches24h: 0,
          avgSearchTimeMs24h: NOT_MEASURED,
        })}
      />
    );

    expect(screen.getByText('No activity yet')).toBeInTheDocument();
    expect(screen.queryByText('Avg search time')).not.toBeInTheDocument();
  });

  it('shows an unmeasured average as "—" and "Not measured yet", not as an outage', () => {
    render(
      <AnalyticsOverview
        data={overview({ avgSearchTimeMs24h: NOT_MEASURED })}
      />
    );

    expect(figure('Avg search time')).toBe('—');
    expect(card('Avg search time')).toHaveTextContent('Not measured yet');
    expect(screen.queryByText('Unavailable right now')).not.toBeInTheDocument();
  });

  it('keeps the outage wording, and the cards, for a figure that failed to load', () => {
    render(
      <AnalyticsOverview
        data={overview({
          documentsIndexed: 0,
          searches24h: 0,
          avgSearchTimeMs24h: null,
        })}
      />
    );

    expect(screen.queryByText('No activity yet')).not.toBeInTheDocument();
    expect(figure('Avg search time')).toBe('—');
    const avg = card('Avg search time');
    expect(avg).toHaveTextContent('Unavailable right now');
    expect(avg).not.toHaveTextContent('Not measured yet');
  });

  it('shows a measured 0 ms average as 0, never as "—"', () => {
    render(<AnalyticsOverview data={overview({ avgSearchTimeMs24h: 0 })} />);

    expect(figure('Avg search time')).toBe('0');
    expect(card('Avg search time')).toHaveTextContent(
      'Milliseconds, last 24 hours'
    );
  });
});
