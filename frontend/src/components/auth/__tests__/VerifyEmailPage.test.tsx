import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen } from '@testing-library/react';
import VerifyEmailPage from '../../../../app/(auth)/verify-email/page';

const verifyOtp = vi.fn();

vi.mock('@/lib/supabase/client', () => ({
  createClient: () => ({ auth: { verifyOtp } }),
}));

const auth = { isAuthenticated: false, isLoading: false };
vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => auth,
}));

// Like Next, read the live URL so the page sees its own replaceState.
vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
}));

vi.mock('framer-motion', () => ({
  motion: new Proxy(
    {},
    {
      get:
        () =>
        ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
    }
  ),
}));

describe('verify-email token_hash flow (GOO-734)', () => {
  afterEach(() => {
    vi.useRealTimers();
    auth.isAuthenticated = false;
    auth.isLoading = false;
    verifyOtp.mockReset();
    window.history.replaceState(null, '', '/');
  });

  it('verifies with the token, then keeps it out of the URL', async () => {
    let finish!: (value: { error: null }) => void;
    verifyOtp.mockReturnValue(
      new Promise((done) => {
        finish = done;
      })
    );
    window.history.replaceState({ kept: true }, '', '/verify-email?token_hash=tok-123&type=signup');

    const { rerender } = render(<VerifyEmailPage />);

    await vi.waitFor(() =>
      expect(verifyOtp).toHaveBeenCalledWith({ token_hash: 'tok-123', type: 'signup' })
    );
    expect(window.location.pathname + window.location.search).toBe('/verify-email');
    expect(window.history.state).toEqual({ kept: true });

    // A re-render now sees no token in the URL; the read value must survive
    // and the in-flight verification must not be cancelled.
    rerender(<VerifyEmailPage />);
    await act(async () => finish({ error: null }));

    expect(await screen.findByText('Your email is confirmed')).toBeInTheDocument();
    expect(verifyOtp).toHaveBeenCalledTimes(1);
  });

  it('leaves the error code in the URL', async () => {
    window.history.replaceState(null, '', '/verify-email?error=otp_expired');
    render(<VerifyEmailPage />);
    expect(await screen.findByText(/has expired/)).toBeInTheDocument();
    expect(window.location.search).toBe('?error=otp_expired');
    expect(verifyOtp).not.toHaveBeenCalled();
  });

  it('PKCE path: a signed-in session shows the confirmation', async () => {
    auth.isAuthenticated = true;
    window.history.replaceState(null, '', '/verify-email');
    render(<VerifyEmailPage />);
    expect(await screen.findByText('Your email is confirmed')).toBeInTheDocument();
    expect(verifyOtp).not.toHaveBeenCalled();
  });

  it('PKCE path: no session after the grace period shows the error', async () => {
    vi.useFakeTimers();
    window.history.replaceState(null, '', '/verify-email');
    render(<VerifyEmailPage />);
    expect(screen.queryByText('Your email is confirmed')).toBeNull();
    await act(async () => {
      vi.advanceTimersByTime(6_001);
    });
    expect(screen.getByText(/could not verify your account/i)).toBeInTheDocument();
  });
});
