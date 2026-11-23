/**
 * GOO-788: unsent turns and composer drafts live in this tab's
 * sessionStorage. A real end of the session removes them: an explicit
 * sign-out, a SIGNED_OUT (another tab, an expiry), a 401/403 from Supabase or
 * our backend. Another account observed in this tab never finds them either.
 * A transient failure to verify the session (offline, timeout, 5xx) keeps
 * them: that is when a send failed and the user reloads.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useAuthStore } from '@/stores/authStore';
import { api } from '@/services/api-client';
import {
  UNSENT_CHAT_STORAGE_KEY,
  loadChatDraft,
  loadUnsentTurns,
  saveChatDraft,
  saveUnsentTurns,
} from '@/hooks/chat/unsentChatStorage';

const auth = vi.hoisted(() => ({
  listener: undefined as
    | undefined
    | ((event: string, session: unknown) => void),
  getUser: vi.fn(),
  getSession: vi.fn(),
  signInWithPassword: vi.fn(),
  signOut: vi.fn(),
}));
vi.mock('@/lib/supabase/client', () => ({
  createClient: () => ({
    auth: {
      ...auth,
      onAuthStateChange: (listener: typeof auth.listener) => {
        auth.listener = listener;
      },
    },
  }),
}));
vi.mock('@/lib/supabase/clearAuthCookies', () => ({
  clearSupabaseAuthCookies: vi.fn(),
}));
vi.mock('@/services/api-client', () => ({
  api: { get: vi.fn(), clearAuth: vi.fn(), upload: vi.fn() },
}));

const session = (id: string): { user: { id: string }; access_token: string } => ({
  user: { id },
  access_token: `synthetic-${id}`,
});

function transportIdentity(id: string): void {
  auth.getUser.mockResolvedValue({ data: { user: { id } }, error: null });
  auth.getSession.mockResolvedValue({ data: { session: session(id) } });
  auth.signInWithPassword.mockResolvedValue({
    data: { session: session(id) },
    error: null,
  });
  vi.mocked(api.get).mockResolvedValue({
    user: { id },
    organization: { id: `org-${id}` },
  });
}

function leaveUnsentTextFor(userId: string): void {
  saveUnsentTurns(userId, 'thread-A', [
    {
      runtimeId: 'r1',
      source: 'local-only',
      role: 'user',
      content: 'rejected question',
      timestamp: 1,
      sendFailed: { message: 'Not sent.' },
    },
  ]);
  saveChatDraft(userId, 'ws-1', 'thread-A', 'half-typed');
  expect(window.sessionStorage.getItem(UNSENT_CHAT_STORAGE_KEY)).not.toBeNull();
}

beforeEach(async () => {
  auth.signOut.mockResolvedValue({ error: null });
  await useAuthStore.getState().signOut();
  window.sessionStorage.clear();
  transportIdentity('A');
  await useAuthStore.getState().signIn('a@example.invalid', 'synthetic-password');
  expect(useAuthStore.getState().user?.id).toBe('A');
});

describe('unsent chat text ends with the session (GOO-788)', () => {
  it('is cleared by a SIGNED_OUT event (another tab, an expiry)', () => {
    leaveUnsentTextFor('A');

    auth.listener!('SIGNED_OUT', null);

    expect(useAuthStore.getState().user).toBeNull();
    expect(window.sessionStorage.getItem(UNSENT_CHAT_STORAGE_KEY)).toBeNull();
    expect(loadUnsentTurns('A', 'thread-A')).toEqual([]);
  });

  it('is cleared when the backend rejects the session (401)', () => {
    leaveUnsentTextFor('A');

    useAuthStore.getState().invalidateRejectedSession('A');

    expect(useAuthStore.getState().user).toBeNull();
    expect(window.sessionStorage.getItem(UNSENT_CHAT_STORAGE_KEY)).toBeNull();
    expect(loadChatDraft('A', 'ws-1', 'thread-A')).toBeUndefined();
  });

  it('is cleared when Supabase rejects the token (401) on verification', async () => {
    leaveUnsentTextFor('A');
    auth.getUser.mockResolvedValueOnce({
      data: { user: null },
      error: Object.assign(new Error('invalid JWT'), { status: 401 }),
    });

    await useAuthStore.getState().fetchProfile();

    expect(window.sessionStorage.getItem(UNSENT_CHAT_STORAGE_KEY)).toBeNull();
  });

  it('is cleared when /auth/me rejects the token (403)', async () => {
    leaveUnsentTextFor('A');
    useAuthStore.setState({ user: null, isAuthenticated: false });
    vi.mocked(api.get).mockRejectedValueOnce({ error: { status_code: 403 } });

    await useAuthStore.getState().fetchProfile();

    expect(useAuthStore.getState().user).toBeNull();
    expect(window.sessionStorage.getItem(UNSENT_CHAT_STORAGE_KEY)).toBeNull();
  });

  it.each([
    ['offline', Object.assign(new Error('Failed to fetch'), { status: 0 })],
    ['a 5xx', Object.assign(new Error('upstream down'), { status: 503 })],
    ['a timeout', Object.assign(new Error('timed out'), { status: undefined })],
  ])(
    'survives a reload whose session check fails with %s',
    async (_label, error) => {
      leaveUnsentTextFor('A');
      // A reload: memory no longer knows the profile.
      useAuthStore.setState({ user: null, isAuthenticated: false });
      auth.getUser.mockResolvedValueOnce({ data: { user: null }, error });

      await useAuthStore.getState().fetchProfile();

      expect(useAuthStore.getState().user).toBeNull();
      expect(loadUnsentTurns('A', 'thread-A')).toEqual([
        expect.objectContaining({ content: 'rejected question' }),
      ]);
      expect(loadChatDraft('A', 'ws-1', 'thread-A')).toBe('half-typed');
    }
  );

  it('survives a reload whose session check throws', async () => {
    leaveUnsentTextFor('A');
    useAuthStore.setState({ user: null, isAuthenticated: false });
    auth.getUser.mockRejectedValueOnce(new TypeError('Failed to fetch'));

    await useAuthStore.getState().fetchProfile();

    expect(loadUnsentTurns('A', 'thread-A')).toHaveLength(1);
    expect(loadChatDraft('A', 'ws-1', 'thread-A')).toBe('half-typed');
  });

  it('is cleared by an explicit sign-out', async () => {
    leaveUnsentTextFor('A');

    await useAuthStore.getState().signOut();

    expect(window.sessionStorage.getItem(UNSENT_CHAT_STORAGE_KEY)).toBeNull();
  });

  it('is dropped when another account is observed, with no sign-out between', () => {
    leaveUnsentTextFor('A');

    auth.listener!('SIGNED_IN', session('B'));

    expect(window.sessionStorage.getItem(UNSENT_CHAT_STORAGE_KEY)).toBeNull();
    expect(loadUnsentTurns('B', 'thread-A')).toEqual([]);
  });

  it('survives the same account refreshing its token', () => {
    leaveUnsentTextFor('A');

    auth.listener!('TOKEN_REFRESHED', session('A'));

    expect(loadChatDraft('A', 'ws-1', 'thread-A')).toBe('half-typed');
  });
});
