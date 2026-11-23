import { act, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  QualityMetricsCard,
  UNAVAILABLE_LABEL,
} from '@/components/metrics/QualityMetricsCard';

describe('QualityMetricsCard (GOO-724)', () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('shows every metric as unavailable instead of a number', () => {
    render(<QualityMetricsCard query="what is RAG" />);

    expect(screen.getByRole('status')).toHaveTextContent(/metrics unavailable/i);
    for (const key of [
      'quality-metric-answerRelevancy',
      'quality-metric-faithfulness',
      'quality-metric-contextualRelevancy',
      'quality-metric-safety',
      'performance-metric-latency',
      'performance-metric-documents',
      'performance-metric-entities',
    ]) {
      const cell = screen.getByTestId(key);
      expect(cell).toHaveTextContent(UNAVAILABLE_LABEL);
      expect(cell.textContent).not.toMatch(/\d/);
    }
    expect(screen.getByText('what is RAG')).toBeInTheDocument();
  });

  it('never generates values over time', () => {
    vi.useFakeTimers();
    const random = vi.spyOn(Math, 'random');
    const { container } = render(<QualityMetricsCard query="q" />);
    const before = container.innerHTML;

    act(() => {
      vi.advanceTimersByTime(10_000);
    });

    expect(random).not.toHaveBeenCalled();
    expect(container.innerHTML).toBe(before);
    expect(container.textContent).not.toMatch(/%|ms\b/);
  });
});
