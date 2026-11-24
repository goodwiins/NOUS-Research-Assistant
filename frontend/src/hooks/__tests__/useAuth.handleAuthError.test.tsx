/**
 * GOO-727: handleAuthError signs out only for a 401 from our backend origin.
 */
import { act, renderHook } from '@testing-library/react';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { API_CONFIG, APIErrorClass } from '@/types/api';

const signOut = vi.fn(async () => undefined);

vi.mock('@/stores/authStore', () => {
  const state = {
    clearPendingEmailConfirmation: vi.fn(),
    error: null,
    fetchProfile: vi.fn(),
    initialize: vi.fn(),
    isAuthenticated: true,
    isLoading: false,
    organization: null,
    pendingConfirmationEmail: null,
    pendingEmailConfirmation: false,
    pendingSignupPossiblyExisting: false,
    resetPassword: vi.fn(),
    signIn: vi.fn(),
    signOut: () => signOut(),
    signUp: vi.fn(),
    user: null,
  };
  return {
    useAuthStore: (selector: (s: typeof state) => unknown) => selector(state),
  };
});

import { AuthProvider, useAuth } from '../useAuth';

const wrapper = ({ children }: { children: ReactNode }): JSX.Element => (
  <AuthProvider>{children}</AuthProvider>
);

function unauthorized(requestUrl: string): APIErrorClass {
  const error = new APIErrorClass({
    message: 'Not authenticated',
    status_code: 401,
    type: 'auth_error',
  });
  error.requestUrl = requestUrl;
  return error;
}

const backendUrl = new URL(
  `${API_CONFIG.BASE_URL}/documents`,
  window.location.origin
).toString();

describe('useAuth().handleAuthError', () => {
  beforeEach(() => signOut.mockClear());

  it('signs out on a 401 from the backend', () => {
    const { result } = renderHook(() => useAuth(), { wrapper });
    act(() => result.current.handleAuthError(unauthorized(backendUrl)));
    expect(signOut).toHaveBeenCalledTimes(1);
  });

  it('ignores a 401 from a third-party origin', () => {
    const { result } = renderHook(() => useAuth(), { wrapper });
    act(() =>
      result.current.handleAuthError(
        unauthorized('https://third-party.example/oauth/token')
      )
    );
    expect(signOut).not.toHaveBeenCalled();
  });

  it('ignores an error that is not a backend APIError', () => {
    const { result } = renderHook(() => useAuth(), { wrapper });
    act(() => result.current.handleAuthError(new Error('Unauthorized')));
    expect(signOut).not.toHaveBeenCalled();
  });
});
