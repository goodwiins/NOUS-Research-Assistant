'use client';

import { useState } from 'react';
import { AlertTriangle, RefreshCw } from 'lucide-react';

interface ProcessedWithWarningsProps {
  warnings: string[];
  /** Already formatted for display. */
  completedAt?: string | null;
  /** Re-runs processing; omitted where the app has no reprocess action. */
  onReprocess?: () => void | Promise<void>;
}

/**
 * Status line for a document that finished processing without some optional
 * output (GOO-744). It replaces the plain "Indexed" confirmation so a partial
 * run never reads as a clean one, and lists each step's warning.
 */
export function ProcessedWithWarnings({
  warnings,
  completedAt,
  onReprocess,
}: ProcessedWithWarningsProps): React.JSX.Element {
  const [busy, setBusy] = useState(false);

  const reprocess = async (): Promise<void> => {
    if (!onReprocess || busy) return;
    setBusy(true);
    try {
      await onReprocess();
    } finally {
      setBusy(false);
    }
  };

  return (
    <section
      aria-label="Processing status"
      className="rounded-xl border border-border bg-card px-5 py-3 shadow-xs"
    >
      <div className="flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2.5">
          <AlertTriangle
            aria-hidden="true"
            className="h-4 w-4 shrink-0 text-(--nous-aurum)"
          />
          <span className="text-sm font-medium text-foreground">
            Processed with warnings
          </span>
          <span className="truncate text-xs text-muted-foreground">
            searchable, but some steps didn&apos;t finish
          </span>
        </div>
        <div className="flex shrink-0 items-center gap-3">
          {completedAt && (
            <span className="text-xs text-muted-foreground tabular-nums">
              {completedAt}
            </span>
          )}
          {onReprocess && (
            <button
              type="button"
              onClick={() => void reprocess()}
              disabled={busy}
              className="inline-flex items-center gap-1.5 rounded text-xs font-medium text-primary hover:underline underline-offset-4 disabled:opacity-60 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
            >
              <RefreshCw aria-hidden="true" className="h-3.5 w-3.5" />
              Reprocess
            </button>
          )}
        </div>
      </div>
      {warnings.length > 0 ? (
        <ul
          aria-label="Processing warnings"
          className="mt-2 list-disc space-y-1 pl-9 text-xs text-muted-foreground"
        >
          {warnings.map((warning, index) => (
            <li key={`${index}-${warning}`}>{warning}</li>
          ))}
        </ul>
      ) : (
        <p className="mt-2 pl-6.5 text-xs text-muted-foreground">
          What didn&apos;t finish wasn&apos;t recorded.
        </p>
      )}
    </section>
  );
}
