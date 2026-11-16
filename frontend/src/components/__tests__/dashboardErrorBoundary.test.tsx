/**
 * GOO-562: the (dashboard) route segment has its own error boundary, so a
 * page render error keeps the dashboard shell instead of replacing it with
 * global-error.tsx. The boundary reports to Sentry, never shows the raw
 * error message, and lets the user retry the segment.
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const captureException = vi.fn();
vi.mock('@sentry/nextjs', () => ({
  captureException: (...args: unknown[]) => captureException(...args),
}));

import DashboardError from '../../../app/(dashboard)/error';

afterEach(() => {
  cleanup();
  captureException.mockReset();
});

describe('(dashboard)/error.tsx', () => {
  it('reports the error, hides its message and retries on demand', () => {
    const error = Object.assign(new Error('db password rejected for nous'), {
      digest: 'abc123',
    });
    const reset = vi.fn();

    render(<DashboardError error={error} reset={reset} />);

    expect(screen.getByRole('alert')).toHaveTextContent(
      'Something went wrong'
    );
    expect(screen.queryByText(/db password/)).toBeNull();
    expect(captureException).toHaveBeenCalledWith(error);

    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(reset).toHaveBeenCalledTimes(1);
  });
});
