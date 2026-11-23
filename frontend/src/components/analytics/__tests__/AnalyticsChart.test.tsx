import { describe, expect, it } from 'vitest';
import { screen } from '@testing-library/react';
import { render } from '@/test/test-utils';
import { AnalyticsChart } from '@/components/analytics/AnalyticsChart';

// 14 points: a prior period exists, so the default chart computes a trend.
const fourteenDays = Array.from({ length: 14 }, (_, i) => ({
  name: `day-${i}`,
  value: i < 7 ? 1 : 10,
}));

describe('AnalyticsChart opt-outs (GOO-649)', () => {
  it('shows the trend badge and range selector by default', () => {
    render(<AnalyticsChart title="Series" data={fourteenDays} />);

    expect(
      screen.getByLabelText('Up 900.0 percent versus the prior period')
    ).toBeInTheDocument();
    expect(screen.getByLabelText('Time range')).toBeInTheDocument();
  });

  it('showTrend={false} renders no trend, even when a prior period exists', () => {
    render(
      <AnalyticsChart title="Series" data={fourteenDays} showTrend={false} />
    );

    expect(
      screen.queryByLabelText(/percent versus the prior period/)
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/%/)).not.toBeInTheDocument();
  });

  it('showTimeRange={false} hides the selector', () => {
    render(
      <AnalyticsChart title="Series" data={fourteenDays} showTimeRange={false} />
    );

    expect(screen.queryByLabelText('Time range')).not.toBeInTheDocument();
  });
});
