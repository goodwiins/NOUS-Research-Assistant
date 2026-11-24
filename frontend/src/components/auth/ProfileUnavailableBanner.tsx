'use client';

import { AlertTriangle, Loader2, RotateCw } from 'lucide-react';
import type { ReactElement } from 'react';
import { useShallow } from 'zustand/react/shallow';

import { useAuthStore } from '@/stores/authStore';

/**
 * GOO-752: getUser() verified the user, but /auth/me kept failing after
 * every retry. The session is not dead, so nothing redirects to login; this
 * banner says what is wrong and offers a retry without blocking the page.
 * The copy is fixed: the stored error can carry raw transport text.
 */
export function ProfileUnavailableBanner(): ReactElement | null {
  const { profileUnavailable, isAuthenticated, isLoading, retryProfile } =
    useAuthStore(
      useShallow((s) => ({
        profileUnavailable: s.profileUnavailable,
        isAuthenticated: s.isAuthenticated,
        isLoading: s.isLoading,
        retryProfile: s.retryProfile,
      }))
    );

  if (!profileUnavailable || isAuthenticated) return null;

  return (
    <div
      role="alert"
      className="fixed top-3 left-1/2 z-50 flex max-w-[calc(100vw-1.5rem)] -translate-x-1/2 items-center gap-3 rounded-lg border border-(--nous-mars)/30 bg-(--nous-bg-1) px-4 py-2.5 text-sm text-(--nous-fg-1) shadow-lg"
      style={{ fontFamily: 'var(--nous-font-ui)' }}
    >
      <AlertTriangle
        className="h-4 w-4 shrink-0 text-(--nous-mars)"
        aria-hidden="true"
      />
      <p>
        We couldn&apos;t load your account details. Some features may not work
        until they load.
      </p>
      <button
        type="button"
        onClick={() => void retryProfile()}
        disabled={isLoading}
        className="inline-flex shrink-0 items-center gap-1.5 rounded-md border border-(--nous-border-1) bg-(--nous-bg-2) px-3 py-1 text-xs font-medium text-(--nous-fg-1) transition-colors hover:border-(--nous-sol)/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-(--nous-sol) disabled:cursor-not-allowed disabled:opacity-60"
      >
        {isLoading ? (
          <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
        ) : (
          <RotateCw className="h-3.5 w-3.5" aria-hidden="true" />
        )}
        {isLoading ? 'Retrying…' : 'Retry'}
      </button>
    </div>
  );
}
