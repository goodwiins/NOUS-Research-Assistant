'use client';

import * as Sentry from '@sentry/nextjs';
import { AlertTriangle } from 'lucide-react';
import { useEffect, type ReactElement } from 'react';

import { EmptyState } from '@/components/ui/EmptyState';

/**
 * Route-level error boundary for every (dashboard) page (GOO-562). It renders
 * inside the dashboard layout, so a render error in one page keeps the app
 * shell and navigation instead of falling through to global-error.tsx.
 *
 * The error message is not shown: it can carry internal detail. The error is
 * reported to Sentry and the user can retry the segment.
 */
export default function DashboardError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}): ReactElement {
  useEffect(() => {
    Sentry.captureException(error);
  }, [error]);

  return (
    <div role="alert">
      <EmptyState
        icon={AlertTriangle}
        title="Something went wrong"
        description="This page could not be displayed. Try again, or open another page from the navigation."
        action={{ label: 'Try again', onClick: reset }}
      />
    </div>
  );
}
