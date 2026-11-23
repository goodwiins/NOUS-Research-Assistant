'use client';

import * as Sentry from '@sentry/nextjs';
import { usePathname, useSearchParams } from 'next/navigation';
import React, { Suspense, useEffect } from 'react';
import { syncReplayWithUrl, type ReplayControl } from '@/lib/replay-privacy';

/**
 * Re-checks the page URL after every client navigation and every
 * `replaceState` that changes the query (Next syncs `useSearchParams` with
 * native history calls), so replay stops on a sensitive URL and resumes once
 * it is clean (GOO-734). The initial page load is handled right after
 * `Sentry.init` in `instrumentation-client.ts`.
 */
function ReplayUrlGuardEffect(): null {
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const search = searchParams.toString();
  useEffect(() => {
    void syncReplayWithUrl(
      window.location.href,
      Sentry.getReplay() as ReplayControl | undefined
    );
  }, [pathname, search]);
  return null;
}

export function ReplayUrlGuard(): React.JSX.Element {
  return (
    <Suspense fallback={null}>
      <ReplayUrlGuardEffect />
    </Suspense>
  );
}
