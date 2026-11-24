import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { APIErrorClass } from '@/types/api';

// GOO-752: /chat must wait while the auth store is still resolving the
// profile, and must not treat a verified user whose /auth/me is unavailable
// as signed out. Real auth store; only Supabase and /auth/me are faked.

const navigationMocks = vi.hoisted(() => ({
  push: vi.fn(),
  replace: vi.fn(),
}));

const authMocks = vi.hoisted(() => ({
  apiGet: vi.fn(),
  getUser: vi.fn(),
  getSession: vi.fn(),
}));

vi.mock('next/navigation', () => ({
  useRouter: () => ({
    push: navigationMocks.push,
    replace: navigationMocks.replace,
  }),
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock('@/lib/supabase/client', () => ({
  createClient: () => ({
    auth: {
      onAuthStateChange: () => ({
        data: { subscription: { unsubscribe: vi.fn() } },
      }),
      signOut: vi.fn().mockResolvedValue({ error: null }),
      getUser: authMocks.getUser,
      getSession: authMocks.getSession,
    },
  }),
}));

vi.mock('@/services/api-client', () => ({
  api: { get: authMocks.apiGet, post: vi.fn(), clearAuth: vi.fn() },
}));

vi.mock('@/services/workspaceService', () => ({
  workspaceService: {},
  clearWorkspaceServiceCache: vi.fn(),
}));

vi.mock('@/hooks/chat/useChatPersistence', () => ({
  // Never settles: these tests are only about the auth guard.
  useChatPersistence: () => ({ initialize: () => new Promise(() => {}) }),
}));

vi.mock('react-hot-toast', () => ({
  default: { error: vi.fn(), success: vi.fn() },
}));

import { useChatSession } from '@/hooks/chat/useChatSession';
import { useAuthStore } from '@/stores/authStore';
import { writeClientOwner } from '@/lib/client-owner';

const USER = { id: 'user-1' };
const PROFILE = { user: { id: 'user-1', email: 'ada@example.com' } };

const unavailable = (): APIErrorClass =>
  new APIErrorClass({
    message: 'Service unavailable',
    status_code: 503,
    type: 'server_error',
  });

async function bootChat(): Promise<{
  current: ReturnType<typeof useChatSession>;
}> {
  const { result } = renderHook(() => useChatSession());
  await act(async () => {
    void useAuthStore.getState().initialize();
    await vi.advanceTimersByTimeAsync(0);
  });
  return result;
}

describe('useChatSession waits for the auth profile (GOO-752)', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.clearAllMocks();
    writeClientOwner(USER.id);
    useAuthStore.setState({
      user: null,
      organization: null,
      isAuthenticated: false,
      isLoading: true,
      error: null,
      profileUnavailable: false,
    });
    authMocks.getUser.mockResolvedValue({ data: { user: USER }, error: null });
    authMocks.getSession.mockResolvedValue({
      data: { session: { access_token: 'token-1', user: USER } },
    });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('does not redirect while /auth/me recovers after two 503s', async () => {
    authMocks.apiGet
      .mockRejectedValueOnce(unavailable())
      .mockRejectedValueOnce(unavailable())
      .mockResolvedValue(PROFILE);

    await bootChat();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    // Effects that the settled auth state scheduled run after act exits;
    // give a would-be redirect timer its full delay.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });

    expect(authMocks.apiGet).toHaveBeenCalledTimes(3);
    expect(useAuthStore.getState().isAuthenticated).toBe(true);
    expect(navigationMocks.push).not.toHaveBeenCalled();
  });

  it('keeps the verified user on /chat when every /auth/me attempt fails', async () => {
    authMocks.apiGet.mockRejectedValue(unavailable());

    await bootChat();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    // Effects that the settled auth state scheduled run after act exits;
    // give a would-be redirect timer its full delay.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });

    expect(authMocks.apiGet).toHaveBeenCalledTimes(3);
    expect(useAuthStore.getState()).toMatchObject({
      isAuthenticated: false,
      isLoading: false,
      profileUnavailable: true,
    });
    expect(navigationMocks.push).not.toHaveBeenCalled();
  });

  it('still redirects to login when /auth/me rejects the token', async () => {
    authMocks.apiGet.mockRejectedValue(
      new APIErrorClass({
        message: 'Invalid token',
        status_code: 401,
        type: 'auth_error',
      })
    );

    await bootChat();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500);
    });

    expect(navigationMocks.push).toHaveBeenCalledWith('/login?next=%2Fchat');
  });

  it('shows the outage, not a wait, once every attempt has failed (GOO-759)', async () => {
    authMocks.apiGet.mockRejectedValue(unavailable());

    const result = await bootChat();
    // Still retrying: the account is loading, not unavailable.
    expect(result.current.isProfileUnavailable).toBe(false);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(result.current.isProfileUnavailable).toBe(true);

    // Retry in flight: back to loading.
    let retry: Promise<void> = Promise.resolve();
    act(() => {
      retry = useAuthStore.getState().retryProfile();
    });
    expect(result.current.isProfileUnavailable).toBe(false);
    await act(async () => {
      await retry;
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(result.current.isProfileUnavailable).toBe(true);
  });

  it('redirects to login when Retry finds the token rejected (GOO-759)', async () => {
    authMocks.apiGet.mockRejectedValue(unavailable());

    await bootChat();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(navigationMocks.push).not.toHaveBeenCalled();

    authMocks.apiGet.mockRejectedValue(
      new APIErrorClass({
        message: 'Invalid token',
        status_code: 401,
        type: 'auth_error',
      })
    );
    await act(async () => {
      await useAuthStore.getState().retryProfile();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500);
    });

    expect(navigationMocks.push).toHaveBeenCalledWith('/login?next=%2Fchat');
  });

  it('still redirects to login when there is no verified user', async () => {
    authMocks.getUser.mockResolvedValue({
      data: { user: null },
      error: { message: 'Auth session missing' },
    });

    await bootChat();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500);
    });

    expect(navigationMocks.push).toHaveBeenCalledWith('/login?next=%2Fchat');
  });
});
