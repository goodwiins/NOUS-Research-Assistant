import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ProfileUnavailableBanner } from '@/components/auth/ProfileUnavailableBanner';
import { useAuthStore } from '@/stores/authStore';

describe('ProfileUnavailableBanner (GOO-752)', () => {
  const retryProfile = vi.fn().mockResolvedValue(undefined);

  beforeEach(() => {
    retryProfile.mockClear();
    useAuthStore.setState({
      user: null,
      isAuthenticated: false,
      isLoading: false,
      profileUnavailable: false,
      error: 'Service unavailable',
      retryProfile,
    });
  });

  it('renders nothing while the profile is available or still loading', () => {
    const { container } = render(<ProfileUnavailableBanner />);
    expect(container).toBeEmptyDOMElement();
  });

  it('shows a fixed message and a retry when the profile is unavailable', () => {
    useAuthStore.setState({ profileUnavailable: true });
    render(<ProfileUnavailableBanner />);

    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent("We couldn't load your account details.");
    // Raw transport text stays out of the UI.
    expect(alert).not.toHaveTextContent('Service unavailable');

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(retryProfile).toHaveBeenCalledTimes(1);
  });

  it('disables the retry while one is running', () => {
    useAuthStore.setState({ profileUnavailable: true, isLoading: true });
    render(<ProfileUnavailableBanner />);

    expect(screen.getByRole('button', { name: 'Retrying…' })).toBeDisabled();
  });
});
