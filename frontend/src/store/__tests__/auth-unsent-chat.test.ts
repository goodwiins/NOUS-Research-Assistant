/**
 * GOO-788: unsent turns and composer drafts live in this tab's
 * sessionStorage. Every route that ends a session must remove them, not just
 * an explicit sign-out: a SIGNED_OUT from another tab or an expiry, and a
 * token the backend rejected. Another account observed in this tab must
 * never find the previous account's text either.
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
